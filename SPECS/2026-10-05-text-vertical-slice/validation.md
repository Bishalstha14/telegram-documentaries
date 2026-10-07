# Validation — Text vertical slice (Bouncer, Interviewer, Scripter)

Merge only when every item below passes. The verifier works these item by item,
against the **actual code and behaviour**, not against intent, and updates the
constitution and README with what was really shipped.

## A. Roadmap acceptance criteria

### A.1 — Phase 2, Bouncer

| # | Criterion | How to verify |
|---|-----------|---------------|
| A1.1 | Photo bytes are validated into a typed model at the edge | `PhotoAttachment` is a frozen Pydantic model built by `from_telegram`; a blank `file_id` or a non-positive dimension raises `InvalidInboundUpdateError` and no attachment is produced. Empty bytes are rejected by `media.save_photo`. |
| A1.2 | Gemini 3.1 Flash Lite classifies human vs non-human; the decision is a typed enum, not a parsed string | `Verdict` is a `StrEnum` returned inside `BouncerVerdict`. `test_judge_returns_a_typed_verdict_and_never_a_parsed_string` passes, and no `src/` file matches a `startswith`/`in` test against a model's reply text. |
| A1.3 | Human → advance to interview | Drive a full interview from an accepted photo; the first question is the first reply. |
| A1.4 | Non-human → playful rejection + state reset | The model's line (or the local fallback) is sent, the session is back at `AWAITING_PHOTO` with no plan and no answers, and the media directory is gone. |
| A1.5 | Text arriving before a photo is handled gracefully | A text message in `AWAITING_PHOTO` gets a prompt for a photo, makes no Gemini call, and does not raise. |
| A1.6 | API failure degrades gracefully for the user and logs loudly | Force `GeminiUnavailableError` and `GeminiResponseError` from the bouncer. The user gets a plain line, no state change, no media written, and a record at `exception` level naming the stage. |

### A.2 — Phase 3, Interviewer

| # | Criterion | How to verify |
|---|-----------|---------------|
| A2.1 | Asks one question at a time and waits for each answer | After each answer, exactly one new message is sent and it is the next question. `test_each_answer_sends_exactly_one_message`. |
| A2.2 | Accumulates a behavioural dossier bound to `chat_id` | `SessionState.answers` grows by exactly one frozen `Answer` per answer, and the state is reachable only through `SessionStore` keyed by the integer `chat_id`. |
| A2.3 | Produces a suggested animal from the dossier | `InterviewPlan.suggested_animal` is non-blank, and the Scripter is given it — asserted by inspecting the captured Gemini request. |
| A2.4 | The conversation is an explicit state machine; illegal transitions are rejected explicitly, not coerced | `LEGAL_TRANSITIONS` is the single table; `test_every_transition_not_in_the_table_is_rejected` passes; `SessionTransitionError` names from-phase, to-phase and event; no handler writes a phase field directly. |
| A2.5 | `/restart` mid-interview purges state and temp media with no process restart | `/restart` after three answers: state is a fresh `AWAITING_PHOTO` with no plan, no answers and no script, and the per-`chat_id` media directory no longer exists. The process was never restarted. |

### A.3 — Phase 5, Scripter (text half only)

| # | Criterion | How to verify |
|---|-----------|---------------|
| A3.1 | Gemini 3.1 Flash Lite writes a 60–90 word British-documentary narration from the dossier | The captured request carries the plan, the suggested animal and every `Answer`; the delivered text is the model's. |
| A3.2 | Length is validated, not assumed | The count is computed locally in `scripter.py`. 59 and 91 words are rejected; 60 and 90 are accepted. No count is read from the model. |
| A3.3 | Off-topic or degenerate output is detected and handled | **Partially met by design — see R8.4.** Verified as: empty, whitespace-only, out-of-range and over-long replies are all rejected explicitly, retried once, and then degraded to a logged, user-visible message with no coerced output. Semantic off-topicness is **not** detected; the deviation is recorded in section F and must be stated in `ROADMAP.md`, not glossed. |

### A.4 — The vertical slice itself (not a roadmap phase; the point of this branch)

| # | Criterion | How to verify |
|---|-----------|---------------|
| A4.1 | A full run works end to end on a phone | **Manual smoke test, section E.** No automated suite can prove it. |
| A4.2 | Exactly three Gemini calls per successful run | Count the fake client's recorded calls across a whole run: 1 verdict, 1 plan, 1 script. This is D2's cost claim, verified. |
| A4.3 | No image, no audio, no voice note | `grep` finds no reference to `send_photo`, `send_voice`, `send_audio`, `.ogg`, `.mp3`, TTS, or the two out-of-scope model ids anywhere in `src/`. The final message is text. |

## B. Requirement coverage

- [ ] **R1.1** — `GeminiRequest` is a frozen model with `stage` (`Stage`),
      `system_instruction`, `prompt`, `image`, `image_mime_type`.
- [ ] **R1.2** — `GeminiClient` is a `runtime_checkable` `Protocol` with one
      `generate` method; `GenAiGeminiClient` is the only importer of
      `google.genai`.
- [ ] **R1.3** — every call sets `response_mime_type="application/json"` and the
      stage's `response_schema`; the return type is the schema type.
- [ ] **R1.4** — all five rejection cases raise `GeminiResponseError`; none
      coerces, completes or retries. Confirmed by reading the parse order, not
      only by the tests.
- [ ] **R1.5** — `HttpOptions.timeout == GEMINI_TIMEOUT_MS` on the constructed
      client; the SDK default is not relied on.
- [ ] **R1.6** — timeout, `httpx.HTTPError`, `ServerError`, 429 and ≥ 500 map to
      `GeminiUnavailableError`; both classes subclass `GeminiError`.
- [ ] **R1.7** — no record and no exception message contains an API key, a raw
      response body, or a rendered `ValidationError`. Verified with sentinels, on
      every failure path.
- [ ] **R1.8** — `MODEL_ID == "gemini-3.1-flash-lite"`, used by all three stages,
      logged on every record, and not a `Settings` field.
- [ ] **R2** — `InboundUpdate.attachment` is a union discriminated on `kind`;
      `PhotoAttachment` validates its ids and dimensions; `UnsupportedAttachment`
      carries a closed `StrEnum` kind; the largest photo is chosen by
      `(file_size, width * height)` and not by list position; a caption is never
      read as an answer.
- [ ] **R3** — `SessionState` is frozen, versioned and typed; `SessionStore` is
      the only reader/writer and exposes no dict; a mismatched version is
      discarded with a `SessionVersionError`; the single-threaded assumption is
      documented.
- [ ] **R4** — `Phase` has exactly three members; `LEGAL_TRANSITIONS` is the only
      transition table; an illegal transition raises `SessionTransitionError`
      with from-phase, to-phase and event; each transition is one function.
- [ ] **R5** — per-`chat_id` directory under the temp dir; empty bytes and
      over-cap declared sizes are rejected before writing; `purge` removes the
      directory, treats absence as a logged debug no-op, and re-raises a genuine
      failure at `exception` level.
- [ ] **R6** — `Verdict` is a typed enum; `UNSURE` is accepted and logged as
      `bouncer_unsure`; a blank or over-long cheeky line falls back to
      `BOUNCER_REJECTION_FALLBACK`; `NOT_HUMAN` resets the session and purges the
      media, in that order; a Gemini failure changes nothing.
- [ ] **R7** — the plan comes from one call made with the Bouncer's subject; the
      5–7 count and the question length bound are schema constraints, not string
      checks; questions are asked from local state with **no** model call between
      answers; the dossier is the typed `Answer` list; an empty answer does not
      advance the plan.
- [ ] **R8** — the word count is computed locally and bounded to 60–90 inclusive;
      the script is built from the plan, the suggested animal and the answers;
      there is exactly one corrective retry that restates the actual count; after
      a second failure nothing is sent, `script_rejected` is logged at
      `exception` level, and the user is pointed at `/restart`; nothing is padded,
      truncated or locally substituted; the script is delivered as one
      `send_message`.
- [ ] **R9** — the decision table covers all eleven (phase × payload) rows; the
      hub depends on the `PhotoFetcher` protocol, not on python-telegram-bot;
      `bot.py` registers three handlers plus the error handler and sends exactly
      one message per update; `build_application(token, pipeline)` replaced the
      old signature and `GREETING` is gone with no shim; every listed error class
      is caught at the hub boundary, logged and answered.
- [ ] **R10** — no test requires network access or a real credential; the three
      fakes are the only seams; assertions are on messages sent, state, and log
      events.

## C. Constitution invariants

- [ ] **No secrets in logs.** The bot token and the Gemini API key appear in no
      record on any path, including every new failure path. Both sweeps
      (`..._contains_the_bot_token`, `..._contains_the_gemini_api_key`) pass.
- [ ] **Typed values at every boundary.** No raw Telegram payload and no raw
      Gemini response reaches business logic. `google.genai` is imported in
      exactly one module. No `dict` crosses a module boundary.
- [ ] **Schemas over regexes.** No reply from Gemini is parsed by pattern
      matching. `grep` finds no `re.` usage against model output in `src/`.
- [ ] **No swallowed exceptions.** No `except: pass`, no bare `except`, no
      un-logged fallback. Verify by inspection and by grep, not by trust.
- [ ] **Nothing anticipates later phases.** No image generation, no TTS, no audio,
      no `send_photo`, no session TTL, no retry/backoff beyond the single
      corrective script retry, no config toggle, no extension point, no unused
      `Settings` field.
- [ ] **Isolation is real.** Two chats driven through interleaved interviews never
      see each other's answers, questions, script or state.
- [ ] **`.env` stays ignored** and no secret is committed.
- [ ] **`scripts/test` passes on a clean tree**, and `scripts/hooks` passes with
      and without staged files.

## D. Evidence to produce

1. `scripts/test` — full output, exit `0`, with the before/after test count.
2. `scripts/hooks` — with and without staged files, exit `0`.
3. The four greps from plan task group 11: no `except: pass`; no bare `except`;
   `google.genai` only in `gemini.py`; no image/TTS/audio/out-of-scope-model
   references.
4. The A.4.2 call count: the fake client's recorded calls for one full run.
5. The isolation run: the two-chat test, with both chats' sent messages printed.
6. The D3 degradation run: state before and after a mid-interview Gemini failure,
   showing the answers and the pending question unchanged.
7. `git status` showing only intended files, and `git check-ignore .env` still
   succeeding.

## E. Manual smoke test (cannot be automated here)

This is the criterion the suite cannot prove, and A4.1 depends on it. Run it on a
phone against the real bot and the real key.

1. `cp .env.example .env` and fill in the real `TELEGRAM_BOT_TOKEN` and
   `GEMINI_API_KEY`.
2. Run `python -m telegram_documentaries`.
3. Send `/start`. Expect a welcome and a request for a portrait photo.
4. Send a **portrait photo of a person**. Expect acceptance, then the first
   question.
5. Answer each question with a real message, one at a time. Expect **exactly one
   new question per answer** — never two questions at once, never a skipped
   question. Count them: between 5 and 7.
6. Expect the narration to arrive as a **text message**. Count its words: 60–90.
   It should read like a wildlife documentary about you.
7. Send another text message. Expect a nudge to send a new photo, and no crash.
8. Send `/restart`. Expect confirmation that the session is wiped. Send a text
   message: expect the request for a portrait photo, proving the state really was
   purged without a process restart. `ls` the temp directory: the previous
   `chat_id`'s directory is gone.
9. Start again, answer two questions, then send a **photo of a pet**. Expect a
   cheeky rejection, a state reset, and a request for another portrait. Then send
   a portrait and confirm the interview starts from question one.
10. Send a **sticker**. Expect a reply naming what was sent, and no crash.
11. Send a **text message before any photo** in a fresh chat. Expect a request for
    a photo, and no crash.
12. Confirm the log throughout: `chat_id` and `update_id` on every record, three
    `gemini`-decorated calls with `stage` and `model`, and **no token and no API
    key in any line**.
13. `Ctrl-C` to stop. The process exits `0`.

If A4.1 cannot be run, the phase is **not** complete: "a portrait photo produces
the narration, asked one question at a time" is an end-to-end claim about Gemini,
Telegram and a real user, and no mock proves it.

## F. Spec-versus-reality reconciliation

- [ ] Every requirement in section B is implemented, or the deviation is recorded
      here with its justification.
- [ ] **`ROADMAP.md` Phase 2 and Phase 3 marked delivered** with details; the
      Status table updated.
- [ ] **`ROADMAP.md` Phase 5 marked delivered for the text half only**, with the
      Converter and Narrator explicitly outstanding, and A3.3's partial
      satisfaction stated rather than glossed.
- [ ] **`TECH.md` updated** with the `google.genai` divergence (D1) and the
      `PhotoFetcher` port, so the next phase starts from the truth.
- [ ] **`MISSION.md` reviewed** and changed only if product scope or behaviour
      actually changed.
- [ ] **`README.md` synchronised** with shipped behaviour, including a plain
      statement that the hybrid image and the voice note are not built yet.
- [ ] **The new logging inventory in `requirements.md` reconciled** against the
      events the code actually emits — every row present, or corrected here.
- [ ] **The decisions table D1–D10 reconciled** against what was built, with any
      reversal recorded and justified.
- [ ] **Phase 1 files reconciled**: `test_bot.py` and `test_main.py` updated for
      the D7 and D10 changes, with the Phase 1 guards (`test_main_names_only_the_missing_field_in_its_message`,
      `test_settings_validation_error_text_never_contains_the_secret_value`,
      `test_start_command_sends_the_greeting_to_the_integer_chat_id`) still
      passing, in intent if not in name.
- [ ] Nothing committed without review.
