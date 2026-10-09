# Requirements — Narrator (Phase 6: voice-note delivery)

## Summary

The narration the Scripter already writes is delivered as a **Telegram voice
note** instead of a text message. Synthesis is `gemini-3.1-flash-tts-preview`;
the raw PCM it returns is encoded to MP3 and sent with `send_voice`.

If synthesis or encoding fails for any reason, **the narration is sent as text
instead** — audio can never eat the narration (ROADMAP Phase 6, acceptance
criterion 4).

This phase is deliberately small: MISSION calls the Narrator "not an agent, just
a direct function", and nothing about state, interviewing or scripting changes.
The whole feature is one new module, one new method on an existing client, one
row of the decision table that can now return a second type, and one `isinstance`
narrow in the adapter.

Everything in this spec was verified against the live API and the live bot
before being written down, rather than assumed — the model's output format, the
Telegram format requirement, the encoder, and an actual voice note delivered to
the real chat.

## Context

- **Constitution:** `SPECS/MISSION.md`, `SPECS/TECH.md`, `SPECS/ROADMAP.md`.
- **Stacking spec:** `SPECS/2026-10-05-text-vertical-slice/requirements.md`
  (referred to below as *the text slice*). This feature continues on
  `feature/2026-10-05-text-vertical-slice`, so it ships in the same PR. Every
  text-slice requirement still holds unless this spec says otherwise.
- **Where the narration comes from:** `pipeline._write_script` calls
  `scripter.write(...)`, completes and saves the session, then returns
  `script.text`. That `return` is the seam this feature changes.
- **The reply contract today:** `handle_*` returns `str`, and
  `bot._send(...)` sends it through `context.bot.send_message`. Exactly one send
  per update (text slice R9.3).

### What was verified before writing this spec

| Question | Answer | Evidence |
|---|---|---|
| Does TTS work on this key? | Yes | `gemini-3.1-flash-tts-preview` returned `audio/l16; rate=24000; channels=1` |
| Can we ask for MP3/OGG/Opus/WAV directly? | **No** | every non-default `response_mime_type` returns HTTP 400 `INVALID_ARGUMENT` |
| Is there an ffmpeg or audio library here? | **No** | no `ffmpeg`, no `pydub`/`soundfile`/`av`/`numpy` |
| What does Telegram actually accept for a voice note? | **OGG/Opus, MP3, or M4A** | Bot API `sendVoice` documentation |
| Does PCM → MP3 → `send_voice` produce a real voice note? | **Yes** | `send_voice` returned a `Message` with a non-null `voice` (message 85) |
| Which encoders are installable? | all of them | `lameenc` 248 KB, `av` 35 MB, `imageio-ffmpeg` 29 MB, `soundfile` 1.3 MB |
| Three voices, comparable | sent | messages 86–88 (Fenrir, Charon, Kore); **Kore chosen** |

## Decisions

**D1 — A voice note is the delivery; text is the fallback.**
On success the user receives a bare voice note: no caption, no duplicated text
message. If synthesis *or* encoding *or* the Telegram send fails, the narration
is delivered as text instead. One *delivered* reply per update either way.

**D2 — `Kore`, as a named constant, behind a `Literal`.**
`DEFAULT_VOICE: VoiceName = "Kore"`. `VoiceName` is
`Literal["Kore", "Fenrir", "Charon"]` — the three voices actually verified in
this repository, so a typo is a `mypy` failure rather than a runtime surprise.
Precedent: `ImageMimeType` (text slice R1.1). Extending the voice means adding a
name that has been verified, which is the honest boundary.

**D3 — MP3 via `lameenc`, not OGG/Opus.**
Gemini returns raw PCM only, so *something* must encode. `lameenc` is 248 KB and
self-contained (a manylinux wheel with liblame bundled); `av` and
`imageio-ffmpeg` are ~30–35 MB for the same job; a system `ffmpeg` is not
installed and would not be reproducible from `pyproject.toml`. Telegram accepts
MP3 for `send_voice`, so OGG/Opus's only advantage (size) does not buy anything
at 60–90 words.

**D4 — `handle_*` returns `Reply = str | VoiceNote`.**
Still exactly one reply per update — only the type widens. This is recorded as a
deviation from the text slice's R9.3, which says `str`. Every non-narration row
still returns `str`, so no existing assertion changes. A full
`TextReply`/`VoiceReply` sum type was considered and rejected: it would rewrite
every existing test assertion to fix a problem only one row has (YAGNI).

**D5 — `GeminiClient` gains a second method, `synthesize`.**
TTS returns audio, not a schema-bound JSON reply, so it cannot travel through
`generate`'s `response_schema` parameter without lying about its shape. One
injected object, one seam, two methods. Recorded as a deviation from the text
slice's R1.2 ("the one method every stage sees").

**D6 — A TTS failure is caught inside `pipeline._write_script`, never allowed to
reach `_respond`.**
`_respond` maps `GeminiError` → `GENERIC_FAILURE`, which would throw away a
perfectly good narration over a delivery problem. The session is completed and
saved **before** synthesis is attempted, so a failure can never strand a session
mid-interview.

**D7 — No retry.**
The Scripter gets one corrective retry because its output can be *wrong*; a
synthesis failure cannot be fixed by asking twice. It falls straight back to
text, which is the specified degradation (YAGNI).

**D8 — Two boundaries, two schemas.**
`SynthesizedAudio` (validated at the Google boundary, in `gemini.py`) and
`VoiceNote` (validated at the Telegram boundary, in `contracts.py`) are separate
models. They are different formats, with different failure modes, at different
boundaries; merging them would put an encoder concern inside the Google client.

**D9 — `VoiceNote` carries its own fallback text.**
The pipeline cannot observe a failed send — it returns before the adapter sends.
Carrying the narration on the reply lets the adapter guarantee delivery without
the domain learning about Telegram, and without `bot.py` owning any
conversational wording (D10): it sends a field it was handed, exactly as it does
for `str`.

**D10 — No retry at the Telegram boundary either.**
If `send_voice` fails, the adapter logs it loudly and sends `fallback_text`. The
first attempt is not repeated — a different transport is.

## Scope

### In scope

- `gemini.py`: `Stage.NARRATOR`, `VoiceName`, `SynthesisRequest`,
  `SynthesizedAudio`, `GeminiClient.synthesize`, its implementation, and the TTS
  request config.
- `narrator.py` (new): `narrate()` — synthesize, encode, return a `VoiceNote`.
- `contracts.py`: `VoiceNote` and the `Reply` alias.
- `pipeline.py`: the script row returns `Reply`; text fallback; `WELCOME`.
- `bot.py`: narrow on type, `send_voice`, adapter-side text fallback.
- `pyproject.toml`: pin `lameenc`.
- Docs: ROADMAP Phase 6, TECH divergence, README, text-slice deviations.

### Explicitly out of scope (YAGNI)

- The **hybrid portrait image** (Phase 4 Converter) — a separate, larger feature.
  This spec delivers voice only; MISSION's "image *and* a voice note" is reached
  when Phase 4 lands.
- Voice cloning, user-chosen voices, streaming audio (MISSION, out of scope).
- A `/voice` command or any voice picker. The voice is a constant.
- Retrying synthesis (D7).
- Multiple audio parts, multi-speaker output, SSML, or background music.
- Any new state, phase, or session field — the interview's shape is untouched.
- Caching or persisting audio to disk; the bytes live for one send.

## Requirements

### R1 — The TTS boundary (`gemini.py`)

**R1.1** `Stage.NARRATOR = "narrator"` joins the enum, so every record on this
path is staged like every other.

**R1.2** `SynthesisRequest` is a frozen `BaseModel` with `extra="forbid"`,
carrying `text: str = Field(min_length=1)` and `voice: VoiceName`. It is a
*different* model from `GemamiRequest` because its shape genuinely differs —
`GeminiRequest.system_instruction` is mandatory and would have to be fabricated
for a call that does not use one.

**R1.3** `SynthesizedAudio` is the **only** place the raw Gemini audio payload is
read. It is validated at the edge:

- The `mime_type` string is split on `;` into `key=value` pairs and validated as
  a typed model — **no regular expression**. It must declare `audio/l16`, an
  integer `rate`, and `channels == 1`.
- `data: bytes` must be non-empty and of even length (little-endian `s16le`
  frames are two bytes each).
- `sample_rate`, `channels` and `duration_seconds` are derived and frozen, so
  downstream code never re-parses the wire format.

**R1.4** `GeminiClient.synthesize(text, voice, chat_id, update_id) ->
SynthesizedAudio` joins the Protocol, and `GenAiGeminiClient` implements it
through the **same** `GeminiTransport` and the **same** error classification as
`generate`. There is exactly one place that knows `google.genai` is leaky, and
this does not become a second one: no `str(exc)`, no leaked response body, and
leaky originals chained `from None`.

**R1.5** The call is wrapped with `observability.logged("gemini_call", extra
={"stage": ..., "model": ...})` exactly as `_stage_call` does today, so the
record shape (`event`, `chat_id`, `update_id`, `duration_ms`, static `stage` and
`model`) is unchanged and the model here is
`gemini-3.1-flash-tts-preview`, not `MODEL_ID`.

**R1.6** Rejections raise `GeminiResponseError(stage=Stage.NARRATOR, ...)` with
class-name-only reasons for: no candidates, no content, no audio part, an
unexpected `mime_type`, empty `data`, a non-integer `rate`, or `channels != 1`.
Reasons are fixed strings; the payload is never rendered.

**R1.7** The request config is `response_modalities=["TEXT", "AUDIO"]` plus a
`speech_config` carrying `voice_name`. There is **no** `response_schema` and
**no** `response_mime_type` — audio is not JSON, so `_typed_reply` is not on this
path and its own validation is; every non-default mime type returns HTTP 400
(verified above).

### R2 — Encoding and the reply contract (`narrator.py`, `contracts.py`)

**R2.1** `contracts.VoiceNote` is a frozen `BaseModel` with `extra="forbid"`:
`data: bytes`, `mime_type: Literal["audio/mpeg"]`, `duration_seconds: float`,
and `fallback_text: str` (D9). It validates that `data` is non-empty, begins
with an ID3 tag or an MPEG frame sync (`0xFF` with the top three bits set), and
is below Telegram's 50 MB voice-note limit.

**R2.2** `contracts.Reply = str | VoiceNote` is the return type of every
`handle_*` method (D4).

**R2.3** `narrator.narrate(client, text, *, chat_id, update_id,
voice=DEFAULT_VOICE) -> VoiceNote` is a plain `async` function, not a class and
not a stage with a state machine (MISSION: "not an agent").

**R2.4** Encoding uses `lameenc` with `set_in_sample_rate` taken from the
**validated** `SynthesizedAudio.sample_rate`, never a hardcoded `24000`; mono;
64 kbps; quality 2. The bit rate and quality are named module constants, not
magic numbers buried in a call.

**R2.5** A failure inside our own encoding raises `NarratorError` — a new
exception type, deliberately *not* a `GeminiError`, so the pipeline can report
which half failed. Whatever it wraps is chained `from None` if its message could
carry a payload.

**R2.6** The resulting bytes are validated as a `VoiceNote` before they leave
this module (R2.1), so the adapter receives something already proven to be a
sendable MP3.

**R2.7** `lameenc` is pinned in `pyproject.toml` at the verified version, so the
`.venv` stays reproducible from it (Phase 1 acceptance criterion).

### R3 — The row (`pipeline.py`)

**R3.1** `_write_script` returns `Reply`. It completes and saves the session
**before** attempting synthesis (D6), so the interview is finished regardless of
what happens to the audio.

**R3.2** The narrator call is wrapped in `except (GeminiError, NarratorError)`.
On failure: log `narration_voice_failed` with `reason` (`"synthesis"` or
`"encoding"`), `error_type` and the correlation ids — the exception's `str()` is
never rendered — then **return `script.text`**.

**R3.3** On success: log `narration_delivered` with `word_count`,
`duration_seconds` and `byte_size`, then return the `VoiceNote`.

**R3.4** Nothing from this path may escape to `_respond`. A failure here is a
*degradation*, not a pipeline failure, and must never become `GENERIC_FAILURE`.

**R3.5** Exactly one reply per update is preserved — the voice note and its text
fallback are two possible *contents* of the same single reply, never two sends.

**R3.6** `WELCOME` is updated to promise a voice note, and must promise only
what exists.

**R3.7** The narration text is never lost. If *anything* in the delivery path
fails, what the user gets is the narration as text.

### R4 — The adapter (`bot.py`)

**R4.1** `_dispatch` narrows on type: a `VoiceNote` goes through a new
`_send_voice(...)`, everything else through the existing `_send(...)`. Both are
decorated `@observability.logged(...)` and both take `chat_id` and `update_id`
by those exact names, so the correlation stamps keep working.

**R4.2** The voice note is **bare**: `send_voice(chat_id=..., voice=...)`, no
caption (D1).

**R4.3** If `send_voice` raises, log `voice_note_send_failed` with `error_type`
and the correlation ids, then send `reply.fallback_text` through `_send`
(D9/D10). The failed attempt is never retried.

**R4.4** D10 is unchanged: `bot.py` still owns no conversational wording. It
branches on a type and sends a field it was handed.

### R5 — Tests never touch the network or a real credential (text slice R10, carried forward)

**R5.1** The TTS seam is the existing `GeminiTransport`. A fake returns a
`GenerateContentResponse` built from canned audio bytes; no socket, no key.

**R5.2** Encoding is real but local: `lameenc` is a pure CPU call with no
network, so tests exercise the actual encoder rather than a mock of it. Fixtures
must supply at least one MPEG frame's worth of PCM (576 samples at 24 kHz) so
the encoder has something to work with.

**R5.3** No test may assert against the live model, the live bot, or `.env`.

## Module layout

```
src/telegram_documentaries/
├── bot.py          → narrow on type; _send_voice; adapter-side text fallback
├── contracts.py    → VoiceNote, Reply        (outbound Telegram boundary)
├── gemini.py       → Stage.NARRATOR, VoiceName, SynthesisRequest,
│                     SynthesizedAudio, GeminiClient.synthesize
├── narrator.py     → NEW: narrate(), DEFAULT_VOICE, NarratorError, MP3 encoding
├── pipeline.py     → _write_script returns Reply; text fallback; WELCOME
└── (all others unchanged)
```

`narrator.py` sits between `gemini.py` (which owns `google.genai`) and
`pipeline.py` (which owns decisions) — it owns "how raw speech becomes a
sendable note", which is a third concern neither of them has.

## Logging inventory for this phase

| Event | Level | Where | Fields |
|---|---|---|---|
| `gemini_call` | INFO / ERROR | `gemini.py`, via `@logged` | `chat_id`, `update_id`, `duration_ms`, static `stage="narrator"`, `model="gemini-3.1-flash-tts-preview"` |
| `narration_delivered` | INFO | `pipeline.py` | `chat_id`, `update_id`, `word_count`, `duration_seconds`, `byte_size` |
| `narration_voice_failed` | WARNING | `pipeline.py` | `chat_id`, `update_id`, `reason`, `error_type` |
| `voice_note_sent` | INFO / ERROR | `bot.py`, via `@logged` | `chat_id`, `update_id`, `duration_ms` |
| `voice_note_send_failed` | WARNING | `bot.py` | `chat_id`, `update_id`, `error_type` |

`script_delivered` continues to be logged where it is today — the script *was*
written, whatever happens to its delivery.

No event carries `str(exc)`, the narration's bytes, or the API key.

## Divergences from project guidance, and why

| # | Divergence | Why |
|---|---|---|
| D-V1 | `handle_*` returns `Reply = str \| VoiceNote`, not `str` (text slice R9.3) | A voice note is not a string. The *intent* — one reply per update — is preserved; only the type widens. |
| D-V2 | `GeminiClient` has two methods, not one (text slice R1.2) | TTS returns audio, not schema-bound JSON. One seam, one injected object. |
| D-V3 | A new third-party dependency, `lameenc` | No encoder exists in this environment; ~248 KB self-contained wheel vs ~35 MB bundled FFmpeg or a non-reproducible system binary. |
| D-V4 | `_write_script` catches `GeminiError` where `_respond` already does | `_respond`'s mapping to `GENERIC_FAILURE` is correct for a *failed step* and wrong for a *failed delivery*; the fallback has to happen one level down. |

## Required documentation changes in this phase

- `SPECS/ROADMAP.md` — Phase 6 marked delivered with its four criteria; Status
  table row 6 updated; Phase 5's "voice delivery remains" text updated.
- `SPECS/TECH.md` — record D-V1/D-V2 as recorded divergences; add `lameenc` to
  the dependency inventory.
- `README.md` — "Current state" and the module list gain `narrator.py`.
- `SPECS/2026-10-05-text-vertical-slice/requirements.md` — R1.2 and R9.3 gain a
  cross-reference to D-V1/D-V2 in the deviations section.
- `SPECS/MISSION.md` — **no change expected**; it already describes the voice
  note. Any delta found during verification must be surfaced to the user first.
