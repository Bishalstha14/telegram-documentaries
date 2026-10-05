# Requirements — Text vertical slice (Bouncer, Interviewer, Scripter)

## Summary

Build the first end-to-end run of the pipeline, and make it a *text* result:
the user sends a portrait photo, is interviewed, and receives the dramatic
British-documentary narration **as a Telegram text message**.

Covers ROADMAP.md Phases 2, 3 and the text half of Phase 5 in one branch. The
user can do all of this on a phone today, end to end:

`/start` → portrait photo → accepted, or playfully rejected → 5–7 questions
asked **one at a time** → narration text arrives in Telegram → `/restart` wipes
everything.

Three Gemini calls per successful run: the vision verdict, the interview plan,
the narration. Not one per turn.

## Context

Phase 1 is merged and verified (`SPECS/2026-10-05-repository-and-gateway/`). Six
modules ship under `src/telegram_documentaries/`: `config.py` (`Settings` with
both secret guards), `observability.py` (`configure_logging` / `get_logger` /
`@logged`), `contracts.py` (`InboundUpdate` at the Telegram boundary), `bot.py`
(a `/start` long-polling gateway), `__main__.py`, and the package. Both dev
scripts ship and are green: `scripts/test` (107 tests, ruff, mypy strict) and
`scripts/hooks`.

The stack already contains what this phase needs. Verified in the project venv:

- `google-adk==2.11.0`, which brings `google-genai==2.28.0`. `Client` exposes
  `client.aio.models.generate_content`, and `GenerateContentConfig` accepts
  `response_mime_type` and `response_schema` — so replies can be schema-first
  rather than regex-parsed.
- `google.genai.errors` exposes `APIError`, `ClientError`, `ServerError`,
  `UnknownApiResponseError`; transport timeouts arrive as
  `httpx.TimeoutException` (httpx 0.28.1 is installed).
- `python-telegram-bot==22.8`: `Message.photo` is a sequence of `PhotoSize`
  (`file_id`, `file_unique_id`, `width`, `height`, `file_size | None`);
  `await bot.get_file(file_id)` returns a `File`, and
  `await File.download_as_bytearray()` is a coroutine. Download methods are on
  `File`, not on `Bot`.
- `gemini-3.1-flash-lite` is present on the configured key and is the model for
  all three stages. (`gemini-3.1-flash-image` and
  `gemini-3.1-flash-tts-preview` also exist and are out of scope here.)

Nothing else in the constitution changes. This phase is the first one that
touches Gemini, so the Gemini boundary is specified in full detail (R1) — it is
the project's biggest new risk surface.

## Decisions

Confirmed with the user before this spec was written. Not open questions.

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Reach Gemini through a thin typed module `gemini.py` over `google.genai`, not ADK's `Runner` / session service** | TECH.md names Google ADK as the agent framework, and this is a deliberate, recorded divergence — see "Divergences" below. ADK's Runner plus a session service would duplicate the state driver we are required to build anyway (TECH.md: "One shared state driver owns all reads and writes"), and mocking an ADK agent means faking deep inside `BaseLlm`. One `generate_content` call behind one protocol is the simplest thing that is still a real boundary: one mock seam, one place where a malformed reply is rejected. ADK can be introduced later, if it earns its place. |
| D2 | **The Interviewer asks questions from one plan call made immediately after the Bouncer passes** | The model returns a typed plan: how many questions (5–7), the questions themselves, and a suggested animal. Asking then happens from local state with **no further model calls**. Total: 3 model calls for a whole run instead of 7–8, and one fewer failure point per answer. The questions are still model-written, so they stay playful, which is the product. |
| D3 | **On a Gemini failure mid-interview, hold the state and re-ask the same question** | Answers already given and the pending question are kept untouched. The user gets one short apologetic line; their next message is consumed as the answer to the same question. Nothing is skipped and nothing is lost — which matters, because throwing away five good answers because of a transient outage is the worst available outcome. |
| D4 | **Three phases: `AWAITING_PHOTO → AWAITING_ANSWER → SCRIPTED`; a photo arriving mid-interview is a fresh start** | The three states are exactly the three things the user can be doing. A re-sent selfie means "start again", not "no" — accepting it as a fresh start is what the user meant. `SCRIPTED` is terminal: it is left only by `/start`, `/restart`, or a new photo. A separate in-flight `SCRIPTING` phase was considered and rejected (YAGNI): the script is produced inside a single handler, so it is not a state the user can be observed in. |
| D5 | **A rejected script gets one corrective retry, then degrades loudly** | Validate the word count locally; on failure re-ask once with the actual count restated in the prompt. If the second attempt is still invalid, log at `exception` level and tell the user plainly, pointing at `/restart`. The reply is never coerced, padded or truncated. |
| D6 | **The Bouncer verdict is a three-valued enum and `UNSURE` is accepted** | `HUMAN` / `NOT_HUMAN` / `UNSURE`, with `UNSURE` accepted and logged at `warning` as `bouncer_unsure`. A two-valued enum would have to fold `UNSURE` into one bucket arbitrarily. Failing *open* is the right side of that choice: Gemini vision is reliable, and a wrongly rejected portrait costs the user the entire experience, whereas a wrongly accepted one costs a slightly odd interview. |
| D7 | **`config.settings_error_fields` becomes a generic `contracts.validation_error_fields`** | Gemini's off-schema reply fails pydantic validation exactly the way a missing `.env` key does, and Phase 1 already solved that leak correctly: extract field **names**, never `str(exc)`. Moving the function to a neutral name and reusing it is six lines instead of a copy. No behaviour change; `__main__.py` and the two Phase 1 tests that reference it are updated (plan task group 2). |
| D8 | **`observability.logged` gains an optional static `extra={...}`** | Every Gemini call wants `stage` and `model` on its record. Threading them through the decorator's message argument would either mangle the log line or force a `log.info` into every stage, which TECH.md explicitly discourages. Static keys are merged into each record and may not overwrite reserved `LogRecord` attributes. |
| D9 | **One timeout constant, not a config setting** | 20 seconds per Gemini call, a module constant in `gemini.py`. It must be bounded (R1.5) but it does not need to be tunable, so it is not a `Settings` field. Retry and rate-limit backoff stay Phase 7. |
| D10 | **`build_application(token)` becomes `build_application(token, pipeline)`; `GREETING` moves out of `bot.py`** | The bot is now a thin Telegram adapter over the hub, so the hub is injected rather than built inside the adapter. `GREETING` moves to `pipeline.py` because its content is now a state-machine outcome, not a transport detail — and because the Phase 1 text ("the pipeline is not built yet") is now false and must not survive. `InboundUpdate` is internal with no external consumers, so no backward-compatibility shim is kept for it either. Phase 1 test call sites are updated explicitly (plan task group 9). |

## Scope

### In scope

1. **`gemini.py`** — the typed Gemini boundary: one request model, one protocol,
   one implementation, strict reply parsing, explicit rejection of malformed or
   truncated replies, a bounded timeout, and two error classes that separate
   "the network is down" from "the model said something unusable".
2. **`contracts.py` extended** — a typed inbound photo and a typed
   "unsupported attachment", so media reaching the hub is already validated.
3. **`state.py`** — the versioned per-`chat_id` session state, the single state
   driver, the declared legal-transition table, and explicit rejection of
   illegal transitions.
4. **`media.py`** — the per-`chat_id` temporary media directory, its write
   validation, and its deletion on `/start` and `/restart`.
5. **`bouncer.py`** — the vision gate: accept a human, cheekily reject anything
   else, reset state on rejection.
6. **`interviewer.py`** — the plan call, one-question-at-a-time delivery from
   local state, the accumulated dossier, and the suggested animal.
7. **`scripter.py`** — the 60–90 word narration, length validated locally, with
   one corrective retry.
8. **`pipeline.py`** — the hub. One decision table mapping
   (phase × payload) to an action and a reply.
9. **`bot.py` rewired** — three thin handlers and exactly one send per update.
10. **Tests** covering all of the above, with **no network access and no real
    credentials**.
11. **Documentation sync** — `README.md`, `ROADMAP.md`, `TECH.md`, and this spec
    folder, in the same change (TECH.md README policy).

### Explicitly out of scope (YAGNI)

- **The Converter** and any image generation (`gemini-3.1-flash-image`). Roadmap
  Phase 4. Nothing in this phase produces or sends an image.
- **TTS and voice notes** (`gemini-3.1-flash-tts-preview`). Roadmap Phase 6. The
  narration arrives as text and nothing else.
- **Audio in any form** — no OGG, no MP3, no encoding, no voice notes.
- **Media rendering beyond accepting the inbound photo.** No resizing, no
  re-encoding, no compression, no image library. The bytes Telegram delivered
  are the bytes the Bouncer judges and the bytes written to the session
  directory.
- **Retry and rate-limit backoff.** Phase 7. This phase bounds every call with a
  timeout and degrades once; it does not retry transport failures.
- **Session expiry, TTL, pruning or an LRU cap** on in-memory sessions. Unbounded
  for now, noted for Phase 7.
- **Localised or non-English output.**
- **A database or any persistence beyond process memory.**
- **Editing the narration, re-running one stage, or any admin tooling.**
- **Detecting semantically off-topic narration.** See R8.4.
- **ADK agents, the ADK `Runner`, and ADK session services.** See D1.

## Requirements

### R1 — The Gemini boundary (`gemini.py`)

One module owns every call to Gemini. No stage imports `google.genai`; no stage
sees a raw response object.

**R1.1 — The request is typed.** `GeminiRequest` is a frozen Pydantic model:

| Field | Type | Meaning |
|-------|------|---------|
| `stage` | `Stage` (a `StrEnum`) | `bouncer`, `interviewer` or `scripter`; the log correlation field. |
| `system_instruction` | `str` | The stage's persona and hard rules. |
| `prompt` | `str` | The turn's instruction and inputs. |
| `image` | `bytes \| None` | The portrait, for the Bouncer only. Never a path, never a `FileId`. |
| `image_mime_type` | `str` | Defaults to `image/jpeg`; validated against a closed set of `image/*` values. |

**R1.2 — The client is a protocol, not a class.** `GeminiClient` is a
`typing.Protocol` with one method,
`generate(request, response_schema, chat_id, update_id) -> <response_schema>`, so
tests inject a fake and never touch a socket. The production implementation
(`GenAiGeminiClient`) is the only thing in the codebase that imports
`google.genai`. Construction takes the API key, a timeout in milliseconds, and
builds one `genai.Client` with `http_options=types.HttpOptions(timeout=...)`.

**R1.3 — Replies are schema-first.** Every call sets
`response_mime_type="application/json"` and passes the stage's Pydantic model as
`response_schema`, and the client's return type is that same model type. This is
TECH.md's "prefer schemas over regexes" applied to model output: structure comes
from a schema, never from string matching. No prompt asks the model to return
JSON as text, and no stage parses a text blob.

**R1.4 — A malformed or truncated reply is rejected explicitly, never coerced.**
`generate` raises `GeminiResponseError` — it does not retry, retry-with-coercion,
or hand back a partially filled model — in each of these cases:

1. `response.candidates` is empty.
2. `candidate.finish_reason` is `MAX_TOKENS`. A truncated JSON string is not a
   short answer, it is a broken one, and completing it would be coercion.
3. Any part of the content carries something other than text (inline data, a
   function call). This phase asks for text-only replies and gets only text; an
   unexpected part is a defect, not a variation.
4. The text is not valid JSON. The `JSONDecodeError` is chained, never rendered.
5. The JSON does not satisfy `response_schema`. The error carries the offending
   field **names** from `ValidationError.errors()` via
   `contracts.validation_error_fields` (D7) — never `str(exc)`, and never the
   reply body.

A `GeminiResponseError` is also raised when the field names cannot be
determined at all; the message must then say so rather than staying silent.

**R1.5 — Every call is bounded.** The timeout is a module constant,
`GEMINI_TIMEOUT_MS = 20_000`, applied through `types.HttpOptions`. The
implementation must not rely on the SDK's default.

**R1.6 — Two error classes, deliberately separate.**

- `GeminiUnavailableError` — the call did not produce a usable answer for
  environmental reasons: `httpx.TimeoutException`, `httpx.HTTPError`,
  `google.genai.errors.ServerError`, and any `APIError` whose `code` is 429 or
  ≥ 500. This is the class that earns the user a gentle apology and a preserved
  session.
- `GeminiResponseError` — a reply arrived and was rejected under R1.4. This is
  our defect, so it is logged loudly; the user still gets a plain apology rather
  than a stack trace.

Both subclass `GeminiError`, so a caller can distinguish "the model was
unusable" from "the model was unusable" only where it matters, and a stage can
catch the base class when it does not.

**R1.7 — No `str(exc)` is ever logged, at any level.** `APIError.__str__` embeds
`self.details`, the raw response body, and HTTP debug logging would render the
API key in a header. Logs carry `stage`, `model`, `chat_id`, `update_id`, the
exception's **class name**, and `getattr(exc, "code", None)` when present —
never the message, never the body, never the reply text. `http_options` debug
logging stays off. The same rule Phase 1 applied to `ValidationError` applies
here, for a different reason and with the same mechanism.

**R1.8 — The model id is one constant.** `MODEL_ID = "gemini-3.1-flash-lite"` in
`gemini.py`, used by all three stages and logged on every record. It is not a
`Settings` field (D9), and the two models that exist but are out of scope are
not named anywhere in the code.

### R2 — The Telegram boundary, extended (`contracts.py`)

`InboundUpdate` gains a third field alongside `update_id` and `text`:

| Field | Type | Meaning |
|-------|------|---------|
| `attachment` | `InboundAttachment \| None` | The message's media, already typed. |

`InboundAttachment` is a **discriminated union on `kind`**, so the hub narrows
on a typed field rather than inspecting a payload:

- `PhotoAttachment` — `kind="photo"`, plus `file_id` (non-blank),
  `file_unique_id` (non-blank), `width` and `height` (`StrictInt`, must be
  positive), and `file_size` (`StrictInt | None`).
- `UnsupportedAttachment` — `kind="unsupported"`, plus `media_kind`, a
  `StrEnum`: `sticker`, `video`, `audio`, `voice`, `animation`, `document`,
  `unknown`. A closed set, so the user-facing message can name what was sent
  without parsing anything.

**R2.1 — The largest photo is selected deterministically.** Telegram sends up to
four sizes ascending. `from_telegram` picks the maximum by
`(file_size or 0, width * height)`, not by list position, so a reordered payload
cannot change which photo is judged. `file_size` is optional, hence the
`or 0` and the `width * height` tiebreak.

**R2.2 — A malformed attachment is rejected explicitly.** A `PhotoSize` with a
blank `file_id`, or with a non-positive width or height, raises
`InvalidInboundUpdateError` — never a partially-populated attachment. The
existing rules are unchanged and still hold: a message-less update returns
`None`, a missing chat or non-integer `chat.id` raises, nothing is coerced.

**R2.3 — A caption is never an answer.** A photo's caption arrives in
`message.caption`, not `message.text`, so it cannot be mistaken for an interview
answer. When an attachment is present the hub ignores `text` entirely; this is
stated so a later change to the parsing does not quietly open the path.

**R2.4 — `validation_error_fields` lives here.** D7: moved from `config.py`,
generalised, and used by both `config` and `gemini`. `config.settings_error_fields`
is deleted rather than aliased — no legacy shim (skill: do not keep legacy code
for its own sake).

### R3 — Versioned session state, one driver (`state.py`)

**R3.1 — `SessionState` is a frozen Pydantic model with a `version` field** whose
value is fixed to `1` (TECH.md: a `version` field so the shape can evolve without
corrupting live sessions). Fields: `version`, `chat_id` (`StrictInt`), `phase`
(`Phase`), `photo` (`StoredPhoto | None`), `plan` (`InterviewPlan | None`),
`pending_question` (`str | None`), `answers` (a tuple of frozen `Answer`
(`question`, `answer`) models), and `script` (`Script | None`).

Because the model is frozen, "updating" state means producing a new instance. The
state is therefore not silently half-mutated by a failure part-way through a
transition — which is exactly what R4 needs.

**R3.2 — `SessionStore` is the only thing that touches the dict.** In-memory,
keyed by `chat_id`. It exposes `load`, `save` and `purge`, and exposes **no**
access to its backing dict, so no module can reach around it (TECH.md: "One
shared state driver owns all reads and writes"). `load` returns a fresh
`AWAITING_PHOTO` state for an unknown `chat_id`, so a brand-new user needs no
special case.

**R3.3 — A version that is not the current one is discarded, not migrated.**
`load` raises `SessionVersionError` carrying the found version; the hub logs it
at `warning` and treats it exactly like an unknown `chat_id` (fresh state). No
migration code ships in this phase, and none is stubbed (YAGNI). This is the
mechanism-level guard that keeps a future schema change from corrupting a live
session.

**R3.4 — The store is single-threaded by design, and says so.** It performs no
locking. python-telegram-bot delivers one chat's updates sequentially under
default settings, which is sufficient today; the docstring records the
assumption and names Phase 7 as the place to revisit it if
`max_concurrent_updates` is ever raised. This is stated so nobody reads the
absence of locks as an oversight.

**R3.5 — Answers are bounded and typed.** Each answer is a frozen
`Answer(question, answer)`; the question must equal the pending question that
was actually asked, so a dossier can never contain a question the user was not
asked. The plan carries 5–7 questions (R7.2), so the dossier is bounded by
construction and no separate cap is needed.

### R4 — The state machine, declared (`state.py`)

**R4.1 — `Phase` is a `StrEnum` with exactly three members:**
`AWAITING_PHOTO`, `AWAITING_ANSWER`, `SCRIPTED`.

**R4.2 — Legal transitions are one table, declared in the module:**

| From | To | Trigger |
|------|----|---------|
| `AWAITING_PHOTO` | `AWAITING_ANSWER` | The Bouncer returns `HUMAN` or `UNSURE` (R6). |
| `AWAITING_ANSWER` | `AWAITING_ANSWER` | A final answer is recorded (a self-transition that advances the plan's position). |
| `AWAITING_ANSWER` | `SCRIPTED` | The plan is exhausted and the script is delivered. |
| `SCRIPTED` | — | Terminal. Left only by `/start`, `/restart`, or a new photo (which purges first). |
| any | `AWAITING_PHOTO` | `/start` or `/restart`, and the Bouncer rejecting a photo. |

**R4.3 — An illegal transition raises `SessionTransitionError`**, naming the
from-phase, the to-phase and the triggering event. The hub catches it, logs it
and replies helpfully; it is never coerced into a legal transition and never
swallowed. An unknown or version-mismatched `chat_id` is *not* an illegal
transition — it is R3.2/R3.3, a fresh session.

**R4.4 — Each transition is one function.** `state` exposes
`begin_interview`, `record_answer`, `complete_interview` and `reset_session`,
each returning a new `SessionState`. A transition is therefore a single code path
that tests can call directly, rather than a set of field assignments scattered
across handlers.

### R5 — Temporary media (`media.py`)

**R5.1 — A per-`chat_id` directory.** `MediaStore` writes to
`<tempdir>/telegram_documentaries/<chat_id>/`, the photo as `photo.jpg`. The
`chat_id` is a `StrictInt` converted with `str()`, so a path segment can never be
attacker-shaped; a chat id cannot contain a separator.

**R5.2 — `save_photo` validates before it writes.** Empty or whitespace-free but
zero-length bytes are rejected as `InvalidInboundPhotoError`; a declared
`file_size` above `MAX_PHOTO_BYTES` is rejected **before** the download is
attempted, so an oversized photo costs nothing. A successful save returns a
`StoredPhoto` (`path`, `byte_size`, `mime_type`) that goes into the session
state, so the media store's opinion and the session's opinion are the same
typed value.

**R5.3 — `purge` deletes the whole per-`chat_id` directory** and accepts that the
directory may not exist. That case is logged at `debug`; a genuine deletion
failure is logged at `exception` and re-raised, because TECH.md lists temp-file
cleanup as work that fails **loud** (it is user-invisible) rather than degrades.
`purge` is called by both `/start` and `/restart`, and by the Bouncer's rejection
path (R6.4).

**R5.4 — The temp base is not configurable** in this phase. `tempfile.gettempdir()`
plus one fixed subdirectory. No `Settings` field, no env var, no cleanup
scheduler — a stale directory survives until its `chat_id` restarts (D9's
reasoning applied to media).

### R6 — Bouncer (`bouncer.py`)

**R6.1 — The verdict is a typed enum, not a parsed string.** `Verdict` is a
`StrEnum` of `HUMAN`, `NOT_HUMAN`, `UNSURE`, returned inside a frozen
`BouncerVerdict` model together with `subject` (what the model says it saw, 1–120
characters) and `line` (the cheeky sentence, up to 300 characters, for the
rejection). The enum is the load-bearing part of the roadmap criterion: the reply
is never matched against a string.

**R6.2 — The model writes the cheeky line; local code guarantees it is sane.**
`line` is used as the rejection message when it is non-blank and within its
length bound. Otherwise `BOUNCER_REJECTION_FALLBACK` is used. The user's one
unfiltered input — the photo — cannot put arbitrary text in front of them.

**R6.3 — `UNSURE` is accepted** (D6), logged at `warning` as `bouncer_unsure` with
`chat_id`, `update_id` and `subject`. `HUMAN` and `UNSURE` both continue to the
interview; only `NOT_HUMAN` rejects.

**R6.4 — Rejection resets the session.** On `NOT_HUMAN`: the session is reset to
`AWAITING_PHOTO` and the media directory is purged, in that order, so no
rejected photo and no half-built plan survives. The reply is the model's line and
then a request for another photo. This is ROADMAP Phase 2's
"Non-human → playful rejection + state reset".

**R6.5 — Degradation.** A `GeminiUnavailableError` leaves the session in
`AWAITING_PHOTO` with no state change and no media written, and replies that the
bot is having trouble. A `GeminiResponseError` is logged at `exception` and gets
the same reply. Neither is raised into the user's flow, and neither is silent.

### R7 — Interviewer (`interviewer.py`)

**R7.1 — One plan call, immediately after acceptance.** The Bouncer's subject
goes in as context, so the questions relate to the photo rather than being
generic.

**R7.2 — The plan is a schema, and the 5–7 rule is in the schema.**
`InterviewPlan` is frozen, with `questions`: a tuple of 5 to 7 `Question` models
(`text`: non-blank, at most 280 characters so it fits one Telegram message), and
`suggested_animal`: non-blank, at most 60 characters. The count constraint lives
on the field, so a reply with 3 or 9 questions fails R1.4.5 like any other
off-schema reply and is retried by the same corrective path — not by string
counting in the hub.

**R7.3 — Questions are asked one at a time, from local state.** Asking a question
is `pending_question = plan.questions[0]`, then `plan.questions[1]`, and so on. No
model call happens between answers. This is the mechanism that makes "one at a
time" a property of the code rather than a prompt's good manners.

**R7.4 — The dossier is the typed Q&A list.** There is no summarisation call and
no free-text dossier. The Scripter is given the plan and the accumulated
`Answer` models (R8.2) and judges the animal itself, which is the one place the
suggestion is actually used. One fewer call, and the Scripter sees the raw
material rather than a lossy summary of it.

**R7.5 — An empty answer does not advance the plan.** A text message that is
empty or whitespace only in `AWAITING_ANSWER` gets a short nudge to answer the
pending question and leaves the state untouched, so the question is never
silently skipped.

### R8 — Scripter (`scripter.py`)

**R8.1 — The narration is validated, not assumed.** `Script` is a frozen model
with `text` (non-blank, at most Telegram's 4096-character message limit) and
`word_count`. The word count is computed **locally** with `str.split()` over
whitespace — no model-reported count is trusted — and must be between 60 and 90
inclusive. Out of range raises `ScriptRejectedError`, carrying the actual count
and the bounds.

**R8.2 — The script is written from the typed dossier.** One call, given the
`InterviewPlan` (including `suggested_animal`) and the accumulated `Answer`
models, instructed for a single paragraph of 60–90 words in a British
natural-history documentary voice, with no preamble and no sign-off. Only what
the model returns is sent; the bot adds nothing around it.

**R8.3 — One corrective retry (D5).** On `ScriptRejectedError`, one further call
restates the required length and reports the count just received. A second
failure logs at `exception` with both counts and raises, and the hub replies
plainly, pointing at `/restart`. No third attempt, no padding, no truncation, no
locally-composed fallback narration — a fallback would be a lie dressed as the
product.

**R8.4 — What "degenerate" means here, stated honestly.** Validation covers
structural degeneracy only: empty, whitespace-only, wrong word count, longer
than Telegram's message limit. Semantic off-topicness is **not** detected,
because detecting it means either a regex over model prose (which TECH.md
discourages in favour of schemas) or a second model call to grade 80 words. The
corrective retry is the accepted mitigation. Recording this is more honest than
claiming roadmap Phase 5's "off-topic or degenerate output is detected" is fully
met; the structural half is met, the semantic half is deferred.

**R8.5 — The script is delivered as one text message** through
`context.bot.send_message`, the same seam as everything else (R9.3). No
`send_photo`, no file, no attachment.

### R9 — The hub and the adapter (`pipeline.py`, `bot.py`)

**R9.1 — `pipeline.py` owns the decision table, and nothing else does.** One
entry per (phase × payload):

| Phase | Payload | Action | The user sees |
|-------|---------|--------|---------------|
| any | `/start` | Purge state and media, begin a fresh `AWAITING_PHOTO` | A welcome and a request for a portrait photo |
| any | `/restart` | Purge state and media | Confirmation that the session is wiped, and a request for a portrait photo |
| `AWAITING_PHOTO` | text | None | The same request for a portrait photo |
| `AWAITING_PHOTO` | `UnsupportedAttachment` | None | A note naming the kind that was sent, and a request for a photo |
| `AWAITING_PHOTO` | `PhotoAttachment` | Fetch bytes, write media, `bouncer.judge` | Acceptance and the first question, or the cheeky rejection and a reset |
| `AWAITING_ANSWER` | text | `record_answer` | The next question, or the narration on the final answer |
| `AWAITING_ANSWER` | `PhotoAttachment` | Purge, then treat as `AWAITING_PHOTO` (D4) | Acceptance and the first question, or the cheeky rejection |
| `AWAITING_ANSWER` | `UnsupportedAttachment` | None | A note naming the kind, and an invitation to carry on with the question |
| `SCRIPTED` | text | None | A nudge to send a new photo, or `/restart` |
| `SCRIPTED` | `PhotoAttachment` | Purge, then treat as `AWAITING_PHOTO` (D4) | Acceptance and the first question, or the cheeky rejection |
| `SCRIPTED` | `UnsupportedAttachment` | None | A note naming the kind, and a nudge to send a new photo |

**R9.2 — The hub depends on a `PhotoFetcher` port, not on python-telegram-bot.**
`PhotoFetcher` is a `Protocol` with one `async` method taking a `PhotoAttachment`
and returning `bytes`. `bot.py` supplies the real implementation
(`await bot.get_file(...)` then `await File.download_as_bytearray()`); tests
supply a fake. This is what makes the entire decision table testable with zero
network access, and it is the reason the table can live in the domain layer
instead of inside a Telegram handler.

**R9.3 — `bot.py` is an adapter with three handlers.** `on_start`, `on_restart`,
and one `on_message` registered on `~filters.COMMAND` that covers text, photos
and unsupported media alike — so the branch lives in one decision table rather
than in three handlers. There is no per-phase handler, because there is no
per-phase behaviour. Each handler does exactly this: parse via
`InboundUpdate.from_telegram`, call `pipeline.handle(...)`, send the returned
text. **One send per update.** If a reply exceeds Telegram's 4096-character
limit the hub's text is already bounded (R8.1) and the adapter asserts nothing;
it sends what it is given.

**R9.4 — The old signature and constant are gone (D10).**
`build_application(token, pipeline)`. `GREETING` is deleted from `bot.py`; the
`/start` text is now a hub output that promises only what exists. The Phase 1
`InvalidInboundUpdateError` handling and the `on_error` handler are unchanged and
still required: a malformed update logs a warning and sends nothing, and an
exception escaping any handler is logged at `exception` level with `chat_id` and
`update_id`.

**R9.5 — Nothing is raised into the user's flow.** Every `GeminiError`,
`SessionTransitionError`, `SessionVersionError`, `InvalidInboundPhotoError` and
`MediaError` is caught at the hub boundary, logged, and answered with a short
human line. `InvalidInboundUpdateError` is caught in the adapter, as in Phase 1,
and answered with nothing.

### R10 — Tests never touch the network or a real credential

No test may require network access, a real bot token, or a real Gemini key. The
mock boundaries are exactly three, and they are all seams rather than internals:

1. **Telegram** — `FakeTelegramBot` records `send_message`; `make_update` builds
   real `Update` objects from raw payload dicts through `Update.de_json`, so the
   real parsing path is exercised. Both already exist in `tests/unit/conftest.py`
   and are extended, not replaced.
2. **Gemini** — a `FakeGeminiClient` implementing the R1.2 protocol, scripted per
   test to return a canned typed reply, a malformed reply, or a raised error. It
   records the requests it received, so a test can assert *what was sent*, not
   just what came back.
3. **The photo fetch** — a `FakePhotoFetcher` returning fixture bytes or raising.

Assertions are on observable outcomes: which messages were sent, in what order,
to which `chat_id`, what the session state became, and which log events fired.
No test asserts on a private attribute of a stage.

## Module layout

```
src/telegram_documentaries/
  __init__.py
  __main__.py        builds the pipeline and injects it (D10)
  config.py          Settings (unchanged); settings_error_fields moved to contracts
  observability.py   + static `extra=` on @logged (D8)
  contracts.py       InboundUpdate, PhotoAttachment, UnsupportedAttachment,
                     validation_error_fields, InvalidInboundUpdateError
  gemini.py          Stage, GeminiRequest, GeminiClient, GenAiGeminiClient,
                     MODEL_ID, GEMINI_TIMEOUT_MS, GeminiError,
                     GeminiUnavailableError, GeminiResponseError
  state.py           Phase, SessionState, Answer, StoredPhoto, SessionStore,
                     LEGAL_TRANSITIONS, begin_interview, record_answer,
                     complete_interview, reset_session, SessionTransitionError,
                     SessionVersionError
  media.py           MediaStore, StoredPhoto, MAX_PHOTO_BYTES, MediaError,
                     InvalidInboundPhotoError
  bouncer.py         Verdict, BouncerVerdict, judge(), BOUNCER_REJECTION_FALLBACK
  interviewer.py     InterviewPlan, Question, plan(), next_question()
  scripter.py        Script, write(), ScriptRejectedError
  pipeline.py        ConversationPipeline, PhotoFetcher, handle_start(),
                     handle_restart(), handle_message(), the decision table
  bot.py             build_application(token, pipeline), on_start, on_restart,
                     on_message, on_error, TelegramPhotoFetcher
tests/unit/
  conftest.py        + FakeGeminiClient, FakePhotoFetcher, session/media fixtures
  test_config.py
  test_observability.py   + the static `extra=` cases (D8)
  test_contracts.py       + attachments, largest-photo selection, the moved helper
  test_bot.py             + the rewired handlers; the Phase 1 cases still hold
  test_main.py            + the new wiring
  test_gemini.py
  test_state.py
  test_media.py
  test_bouncer.py
  test_interviewer.py
  test_scripter.py
  test_pipeline.py
scripts/test
scripts/hooks
```

**Dependency direction.** `bot` → `pipeline` → (`bouncer`, `interviewer`,
`scripter`, `state`, `media`, `gemini`) → (`contracts`, `observability`). No
stage imports another stage; `gemini` imports nothing from the project but
`observability`; nothing imports `bot` except `__main__`.

## Logging inventory for this phase

Phase 1's events all remain. New events:

| Event | Level | Fields | Emitted by |
|-------|-------|--------|-----------|
| `gemini_call_failed` | ERROR | `stage`, `model`, `chat_id`, `update_id`, `error_type`, `error_code` | `gemini` (R1.7) |
| `gemini_reply_rejected` | ERROR | `stage`, `reason`, `fields`, `chat_id`, `update_id` | `gemini` (R1.4) |
| `photo_saved` | INFO | `chat_id`, `update_id`, `byte_size` | `media` |
| `photo_rejected` | WARNING | `chat_id`, `update_id`, `reason` | `media` |
| `media_purged` | INFO | `chat_id` | `media` |
| `media_purge_skipped` | DEBUG | `chat_id`, `reason` | `media` (R5.3) |
| `media_purge_failed` | EXCEPTION | `chat_id` | `media` (R5.3) |
| `bouncer_judged` | INFO | `chat_id`, `update_id`, `verdict`, `subject` | `bouncer` |
| `bouncer_unsure` | WARNING | `chat_id`, `update_id`, `subject` | `bouncer` (D6) |
| `bouncer_rejected` | INFO | `chat_id`, `update_id`, `subject` | `bouncer` (R6.4) |
| `interview_planned` | INFO | `chat_id`, `update_id`, `question_count`, `suggested_animal` | `interviewer` |
| `question_asked` | INFO | `chat_id`, `update_id`, `position`, `total` | `pipeline` |
| `answer_recorded` | INFO | `chat_id`, `update_id`, `position`, `total` | `pipeline` |
| `script_written` | INFO | `chat_id`, `update_id`, `word_count` | `scripter` |
| `script_rejected` | EXCEPTION | `chat_id`, `update_id`, `word_count`, `min_words`, `max_words`, `attempt` | `scripter` (R8.3) |
| `script_delivered` | INFO | `chat_id`, `update_id`, `word_count` | `pipeline` |
| `session_reset` | INFO | `chat_id`, `reason` | `pipeline` |
| `session_version_discarded` | WARNING | `chat_id`, `found_version`, `expected_version` | `state` (R3.3) |
| `transition_rejected` | WARNING | `chat_id`, `update_id`, `from_phase`, `to_phase`, `event` | `pipeline` (R4.3) |
| `unsupported_media` | INFO | `chat_id`, `update_id`, `media_kind` | `pipeline` (R9.1) |
| `out_of_order_input` | INFO | `chat_id`, `update_id`, `phase`, `payload` | `pipeline` (R9.1) |

Every Gemini call is additionally wrapped in `@logged(...)` with
`extra={"stage": ..., "model": MODEL_ID}` (D8), so the call itself carries
`event`, `stage`, `model`, `chat_id`, `update_id` and `duration_ms` whether it
succeeds or fails.

**No record may contain**: the bot token, the Gemini API key, a raw Gemini
response body, a raw model reply, or a `ValidationError` rendered with
`str(exc)`. The Phase 1 test
`test_no_log_record_anywhere_contains_the_bot_token` is re-run across every new
path, and a sibling test sweeps for the API key.

## Divergences from project guidance, and why

1. **`google.genai` directly, not Google ADK agents (D1).** TECH.md names
   `google-adk` as the agent framework. This phase uses `google.genai`, which
   `google-adk` depends on and pins, and keeps `google-adk` installed. The
   divergence is deliberate and recorded here and in `TECH.md`: the Runner and
   its session service would duplicate R3's state driver, and mocking an ADK
   agent means faking inside `BaseLlm` rather than at a seam. The five stages
   remain discrete modules with a single job each, which is the architectural
   requirement TECH.md actually states. ADK may be adopted later, if the
   Converter or a retry harness makes it earn its keep. This is the one
   divergence, and it is visible in the code.
2. **`tests/unit/` only, as in Phase 1.** No `integration` or `component` tier,
   because TECH.md forbids tests that need network access or real credentials —
   which is what those tiers would mean here. The three fakes in R10 are the
   closest honest equivalent.
3. **`mypy` is not run over `tests/`,** unchanged from Phase 1's D-file: the
   fakes are intentionally loose doubles.
4. **Semantic off-topic detection is not built (R8.4).** Roadmap Phase 5's
   criterion is half met, by design, and the gap is written down rather than
   papered over.
5. **No session TTL or pruning,** and no `SCRIPTING` phase (D4).

## Required documentation changes in this phase

Per TECH.md's README policy, documentation ships in the same change as behaviour.

1. **`README.md` — "Use" section.** It currently says `/start` is the entire
   feature and that a photo is ignored. That is now false. It must describe the
   real flow: `/start`, a portrait photo, a cheeky rejection if the photo is not
   a person, 5–7 questions one at a time, the narration arriving **as text**,
   and `/restart`. It must state plainly that the hybrid image and the voice
   note are later phases, so the README does not repeat Phase 1's mistake of
   overclaiming.
2. **`README.md` — "Status".** Phases 2, 3 and text-5 shipped; 4 and 6 have not.
3. **`ROADMAP.md`.** Phases 2 and 3 marked delivered with details, Phase 5
   marked delivered **for the text half only** with the Converter and Narrator
   explicitly outstanding, and the Status table updated.
4. **`TECH.md`.** Record the `google.genai` divergence (D1) and the
   `PhotoFetcher` port, so the next phase does not rediscover them.
5. **`MISSION.md`.** Unchanged. Product scope and the end-to-end experience are
   unchanged; this phase delivers a subset, and the Roadmap is where that is
   recorded.
