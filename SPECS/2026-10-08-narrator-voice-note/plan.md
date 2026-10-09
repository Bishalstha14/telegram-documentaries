# Plan — Narrator (Phase 6: voice-note delivery)

Red/Green TDD throughout. **Invoke the `write-tests` skill before writing any
tests** — it is mandatory in this repository. Run checks with the dev scripts
referenced in `README.md`, never by hand:

```bash
./scripts/test    # pytest -> ruff -> mypy (strict); must exit 0
./scripts/hooks   # pre-commit; also must exit 0 before the final commit
```

Baseline entering this plan: **501 passed, ruff clean, mypy strict clean.**

Branch: continue on `feature/2026-10-05-text-vertical-slice` (user's choice —
voice stacks on the text slice and ships in one PR). Do not create a branch.

---

## Task group 1 — Baseline and dependency

1. Confirm `git status` is clean and `git log --oneline -1` is `fd9bccc` or
   later.
2. Add `lameenc` to `pyproject.toml` at the version already proven in the venv
   (`1.8.4`), in the main dependency group. It is already installed in `.venv`
   from the feasibility run — the pin is what makes it *reproducible*, which is
   a Phase 1 acceptance criterion.
3. Verify `.venv/bin/python -c "import lameenc"` succeeds and
   `./scripts/test` still exits 0.

No tests to write: this is a dependency, not behaviour.

---

## Task group 2 — The reply contract (`contracts.py`)  (RED → GREEN)

**RED first.** In `tests/unit/test_contracts.py`, write the cases for:

- `VoiceNote` accepts a well-formed note (non-empty `data` with an `ID3` or
  `0xFF`-sync head, `mime_type="audio/mpeg"`, a positive `duration_seconds`, a
  non-empty `fallback_text`).
- Rejects, with a `ValidationError`: empty `data`; `data` whose head is neither
  `ID3` nor an MPEG frame sync; a `mime_type` other than `audio/mpeg`; a
  non-positive `duration_seconds`; blank `fallback_text`; any extra field (the
  model is `extra="forbid"`); a frozen-field assignment.
- Rejects `data` at or above Telegram's 50 MB voice-note limit.
- `Reply` is `str | VoiceNote` — assert a plain string and a `VoiceNote` are
  both acceptable to it, and that it is genuinely a union (e.g. a literal
  `bool` is not).

**GREEN.** Implement `VoiceNote` and `Reply` in `src/telegram_documentaries/contracts.py`
following the module's existing model style (`model_config = ConfigDict(frozen=True, extra="forbid")`,
a `Field` where a bound applies, a docstring that states *why*).

Keep the MP3 header check in one named helper — it is used by both validation
and the narrator.

Run `./scripts/test`.

---

## Task group 3 — The TTS value types (`gemini.py`)  (RED → GREEN)

**RED first.** In `tests/unit/test_gemini.py`:

- `Stage.NARRATOR` exists and its value is `"narrator"`.
- `VoiceName` accepts `"Kore"`, `"Fenrir"`, `"Charon"` and rejects any other
  string at type-check time (assert `mypy` catches a bad literal by including a
  correctly-typed usage of all three and a `# type: ignore[valid-type]`-free
  positive case; a negative case belongs in the type-checker, not at runtime).
- `SynthesisRequest`: frozen, `extra="forbid"`, rejects blank `text`, rejects
  an unknown voice.
- `SynthesizedAudio` parses a real mime string
  `audio/l16; rate=24000; channels=1` into `sample_rate=24000`,
  `channels=1`, `duration_seconds` derived from `len(data)` — **no regex**.
  Feed it the pair-splitting path: assert a malformed `rate` (`rate=fast`), a
  non-`audio/l16` mime, `channels=2`, a missing `rate`, an empty `data`, and an
  odd-length `data` are all rejected.

**GREEN.** Add `Stage.NARRATOR`, `VoiceName`, `SynthesisRequest`,
`SynthesizedAudio`, and the mime pair-splitter to `gemini.py`. The splitter
takes `mime_type.split(";")`, splits each part on `"="`, and hands the resulting
mapping to a Pydantic model — no `re` anywhere.

Run `./scripts/test`.

---

## Task group 4 — `synthesize` on the client (`gemini.py`)  (RED → GREEN)

**RED first.** These tests drive the existing fake transport, so they are all
network-free:

- `GeminiClient` (the `runtime_checkable` Protocol) now has `synthesize`; a
  class implementing only `generate` no longer satisfies `isinstance` against it
  — this is the guard that keeps `GenAiGeminiClient` honest.
- `GenAiGeminiClient.synthesize(...)` returns a `SynthesizedAudio` from a
  canned `GenerateContentResponse` carrying one `inline_data` audio part.
- Rejects, each raising `GeminiResponseError` with `stage == Stage.NARRATOR`
  and a *fixed* reason string, never the payload: no candidates; candidates
  with no content; a candidate whose only part has text instead of audio; a part
  whose `mime_type` is not `audio/l16...`; empty `data`; `rate=fast`.
- The returned record never contains the API key (extend the existing key
  sweep if the new seam can render anything).
- A transport-level `APIError` is classified into `GeminiUnavailableError` /
  `GeminiResponseError` exactly as `generate` does — reuse, do not re-derive.
- The call is logged as `gemini_call` with static `stage="narrator"` and
  `model="gemini-3.1-flash-tts-preview"` (assert on the captured record, in the
  style of the existing `@logged` assertions).

**GREEN.**

1. Add `async def synthesize(self, text: str, voice: VoiceName, chat_id: int,
   update_id: int) -> SynthesizedAudio: ...` to the `GeminiClient` Protocol.
2. Build the request config in one place: `response_modalities=["TEXT",
   "AUDIO"]` and `speech_config` with `voice_name`. **No `response_schema`, no
   `response_mime_type`** — both were verified to be wrong for this call.
3. Route it through the same `GeminiTransport` and the same error-classification
   helpers `generate` uses. Wrap it with `observability.logged("gemini_call",
   extra={"stage": ..., "model": TTS_MODEL_ID})` in the same shape as
   `_stage_call` — if that means a second small binding helper, add one; do not
   copy the classification logic.
4. Add `TTS_MODEL_ID = "gemini-3.1-flash-tts-preview"` beside `MODEL_ID`.
5. Enforce the leak rules: class-name-only reasons, `from None` on anything
   whose `str()` could carry a payload, and no `str(exc)` in any record.

Run `./scripts/test`.

---

## Task group 5 — `narrator.py`  (RED → GREEN)

**RED first.** New `tests/unit/test_narrator.py`:

- `narrate(...)` returns a `VoiceNote` whose `mime_type` is `"audio/mpeg"`,
  whose bytes start with `ID3` or an MPEG sync, whose `duration_seconds`
  matches the PCM length, and whose `fallback_text` is the narration it was
  given.
- The sample rate used for encoding is the one from the *validated* audio, not
  `24000` — build a fixture at a different rate and assert the encoder was told
  that rate (assert via the resulting `duration_seconds`, which is derived from
  the same source).
- A transport failure (`GeminiUnavailableError`) propagates — `narrate` does
  **not** swallow Gemini errors; that is the pipeline's decision (D6).
- A failure inside the encoder raises `NarratorError`, and the chained cause is
  not rendered into its message.
- `NarratorError` is **not** a `GeminiError` (assert `not issubclass`).
- Too-short PCM (under one MPEG frame) raises `NarratorError` rather than
  producing a silently empty note.

**GREEN.** Create `src/telegram_documentaries/narrator.py`:
`DEFAULT_VOICE`, the `narrate()` coroutine, the `NarratorError`, the encoder
constants (named: bit rate, quality), and the PCM → MP3 conversion using
`lameenc`. Read `sample_rate` from the `SynthesizedAudio` the client returned.

Run `./scripts/test`.

---

## Task group 6 — The row (`pipeline.py`)  (RED → GREEN)

**RED first.** In `tests/unit/test_pipeline.py`:

- The happy path returns a `VoiceNote` (not a `str`) carrying the narration as
  `fallback_text`, with the session already `SCRIPTED` and saved.
- When `narrate` raises `GeminiUnavailableError` → the row returns `script.text`
  **verbatim**, the session is still `SCRIPTED`, and `narration_voice_failed`
  is logged with `reason="synthesis"`.
- When `narrate` raises `NarratorError` → same, with `reason="encoding"`.
- Neither failure reaches `_respond`: assert the reply is the narration and
  **not** `GENERIC_FAILURE`.
- On success `narration_delivered` carries `word_count`, `duration_seconds`
  and `byte_size`.
- Exactly one reply: the row returns one object, never a list.
- The session is completed *before* synthesis — assert `SCRIPTED` even when
  synthesis raises.

**GREEN.** Change `_write_script` to return `Reply`, wrap the narrator call in
`except (GeminiError, NarratorError)`, log per the inventory, and return
`script.text` on failure. Update `WELCOME` to promise a voice note, and update
the module docstring so "returns one `str`" reflects `Reply`.

Run `./scripts/test`.

---

## Task group 7 — The adapter (`bot.py`)  (RED → GREEN)

**RED first.** In `tests/unit/test_bot.py`:

- A `VoiceNote` reply drives `send_voice` (not `send_message`), with **no
  caption**, to the right `chat_id`.
- A `str` reply still drives `send_message` — the existing assertions must not
  need editing.
- When `send_voice` raises, `voice_note_send_failed` is logged with
  `error_type` and the correlation ids, and `send_message` is then called once
  with `VoiceNote.fallback_text`. Exactly one message reaches the chat.
- The failed voice send is not retried (assert `send_voice` was called once).
- `send_voice` receives the bytes in a file-like object (Telegram rejects a
  bare `bytes` for a filename-bearing upload — use `io.BytesIO`).

**GREEN.** Add `_send_voice(...)` decorated `@observability.logged("voice_note_sent")`
with `chat_id` and `update_id` parameters **by those exact names**, and narrow
in `_dispatch`. Update the module docstring's "one send per update" wording.

Run `./scripts/test`.

---

## Task group 8 — Documentation sync

Update, matching the existing prose style (past tense, evidence-led, no
boilerplate):

- `SPECS/ROADMAP.md` — Phase 6 delivered, with its four acceptance criteria
  stated as met; Status table row 6; the Phase 5 note's "voice delivery
  remains" corrected.
- `SPECS/TECH.md` — add D-V1/D-V2 to the recorded-divergences section; add
  `lameenc` to the dependency inventory.
- `README.md` — "Current state" and the module tree gain `narrator.py`.
- `SPECS/2026-10-05-text-vertical-slice/requirements.md` — cross-reference D-V1
  and D-V2 from its deviations section against R1.2 and R9.3.
- `SPECS/MISSION.md` — inspect and report any delta; **do not edit it without
  raising the change with the user first.**

---

## Task group 9 — Final gates

1. `./scripts/test` exits 0.
2. `./scripts/hooks` exits 0.
3. Leak sweeps, all must return nothing:
   ```bash
   grep -rn "except:\|except Exception:\s*pass\|except:\s*pass" src/
   grep -rn "from google" src/ --include=*.py | grep -v "gemini\.py"
   grep -rn "str(exc)" src/
   grep -rnE "send_photo|\.ogg|\.mp3\"|audio/ogg" src/
   grep -rn "lameenc" pyproject.toml
   ```
4. `git status` is clean; `.env` is still ignored and unchanged;
   `GITHUB-SSH-KEY.txt` is not staged.
5. Restart the live bot onto the new code, then hand over to the user for the
   phone test (validation.md section E).
