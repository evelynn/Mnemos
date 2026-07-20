"""PR-156 — Moonshine tiny-ko STT (default) + Kokoro-82M TTS (read aloud).

Two changes, both pinned here without needing the heavy model weights or
any network (the real engines can't download in CI):

* STT default switched to **Moonshine** "Flavors of Moonshine" (lightest
  strong-Korean engine; faster-whisper kept as a selectable alternative).
  Covered: the engine dispatch, the PyAV 16 kHz mono decoder, transcript →
  text mapping with a fake transcriber, and model-load → 503.
* **Kokoro-82M TTS** added so the Ask tab can read answers aloud. Covered:
  the availability gate, voice→lang derivation, the stdlib WAV encoder, the
  KokoroEngine with a fake pipeline, and the ``/voice/speak`` endpoint
  (200/400/503/422 + audit + no operator gate).

Frontend/header/packaging wiring is asserted by source inspection.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr156")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_TPL = _APP / "dashboard" / "templates"
_STATIC = _APP / "dashboard" / "static"


# ═══ STT — Moonshine default ══════════════════════════════════════


def test_default_engine_is_moonshine():
    from app.voice import engine

    assert engine.DEFAULT_ENGINE == "moonshine"
    assert engine.DEFAULT_MODEL == "tiny"
    assert engine.DEFAULT_MOONSHINE_LANGUAGE == "ko"


def test_moonshine_availability_needs_pkg_and_pyav(monkeypatch):
    from app.voice import engine

    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "moonshine")

    seen = {}

    def fake_importable(mod):
        seen[mod] = seen.get(mod, 0) + 1
        return {"moonshine_voice": True, "av": True}.get(mod, False)

    monkeypatch.setattr(engine, "_importable", fake_importable)
    assert engine.is_stt_available() is True

    # Missing PyAV → unavailable (can't decode the clip).
    monkeypatch.setattr(
        engine, "_importable", lambda m: m == "moonshine_voice"
    )
    assert engine.is_stt_available() is False
    # Missing the package → unavailable.
    monkeypatch.setattr(engine, "_importable", lambda m: m == "av")
    assert engine.is_stt_available() is False


def test_moonshine_engine_maps_transcript_lines(monkeypatch):
    from app.voice import engine
    from app.voice.engine import MoonshineEngine, STTConfig, TranscriptionResult

    eng = MoonshineEngine(STTConfig(engine="moonshine", model="tiny", language="ko"))

    captured = {}

    class _FakeTranscriber:
        def transcribe_without_streaming(self, audio_data, sample_rate=16000):
            captured["n"] = len(audio_data)
            captured["sr"] = sample_rate
            line1 = SimpleNamespace(text=" 주문 처리기 ")
            line2 = SimpleNamespace(text="어디야")
            return SimpleNamespace(lines=[line1, line2])

    monkeypatch.setattr(eng, "_ensure_model", lambda: _FakeTranscriber())
    # Avoid needing a real codec: stub the decoder with 16000 samples (1 s).
    import array

    monkeypatch.setattr(
        engine, "_decode_pcm_16k_mono", lambda raw: array.array("f", [0.0] * 16000)
    )

    res = eng.transcribe(b"\x00\x01\x02", language="en")  # per-req hint ignored
    assert isinstance(res, TranscriptionResult)
    assert res.text == "주문 처리기 어디야"
    assert res.language == "ko"  # flavor language, not the per-request hint
    assert res.engine == "moonshine"
    assert res.model == "tiny-ko"
    assert captured["sr"] == 16000
    assert captured["n"] == 16000


def test_moonshine_model_load_failure_is_unavailable(monkeypatch):
    from app.voice.engine import MoonshineEngine, STTConfig, STTUnavailable

    eng = MoonshineEngine(STTConfig(engine="moonshine", language="ko"))

    def _boom():
        raise OSError("download.moonshine.ai not in allowlist")

    monkeypatch.setattr(eng, "_ensure_model", _boom)
    with pytest.raises(STTUnavailable):
        eng.transcribe(b"\x00\x01\x02")


def test_get_engine_dispatches_moonshine(monkeypatch):
    from app.voice import engine

    engine.get_engine.cache_clear()
    monkeypatch.delenv("MNEMOS_DISABLE_STT", raising=False)
    monkeypatch.setenv("MNEMOS_STT_ENGINE", "moonshine")
    monkeypatch.setattr(engine, "_importable", lambda m: m in ("moonshine_voice", "av"))
    eng = engine.get_engine()
    assert eng.name == "moonshine"
    engine.get_engine.cache_clear()


def test_pyav_decoder_resamples_to_16k_mono():
    """Real PyAV: a 44.1 kHz stereo WAV decodes to ~16 kHz mono float32."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("av")
    import io
    import math
    import struct
    import wave

    from app.voice.engine import _decode_pcm_16k_mono

    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(44100)
    w.writeframes(
        b"".join(
            struct.pack("<hh", int(1000 * math.sin(t / 20)), int(900 * math.sin(t / 25)))
            for t in range(44100)
        )
    )
    w.close()

    pcm = _decode_pcm_16k_mono(buf.getvalue())
    assert pcm.dtype == np.float32
    assert abs(len(pcm) - 16000) < 800  # ~1 s at 16 kHz


def test_pyav_decoder_rejects_garbage():
    pytest.importorskip("av")
    from app.voice.engine import _decode_pcm_16k_mono

    with pytest.raises(ValueError):
        _decode_pcm_16k_mono(b"this is not audio at all")


# ═══ TTS — Kokoro availability + config ═══════════════════════════


def test_is_tts_available_respects_opt_out(monkeypatch):
    from app.voice import tts

    monkeypatch.setattr(tts, "_kokoro_importable", lambda: True)
    monkeypatch.setenv("MNEMOS_TTS_ENGINE", "kokoro")
    monkeypatch.setenv("MNEMOS_DISABLE_TTS", "1")
    assert tts.is_tts_available() is False


def test_is_tts_available_false_when_package_missing(monkeypatch):
    from app.voice import tts

    monkeypatch.delenv("MNEMOS_DISABLE_TTS", raising=False)
    monkeypatch.setenv("MNEMOS_TTS_ENGINE", "kokoro")
    monkeypatch.setattr(tts, "_kokoro_importable", lambda: False)
    assert tts.is_tts_available() is False


def test_is_tts_available_true_and_unknown_engine(monkeypatch):
    from app.voice import tts

    monkeypatch.delenv("MNEMOS_DISABLE_TTS", raising=False)
    monkeypatch.setattr(tts, "_kokoro_importable", lambda: True)
    monkeypatch.setenv("MNEMOS_TTS_ENGINE", "kokoro")
    assert tts.is_tts_available() is True
    monkeypatch.setenv("MNEMOS_TTS_ENGINE", "bogus")
    assert tts.is_tts_available() is False


def test_tts_config_derives_lang_from_voice(monkeypatch):
    from app.voice.tts import DEFAULT_VOICE, TTSConfig

    for k in ("MNEMOS_TTS_ENGINE", "MNEMOS_TTS_VOICE", "MNEMOS_TTS_LANG",
              "MNEMOS_TTS_SPEED"):
        monkeypatch.delenv(k, raising=False)
    cfg = TTSConfig.from_env()
    assert cfg.engine == "kokoro"
    assert cfg.voice == DEFAULT_VOICE
    assert cfg.lang_code == "a"  # af_heart -> American English
    assert cfg.speed == 1.0

    # A Korean voice id implies the Korean lang_code.
    monkeypatch.setenv("MNEMOS_TTS_VOICE", "kf_dora")
    assert TTSConfig.from_env().lang_code == "k"
    # Explicit override wins.
    monkeypatch.setenv("MNEMOS_TTS_LANG", "j")
    assert TTSConfig.from_env().lang_code == "j"
    # Speed clamps to the safe range; garbage falls back to default.
    monkeypatch.setenv("MNEMOS_TTS_SPEED", "9")
    assert TTSConfig.from_env().speed == 2.0
    monkeypatch.setenv("MNEMOS_TTS_SPEED", "x")
    assert TTSConfig.from_env().speed == 1.0


def test_max_tts_chars(monkeypatch):
    from app.voice.tts import DEFAULT_MAX_TTS_CHARS, max_tts_chars

    monkeypatch.delenv("MNEMOS_TTS_MAX_CHARS", raising=False)
    assert max_tts_chars() == DEFAULT_MAX_TTS_CHARS
    monkeypatch.setenv("MNEMOS_TTS_MAX_CHARS", "500")
    assert max_tts_chars() == 500
    monkeypatch.setenv("MNEMOS_TTS_MAX_CHARS", "0")
    assert max_tts_chars() == DEFAULT_MAX_TTS_CHARS


def test_pcm_wav_encoder_is_valid_wav():
    np = pytest.importorskip("numpy")
    import io
    import wave

    from app.voice.tts import KOKORO_SAMPLE_RATE, _pcm_wav

    samples = np.sin(np.linspace(0, 6.28, 2400, dtype=np.float32))
    wav = _pcm_wav(samples, KOKORO_SAMPLE_RATE)
    with wave.open(io.BytesIO(wav), "rb") as r:
        assert r.getnchannels() == 1
        assert r.getsampwidth() == 2
        assert r.getframerate() == KOKORO_SAMPLE_RATE
        assert r.getnframes() == 2400


def test_kokoro_engine_synthesizes_wav(monkeypatch):
    np = pytest.importorskip("numpy")
    import io
    import wave

    from app.voice.tts import KokoroEngine, SynthesisResult, TTSConfig

    eng = KokoroEngine(TTSConfig(voice="af_heart", lang_code="a"))

    class _FakePipe:
        def __call__(self, text, voice=None, speed=1.0):
            # Two chunks, tuple form (graphemes, phonemes, audio).
            yield ("g1", "p1", np.zeros(1200, dtype=np.float32))
            yield ("g2", "p2", np.ones(800, dtype=np.float32) * 0.1)

    monkeypatch.setattr(eng, "_ensure_pipe", lambda: _FakePipe())
    res = eng.synthesize("hello world", voice="af_bella")
    assert isinstance(res, SynthesisResult)
    assert res.engine == "kokoro"
    assert res.voice == "af_bella"
    with wave.open(io.BytesIO(res.audio_wav), "rb") as r:
        assert r.getframerate() == 24000
        assert r.getnframes() == 2000  # 1200 + 800 concatenated


def test_kokoro_model_load_failure_is_unavailable(monkeypatch):
    from app.voice.tts import KokoroEngine, TTSConfig, TTSUnavailable

    eng = KokoroEngine(TTSConfig())

    def _boom():
        raise RuntimeError("HF unreachable")

    monkeypatch.setattr(eng, "_ensure_pipe", _boom)
    with pytest.raises(TTSUnavailable):
        eng.synthesize("hi")


def test_get_tts_engine_dispatch(monkeypatch):
    from app.voice import tts

    tts.get_tts_engine.cache_clear()
    monkeypatch.delenv("MNEMOS_DISABLE_TTS", raising=False)
    monkeypatch.setenv("MNEMOS_TTS_ENGINE", "kokoro")
    monkeypatch.setattr(tts, "_kokoro_importable", lambda: True)
    assert tts.get_tts_engine().name == "kokoro"
    tts.get_tts_engine.cache_clear()
    monkeypatch.setenv("MNEMOS_TTS_ENGINE", "bogus")
    with pytest.raises(tts.TTSUnavailable):
        tts.get_tts_engine()
    tts.get_tts_engine.cache_clear()


# ═══ endpoints ════════════════════════════════════════════════════


def _build_app(role: str = "operator", *, override_user: bool = True):
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


def test_speak_route_registered():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/v1/voice/speak" in paths


def test_status_reports_both_stt_and_tts(monkeypatch):
    from app.api import voice as voice_api

    monkeypatch.setattr(voice_api, "is_stt_available", lambda: False)
    monkeypatch.setattr(voice_api, "is_tts_available", lambda: True)
    r = _client(_build_app("operator")).get("/api/v1/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["stt"]["available"] is False
    assert body["tts"]["available"] is True
    assert "voice" in body["tts"]
    # Flat STT keys retained for backward compatibility with PR-155 JS.
    assert body["available"] is False


def test_speak_503_when_unavailable(monkeypatch):
    from app.api import voice as voice_api

    monkeypatch.setattr(voice_api, "is_tts_available", lambda: False)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/speak", json={"text": "hello"}
    )
    assert r.status_code == 503
    assert r.json()["detail"] == "tts_unavailable"


def test_speak_happy_path_returns_wav_and_audits(monkeypatch):
    from app.api import voice as voice_api
    from app.voice.tts import SynthesisResult

    captured: dict = {}

    class _FakeEngine:
        name = "kokoro"

        def synthesize(self, text, *, voice=None):
            captured["text"] = text
            captured["voice"] = voice
            return SynthesisResult(
                audio_wav=b"RIFF\x00\x00WAVEfake", sample_rate=24000,
                voice=voice or "af_heart", engine="kokoro",
            )

    async def _noop_audit(**kw):
        captured["audit"] = kw

    monkeypatch.setattr(voice_api, "is_tts_available", lambda: True)
    monkeypatch.setattr(voice_api, "get_tts_engine", lambda: _FakeEngine())
    monkeypatch.setattr(voice_api, "audit_record", _noop_audit)

    r = _client(_build_app("viewer")).post(  # viewer is fine — no operator gate
        "/api/v1/voice/speak", json={"text": "  주문 처리기 어디 ", "voice": "kf_dora"}
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["X-TTS-Engine"] == "kokoro"
    assert r.headers["X-TTS-Voice"] == "kf_dora"
    assert r.content == b"RIFF\x00\x00WAVEfake"
    assert captured["text"] == "주문 처리기 어디"  # stripped
    assert captured["voice"] == "kf_dora"
    assert captured["audit"]["action"] == "voice.speak"
    assert "주문" in captured["audit"]["target"]


def test_speak_empty_text_400(monkeypatch):
    from app.api import voice as voice_api

    monkeypatch.setattr(voice_api, "is_tts_available", lambda: True)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/speak", json={"text": "   "}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "empty_text"


def test_speak_truncates_to_cap(monkeypatch):
    from app.api import voice as voice_api
    from app.voice.tts import SynthesisResult

    seen: dict = {}

    class _FakeEngine:
        name = "kokoro"

        def synthesize(self, text, *, voice=None):
            seen["len"] = len(text)
            return SynthesisResult(b"x", 24000, "af_heart", "kokoro")

    async def _noop(**kw):
        seen["audit"] = kw

    monkeypatch.setattr(voice_api, "is_tts_available", lambda: True)
    monkeypatch.setattr(voice_api, "get_tts_engine", lambda: _FakeEngine())
    monkeypatch.setattr(voice_api, "audit_record", _noop)
    monkeypatch.setattr(voice_api, "max_tts_chars", lambda: 10)

    r = _client(_build_app("operator")).post(
        "/api/v1/voice/speak", json={"text": "x" * 50}
    )
    assert r.status_code == 200
    assert seen["len"] == 10
    assert seen["audit"]["details"]["truncated"] is True


def test_speak_synthesis_failure_422(monkeypatch):
    from app.api import voice as voice_api

    class _BadEngine:
        name = "kokoro"

        def synthesize(self, text, *, voice=None):
            raise ValueError("boom")

    async def _noop(**kw):
        return None

    monkeypatch.setattr(voice_api, "is_tts_available", lambda: True)
    monkeypatch.setattr(voice_api, "get_tts_engine", lambda: _BadEngine())
    monkeypatch.setattr(voice_api, "audit_record", _noop)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/speak", json={"text": "hi"}
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "synthesis_failed"


def test_speak_model_not_ready_503(monkeypatch):
    from app.api import voice as voice_api
    from app.voice.tts import TTSUnavailable

    class _NotReady:
        name = "kokoro"

        def synthesize(self, text, *, voice=None):
            raise TTSUnavailable("model_load_failed")

    async def _noop(**kw):
        return None

    monkeypatch.setattr(voice_api, "is_tts_available", lambda: True)
    monkeypatch.setattr(voice_api, "get_tts_engine", lambda: _NotReady())
    monkeypatch.setattr(voice_api, "audit_record", _noop)
    r = _client(_build_app("operator")).post(
        "/api/v1/voice/speak", json={"text": "hi"}
    )
    assert r.status_code == 503


def test_speak_requires_auth():
    r = _client(_build_app(override_user=False)).post(
        "/api/v1/voice/speak", json={"text": "hi"}
    )
    assert r.status_code == 401


# ═══ frontend / header / packaging wiring ═════════════════════════


def test_csp_allows_media_blob():
    src = (_APP / "security" / "headers.py").read_text(encoding="utf-8")
    assert "media-src 'self' blob:" in src


def test_ask_template_has_listen_button():
    html = (_TPL / "ask.html").read_text(encoding="utf-8")
    assert 'id="aq-listen"' in html
    assert "MnemosUI.speak" in html
    assert "voiceStatus" in html


def test_ui_js_has_tts_helpers_exported():
    js = (_STATIC / "ui.js").read_text(encoding="utf-8")
    assert "function speak(" in js
    assert "function voiceStatus(" in js
    assert "/api/v1/voice/speak" in js
    idx = js.find("window.MnemosUI = {")
    slab = js[idx:idx + 2600]
    assert "speak: speak" in slab
    assert "voiceStatus: voiceStatus" in slab


def test_ui_js_tts_korean_phrases():
    js = (_STATIC / "ui.js").read_text(encoding="utf-8")
    assert "듣기" in js
    assert "이 서버에서는 음성 출력이 활성화되어 있지 않습니다." in js


def test_listen_button_css_present():
    css = (_STATIC / "app.css").read_text(encoding="utf-8")
    assert ".listen-btn" in css


def test_pyproject_extras():
    pp = (_SERVER / "pyproject.toml").read_text(encoding="utf-8")
    assert "moonshine-voice" in pp  # default STT
    assert "voice-whisper = [" in pp  # alt STT
    assert "tts = [" in pp and "kokoro" in pp  # TTS


def test_env_example_documents_tts():
    env = (_SERVER.parent / ".env.example").read_text(encoding="utf-8")
    for key in ("MNEMOS_DISABLE_TTS", "MNEMOS_TTS_VOICE", "MNEMOS_TTS_ENGINE"):
        assert key in env, f".env.example missing {key}"


def test_env_example_still_under_budget():
    env = (_SERVER.parent / ".env.example").read_text(encoding="utf-8")
    # Immutable-source settings and the bounded seen-index knob are part of
    # the source-analysis safety contract.  Keep a modest ceiling without
    # forcing those operator-visible settings out of the example.
    assert len(env) < 6500
