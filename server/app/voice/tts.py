"""PR-156 — local text-to-speech (TTS) so Mnemos can read answers aloud.

The Ask tab gets a "listen" button: the recognised/typed question is
answered as usual, and the operator can have the answer **spoken back**.
Together with the PR-155 voice *input*, this closes the loop — ask by
voice, hear the answer by voice.

Engine — Kokoro-82M
───────────────────
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) is the model the
operator asked for, and it's an excellent fit: 82M params, **Apache-2.0**,
~350 MB, and it punches well above its weight on TTS leaderboards while
staying light enough to run on CPU. v1.0 covers multiple languages incl.
**Korean** (voices prefixed ``k``, via the ``misaki[ko]`` G2P).

Like the STT side this is *local* (runs on the operator's deployment, no
text leaves the box) and an *optional* dependency — install with
``pip install 'mnemos-platform[tts]'``. Absent → ``/voice/speak`` returns
503 and the listen button hides.

The backend is pluggable (``MNEMOS_TTS_ENGINE``). The default ``kokoro``
package uses PyTorch; an operator who wants to avoid torch can point at the
lighter ONNX runtime build
([kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)) in a future
backend — the API and UI don't change.

Voice ⇒ language: a Kokoro voice id encodes its language in the first
letter (``a``=US English, ``b``=British, ``k``=Korean, ``j``=Japanese,
``z``=Chinese …) and gender in the second. So the lang_code is derived
from the chosen voice (``af_heart`` → ``a``; ``kf_*`` → ``k``); override
with ``MNEMOS_TTS_LANG`` if needed. Default voice ``af_heart`` works out of
the box (English); Korean narration is an opt-in env + ``misaki[ko]``.
"""

from __future__ import annotations

import io
import logging
import os
import wave
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

log = logging.getLogger(__name__)

DEFAULT_TTS_ENGINE = "kokoro"
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0
# Kokoro emits 24 kHz mono float audio.
KOKORO_SAMPLE_RATE = 24000
# A spoken answer is short; cap so a pasted wall of text can't pin the CPU.
DEFAULT_MAX_TTS_CHARS = 2000

_KOKORO_ALIASES = frozenset({"kokoro", "kokoro-82m", "kokoro_82m"})


class TTSUnavailable(RuntimeError):
    """No TTS engine can run (opted out, package missing, model can't load,
    or unknown engine). The API maps this to ``503 tts_unavailable``."""


@dataclass(frozen=True)
class TTSConfig:
    engine: str = DEFAULT_TTS_ENGINE
    voice: str = DEFAULT_VOICE
    lang_code: str = "a"  # derived from voice[0] unless MNEMOS_TTS_LANG set
    speed: float = DEFAULT_SPEED

    @classmethod
    def from_env(cls) -> TTSConfig:
        voice = (os.environ.get("MNEMOS_TTS_VOICE") or DEFAULT_VOICE).strip()
        # A Kokoro voice id encodes language in its first letter; honour an
        # explicit override but otherwise derive it so the operator only has
        # to set one knob.
        lang = (os.environ.get("MNEMOS_TTS_LANG") or "").strip()
        if not lang:
            lang = voice[:1] or "a"
        try:
            speed = float(os.environ.get("MNEMOS_TTS_SPEED", "") or DEFAULT_SPEED)
        except ValueError:
            speed = DEFAULT_SPEED
        return cls(
            engine=(os.environ.get("MNEMOS_TTS_ENGINE") or DEFAULT_TTS_ENGINE).strip().lower(),
            voice=voice or DEFAULT_VOICE,
            lang_code=lang,
            # Kokoro accepts roughly 0.5–2.0×; clamp so a bad env can't make
            # speech unintelligible.
            speed=max(0.5, min(2.0, speed)),
        )


@dataclass(frozen=True)
class SynthesisResult:
    audio_wav: bytes
    sample_rate: int
    voice: str
    engine: str


class TTSEngine(Protocol):
    name: str

    def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult:
        ...


def max_tts_chars() -> int:
    try:
        v = int(os.environ.get("MNEMOS_TTS_MAX_CHARS", "") or DEFAULT_MAX_TTS_CHARS)
    except ValueError:
        return DEFAULT_MAX_TTS_CHARS
    return v if v > 0 else DEFAULT_MAX_TTS_CHARS


def _pcm_wav(samples: "np.ndarray", sample_rate: int) -> bytes:
    """Encode a float32 [-1, 1] mono signal as a 16-bit PCM WAV using only
    the stdlib (no soundfile dependency). numpy is always present when a
    real engine produced the samples."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size:
        arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _kokoro_importable() -> bool:
    try:
        import kokoro  # noqa: F401
        return True
    except Exception as exc:  # noqa: BLE001 - a broken torch load is "unavailable"
        log.debug("kokoro not importable: %s: %s", exc.__class__.__name__, exc)
        return False


def is_tts_available() -> bool:
    """True iff speaking answers can run now: not opted out, a known engine
    is selected, and its backend imports. Mirrors ``is_stt_available``."""
    if os.environ.get("MNEMOS_DISABLE_TTS") == "1":
        return False
    cfg = TTSConfig.from_env()
    if cfg.engine in _KOKORO_ALIASES:
        return _kokoro_importable()
    return False


class KokoroEngine:
    """Kokoro-82M via the official ``kokoro`` (KPipeline) package. The
    pipeline is lazy-loaded on first use (it downloads/loads the model) and
    cached for the process lifetime."""

    name = "kokoro"

    def __init__(self, cfg: TTSConfig) -> None:
        self._cfg = cfg
        self._pipe = None  # loaded on first synth

    def _ensure_pipe(self):
        if self._pipe is None:
            from kokoro import KPipeline

            log.info("loading Kokoro pipeline lang_code=%s", self._cfg.lang_code)
            self._pipe = KPipeline(lang_code=self._cfg.lang_code)
        return self._pipe

    def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult:
        # Loading the model is a *readiness* concern (air-gapped, HF
        # unreachable on first use) → surface as TTSUnavailable (503), not
        # a synthesis 422. Do this before importing numpy so a load failure
        # is reported as 503 even where numpy (a kokoro dep) isn't present.
        try:
            pipe = self._ensure_pipe()
        except TTSUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Kokoro pipeline load failed (lang=%s): %s: %s",
                self._cfg.lang_code, exc.__class__.__name__, exc,
            )
            raise TTSUnavailable(f"model_load_failed:{exc.__class__.__name__}") from exc

        import numpy as np

        v = (voice or self._cfg.voice).strip() or DEFAULT_VOICE
        chunks: list[np.ndarray] = []
        # KPipeline yields one result per chunk. Across versions a result is
        # either a NamedTuple with ``.audio`` or a ``(graphemes, phonemes,
        # audio)`` tuple — handle both. ``audio`` is a torch tensor.
        for item in pipe(text, voice=v, speed=self._cfg.speed):
            audio = getattr(item, "audio", None)
            if audio is None and isinstance(item, (tuple, list)) and len(item) >= 3:
                audio = item[2]
            if audio is None:
                continue
            a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(np.asarray(a, dtype=np.float32).reshape(-1))

        samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return SynthesisResult(
            audio_wav=_pcm_wav(samples, KOKORO_SAMPLE_RATE),
            sample_rate=KOKORO_SAMPLE_RATE,
            voice=v,
            engine=self.name,
        )


@lru_cache(maxsize=1)
def get_tts_engine() -> TTSEngine:
    """Return the configured TTS engine (cached so the loaded model stays
    warm). Raises :class:`TTSUnavailable` when none can run."""
    if os.environ.get("MNEMOS_DISABLE_TTS") == "1":
        raise TTSUnavailable("tts_disabled")
    cfg = TTSConfig.from_env()
    if cfg.engine in _KOKORO_ALIASES:
        if not _kokoro_importable():
            raise TTSUnavailable("kokoro_not_installed")
        return KokoroEngine(cfg)
    raise TTSUnavailable(f"unknown_tts_engine:{cfg.engine}")
