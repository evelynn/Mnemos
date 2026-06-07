"""PR-155/156 — pluggable local speech-to-text (STT) engine.

Two backends, both local + offline + free + open-source, selected via
``MNEMOS_STT_ENGINE``:

1. **moonshine** *(default)* — Moonshine "Flavors of Moonshine" tiny models
   (``moonshine-voice``, MIT). Purpose-built for the edge: the Korean tiny
   model is ~26M params with ~6.46% WER and **outperforms Whisper-tiny**,
   runs on ONNX/ORT (no torch), and is the *lightest* option with strong
   Korean — which is the binding constraint here (the operators are Korean).
   The catch: each "flavor" is one language, so the engine's language is
   fixed at config time (default ``ko`` → the ``tiny-ko`` model). Pick the
   language with ``MNEMOS_STT_LANGUAGE`` (``ko``/``en``/``ja``/``zh``/…).

2. **faster-whisper** — Whisper on CTranslate2 (multilingual, CPU INT8).
   Heavier than Moonshine but handles mixed Korean+English in one model and
   auto-detects language. Use it when commands aren't single-language:
   ``MNEMOS_STT_ENGINE=faster-whisper`` (install the ``voice-whisper`` extra).

Both are *optional* dependencies (slim base, air-gappable). When the chosen
engine isn't installed the transcribe endpoint returns ``503`` and the Ask
tab hides the mic — the same graceful-degradation pattern as the Claude
Agent SDK (``is_agent_sdk_available`` → 503).

Tuning (all optional env, sensible defaults):
* ``MNEMOS_STT_ENGINE``  — ``moonshine`` (default) | ``faster-whisper``.
* ``MNEMOS_STT_MODEL``   — ``tiny`` (default) | ``base`` | (whisper also
  ``small``/…). For Moonshine this maps to the model arch; for whisper to
  the model size. ``tiny`` is the lightest.
* ``MNEMOS_STT_LANGUAGE``— Moonshine: selects the flavor (default ``ko``).
  faster-whisper: force a language, empty = auto-detect.
* ``MNEMOS_STT_DEVICE``  — ``cpu`` (default) | ``cuda`` (faster-whisper).
* ``MNEMOS_STT_COMPUTE`` — ``int8`` (default) | … (faster-whisper).
* ``MNEMOS_STT_BEAM_SIZE``— faster-whisper decoding beam (default 5).
* ``MNEMOS_DISABLE_STT=1``— hard off even if installed (air-gapped opt-out).
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

log = logging.getLogger(__name__)

DEFAULT_ENGINE = "moonshine"
DEFAULT_MODEL = "tiny"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE = "int8"
DEFAULT_BEAM_SIZE = 5
# Moonshine flavors are per-language; default to Korean since the operators
# are Korean (and the user asked for the tiny-ko model specifically).
DEFAULT_MOONSHINE_LANGUAGE = "ko"
# Moonshine consumes 16 kHz mono float PCM.
MOONSHINE_SAMPLE_RATE = 16000

# Hard cap on accepted upload size. A spoken command is a few seconds of
# Opus — well under a megabyte — so 25 MB is generous head-room while
# still refusing a multi-hour file that would pin the CPU. Override with
# ``MNEMOS_STT_MAX_UPLOAD_BYTES``.
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_FASTER_WHISPER_ALIASES = frozenset({"faster-whisper", "faster_whisper", "whisper"})
_MOONSHINE_ALIASES = frozenset({"moonshine", "moonshine-voice", "moonshine_voice"})


class STTUnavailable(RuntimeError):
    """No engine can run (opted out, package missing, model can't load, or
    unknown engine). Callers gate on :func:`is_stt_available` and translate
    this into a ``503 stt_unavailable`` rather than a 500."""


@dataclass(frozen=True)
class STTConfig:
    """Resolved engine configuration, built from env so the always-loaded
    ``app.config.Settings`` stays free of optional-feature knobs (same
    convention as ``security/headers.py`` and ``extractor/agent_sdk.py``)."""

    engine: str = DEFAULT_ENGINE
    model: str = DEFAULT_MODEL
    device: str = DEFAULT_DEVICE
    compute_type: str = DEFAULT_COMPUTE
    language: str | None = None  # None = auto (whisper) / default flavor (moonshine)
    beam_size: int = DEFAULT_BEAM_SIZE

    @classmethod
    def from_env(cls) -> STTConfig:
        lang = (os.environ.get("MNEMOS_STT_LANGUAGE") or "").strip() or None
        try:
            beam = int(os.environ.get("MNEMOS_STT_BEAM_SIZE", "") or DEFAULT_BEAM_SIZE)
        except ValueError:
            beam = DEFAULT_BEAM_SIZE
        return cls(
            engine=(os.environ.get("MNEMOS_STT_ENGINE") or DEFAULT_ENGINE).strip().lower(),
            model=(os.environ.get("MNEMOS_STT_MODEL") or DEFAULT_MODEL).strip(),
            device=(os.environ.get("MNEMOS_STT_DEVICE") or DEFAULT_DEVICE).strip(),
            compute_type=(os.environ.get("MNEMOS_STT_COMPUTE") or DEFAULT_COMPUTE).strip(),
            language=lang,
            beam_size=max(1, beam),
        )


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None
    duration_s: float
    engine: str
    model: str


class STTEngine(Protocol):
    """Minimal contract a backend must satisfy. ``transcribe`` is sync and
    CPU-bound; the API layer runs it in a threadpool so it never blocks the
    event loop."""

    name: str

    def transcribe(self, audio: bytes, *, language: str | None = None) -> TranscriptionResult:
        ...


def max_upload_bytes() -> int:
    try:
        v = int(os.environ.get("MNEMOS_STT_MAX_UPLOAD_BYTES", "") or DEFAULT_MAX_UPLOAD_BYTES)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return v if v > 0 else DEFAULT_MAX_UPLOAD_BYTES


def _importable(module: str) -> bool:
    """True iff ``module`` imports. Catches more than ImportError on purpose
    — a broken C-extension / shared library surfaces as ``OSError`` /
    ``RuntimeError`` at import, which must read as "unavailable", not crash
    availability probing."""
    try:
        __import__(module)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("%s not importable: %s: %s", module, exc.__class__.__name__, exc)
        return False


def _faster_whisper_importable() -> bool:
    return _importable("faster_whisper")


def _moonshine_importable() -> bool:
    # Moonshine needs its own package *and* PyAV to decode the browser's
    # webm/opus clip down to the 16 kHz mono PCM it expects.
    return _importable("moonshine_voice") and _importable("av")


def is_stt_available() -> bool:
    """True iff voice transcription can run right now: not opted out, the
    configured engine is known, and its backend imports. Mirrors
    ``is_agent_sdk_available`` so the UI/endpoint can degrade gracefully."""
    if os.environ.get("MNEMOS_DISABLE_STT") == "1":
        return False
    cfg = STTConfig.from_env()
    if cfg.engine in _MOONSHINE_ALIASES:
        return _moonshine_importable()
    if cfg.engine in _FASTER_WHISPER_ALIASES:
        return _faster_whisper_importable()
    return False


def _decode_pcm_16k_mono(raw: bytes) -> "np.ndarray":
    """Decode an arbitrary browser audio blob (webm/opus, mp4, ogg…) to a
    16 kHz mono float32 signal via PyAV (ffmpeg). A missing PyAV is a
    readiness problem (STTUnavailable → 503); an undecodable blob is the
    operator's input (ValueError → 422)."""
    import numpy as np

    try:
        import av
    except ImportError as exc:  # pragma: no cover - guarded by availability gate
        raise STTUnavailable("pyav_not_installed") from exc

    try:
        # mode="r" selects the InputContainer overload (it has .decode()).
        container = av.open(io.BytesIO(raw), mode="r")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"undecodable_audio:{exc.__class__.__name__}") from exc

    resampler = av.audio.resampler.AudioResampler(
        format="flt", layout="mono", rate=MOONSHINE_SAMPLE_RATE
    )
    chunks: list[np.ndarray] = []
    try:
        for frame in container.decode(audio=0):
            for rframe in resampler.resample(frame):
                chunks.append(rframe.to_ndarray().reshape(-1))
        # Flush any samples buffered in the resampler.
        for rframe in resampler.resample(None):
            chunks.append(rframe.to_ndarray().reshape(-1))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"decode_failed:{exc.__class__.__name__}") from exc
    finally:
        container.close()

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32)


class FasterWhisperEngine:
    """faster-whisper (CTranslate2) backend — multilingual, auto-detecting.

    The model is *lazy-loaded* on first transcribe and cached for the
    process lifetime — loading weights is the expensive step, so the first
    request after boot pays it once and every later request is warm.
    """

    name = "faster-whisper"

    def __init__(self, cfg: STTConfig) -> None:
        self._cfg = cfg
        self._model = None  # loaded on first use

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info(
                "loading faster-whisper model=%s device=%s compute=%s",
                self._cfg.model, self._cfg.device, self._cfg.compute_type,
            )
            self._model = WhisperModel(
                self._cfg.model,
                device=self._cfg.device,
                compute_type=self._cfg.compute_type,
            )
        return self._model

    def transcribe(self, audio: bytes, *, language: str | None = None) -> TranscriptionResult:
        # Loading the weights is a *readiness* concern, not a bad-input one:
        # a host that hasn't downloaded/pre-baked the model (e.g. air-gapped,
        # HF unreachable on first use) should surface 503 "not ready", not
        # 422 "your clip was bad". Wrap load failures as STTUnavailable so
        # the API maps them to 503 and the UI hides the mic accordingly.
        try:
            model = self._ensure_model()
        except STTUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "faster-whisper model load failed (model=%s): %s: %s",
                self._cfg.model, exc.__class__.__name__, exc,
            )
            raise STTUnavailable(f"model_load_failed:{exc.__class__.__name__}") from exc
        lang = (language or self._cfg.language) or None
        # faster-whisper decodes the container itself (PyAV/ffmpeg), so the
        # browser's webm/opus or mp4 blob is handed straight through.
        segments, info = model.transcribe(
            io.BytesIO(audio),
            language=lang,
            beam_size=self._cfg.beam_size,
            # Silero VAD trims silence around a short command → faster and
            # avoids hallucinated text on the trailing quiet.
            vad_filter=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", lang),
            duration_s=float(getattr(info, "duration", 0.0) or 0.0),
            engine=self.name,
            model=self._cfg.model,
        )


class MoonshineEngine:
    """Moonshine "Flavors of Moonshine" backend via ``moonshine-voice``.

    A flavor is one language, so the model is chosen from
    ``cfg.language`` (default ``ko`` → ``tiny-ko``) and the per-request
    language hint is ignored. The Transcriber is lazy-loaded + cached."""

    name = "moonshine"

    def __init__(self, cfg: STTConfig) -> None:
        self._cfg = cfg
        self._lang = (cfg.language or DEFAULT_MOONSHINE_LANGUAGE).lower()
        self._tr = None  # loaded on first use

    def _ensure_model(self):
        if self._tr is None:
            from moonshine_voice import (
                Transcriber,
                get_model_for_language,
                string_to_model_arch,
            )

            try:
                arch = string_to_model_arch(self._cfg.model)
            except Exception:  # noqa: BLE001 - unknown size string → let the lib default
                arch = None
            log.info("loading Moonshine flavor lang=%s arch=%s", self._lang, self._cfg.model)
            # Resolves + downloads (from download.moonshine.ai) the flavor.
            model_path, model_arch = get_model_for_language(self._lang, arch)
            self._tr = Transcriber(model_path, model_arch)
        return self._tr

    def transcribe(self, audio: bytes, *, language: str | None = None) -> TranscriptionResult:
        try:
            tr = self._ensure_model()
        except STTUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Moonshine model load failed (lang=%s): %s: %s",
                self._lang, exc.__class__.__name__, exc,
            )
            raise STTUnavailable(f"model_load_failed:{exc.__class__.__name__}") from exc

        samples = _decode_pcm_16k_mono(audio)  # raises ValueError on bad input -> 422
        transcript = tr.transcribe_without_streaming(
            samples.tolist(), sample_rate=MOONSHINE_SAMPLE_RATE
        )
        text = " ".join(
            (line.text or "").strip() for line in transcript.lines
        ).strip()
        return TranscriptionResult(
            text=text,
            language=self._lang,
            duration_s=round(len(samples) / MOONSHINE_SAMPLE_RATE, 2),
            engine=self.name,
            model=f"{self._cfg.model}-{self._lang}",
        )


@lru_cache(maxsize=1)
def get_engine() -> STTEngine:
    """Return the configured STT engine (cached so the loaded model stays
    warm across requests). Raises :class:`STTUnavailable` when none can
    run; callers should check :func:`is_stt_available` first and map the
    exception to ``503``."""
    if os.environ.get("MNEMOS_DISABLE_STT") == "1":
        raise STTUnavailable("stt_disabled")
    cfg = STTConfig.from_env()
    if cfg.engine in _MOONSHINE_ALIASES:
        if not _importable("moonshine_voice"):
            raise STTUnavailable("moonshine_not_installed")
        if not _importable("av"):
            raise STTUnavailable("pyav_not_installed")
        return MoonshineEngine(cfg)
    if cfg.engine in _FASTER_WHISPER_ALIASES:
        if not _faster_whisper_importable():
            raise STTUnavailable("faster_whisper_not_installed")
        return FasterWhisperEngine(cfg)
    raise STTUnavailable(f"unknown_stt_engine:{cfg.engine}")
