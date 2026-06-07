"""PR-155 — voice commands for the Ask (Q&A) surface.

The operator can speak a question/command on the Ask tab instead of
typing it. The browser records a short audio clip; the platform converts
it to text with a *local* speech-to-text model and drops the recognised
text into the question box.

"Local" is deliberate (spec §2 principle #8 — least-privilege, audited;
README "air-gapped"): recognition runs on the operator's own deployment
via faster-whisper, so no audio ever leaves the box. This is the opposite
of the browser ``SpeechRecognition`` API, which streams microphone audio
to a cloud vendor — unacceptable for a tool that analyses a customer's
private production system.

The STT engine is an *optional* dependency, mirroring the rest of the
platform's slim-base philosophy (``[search]``, ``[local]`` extras). Install
with ``pip install 'mnemos-platform[voice]'``. When it is absent the
transcribe endpoint returns ``503 stt_unavailable`` and the Ask tab hides
the mic button — exactly the graceful-degradation pattern already used for
the Claude Agent SDK (``is_agent_sdk_available`` → 503).
"""

from app.voice.engine import (
    STTConfig,
    STTUnavailable,
    TranscriptionResult,
    get_engine,
    is_stt_available,
    max_upload_bytes,
)
from app.voice.tts import (
    SynthesisResult,
    TTSConfig,
    TTSUnavailable,
    get_tts_engine,
    is_tts_available,
    max_tts_chars,
)

__all__ = [
    # STT (speech -> text, PR-155)
    "STTConfig",
    "STTUnavailable",
    "TranscriptionResult",
    "get_engine",
    "is_stt_available",
    "max_upload_bytes",
    # TTS (text -> speech, PR-156)
    "SynthesisResult",
    "TTSConfig",
    "TTSUnavailable",
    "get_tts_engine",
    "is_tts_available",
    "max_tts_chars",
]
