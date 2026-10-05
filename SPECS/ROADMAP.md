# ROADMAP

Ordered build plan, one phase per pipeline capability. A phase is marked
complete **only when verified** against its acceptance criteria.

---

## Phase 1 — Repository & gateway

Project skeleton, `.env` loading, and a long-polling loop that proves the
transport works end to end before any AI is involved.

**Acceptance criteria**
- `pyproject.toml` pins dependencies; `.venv` reproducible from it.
- `.env.example` documents both secrets; `.env` is gitignored.
- Settings load via `pydantic-settings` and **fail loudly** on a missing key.
- The bot long-polls, receives `/start`, and replies — echo reply is fine.
- `scripts/test` and `scripts/hooks` exist and pass on an empty suite.
- `README.md` covers setup, run, and checks.

**Rubric:** foundation. Serves the happy-path criterion only in skeletal form.

---

## Phase 2 — Bouncer

The vision gate: accept human portraits, reject everything else with a cheeky
message, and reset state on rejection.

**Acceptance criteria**
- Photo bytes are validated into a typed model at the edge.
- Gemini 3.1 Flash Lite classifies human vs non-human; decision is a typed enum,
  not a parsed string.
- Human → advance to interview. Non-human → playful rejection + state reset.
- Text arriving before a photo is handled gracefully.
- API failure degrades gracefully for the user and logs loudly.

**Rubric:** happy path (partial), out-of-order input.

---

## Phase 3 — Interviewer

Sequential stateful Q&A, dossier summary, suggested animal.

**Acceptance criteria**
- Asks **one question at a time** and waits for each answer.
- Accumulates a behavioural dossier bound to `chat_id`.
- Produces a suggested animal from the dossier.
- Conversation is an explicit state machine; illegal transitions are rejected
  explicitly, not coerced.
- `/restart` mid-interview purges state and temp media with no process restart.

**Rubric:** happy path, `/restart` reset, out-of-order input, isolation.

---

## Phase 4 — Converter

Hybrid animal portrait, fused from the real photo and the dossier.

**Acceptance criteria**
- Original photo + dossier go to Gemini 3.1 Flash Image in **one** call.
- Result goes straight to Telegram — **no intermediate text hop**.
- Output bytes are validated before sending.
- Generation failure degrades gracefully and logs loudly.

**Rubric:** happy path.

---

## Phase 5 — Scripter

One-paragraph dramatic narration.

**Acceptance criteria**
- Gemini 3.1 Flash Lite writes a 60–90 word British-documentary narration from
  the dossier.
- Length is validated, not assumed.
- Off-topic or degenerate output is detected and handled.

**Rubric:** happy path.

---

## Phase 6 — Narrator

TTS synthesis and audio delivery. **Not an agent** — a direct function.

**Acceptance criteria**
- Script routes to `gemini-3.1-flash-tts-preview`.
- Audio is rendered to a Telegram-compatible format (OGG/MP3).
- Voice note is delivered to the correct `chat_id`.
- Audio failure degrades gracefully — text still reaches the user.

**Rubric:** happy path.

---

## Phase 7 — Resilience

Hardening across the whole pipeline.

**Acceptance criteria**
- `/start` and `/restart` both purge state and temp files.
- Wrong-payload-at-wrong-stage guards on every state.
- API timeout and rate-limit fallbacks.
- No `except: pass` anywhere; no un-logged failure paths.

**Rubric:** all criteria.

---

## Status

| Phase | Name | Status |
|-------|------|--------|
| 1 | Repository & gateway | Not started |
| 2 | Bouncer | Not started |
| 3 | Interviewer | Not started |
| 4 | Converter | Not started |
| 5 | Scripter | Not started |
| 6 | Narrator | Not started |
| 7 | Resilience | Not started |