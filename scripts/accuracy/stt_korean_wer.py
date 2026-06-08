"""PR-157 — Korean STT recognition-rate harness (CER / WER).

Quantifies how well the voice-input pipeline (``app.voice.engine``)
transcribes Korean speech, the same way ``measure.py`` quantifies the
analyzers. It drives the *actual configured engine* — Moonshine tiny-ko by
default, or faster-whisper — so the number reflects what ships, not a
stand-in.

Two input shapes:

    # sherpa-onnx style: a dir of <name>.wav + a trans.txt of "<name> <text>"
    python scripts/accuracy/stt_korean_wer.py --sherpa-dir /path/test_wavs

    # explicit manifest: JSONL of {"audio": "a.wav", "text": "정답"}
    python scripts/accuracy/stt_korean_wer.py --manifest ko.jsonl

Metrics (per-utterance + aggregate):
* **CER** — character error rate with spaces removed (the standard Korean
  metric; Korean spacing is inconsistent so it's excluded).
* **WER** — word error rate over whitespace tokens.
* exact-sentence match count.

``--max-cer FLOOR`` exits non-zero when the aggregate CER exceeds the floor
so CI can gate. Without it the harness only reports.

Note on sandboxes: the engine downloads its model on first use. Where the
model host is unreachable (air-gapped / blocked allowlist) the engine
reports unavailable and this harness exits 2 — measure where the weights
are reachable, or pre-bake them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make ``app`` importable when run from the repo root.
_SERVER = Path(__file__).resolve().parents[2] / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))


def levenshtein(a: list, b: list) -> int:
    """Edit distance between two sequences (lists of chars or tokens)."""
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        ai = a[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


def cer(ref: str, hyp: str) -> tuple[int, int]:
    """Character errors / reference characters, spaces removed (Korean CER)."""
    r = ref.replace(" ", "")
    h = hyp.replace(" ", "")
    return levenshtein(list(r), list(h)), len(r)


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """Word errors / reference words, split on whitespace."""
    r, h = ref.split(), hyp.split()
    return levenshtein(r, h), len(r)


def load_sherpa_dir(d: Path) -> list[tuple[str, Path, str]]:
    """A dir with ``trans.txt`` (``<name> <transcript>`` lines) + the wavs."""
    items: list[tuple[str, Path, str]] = []
    trans = d / "trans.txt"
    if not trans.exists():
        raise SystemExit(f"no trans.txt in {d}")
    for line in trans.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        name, text = line.split(" ", 1)
        wav = d / name
        if wav.exists():
            items.append((name, wav, text))
    return items


def load_manifest(path: Path) -> list[tuple[str, Path, str]]:
    """JSONL of ``{"audio": "...", "text": "..."}``."""
    items: list[tuple[str, Path, str]] = []
    base = path.parent
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        audio = Path(rec["audio"])
        if not audio.is_absolute():
            audio = base / audio
        items.append((audio.name, audio, rec["text"]))
    return items


def measure(items, *, language: str | None) -> dict:
    from app.voice.engine import get_engine, is_stt_available

    if not is_stt_available():
        raise SystemExit(
            "2: STT engine unavailable — install the extra and ensure the "
            "model can download (see docs/voice-commands.md)."
        )
    engine = get_engine()

    tce = tcn = twe = twn = exact = 0
    rows = []
    for name, wav, ref in items:
        audio = wav.read_bytes()
        res = engine.transcribe(audio, language=language)
        hyp = (res.text or "").strip()
        ce, cn = cer(ref, hyp)
        we, wn = wer(ref, hyp)
        tce += ce
        tcn += cn
        twe += we
        twn += wn
        exact += int(ref.strip() == hyp)
        rows.append({
            "name": name, "ref": ref, "hyp": hyp,
            "cer": (100 * ce / cn) if cn else 0.0,
            "wer": (100 * we / wn) if wn else 0.0,
        })
    return {
        "engine": engine.name,
        "rows": rows,
        "cer": (100 * tce / tcn) if tcn else 0.0,
        "wer": (100 * twe / twn) if twn else 0.0,
        "chars": tcn, "words": twn,
        "exact": exact, "n": len(items),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Korean STT CER/WER harness")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sherpa-dir", type=Path, help="dir with wavs + trans.txt")
    src.add_argument("--manifest", type=Path, help="JSONL of {audio,text}")
    ap.add_argument("--language", default=None, help="STT language hint (e.g. ko)")
    ap.add_argument("--max-cer", type=float, default=None,
                    help="fail (exit 1) if aggregate CER%% exceeds this floor")
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    args = ap.parse_args(argv)

    items = (load_sherpa_dir(args.sherpa_dir) if args.sherpa_dir
             else load_manifest(args.manifest))
    if not items:
        raise SystemExit("no (audio, reference) pairs found")

    rep = measure(items, language=args.language)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("=" * 78)
        print(f"engine: {rep['engine']}   utterances: {rep['n']}")
        print("=" * 78)
        for r in rep["rows"]:
            print(f"[{r['name']}] CER {r['cer']:5.1f}%  WER {r['wer']:5.1f}%")
            print(f"  ref: {r['ref']}")
            print(f"  hyp: {r['hyp']}")
        print("=" * 78)
        print(f"AGGREGATE  CER = {rep['cer']:.2f}%  ({rep['chars']} chars) | "
              f"WER = {rep['wer']:.2f}%  ({rep['words']} words) | "
              f"exact {rep['exact']}/{rep['n']}")

    if args.max_cer is not None and rep["cer"] > args.max_cer:
        print(f"FAIL: CER {rep['cer']:.2f}% > floor {args.max_cer}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
