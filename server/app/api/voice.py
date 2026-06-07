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

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

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

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])


@router.get("/status")
async def status(user: CurrentUser) -> dict:
    """Report whether voice transcription is available and which engine /
    model is configured. The Ask tab uses this to show or hide the mic
    button instead of letting the operator click into a 503."""
    cfg = STTConfig.from_env()
    return {
        "available": is_stt_available(),
        "engine": cfg.engine,
        "model": cfg.model,
        "language": cfg.language,
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
