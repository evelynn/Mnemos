# Voice commands (Ask tab)

Speak a question/command instead of typing it. On the **Ask** tab a mic
button records a short clip in the browser; the platform converts speech to
text with a **local** model and drops the recognised text into the question
box for the operator to review before asking.

> **Why server-side / local, not the browser's `SpeechRecognition`?**
> The browser API streams microphone audio to a cloud vendor (Google, in
> Chrome). Mnemos analyses customers' private production systems, so that is
> unacceptable. Recognition runs on *your* deployment — no audio leaves the
> box. This also keeps the feature usable on an air-gapped host.

## Engine choice — the research

The brief was: the lightest, latest, free open-source engine with a good
recognition rate, run locally. The **binding constraint** is that Mnemos's
operators are Korean (the UI ships an EN/한국어 switcher), so a spoken
command is as likely to be Korean as English. The 2026 landscape:

| Engine | Size / weight | Korean | Verdict |
|--------|---------------|--------|---------|
| **Moonshine** | smallest (27–60 MB, ONNX) | ✗ no released non-English ONNX | English-only — out |
| **Vosk** | tiny, Kaldi, CPU | △ small KO model, weaker accuracy | great for embedded English, trails on free-form KO |
| **SenseVoice** | medium | ✓ excellent CJK | pulls in the heavy FunASR stack — too much for an optional extra |
| **NVIDIA Parakeet** | large, GPU-leaning | ✗ English-first | not a light CPU fit |
| **faster-whisper** | `tiny` ≈ 75 MB, `base` ≈ 145 MB, CPU INT8 | ✓ strong KO + EN (99 langs) | **chosen** |

**[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** (Whisper
re-implemented on CTranslate2) wins the balance the brief asks for:

- multilingual incl. strong **Korean and English** in one model;
- light + fast on CPU — INT8 quantization, ~2× faster and lower memory than
  `openai-whisper` for the same accuracy;
- small models, downloadable once then **fully offline**;
- trivially installable, actively maintained in 2026.

The engine is **pluggable** (`MNEMOS_STT_ENGINE`) so a future deployment can
swap in Vosk/Moonshine for an ultra-light English-only host without touching
the API or the UI.

Sources:
[Gladia — best open-source STT 2026](https://www.gladia.io/blog/best-open-source-speech-to-text-models),
[Northflank STT benchmarks 2026](https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper),
[Moonshine](https://github.com/moonshine-ai/moonshine).

## Install

Voice is an **optional extra** — a base install stays slim and the platform
boots fine without it (the mic simply hides):

```bash
pip install 'mnemos-platform[voice]'
```

The first transcription downloads the model (default `base` ≈ 145 MB) from
Hugging Face and caches it. **Air-gapped hosts**: pre-bake the model into the
image, or set `HF_HOME` to a pre-populated cache, so the first request
doesn't reach for the network. If the model can't load, the endpoint returns
`503` and the mic hides — exactly the "not installed" experience.

## Configuration

All optional, with sensible defaults (see `.env.example`):

| Env | Default | Notes |
|-----|---------|-------|
| `MNEMOS_DISABLE_STT` | _(off)_ | `1` = hard-off even if installed (air-gapped opt-out) |
| `MNEMOS_STT_ENGINE` | `faster-whisper` | backend selector |
| `MNEMOS_STT_MODEL` | `base` | `tiny` (lightest) → `small`/`medium`/`large-v3` (better KO) |
| `MNEMOS_STT_DEVICE` | `cpu` | or `cuda` |
| `MNEMOS_STT_COMPUTE` | `int8` | lightest; `int8_float16` / `float16` for GPU |
| `MNEMOS_STT_LANGUAGE` | _(auto)_ | force `ko`/`en`; empty = auto-detect |
| `MNEMOS_STT_BEAM_SIZE` | `5` | best recognition; `1` = fastest |
| `MNEMOS_STT_MAX_UPLOAD_BYTES` | `26214400` (25 MB) | reject larger clips |

The UI sends the operator's chosen locale (EN/한국어) as a recognition
hint, so a Korean operator's command is recognised as Korean, not guessed.

## How it works

```
Ask tab mic ──getUserMedia/MediaRecorder──▶ webm/opus clip
   │  POST /api/v1/voice/transcribe (multipart, CSRF-protected)
   ▼
faster-whisper (local, CPU INT8, lazy-loaded + cached)
   │  text
   ▼
question box (operator reviews, then "Ask")
```

- **API:** `GET /api/v1/voice/status` (is it available + which engine/model)
  and `POST /api/v1/voice/transcribe` (multipart `audio`, optional
  `language` + `project_id`).
- **Auth:** operator role — voice is a command surface, same bar as
  `POST /ask`. A viewer sees the mic disabled.
- **Audited:** every transcription is logged as `voice.transcribe` with the
  recognised text (truncated) + engine/model/duration.
- **Permissions-Policy:** `microphone=(self)` lets the dashboard capture on
  its own origin; camera/geolocation/etc stay denied.

## Recognition isn't perfect

The recognised text lands in the box for the operator to **review and edit**
before asking — by design. A misheard command never auto-fires a query.
Speak a second time to append rather than overwrite.
