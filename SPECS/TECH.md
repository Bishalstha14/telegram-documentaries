# TECH

The technical contract. Every agent defers to this file.

## Stack

- **Language:** Python 3.11+
- **Transport:** Telegram Bot API via **long polling** (`python-telegram-bot` 22.x).
  No webhooks, no public URL, no inbound ports.
- **Agent framework:** Google ADK (`google-adk` 2.x) — hub-and-spoke.
- **Models:**
  - Bouncer — Gemini 3.1 Flash Lite (vision)
  - Interviewer — Gemini 3.1 Flash Lite
  - Converter — Gemini 3.1 Flash Image (multimodal in/out)
  - Scripter — Gemini 3.1 Flash Lite
  - Narrator — `gemini-3.1-flash-tts-preview`
- **Validation:** Pydantic v2
- **Config:** `pydantic-settings`, reading `.env`
- **Tooling:** pytest, ruff, mypy (strict)
- **Environment:** project-local `.venv`

## Architecture

ADK hub-and-spoke. The **Interviewer** is the hub: it orchestrates and decides
what runs next.

- Each pipeline stage is a **discrete agent/module** with a single job.
- The conversation is an **explicit state machine**. Legal transitions are
  declared, not implied. A payload arriving at an illegal state is rejected
  explicitly — never coerced into the current state.
- Stage boundaries pass **typed models**, never raw dicts.

### Two recorded divergences

Both are deliberate. Neither should be "corrected" back without a new spec.

**D1 — the Gemini calls go through `google.genai` directly, not ADK's `Runner`.**
The project is written with ADK in its description, but every stage builds a
typed `GeminiRequest` and calls `gemini.py`, which is the single importer of
`google.genai`. `Runner` would mean agent objects, `LlmAgent` configuration and
an async run loop around what is, at each stage, one structured call and one
validated reply. The boundary contract — typed request in, validated model or
one of two typed errors out — is the part that matters, and it is preserved
exactly. If ADK is ever adopted, this is the seam to move, and the stages do
not change.

**D2 — dispatch lives in `pipeline.py`; the Interviewer is the hub, not the
dispatch table.** The Interviewer decides *what the conversation does next*
(ask, record, script), but "which `handle_*` method answers this update" is a
separate concern and lives in one decision table in `pipeline.py`. Keeping them
apart is what lets the decision table be tested with no Telegram, no Gemini and
no filesystem: it takes an `InboundUpdate` and returns a string.
`state.py` holds the phases and transitions as a plain class with no I/O, and
imports the `Interviewer`'s types only under `TYPE_CHECKING` to keep the
runtime dependency cycle one-way.

### The photo port

Photo bytes are fetched through one seam:

```python
class PhotoFetcher(Protocol):
    async def fetch(self, attachment: PhotoAttachment) -> bytes: ...
```

Production uses `TelegramPhotoFetcher(Bot)` in `bot.py` — the only code in the
project that talks to Telegram's file API. Tests pass a fake. The hub sees an
`InboundUpdate` with a `PhotoAttachment` and gets bytes; *how* they travel is
not its business, which is what makes its decision table testable offline.

Note the port is typed against `Bot`, not `context.bot`: the hub needs it at
construction time, which is before any handler — and therefore before any
context — exists. `Bot` is a thin stateless HTTP client, so `__main__` builds a
second one over the same token rather than reaching into the `Application`.

## Contracts at boundaries

At every edge — parsing Telegram updates, parsing Gemini responses, parsing TTS
output — convert to a **typed Pydantic model** before the value crosses a module
boundary.

- Never pass raw dicts or unvalidated payloads between modules.
- Treat **all external input as untrusted and arbitrary**: a malformed Telegram
  update, a truncated model reply, an empty TTS body. Each must be handled
  explicitly, not by hope.
- Prefer **schemas over regexes**. Parse structure with a model, not with string
  matching.
- Malformed payloads are **rejected explicitly, never coerced**. A missing
  attribute must surface through the module's own typed error, not as an
  `AttributeError` or a pydantic error that callers do not catch. This includes
  absent attributes, not just wrongly-typed ones.
- Exception names carry an `Error` suffix (`InvalidInboundUpdateError`), enforced
  by ruff `N818`.

## Secrets

Two guards, both verified necessary in the venv rather than assumed:

- **A blank secret must be rejected as missing.** `Field(min_length=1)` does not
  enforce on `SecretStr`, so a whitespace-only token loads successfully and then
  fails as `InvalidToken` deep inside the Telegram library. An explicit non-blank
  validator is required.
- **A validation error must never be printed verbatim.** Pydantic renders the
  sibling field's value inside `input_value`, so printing `str(exc)` leaks the
  other secret. Extract field **names** only, via `ValidationError.errors()`.

Module naming: the logging module is `observability.py`, **not** `logging.py` — it
shadows a stdlib module name inside the package.

## Logging & error policy

Comprehensive **structured logging** throughout. Use `structlog` or
`logging` with key-value context (`chat_id`, `stage`, `update_id`).

- Prefer **decorators** over scattering `log.info(...)` through business logic.
- **Fail loudly and log** for non-critical, user-invisible work (image
  optimisation, temp-file cleanup, analytics).
- On a **validated user's conversation path**, catch errors and **degrade
  gracefully** so the conversation continues: log loudly, reply helpfully, never
  raise into the user's flow.
- Forbidden: bare `except: pass`, swallowed exceptions, un-logged fallbacks.
- Never log secrets or raw token values.

## Session state

- **Versioned, per-`chat_id` schema.** Include a `version` field so the shape can
  evolve without corrupting live sessions.
- **One shared state driver** owns all reads and writes. No module reaches into
  another's state.
- **Reset semantics:** `/start` and `/restart` purge that `chat_id`'s state and
  its temporary media, without restarting the process. `/restart` must also work
  mid-conversation.
- Temporary media lives in a per-session directory that the reset deletes.

## Testing

**Red/Green TDD. Tests are written before code.** No production code lands
without a failing test that justifies it.

- `scripts/test` — the ground truth. Runs pytest, ruff, and mypy.
- `scripts/hooks` — the same checks scoped to staged files, run before commit.
- Test **behaviour, not implementation**. Assert on observable outcomes.
- Cover the happy path *and* edge cases: empty input, malformed payloads,
  upstream API failure, out-of-order stage arrival, session isolation.
- Never make tests require network access or real API keys. Mock at the
  boundary.

## Repo hygiene

- `.env` is in `.gitignore` and stays there. `.env.example` is committed.
- Dependencies are pinned in `pyproject.toml`; the environment is reproducible
  from it.
- Generated media (`.ogg`, `.mp3`), session directories, and caches are
  gitignored.

## README policy

The README documents developer-facing behaviour and is kept in sync with the
code. It must state: what the bot does, how to set up `.env`, how to run it,
how to run the checks, and the current roadmap phase.

When a feature changes behaviour, the README and the affected constitution or
roadmap file are updated in the same change — not deferred.