"""PR-155 — voice commands on the Ask tab.

The operator can speak a question instead of typing it. The browser
captures a short clip; the platform converts it to text with a *local*
faster-whisper model and drops the recognised text into the question box.

These tests pin the whole feature without needing faster-whisper installed
or any external service:

* the pluggable STT engine (availability gate, env config, result mapping)
  is exercised with a fake model;
* the ``/api/v1/voice/{transcribe,status}`` endpoints are driven through a
  minimal FastAPI app with the auth seam overridden and a fake engine
  injected — covering 503-when-disabled, the happy path + audit, the
  empty/oversize guards, and the operator-only gate;
* the frontend wiring, the security-header allowance, the i18n strings,
  and the optional-dependency packaging are asserted by source inspection
  (same style as test_pr150_ask_tab).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr155")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_TPL = _APP / "dashboard" / "templates"
_STATIC = _APP / "dashboard" / "static"


# ─── engine: availability gate ────────────────────────────────────


def test_is_stt_available_respects_opt_out(monkeypatch):
    from app.voice import engine

    monkeypatch.setattr(engine, "_faster_whisper_importable", lambda: True)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "faster-whisper")
    monkeypatch.setenv("MNEMOS_DISABLE_STT", "1")
    assert engine.is_stt_available() is False


def test_is_stt_available_false_when_package_missing(monkeypatch):
    from app.voice import engine

    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "faster-whisper")
    monkeypatch.setattr(engine, "_faster_whisper_importable", lambda: False)
    assert engine.is_stt_available() is False


def test_is_stt_available_true_when_importable(monkeypatch):
    from app.voice import engine

    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "faster-whisper")
    monkeypatch.setattr(engine, "_faster_whisper_importable", lambda: True)
    assert engine.is_stt_available() is True


def test_is_stt_available_false_for_unknown_engine(monkeypatch):
    from app.voice import engine

    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "bogus")
    monkeypatch.setattr(engine, "_faster_whisper_importable", lambda: True)
    assert engine.is_stt_available() is False


# ─── engine: config + bounds ──────────────────────────────────────


def test_config_from_env_defaults_and_overrides(monkeypatch):
    from app.voice.engine import (
        DEFAULT_BEAM_SIZE,
        DEFAULT_MODEL,
        STTConfig,
    )

    for k in (
        "MNEMOS_STT_ENGINE", "MNEMOS_STT_MODEL", "MNEMOS_STT_DEVICE",
        "MNEMOS_STT_COMPUTE", "MNEMOS_STT_LANGUAGE", "MNEMOS_STT_BEAM_SIZE",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = STTConfig.from_env()
    assert cfg.engine == "faster-whisper"
    assert cfg.model == DEFAULT_MODEL
    assert cfg.device == "cpu"
    assert cfg.compute_type == "int8"
    assert cfg.language is None
    assert cfg.beam_size == DEFAULT_BEAM_SIZE

    monkeypatch.setenv("MNEMOS_STT_MODEL", "small")
    monkeypatch.setenv("MNEMOS_STT_LANGUAGE", "ko")
    monkeypatch.setenv("MNEMOS_STT_BEAM_SIZE", "1")
    cfg2 = STTConfig.from_env()
    assert cfg2.model == "small"
    assert cfg2.language == "ko"
    assert cfg2.beam_size == 1

    # A non-numeric beam falls back to the default rather than crashing.
    monkeypatch.setenv("MNEMOS_STT_BEAM_SIZE", "abc")
    assert STTConfig.from_env().beam_size == DEFAULT_BEAM_SIZE


def test_max_upload_bytes(monkeypatch):
    from app.voice.engine import DEFAULT_MAX_UPLOAD_BYTES, max_upload_bytes

    monkeypatch.delenv("MNEMOS_STT_MAX_UPLOAD_BYTES", raising=False)
    assert max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES
    monkeypatch.setenv("MNEMOS_STT_MAX_UPLOAD_BYTES", "1000")
    assert max_upload_bytes() == 1000
    # Zero / negative / garbage all fall back to the default.
    monkeypatch.setenv("MNEMOS_STT_MAX_UPLOAD_BYTES", "-5")
    assert max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES
    monkeypatch.setenv("MNEMOS_STT_MAX_UPLOAD_BYTES", "xx")
    assert max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES


# ─── engine: faster-whisper result mapping (fake model) ───────────


def test_faster_whisper_engine_maps_segments(monkeypatch):
    """Segment join + info → TranscriptionResult, and the decoding knobs
    are forwarded — all without faster-whisper installed."""
    from app.voice.engine import FasterWhisperEngine, STTConfig, TranscriptionResult

    eng = FasterWhisperEngine(STTConfig(model="base", beam_size=5))

    class _Seg:
        def __init__(self, t):
            self.text = t

    class _Info:
        language = "ko"
        duration = 3.5

    class _FakeModel:
        def __init__(self):
            self.calls = []

        def transcribe(self, audio, **kw):
            self.calls.append(kw)
            return iter([_Seg(" 안녕하세요 "), _Seg("주문 처리기 어디야")]), _Info()

    fake = _FakeModel()
    monkeypatch.setattr(eng, "_ensure_model", lambda: fake)

    res = eng.transcribe(b"\x00\x01\x02", language="ko")
    assert isinstance(res, TranscriptionResult)
    assert res.text == "안녕하세요 주문 처리기 어디야"
    assert res.language == "ko"
    assert res.duration_s == 3.5
    assert res.engine == "faster-whisper"
    assert res.model == "base"
    # Language hint + VAD + beam forwarded to the underlying model.
    assert fake.calls and fake.calls[0]["language"] == "ko"
    assert fake.calls[0]["vad_filter"] is True
    assert fake.calls[0]["beam_size"] == 5


def test_faster_whisper_model_load_failure_is_unavailable(monkeypatch):
    """A host that can't download/load the weights (air-gapped, HF
    unreachable) is a *readiness* failure → STTUnavailable (→ 503),
    not a bad-input 422."""
    from app.voice.engine import FasterWhisperEngine, STTConfig, STTUnavailable

    eng = FasterWhisperEngine(STTConfig(model="tiny"))

    def _boom():
        raise OSError("Host not in allowlist")

    monkeypatch.setattr(eng, "_ensure_model", _boom)
    with pytest.raises(STTUnavailable):
        eng.transcribe(b"\x00\x01\x02")


# ─── engine: factory ──────────────────────────────────────────────


def test_get_engine_raises_when_unavailable(monkeypatch):
    from app.voice import engine

    engine.get_engine.cache_clear()
    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "faster-whisper")
    monkeypatch.setattr(engine, "_faster_whisper_importable", lambda: False)
    with pytest.raises(engine.STTUnavailable):
        engine.get_engine()
    engine.get_engine.cache_clear()


def test_get_engine_returns_faster_whisper(monkeypatch):
    from app.voice import engine

    engine.get_engine.cache_clear()
    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "faster-whisper")
    # Pretend the package is importable; the model itself is lazy so this
    # returns the engine object without loading any weights.
    monkeypatch.setattr(engine, "_faster_whisper_importable", lambda: True)
    eng = engine.get_engine()
    assert eng.name == "faster-whisper"
    engine.get_engine.cache_clear()


def test_get_engine_unknown_engine_raises(monkeypatch):
    from app.voice import engine

    engine.get_engine.cache_clear()
    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "bogus")
    with pytest.raises(engine.STTUnavailable):
        engine.get_engine()
    engine.get_engine.cache_clear()


def test_coerce_project_id():
    from app.api.voice import _coerce_project_id

    assert _coerce_project_id("") is None
    assert _coerce_project_id(None) is None
    assert _coerce_project_id("not-a-uuid") is None
    u = uuid.uuid4()
    assert _coerce_project_id(str(u)) == u


# ─── endpoint wiring + behaviour ──────────────────────────────────


def test_voice_routes_registered():
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/v1/voice/transcribe" in paths
    assert "/api/v1/voice/status" in paths


def _build_app(role: str = "operator", *, override_user: bool = True):
    """Minimal app with just the voice router so we sidestep the CSRF /
    rate-limit / session middleware stack and any live DB/Redis. The auth
    seam (``current_user``) is the single override point — both
    ``CurrentUser`` and ``require_operator`` depend on it."""
    from fastapi import FastAPI

    from app.api import voice as voice_api
    from app.auth.deps import current_user
    from app.db import get_session

    app = FastAPI()
    app.include_router(voice_api.router)

    async def _fake_session():
        yield None

    app.dependency_overrides[get_session] = _fake_session
    if override_user:
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=uuid.uuid4(), role=role
        )
    return app


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_transcribe_503_when_unavailable(monkeypatch):
    from app.api import voice as voice_api

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: False)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/transcribe",
        files={"audio": ("c.webm", b"\x00\x01\x02", "audio/webm")},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == "stt_unavailable"


def test_transcribe_happy_path_and_audit(monkeypatch):
    from app.api import voice as voice_api
    from app.voice.engine import TranscriptionResult

    captured: dict = {}

    class _FakeEngine:
        name = "faster-whisper"

        def transcribe(self, audio, *, language=None):
            captured["audio"] = audio
            captured["language"] = language
            return TranscriptionResult(
                text="주문 처리기 어디",
                language="ko",
                duration_s=2.0,
                engine="faster-whisper",
                model="base",
            )

    async def _noop_audit(**kw):
        captured["audit"] = kw

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
    monkeypatch.setattr(voice_api, "get_engine", lambda: _FakeEngine())
    monkeypatch.setattr(voice_api, "audit_record", _noop_audit)

    r = _client(_build_app("operator")).post(
        "/api/v1/voice/transcribe",
        files={"audio": ("c.webm", b"\x00\x01\x02\x03", "audio/webm")},
        data={"language": "ko", "project_id": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "주문 처리기 어디"
    assert body["language"] == "ko"
    assert body["engine"] == "faster-whisper"
    assert body["model"] == "base"
    # The raw audio + language hint reached the engine.
    assert captured["audio"] == b"\x00\x01\x02\x03"
    assert captured["language"] == "ko"
    # Audited as voice.transcribe with a truncated transcript + metadata,
    # and the empty project_id coerced to None (not a 422).
    audit = captured["audit"]
    assert audit["action"] == "voice.transcribe"
    assert "주문" in audit["target"]
    assert audit["project_id"] is None
    assert audit["details"]["engine"] == "faster-whisper"
    assert audit["details"]["chars"] == len("주문 처리기 어디")


def test_transcribe_model_not_ready_returns_503(monkeypatch):
    """If the engine raises STTUnavailable mid-call (model can't load),
    the endpoint returns 503, not 500/422 — the UI then hides the mic."""
    from app.api import voice as voice_api
    from app.voice.engine import STTUnavailable

    class _FailingEngine:
        name = "faster-whisper"

        def transcribe(self, audio, *, language=None):
            raise STTUnavailable("model_load_failed:OSError")

    async def _noop(**kw):
        return None

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
    monkeypatch.setattr(voice_api, "get_engine", lambda: _FailingEngine())
    monkeypatch.setattr(voice_api, "audit_record", _noop)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/transcribe",
        files={"audio": ("c.webm", b"\x00\x01\x02", "audio/webm")},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == "stt_unavailable"


def test_transcribe_decode_failure_returns_422(monkeypatch):
    """A genuine decode failure (corrupt clip) is the operator's input —
    that stays a 422, distinct from the 503 readiness case above."""
    from app.api import voice as voice_api

    class _BadAudioEngine:
        name = "faster-whisper"

        def transcribe(self, audio, *, language=None):
            raise ValueError("could not decode audio")

    async def _noop(**kw):
        return None

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
    monkeypatch.setattr(voice_api, "get_engine", lambda: _BadAudioEngine())
    monkeypatch.setattr(voice_api, "audit_record", _noop)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/transcribe",
        files={"audio": ("c.webm", b"\x00\x01\x02", "audio/webm")},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "transcription_failed"


def test_transcribe_empty_audio_400(monkeypatch):
    from app.api import voice as voice_api

    async def _noop(**kw):
        return None

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
    monkeypatch.setattr(voice_api, "audit_record", _noop)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/transcribe",
        files={"audio": ("c.webm", b"", "audio/webm")},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "empty_audio"


def test_transcribe_too_large_413(monkeypatch):
    from app.api import voice as voice_api

    async def _noop(**kw):
        return None

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
    monkeypatch.setattr(voice_api, "max_upload_bytes", lambda: 4)
    monkeypatch.setattr(voice_api, "audit_record", _noop)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/transcribe",
        files={"audio": ("c.webm", b"\x00\x01\x02\x03\x04\x05\x06\x07", "audio/webm")},
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "audio_too_large"


def test_transcribe_requires_operator(monkeypatch):
    from app.api import voice as voice_api

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
    r = _client(_build_app("viewer")).post(
        "/api/v1/voice/transcribe",
        files={"audio": ("c.webm", b"\x00\x01\x02", "audio/webm")},
    )
    assert r.status_code == 403


def test_status_reports_availability(monkeypatch):
    from app.api import voice as voice_api

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
    r = _client(_build_app("operator")).get("/api/v1/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert "engine" in body and "model" in body


def test_status_requires_auth():
    r = _client(_build_app(override_user=False)).get("/api/v1/voice/status")
    assert r.status_code == 401


# ─── frontend wiring (source inspection) ──────────────────────────


def test_ask_template_has_mic_and_wires_voice():
    html = (_TPL / "ask.html").read_text(encoding="utf-8")
    assert 'id="aq-mic"' in html
    assert 'class="mic-btn"' in html
    assert "mountVoiceInput" in html
    # The mic targets the question box and passes the project picker.
    assert '"#aq-q"' in html


def test_ui_js_has_voice_helper_exported():
    js = (_STATIC / "ui.js").read_text(encoding="utf-8")
    assert "function mountVoiceInput(" in js
    idx = js.find("window.MnemosUI = {")
    assert idx != -1
    assert "mountVoiceInput: mountVoiceInput" in js[idx:idx + 2500]
    # Browser-side capture, server-side (local) recognition.
    assert "/api/v1/voice/transcribe" in js
    assert "/api/v1/voice/status" in js
    assert "MediaRecorder" in js
    assert "getUserMedia" in js


def test_ui_js_voice_korean_phrases():
    js = (_STATIC / "ui.js").read_text(encoding="utf-8")
    assert "음성 입력 시작" in js
    assert "이 서버에서는 음성 인식이 활성화되어 있지 않습니다." in js
    assert "마이크 접근이 차단되었습니다" in js


def test_mic_button_css_present():
    css = (_STATIC / "app.css").read_text(encoding="utf-8")
    assert ".mic-btn" in css
    assert ".mic-btn.recording" in css


def test_ask_template_still_parses():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(_TPL)),
        autoescape=select_autoescape(["html"]),
    )
    env.parse((_TPL / "ask.html").read_text(encoding="utf-8"))


# ─── security header + packaging ──────────────────────────────────


def test_security_header_allows_microphone_self():
    src = (_APP / "security" / "headers.py").read_text(encoding="utf-8")
    assert "microphone=(self)" in src
    # Everything else the dashboard doesn't need stays denied.
    assert "camera=()" in src
    assert "geolocation=()" in src


def test_pyproject_voice_extra():
    pp = (_SERVER / "pyproject.toml").read_text(encoding="utf-8")
    assert "voice = [" in pp
    assert "faster-whisper" in pp


def test_env_example_documents_stt():
    env = (_SERVER.parent / ".env.example").read_text(encoding="utf-8")
    for key in (
        "MNEMOS_DISABLE_STT",
        "MNEMOS_STT_MODEL",
        "MNEMOS_STT_ENGINE",
        "MNEMOS_STT_LANGUAGE",
    ):
        assert key in env, f".env.example missing {key}"
