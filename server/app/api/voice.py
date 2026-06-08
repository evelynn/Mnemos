"""PR-155 — voice-command transcription endpoint.

The Ask tab records a short clip and POSTs it here; the platform converts
speech to text with the local STT engine (see ``app/voice/engine.py``) and
returns the recognised text, which the UI drops into the question box.

Design notes
* **Operator-gated.** Voice is a command surface — the same authority bar
  as ``POST /ask`` (``require_operator``). A viewer can't drive the system
  by voice any more than they can by typing.
* **Not project-scoped.** Transcription just turns audio into text; it
  touches no project graph or DB, so it needs no org/project ACL. The
  *resulting* text is then sent to the already-scoped ``/ask`` endpoint.
* **Graceful when disabled.** No engine installed → ``503 stt_unavailable``
  (mirrors ``ask.py``'s ``agent_sdk_unavailable``). The UI calls
  ``GET /voice/status`` first and hides the mic when unavailable.
* **Audited.** Every transcription is audit-logged (``voice.transcribe``)
  with the recognised text truncated to 120 chars — same shape as
  ``qa.ask`` logging the question. Spec §14.4 "append-only, mandatory".
* **Non-blocking.** The model is CPU-bound and synchronous, so it runs in
  a threadpool and never stalls the event loop.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.rbac import require_operator
from app.voice.engine import (
    STTConfig,
    STTUnavailable,
    get_engine,
    is_stt_available,
    max_upload_bytes,
)
from app.voice.tts import (
    TTSConfig,
    TTSUnavailable,
    get_tts_engine,
    is_tts_available,
    max_tts_chars,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])


@router.get("/status")
async def status(user: CurrentUser) -> dict:
    """Report whether voice input (STT) and output (TTS) are available and
    which engine/model/voice is configured. The Ask tab uses this to show
    or hide the mic + listen buttons instead of clicking into a 503.

    The flat ``available``/``engine``/``model`` keys describe STT and are
    kept for backward compatibility; ``stt``/``tts`` carry the full pair."""
    stt = STTConfig.from_env()
    tts = TTSConfig.from_env()
    return {
        "available": is_stt_available(),
        "engine": stt.engine,
        "model": stt.model,
        "language": stt.language,
        "stt": {
            "available": is_stt_available(),
            "engine": stt.engine,
            "model": stt.model,
            "language": stt.language,
        },
        "tts": {
            "available": is_tts_available(),
            "engine": tts.engine,
            "voice": tts.voice,
            "lang_code": tts.lang_code,
        },
    }


def _coerce_project_id(raw: str | None) -> uuid.UUID | None:
    """The form may send an empty string (no project picked) — treat that
    and any unparseable value as "no project" rather than 422-ing the
    whole transcription over an audit-only field."""
    if not raw:
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        return None


@router.post("/transcribe", dependencies=[Depends(require_operator)])
async def transcribe(
    user: CurrentUser,
    audio: UploadFile = File(...),
    language: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
) -> dict:
    if not is_stt_available():
        raise HTTPException(status_code=503, detail="stt_unavailable")

    cap = max_upload_bytes()
    # Reject oversize before reading the body into memory when Starlette
    # has the part size from the multipart headers.
    if audio.size is not None and audio.size > cap:
        raise HTTPException(status_code=413, detail="audio_too_large")

    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty_audio")
    if len(raw) > cap:
        raise HTTPException(status_code=413, detail="audio_too_large")

    lang = (language or "").strip() or None
    try:
        engine = get_engine()
        result = await run_in_threadpool(engine.transcribe, raw, language=lang)
    except STTUnavailable:
        # Engine vanished between the availability probe and now (opt-out
        # flipped, package broken) — still a 503, not a 500.
        raise HTTPException(status_code=503, detail="stt_unavailable")
    except Exception as exc:  # noqa: BLE001
        # A corrupt/undecodable clip is the operator's input, not a server
        # fault — surface it as 422 and keep the trace out of the response
        # (PR-132b "no exc-str leak").
        log.warning(
            "voice.transcribe failed: %s: %s", exc.__class__.__name__, exc
        )
        raise HTTPException(status_code=422, detail="transcription_failed")

    text = (result.text or "").strip()
    await audit_record(
        actor=f"user:{user.id}",
        action="voice.transcribe",
        target=text[:120],
        project_id=_coerce_project_id(project_id),
        details={
            "engine": result.engine,
            "model": result.model,
            "language": result.language,
            "duration_s": round(result.duration_s, 2),
            "chars": len(text),
            "bytes": len(raw),
        },
    )
    return {
        "text": text,
        "language": result.language,
        "duration_s": result.duration_s,
        "engine": result.engine,
        "model": result.model,
    }


class SpeakRequest(BaseModel):
    # Hard upper bound stops an abusive payload at the schema; the engine
    # then truncates to max_tts_chars() (default 2000) for what it reads.
    text: str = Field(min_length=1, max_length=8000)
    voice: str | None = Field(default=None, max_length=64)


@router.post("/speak")
async def speak(user: CurrentUser, body: SpeakRequest) -> Response:
    """Synthesise speech for ``text`` and return audio/wav. Any signed-in
    user may have an answer read aloud — it's a read-only transform of text
    they can already see, so no operator gate. 503 when no TTS engine is
    installed; the listen button hides accordingly."""
    if not is_tts_available():
        raise HTTPException(status_code=503, detail="tts_unavailable")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty_text")
    cap = max_tts_chars()
    truncated = len(text) > cap
    if truncated:
        text = text[:cap]

    voice = (body.voice or "").strip() or None
    try:
        engine = get_tts_engine()
        result = await run_in_threadpool(engine.synthesize, text, voice=voice)
    except TTSUnavailable:
        raise HTTPException(status_code=503, detail="tts_unavailable")
    except Exception as exc:  # noqa: BLE001
        log.warning("voice.speak failed: %s: %s", exc.__class__.__name__, exc)
        raise HTTPException(status_code=422, detail="synthesis_failed")

    await audit_record(
        actor=f"user:{user.id}",
        action="voice.speak",
        target=text[:120],
        details={
            "engine": result.engine,
            "voice": result.voice,
            "chars": len(text),
            "truncated": truncated,
            "bytes": len(result.audio_wav),
        },
    )
    return Response(
        content=result.audio_wav,
        media_type="audio/wav",
        headers={
            "X-TTS-Engine": result.engine,
            "X-TTS-Voice": result.voice,
            "Cache-Control": "no-store",
        },
    )
