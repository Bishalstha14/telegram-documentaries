# Validation — Narrator (Phase 6: voice-note delivery)

Every step here must pass before this feature is considered merge-ready. Where
something cannot be automated in this environment, it is called out explicitly
and the evidence required is named.

---

## A. Roadmap acceptance criteria (Phase 6)

### A.1 — Script routes to `gemini-3.1-flash-tts-preview`

- [ ] `TTS_MODEL_ID` names that model and is the value passed to the transport.
- [ ] A test asserts the transport receives it (via the `gemini_call` record's
      static `model`, or the captured transport argument).
- [ ] It is **not** `MODEL_ID` — the text model and the speech model are
      distinct constants.

### A.2 — Audio is rendered to a Telegram-compatible format (OGG/MP3)

- [ ] `narrate()` returns bytes with `mime_type="audio/mpeg"`.
- [ ] The bytes open as an MP3 (ID3 tag or MPEG frame sync) — asserted in a
      test, not eyeballed.
- [ ] The note is under Telegram's 50 MB voice limit.
- [ ] `lameenc` is pinned in `pyproject.toml` and the `.venv` is reproducible
      from it alone.

### A.3 — Voice note is delivered to the correct `chat_id`

- [ ] A test asserts `send_voice` receives the reply's `chat_id`.
- [ ] **Live:** the phone test in section E produces a voice note in the
      user's own chat, and it plays.

### A.4 — Audio failure degrades gracefully; text still reaches the user

- [ ] Unit: synthesis failure → the narration is returned as text, session is
      `SCRIPTED`, `GENERIC_FAILURE` is **not** returned.
- [ ] Unit: encoding failure → same, with `reason="encoding"`.
- [ ] Unit: `send_voice` failure → `fallback_text` is sent instead, exactly one
      message reaches the chat, and the voice send is not retried.
- [ ] **Live:** the forced-failure run in section E delivers the narration as
      text.

---

## B. Requirement coverage

| Requirement | Verified by |
|---|---|
| R1.1 `Stage.NARRATOR` | enum test |
| R1.2 `SynthesisRequest` frozen / forbidden / blank-text / bad-voice | model tests |
| R1.3 `SynthesizedAudio` mime parsed without regex; rate, channels, length | model tests + a grep for `re.` in `gemini.py` |
| R1.4 `synthesize` on the Protocol; `GenAiGeminiClient` implements it | `isinstance` guard test + happy-path test |
| R1.5 logged as `gemini_call` with static `stage`/`model` | record-capture test |
| R1.6 every rejection is a fixed-reason `GeminiResponseError` at `Stage.NARRATOR` | parametrised rejection tests |
| R1.7 no `response_schema`, no `response_mime_type` | config assertion test |
| R2.1 `VoiceNote` validation incl. header and size bound | model tests |
| R2.2 `Reply = str \| VoiceNote` | alias test |
| R2.3 `narrate()` is a plain coroutine returning `VoiceNote` | narrator tests |
| R2.4 sample rate comes from validated audio, not a literal | narrator test at a non-24 kHz rate |
| R2.5 `NarratorError` is not a `GeminiError`; cause not rendered | narrator tests |
| R2.6 bytes validated before leaving the module | narrator test |
| R2.7 `lameenc` pinned | `pyproject.toml` inspection |
| R3.1–R3.3 the row: `Reply`, state saved first, both log events | pipeline tests |
| R3.4 failure never reaches `_respond` | pipeline test asserting the reply is the narration |
| R3.5 one reply per update | pipeline test |
| R3.6 `WELCOME` promises a voice note and only what exists | constant test |
| R3.7 narration text never lost | the three failure tests above |
| R4.1–R4.4 adapter narrowing, bare note, adapter fallback, no wording owned | bot tests |
| R5.1 no network or credential in tests | existing R10 sweeps + the key sweep extended to the new seam |
| R5.2 real encoder, ≥1 frame of PCM | narrator fixtures |
| R5.3 no live model/bot/`.env` in tests | grep |

---

## C. Constitution invariants

- [ ] **No secret leaks.** `GEMINI`/`TELEGRAM` key values appear in no test
      output, no log record, no exception message. Extend the existing API-key
      sweep to cover the `synthesize` seam.
- [ ] **No `str(exc)` anywhere in `src/`.** Grep returns nothing.
- [ ] **No payload in a log.** Every new record carries names, counts,
      durations and class names — never the narration bytes, never a Gemini
      response body, never an exception's `str()`.
- [ ] **State scoped to `chat_id`.** Unchanged; re-run the existing isolation
      tests to prove the new row did not disturb them.
- [ ] **Untrusted input validated at the edge.** The TTS payload and the encoded
      bytes both cross a boundary and are both validated before use (R1.3, R2.1).
- [ ] **No silenced failure.** Every new failure path logs at WARNING or ERROR
      *and* answers the user. Neither half may be skipped.
- [ ] **No bare `except`.** Grep returns nothing.

---

## D. Evidence to produce

```bash
./scripts/test          # exit 0: pytest -> ruff -> mypy strict
./scripts/hooks         # exit 0
```

```bash
# Leak and scope sweeps — every one must print nothing:
grep -rn "str(exc)" src/
grep -rn "except:" src/
grep -rn "except Exception:" src/ | grep "pass"
grep -rn "from google" src/ --include=*.py | grep -v "gemini\.py"
grep -rnE "send_photo|\.ogg|\.mp3\b|audio/ogg" src/     # no image work, no OGG
grep -rn "\bre\." src/telegram_documentaries/gemini.py   # mime parsed without regex
grep -n "lameenc" pyproject.toml                          # pinned, not just installed
```

```bash
git status --porcelain        # empty
git diff --name-only HEAD     # nothing unintended
```

Also record:

- The `VoiceName` literal contains exactly the voices verified in this repo.
- The model id used is `gemini-3.1-flash-tts-preview`, not `MODEL_ID`.
- A `git log --oneline` showing this feature's commits on
  `feature/2026-10-05-text-vertical-slice`.

---

## E. Manual smoke test (cannot be automated here)

This feature's whole purpose is a sound a human hears. **It is not complete
until this passes.**

### E.1 — Happy path (required)

1. Restart the bot onto the new code.
2. `/start` → the welcome asks for a portrait photo and mentions a voice note.
3. Send a portrait photo → 5–7 questions, one at a time.
4. Answer the last question.
5. **A bare voice note arrives and plays.** No caption, no duplicated text
   message.
6. The narration in that voice note matches a 60–90 word British-documentary
   narration about the answers given.
7. `/restart` → state and temp media purged; ready for a new photo.
8. A second run produces a voice note again.

### E.2 — Forced synthesis failure (required)

To prove the fallback with real code rather than a mock:

1. Temporarily point `TTS_MODEL_ID` at a non-existent model (one-line change).
2. Restart; run a full interview to the last answer.
3. **The narration arrives as a text message**, not a failure line —
   `GENERIC_FAILURE` must not appear.
4. The session is `SCRIPTED`: sending another message gets `SCRIPTED_NUDGE`.
5. The log shows `gemini_call` at ERROR with `stage="narrator"`, then
   `narration_voice_failed`.
6. **Revert the change** and confirm `git diff` is clean.

### E.3 — Out of scope for live testing

The adapter-side fallback (Telegram rejecting a valid MP3) cannot be forced
without stubbing Telegram. It is covered by unit tests only, and this must be
stated as a known gap in the verification report rather than presented as
verified live.

---

## F. Spec-versus-reality reconciliation

After implementation, walk this list and surface **every** difference between
what was written here and what was built:

- [ ] `SPECS/ROADMAP.md` — Phase 6 marked delivered with all four criteria;
      Status table row 6; Phase 5's "voice delivery remains" corrected.
- [ ] `SPECS/TECH.md` — D-V1 (`Reply` union) and D-V2 (two-method
      `GeminiClient`) recorded as divergences; `lameenc` in the dependency
      inventory; the new module named.
- [ ] `README.md` — current state, module tree, dependency list.
- [ ] `SPECS/2026-10-05-text-vertical-slice/requirements.md` — R1.2 and R9.3
      cross-referenced to the new deviations.
- [ ] `SPECS/MISSION.md` — checked; any delta **raised with the user before**
      editing.
- [ ] The logging inventory in `requirements.md` matches the events the code
      actually emits — no event added, none invented, none renamed.
- [ ] The divergence table lists exactly what diverged; anything discovered
      while building is added with its reason.
- [ ] Text-slice `validation.md` section F conclusions re-checked, since this
      feature changed `pipeline.py`, `bot.py` and `gemini.py` after them.
- [ ] The user's phone test (E.1 and E.2) reported back with the actual outcome,
      not an assumption.
