# Voice on the Ask tab — speak questions, hear answers

A full voice loop on the **Ask** tab:

1. **Speak the question** — a mic button records a short clip; the platform
   transcribes it (STT) into the question box for review.
2. **Hear the answer** — a 🔊 listen button reads the answer aloud (TTS).

Everything runs **locally** on your deployment — no audio or text leaves
the box (unlike the browser's `SpeechRecognition`, which streams to a cloud
vendor). All engines are free, open-source, optional dependencies; absent,
the buttons simply hide and the endpoints return `503`.

---

## Speech-to-text (voice input)

### Engine choice — the research

The brief: the lightest, latest, free open-source engine with good
recognition, run locally. The **binding constraint** is that the operators
are Korean, so a command is as likely to be Korean as English.

| Engine | Weight | Korean | Verdict |
|--------|--------|--------|---------|
| **Moonshine tiny-ko** | ~26M params, ONNX, no torch | ✓ purpose-trained, beats Whisper-tiny | **default** |
| faster-whisper | tiny ~75MB / base ~145MB, CPU INT8 | ✓ multilingual (KO+EN, one model) | alternative |
| Vosk | tiny | △ weaker KO | not used |
| Moonshine (English) | smallest (27MB) | ✗ | not used |

**Default: [Moonshine](https://github.com/moonshine-ai/moonshine) "Flavors
of Moonshine"** (`moonshine-voice`, MIT). The Korean `tiny-ko` flavor is
~26M params with ~6.46% WER, runs on ONNX/ORT (**no PyTorch**), is
purpose-trained for Korean, and **outperforms Whisper-tiny** — the lightest
strong-Korean option, which is exactly the brief. Each "flavor" is one
language, so the flavor is chosen at config time (`MNEMOS_STT_LANGUAGE`,
default `ko`) and the per-request hint is ignored.

**Alternative: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)**
(CTranslate2 Whisper). Heavier, but **multilingual + auto-detecting** — use
it when commands mix Korean and English in one utterance:
`MNEMOS_STT_ENGINE=faster-whisper` + the `[voice-whisper]` extra.

> Browser audio (webm/opus) is decoded to the 16 kHz mono PCM Moonshine
> wants via **PyAV** (ffmpeg), pulled in by the `[voice]` extra.

Sources:
[Moonshine](https://github.com/moonshine-ai/moonshine) ·
[moonshine-tiny-ko](https://huggingface.co/UsefulSensors/moonshine-tiny-ko) ·
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) ·
[Northflank STT 2026](https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks).

### Install

```bash
pip install 'mnemos-platform[voice]'              # Moonshine tiny-ko (default)
pip install 'mnemos-platform[voice-whisper]'      # + multilingual faster-whisper
```

The first transcription downloads the model (Moonshine: from
`download.moonshine.ai`; faster-whisper: from Hugging Face) and caches it.
**Air-gapped hosts**: pre-bake the model into the image / a pre-populated
cache. If it can't load, the endpoint returns `503` and the mic hides.

### Configuration

| Env | Default | Notes |
|-----|---------|-------|
| `MNEMOS_DISABLE_STT` | _(off)_ | `1` = hard-off (air-gapped opt-out) |
| `MNEMOS_STT_ENGINE` | `moonshine` | or `faster-whisper` |
| `MNEMOS_STT_MODEL` | `tiny` | Moonshine arch / Whisper size (`tiny`/`base`/`small`…) |
| `MNEMOS_STT_LANGUAGE` | `ko` (Moonshine) | Moonshine flavor; faster-whisper: force lang, empty=auto |
| `MNEMOS_STT_DEVICE` / `_COMPUTE` / `_BEAM_SIZE` | `cpu` / `int8` / `5` | faster-whisper knobs |
| `MNEMOS_STT_MAX_UPLOAD_BYTES` | 25 MB | reject larger clips |

---

## Text-to-speech (voice output)

**Engine: [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)**
(`kokoro`, **Apache-2.0**) — 82M params, multilingual incl. **Korean**,
punches above its weight on TTS leaderboards while staying light.

A Kokoro voice id encodes its language in the first letter (`a`=US English,
`k`=Korean, `j`=Japanese, `z`=Chinese …); the lang is derived from the
chosen voice. The default `af_heart` is English and works out of the box;
Korean narration is an opt-in (`MNEMOS_TTS_VOICE=kf_*` + the `misaki[ko]`
G2P). Kokoro uses PyTorch; an operator avoiding torch can wire the ONNX
build ([kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)) via
`MNEMOS_TTS_ENGINE` — the API and UI don't change.

```bash
pip install 'mnemos-platform[tts]'   # also needs the espeak-ng system package
```

| Env | Default | Notes |
|-----|---------|-------|
| `MNEMOS_DISABLE_TTS` | _(off)_ | `1` = hard-off |
| `MNEMOS_TTS_ENGINE` | `kokoro` | backend selector |
| `MNEMOS_TTS_VOICE` | `af_heart` | a `kf_*` voice ⇒ Korean |
| `MNEMOS_TTS_LANG` | _(from voice)_ | override the derived lang_code |
| `MNEMOS_TTS_SPEED` | `1.0` | clamped 0.5–2.0 |
| `MNEMOS_TTS_MAX_CHARS` | `2000` | longer text is truncated |

---

## How it works

```
mic ─getUserMedia/MediaRecorder─▶ webm/opus ─POST /voice/transcribe─▶ STT (local) ─▶ question box
                                                                          │ answer
listen 🔊 ◀─ <audio> blob ◀─ audio/wav ◀─ POST /voice/speak ◀─ TTS (local) ◀──┘
```

- **API:** `GET /voice/status` (STT + TTS availability), `POST
  /voice/transcribe` (multipart `audio`), `POST /voice/speak` (`{text,
  voice?}` → audio/wav).
- **Auth:** transcribe needs the **operator** role (it's a command surface,
  same bar as `POST /ask`); speak needs only a signed-in user (it just reads
  text aloud).
- **Audited:** `voice.transcribe` / `voice.speak`, each with engine + model
  + truncated text.
- **Permissions-Policy:** `microphone=(self)` for capture; CSP `media-src
  'self' blob:` so the synthesized audio can play. Camera/geolocation/etc
  stay denied.

## Recognition isn't perfect

The recognised text lands in the box for the operator to **review and edit**
before asking — by design. A misheard command never auto-fires a query.
