"""PR-155 — pluggable local speech-to-text (STT) engine.

Why faster-whisper is the default
─────────────────────────────────
The brief: research the lightest, latest, free open-source engine with a
good recognition rate, run it *locally*. The binding constraint for this
platform is that its operators are Korean (the whole UI ships an EN/한국어
switcher), so a spoken command is as likely to be Korean as English. That
rules out the very-lightest engines that are English-first:

* **Moonshine** (27-60 MB, ONNX) — the smallest, but its non-English
  models have no released ONNX exports yet; Korean isn't covered.
* **Vosk** — mature and tiny, but its small Korean model trails Whisper
  on accuracy for free-form commands.
* **SenseVoice** — excellent CJK accuracy, but pulls in the heavy FunASR
  stack — too much for an optional extra.

**faster-whisper** (Whisper re-implemented on CTranslate2) wins the
balance the brief asks for:

* multilingual incl. strong Korean *and* English (99 languages, one model);
* light + fast on CPU — INT8 quantization, ~2× faster and lower memory
  than ``openai-whisper`` for the same accuracy;
* small models — ``tiny`` ≈ 75 MB, ``base`` ≈ 145 MB — downloadable once
  and then fully offline (bake them into the image for an air-gapped host);
* trivially installable (``pip install faster-whisper``) and actively
  maintained in 2026.

The engine is intentionally pluggable (``MNEMOS_STT_ENGINE``) so a future
PR can add Vosk/Moonshine for ultra-light English-only deployments without
touching the API or the UI.

Tuning (all optional env, sensible defaults):
* ``MNEMOS_STT_MODEL``   — ``tiny``|``base``|``small``|… (default ``base``;
  drop to ``tiny`` for the absolute lightest, raise to ``small`` for better
  Korean at ~2× the footprint).
* ``MNEMOS_STT_DEVICE``  — ``cpu`` (default) | ``cuda``.
* ``MNEMOS_STT_COMPUTE`` — ``int8`` (default, lightest) | ``int8_float16`` | ``float16``.
* ``MNEMOS_STT_LANGUAGE``— force a language (``ko``/``en``); empty = auto-detect.
* ``MNEMOS_STT_BEAM_SIZE``— decoding beam (default 5 = Whisper's own default,
  best recognition; set 1 for greedy/fastest).
* ``MNEMOS_DISABLE_STT=1``— hard off, even if the package is installed
  (air-gapped opt-out, mirrors ``MNEMOS_DISABLE_AGENT_SDK``).
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

log = logging.getLogger(__name__)

DEFAULT_ENGINE = "faster-whisper"
DEFAULT_MODEL = "base"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE = "int8"
DEFAULT_BEAM_SIZE = 5

# Hard cap on accepted upload size. A spoken command is a few seconds of
# Opus — well under a megabyte — so 25 MB is generous head-room while
# still refusing a multi-hour file that would pin the CPU. Override with
# ``MNEMOS_STT_MAX_UPLOAD_BYTES``.
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Engine aliases that map to the faster-whisper backend.
_FASTER_WHISPER_ALIASES = frozenset({"faster-whisper", "faster_whisper", "whisper"})


class STTUnavailable(RuntimeError):
    """No engine can run (opted out, package missing, or unknown engine).

    Callers gate on :func:`is_stt_available` and translate this into a
    ``503 stt_unavailable`` rather than a 500.
    """


@dataclass(frozen=True)
class STTConfig:
    """Resolved engine configuration. Built from env so the always-loaded
    ``app.config.Settings`` stays free of optional-feature knobs (same
    convention as ``security/headers.py`` and ``extractor/agent_sdk.py``)."""

    engine: str = DEFAULT_ENGINE
    model: str = DEFAULT_MODEL
    device: str = DEFAULT_DEVICE
    compute_type: str = DEFAULT_COMPUTE
    language: str | None = None  # None = auto-detect
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


def _faster_whisper_importable() -> bool:
    """True iff faster-whisper can actually be imported. Catches more than
    ImportError on purpose — a broken CTranslate2/cuDNN shared library
    surfaces as ``OSError``/``RuntimeError`` at import, which must read as
    "unavailable", not crash availability probing."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("faster-whisper not importable: %s: %s", exc.__class__.__name__, exc)
        return False


def is_stt_available() -> bool:
    """True iff voice transcription can run right now: not opted out, the
    configured engine is known, and its backend imports. Mirrors
    ``is_agent_sdk_available`` so the UI/endpoint can degrade gracefully."""
    if os.environ.get("MNEMOS_DISABLE_STT") == "1":
        return False
    cfg = STTConfig.from_env()
    if cfg.engine in _FASTER_WHISPER_ALIASES:
        return _faster_whisper_importable()
    return False


class FasterWhisperEngine:
    """faster-whisper (CTranslate2) backend.

    The model is *lazy-loaded* on first transcribe and cached for the
    process lifetime — loading weights is the expensive step, so the
    first request after boot pays it once and every later request is warm.
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


@lru_cache(maxsize=1)
def get_engine() -> STTEngine:
    """Return the configured STT engine (cached so the loaded model stays
    warm across requests). Raises :class:`STTUnavailable` when none can
    run; callers should check :func:`is_stt_available` first and map the
    exception to ``503``."""
    if os.environ.get("MNEMOS_DISABLE_STT") == "1":
        raise STTUnavailable("stt_disabled")
    cfg = STTConfig.from_env()
    if cfg.engine in _FASTER_WHISPER_ALIASES:
        if not _faster_whisper_importable():
            raise STTUnavailable("faster_whisper_not_installed")
        return FasterWhisperEngine(cfg)
    raise STTUnavailable(f"unknown_stt_engine:{cfg.engine}")
