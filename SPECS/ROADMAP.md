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

**Delivered (2026-10-05).** Six modules under `src/telegram_documentaries/`:
`config.py` (`Settings`, both secret guards), `observability.py`
(`configure_logging` / `get_logger` / `@logged`), `contracts.py`
(`InboundUpdate` at the Telegram boundary), `bot.py` (the `/start` gateway),
`__main__.py` (entry point), plus the package. Both dev scripts ship and are
executable: `scripts/test` (107 tests, ruff, mypy strict) and `scripts/hooks`
(pre-commit; ruff + pytest staged-scoped, mypy always over all of `src/`).

Verified against the live bot `@BishalTech_bot`: the process starts, long-polls,
received `/start` (update_id 75407220) and replied in 283.61 ms, logging
`settings_loaded`, `gateway_started`, `start_command_received` and
`start_reply_sent`. The token never appears in any log. A missing or blank
secret exits 1 naming only the offending field, with the sibling secret absent
from stdout and stderr.

Two behaviours beyond the original letter of the phase, both required by R2 and
verified in the venv: a non-blank validator on each secret (`Field(min_length=1)`
does not enforce on `SecretStr`, so a whitespace-only token would otherwise load
and then fail as `InvalidToken` deep inside the library), and field-names-only
error reporting (Pydantic's `ValidationError` embeds a sibling secret in
`input_value`, so it must never be printed verbatim).

Deviation from the spec text: the boundary exception is
`InvalidInboundUpdateError`, renamed from `InvalidInboundUpdate` to satisfy ruff
`N818`; the spec's `requirements.md` and `validation.md` were updated to match,
and `N818` stays enabled as the regression guard.

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

**Delivered (2026-10-05), as part of the text vertical slice.** `bouncer.py`
turns photo bytes into a typed verdict through `gemini.py`; the decision itself
lives in `pipeline.py`, which is where "human → advance, otherwise → reject and
reset" is written down once. `/start` asks for a photo; a text message arriving
first is nudged back to the conversation rather than treated as an answer
(out-of-order input); an unreadable photo is answered with one short line and
logged, never crashed on. The user-facing text comes from `pipeline.py`'s
constants, so no stage owns a string.

Verified by `tests/unit/test_pipeline.py` and `tests/unit/test_bouncer.py`
against a fake transport — zero network.

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

**Delivered (2026-10-05), as part of the text vertical slice.**
`interviewer.py` holds `plan()` and `next_question()` with a schema-level 5–7
question rule; `state.py` holds `Phase`, `SessionStore` and every transition, as
a plain class with no I/O so illegal transitions can be asserted directly rather
than inferred from a mock. `/restart` purges the session *and* the stored photo
without a process restart, and two chats never see each other's answers. The
dossier accumulates one answer at a time; the suggested animal is produced when
the Scripter asks for it.

Verified by `tests/unit/test_interviewer.py` and `tests/unit/test_state.py`.

---

## Phase 4 — Converter

Hybrid animal portrait, fused from the real photo and the dossier.

**Acceptance criteria**
- Original photo + dossier go to Gemini 3.1 Flash Image in **one** call.
- Result goes straight to Telegram — **no intermediate text hop**.
- Output bytes are validated before sending.
- Generation failure degrades gracefully and logs loudly.

**Rubric:** happy path.

**Blocked (verified 2026-10-08) on image-generation quota — a billing matter,
not a code one.** Every image-capable model on the project's API key returns
`429 RESOURCE_EXHAUSTED: generate_content_free_tier_requests`:
`gemini-3.1-flash-image`, `gemini-2.5-flash-image`,
`gemini-3.1-flash-lite-image`, `gemini-3-pro-image` and
`gemini-3-pro-image-preview` were all probed, and Google's published plans list
the free tier as *not available* for image generation. Enabling billing makes
the feature runnable as specified — at roughly **$0.067 per portrait** by the
schedule's pricing; the call shape, the typed-reply parsing and the graceful
degradation are all already written and waiting. The phase resumes when the key
has billing, no code change anticipated.

---

## Phase 5 — Scripter

One-paragraph dramatic narration.

**Acceptance criteria**
- Gemini 3.1 Flash Lite writes a 60–90 word British-documentary narration from
  the dossier.
- Length is validated, not assumed.
- Off-topic or degenerate output is detected and handled.

**Rubric:** happy path.

**Delivered (2026-10-05 for the text; 2026-10-08 for delivery).**
`scripter.py` writes the 60–90 word narration, counts the words locally rather
than trusting the model, and allows exactly one corrective retry that restates
the bounds and the rejected count. A second failure is reported, never padded,
truncated or replaced with a canned sentence. Delivery is complete: the
narration reaches the user as a Telegram **voice note** voiced by Phase 6's
Narrator (voice `Kore`, MP3 encoded in-process with `lameenc`), and when speech
synthesis fails the narration still reaches the user as text (D6 / D-V4). The
Converter path (Phase 4) remains the only outstanding leg.

Verified by `tests/unit/test_scripter.py`, including both retry outcomes.

---

## Phase 6 — Narrator

TTS synthesis and audio delivery. **Not an agent** — a direct function.

**Acceptance criteria**
- Script routes to `gemini-3.1-flash-tts-preview`.
- Audio is rendered to a Telegram-compatible format (OGG/MP3).
- Voice note is delivered to the correct `chat_id`.
- Audio failure degrades gracefully — text still reaches the user.

**Rubric:** happy path.

**Delivered (2026-10-08), live-verified.** Four criteria met: the script routes
to `gemini-3.1-flash-tts-preview`; audio is rendered in-process to MP3 via
`lameenc` (no system encoder); the voice note goes to the conversation's own
`chat_id`; and a synthesis failure degrades to the narration as text (D6).
Verified live against `@BishalTech_bot` — the end-to-end run logged
`narration_delivered` then `voice_note_sent`, and a received voice note played
the narration. The follow-up timeout fix (`fb2b675`) gave synthesis its own
60 s budget — a live voice note had twice died on the shared 20 s text ceiling —
regression-guarded and live-retested on the fixed code. See
`SPECS/2026-10-08-narrator-voice-note/`.

---

## Phase 7 — Resilience

Hardening across the whole pipeline.

**Acceptance criteria**
- `/start` and `/restart` both purge state and temp files.
- Wrong-payload-at-wrong-stage guards on every state.
- API timeout and rate-limit fallbacks.
- No `except: pass` anywhere; no un-logged failure paths.

**Rubric:** all criteria.

**Delivered (2026-10-09).** `/start` and `/restart` purge state and temp media,
every (phase × payload) row of the decision table has a defined outcome proven
cell-by-cell by the matrix test, wrong-payload guards cover every state, and
the two API-failure fallbacks ship: a bounded backoff retry for `429`s (at most
three attempts, 1 s then 2 s — the only failure the transport re-attempts) and
per-class timeouts (text 20 s, synthesis 60 s). A spent throttle is answered
with a "try again" line, the one reply that never says `/restart`, because the
session is held for a resend. A silent-failure guard walks `src/` and refuses
any `except:` that swallows — bare `except:`, or a body of only `pass`/`...` —
and is self-tested so it cannot rot. See
`SPECS/2026-10-09-resilience/`. Suite: 608 tests, ruff, mypy strict.

---

## Status

| Phase | Name | Status |
|-------|------|--------|
| 1 | Repository & gateway | **Complete** (verified live) |
| 2 | Bouncer | **Complete** (text slice; part of the 608-test suite) |
| 3 | Interviewer | **Complete** (text slice; part of the 608-test suite) |
| 4 | Converter | **Blocked** — image-generation quota on the API key (`429 generate_content_free_tier_requests` on every image model); resumes when billing is enabled |
| 5 | Scripter | **Complete** — delivered as a voice note (`lameenc`, voice `Kore`); Converter leg still blocked |
| 6 | Narrator | **Complete** — verified live: `narration_delivered` + `voice_note_sent`, timeout fix `fb2b675` |
| 7 | Resilience | **Complete** — per-class timeouts, bounded 429 retry, wrong-payload matrix, no-silent-except guard; 608 tests green |

**Live verification: done.** The end-to-end flow — portrait photo → Bouncer →
5–7 questions → narration → **voice note** — ran against the live bot
`@BishalTech_bot`: the run logged `narration_delivered` then `voice_note_sent`,
and the received voice note played the narration. Phase 2/3/5/6 are complete on
the suite *and* live-verified; Phase 7 is complete on the suite. The one
outstanding leg is Phase 4, blocked on image-generation quota (see the Phase 4
entry).