"""PR-157 — Korean STT recognition-rate harness (scripts/accuracy/stt_korean_wer.py).

The harness drives the *shipped* ``app.voice.engine`` over a Korean
audio+reference set and reports CER/WER. The real engine needs model
weights (network), so here we pin the scoring math + the loaders + the
measure() orchestration with a fake engine — no audio or model required.

(The empirical run lives in the PR description: a lightweight int8 Korean
ASR scored CER 0.00% / WER 0.00% / 4-of-4 exact on real Korean speech;
the exact shipped tiny-ko weights couldn't download in the sandbox.)
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr157")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")

_HARNESS = (
    Path(__file__).resolve().parents[2] / "scripts" / "accuracy" / "stt_korean_wer.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("stt_korean_wer", _HARNESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_harness_file_exists_and_loads():
    assert _HARNESS.exists(), "harness script missing"
    mod = _load()
    for fn in ("levenshtein", "cer", "wer", "load_sherpa_dir", "measure", "main"):
        assert hasattr(mod, fn), f"harness missing {fn}"


def test_levenshtein_and_metrics():
    mod = _load()
    assert mod.levenshtein(list("kitten"), list("sitting")) == 3
    assert mod.levenshtein([], list("abc")) == 3
    assert mod.levenshtein(["a", "b"], ["a", "b"]) == 0


def test_cer_korean_ignores_spaces():
    mod = _load()
    # Identical → 0 errors over 5 chars.
    assert mod.cer("안녕하세요", "안녕하세요") == (0, 5)
    # One substitution (처→치); spaces removed so "주문처리기" (5 chars), 1 error.
    e, n = mod.cer("주문 처리기", "주문 치리기")
    assert (e, n) == (1, 5)
    # Spacing differences alone don't count as errors for CER.
    assert mod.cer("주문 처리기", "주문처리기")[0] == 0


def test_wer_word_level():
    mod = _load()
    assert mod.wer("a b c", "a x c") == (1, 3)
    assert mod.wer("그는 괜찮은 척", "그는 괜찮은 척") == (0, 3)


def test_load_sherpa_dir(tmp_path):
    mod = _load()
    (tmp_path / "0.wav").write_bytes(b"\x00")
    (tmp_path / "1.wav").write_bytes(b"\x00")
    (tmp_path / "trans.txt").write_text(
        "0.wav 안녕하세요\n1.wav 주문 처리기 어디\n", encoding="utf-8"
    )
    items = mod.load_sherpa_dir(tmp_path)
    assert [i[0] for i in items] == ["0.wav", "1.wav"]
    assert items[1][2] == "주문 처리기 어디"


def test_measure_with_fake_engine(tmp_path, monkeypatch):
    """measure() drives the real app.voice.engine seam; with a fake engine
    we verify a perfect transcript scores 0% and a one-char slip is scored
    correctly."""
    mod = _load()
    import app.voice.engine as eng_mod
    from app.voice.engine import TranscriptionResult

    w0, w1 = tmp_path / "0.wav", tmp_path / "1.wav"
    w0.write_bytes(b"\x00")
    w1.write_bytes(b"\x00")
    items = [
        ("0.wav", w0, "그는 괜찮은 척하려고 애쓰는 것 같았다"),
        ("1.wav", w1, "주문 처리기 어디"),
    ]
    # First utterance perfect; second has one wrong character (어→여).
    hyps = ["그는 괜찮은 척하려고 애쓰는 것 같았다", "주문 처리기 여디"]

    class _FakeEngine:
        name = "moonshine"

        def transcribe(self, audio, *, language=None):
            return TranscriptionResult(
                text=hyps.pop(0), language="ko", duration_s=1.0,
                engine="moonshine", model="tiny-ko",
            )

    monkeypatch.setattr(eng_mod, "is_stt_available", lambda: True)
    monkeypatch.setattr(eng_mod, "get_engine", lambda: _FakeEngine())

    rep = mod.measure(items, language="ko")
    assert rep["engine"] == "moonshine"
    assert rep["n"] == 2
    assert rep["exact"] == 1  # only the first matches exactly
    # 1 char error over the total non-space reference characters.
    ref_chars = sum(len(r.replace(" ", "")) for _, _, r in items)
    assert rep["chars"] == ref_chars
    assert abs(rep["cer"] - (100 * 1 / ref_chars)) < 1e-6


def test_measure_exits_when_unavailable(tmp_path, monkeypatch):
    import pytest

    mod = _load()
    import app.voice.engine as eng_mod

    monkeypatch.setattr(eng_mod, "is_stt_available", lambda: False)
    w = tmp_path / "0.wav"
    w.write_bytes(b"\x00")
    with pytest.raises(SystemExit):
        mod.measure([("0.wav", w, "안녕")], language="ko")
