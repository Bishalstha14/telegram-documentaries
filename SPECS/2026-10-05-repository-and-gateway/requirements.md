# Requirements — Phase 1: Repository & gateway

## Summary

Stand up the project skeleton and a provably runnable Telegram long-polling
gateway, before any AI is involved. The bot accepts `/start` and replies. That
is the whole of it.

This phase exists to prove the transport, the configuration contract and the
quality gates work end to end, so that Phases 2–7 can be built on a foundation
that is already green.

Implements ROADMAP.md "Phase 1 — Repository & gateway".

## Context

Greenfield repo. The constitution (`SPECS/MISSION.md`, `SPECS/TECH.md`) and
`README.md` are committed. `.env.example` and `.gitignore` are committed. The
project-local `.venv` exists with the stack installed. No application code has
been written.

## Decisions

Confirmed with the user before this spec was written. Not open questions.

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **stdlib `logging`**, not `structlog` | TECH.md allows either. `structlog` is not installed and Phase 1 does not need it. |
| D2 | **`Settings` requires both secrets** | Mirrors the already-committed `.env.example` and README, so configuration has one source of truth and cannot drift. Adding `GEMINI_API_KEY` in Phase 2 would be churn for no benefit. |
| D3 | **Typed inbound contract introduced now** | MISSION.md non-negotiable #3 forbids treating untrusted input as trusted. Deferring would mean Phase 1 establishes an unvalidated handler pattern that Phase 2 must undo. |
| D4 | **`scripts/hooks` scopes ruff + pytest to staged files; mypy always runs over all of `src/`** | Type-checking a subset of files cannot prove the tree type-checks. A partially-typed tree must not pass the pre-commit gate. |
| D5 | **Bad configuration → one clear message naming the missing field(s), exit 1** | Still fails loud: no default, no fallback, non-zero exit, original error chained. |
| D6 | **`==` pins on direct dependencies only; no lockfile** | Satisfies TECH.md "the environment is reproducible from it". Transitives resolve normally. A lockfile is speculative maintenance for seven phases (YAGNI). |

## Scope

### In scope

1. `pyproject.toml` — pinned dependencies, `src/` package layout, tool config.
2. `src/telegram_documentaries/` package with `__main__.py` entry point, so
   `python -m telegram_documentaries` runs.
3. `Settings` loaded via `pydantic-settings` from `.env`, **failing loudly** on
   a missing or blank required key.
4. A long-polling bot that receives `/start` and replies with a hardcoded
   greeting. No model, no AI, no network beyond Telegram.
5. `scripts/test` and `scripts/hooks` — executable shell scripts.
6. Tests covering the above, with **no network access and no real API keys**.
7. README corrections so the documentation states what is actually live.

### Explicitly out of scope (YAGNI)

- Any Gemini or Google ADK agent code.
- Session state machinery, `chat_id` state, the state machine.
- The Bouncer vision gate, the Converter, the Scripter, the Narrator, audio.
- `/restart`, and any state purge or temp-media deletion.
- Graceful per-stage degradation and retry/rate-limit handling (Phase 7).
- A catch-all handler for non-`/start` messages. Out-of-order input handling is
  Phase 2. In this phase such a message is silently ignored by Telegram.
- `tests/integration/` and `tests/component/`. See "Divergences" below.
- A dependency lockfile.
- `[project.scripts]` console entry point. The README documents
  `python -m telegram_documentaries`; a second entry point is unused surface.

## Requirements

### R1 — Project skeleton

`pyproject.toml` declares the package as `telegram-documentaries`,
`requires-python = ">=3.11"` (TECH.md), a setuptools `src/` layout, and:

- **Runtime dependencies, pinned exactly:**
  `google-adk==2.11.0`, `python-telegram-bot==22.8`, `pydantic==2.13.5`,
  `pydantic-settings==2.15.0`.
- **`[project.optional-dependencies].dev`:** `pytest==9.1.1`,
  `pytest-asyncio==1.4.0`, `ruff==0.16.10`, `mypy==2.4.0`. The README already
  instructs `pip install -e ".[dev]"`, so this extra is a contract, not a
  convenience.
- **`[tool.pytest.ini_options]`:** `asyncio_mode = "auto"`,
  `testpaths = ["tests"]`.
- **`[tool.ruff]`:** configured, non-default rule selection.
- **`[tool.mypy]`:** `strict = true`, `python_version = "3.11"`,
  `mypy_path = "src"`, `files = ["src"]`, pydantic plugin, `warn_unreachable`.

### R2 — Settings fail loudly (and never leak the secret)

`config.Settings` reads `.env` via `SettingsConfigDict`, with two required
fields, `telegram_bot_token` and `gemini_api_key`.

**R2.1 — Values are `SecretStr`.** Both secrets are typed `SecretStr`, so a
plain `repr`, `model_dump` or accidental f-string renders `**********`.

**R2.2 — Blank is treated as missing.** A field validator rejects empty or
whitespace-only values. This is a hard requirement, not a nicety.

> Verified in the project venv: `Field(min_length=1)` does **not** enforce on
> `SecretStr`. `TELEGRAM_BOT_TOKEN="   "` loads successfully, and the failure
> then surfaces deep inside python-telegram-bot as `InvalidToken: You must pass
> the token you received from ...`. A blank value in `.env` is the most likely
> real-world mistake; it must be caught at load.

**R2.3 — The validation error must never be printed verbatim.** Verified in the
project venv: when one key is missing, `str(ValidationError)` renders the whole
input mapping, including the sibling key's value:

```
1 validation error for Settings
gemini_api_key
  Field required [type=missing,
 input_value={'telegram_bot_token': 'SUPERSECRET'}, input_type=dict]
```

Printing that would violate MISSION.md non-negotiable #2 and TECH.md's "Never
log secrets or raw token values". The implementation must extract **field names
only** (from `ValidationError.errors()`) and surface those.

**R2.4 — Failure behaviour.** A missing or blank key produces a single fatal
log line naming the offending field(s) and pointing at `.env.example`, then exit
code `1`. There is no default, no fallback, and no partial start. The original
error is chained for a developer traceback but is not the user-facing message.

### R3 — Typed contract at the Telegram boundary

`contracts.InboundUpdate` is a Pydantic model, `frozen=True`,
`extra="ignore"`, with fields:

| Field | Type | Meaning |
|-------|------|---------|
| `update_id` | `int` | Telegram update identifier; also the log correlation key. |
| `chat_id` | `int` | Telegram chat identifier. **Typed as `int`, never `str`.** |
| `text` | `str \| None` | Message text, if any. |

`InboundUpdate.from_telegram(update: Update) -> InboundUpdate | None` is the only
way a Telegram update enters the codebase.

- Returns `None` when the update carries no message (e.g. an edited message, a
  poll update). Logged at debug level; the caller returns early.
- Raises `InvalidInboundUpdateError` when the message has no chat, or `chat.id` is
  absent or not an integer. Malformed payloads are **rejected explicitly and
  never coerced** into a well-formed update (TECH.md: "A payload arriving at an
  illegal state is rejected explicitly — never coerced").

  The exception carries the `Error` suffix because that is the project-wide
  convention for exception types (PEP 8 naming; enforced by ruff N818).

`chat_id` is typed as `int` deliberately. A string-vs-int mismatch on `chat_id`
is the failure mode that silently breaks session lookups once Phase 3 lands;
fixing the type now costs nothing.

### R4 — The `/start` gateway

`bot.build_application(token: str)` returns a configured
`telegram.ext.Application` in long-polling mode with a single registered
handler for `/start`.

**R4.1 — Reply goes through `context.bot.send_message(chat_id=<typed int>, ...)`,
not `update.effective_message.reply_text(...)`.** Two reasons, both required:

1. It is what MISSION.md #3 and TECH.md's "stage boundaries pass typed models,
   never raw dicts" actually demand at the Telegram edge — the handler works
   with the typed `InboundUpdate`, not with a library object.
2. It puts the mock boundary at the network seam, so the handler is testable
   with **zero network access** (TECH.md: "Never make tests require network
   access or real API keys").

**R4.2 — Failure behaviour for malformed input.** `on_start` catches
`InvalidInboundUpdateError`, logs a warning carrying `update_id`, and returns
without
replying. Loud in logs, silent to the user, never a crash and never a raise.

**R4.3 — Failure behaviour for a Telegram send error.** An `error` handler is
registered. An exception escaping any handler is logged at `exception` level
with `update_id` and `chat_id` context. Per-stage user-facing degradation is
Phase 7 and is explicitly not attempted here.

**R4.4 — No secret is logged.** No token, and no field value from `Settings`,
may appear in any log record. The bot username is safe and may be logged.

### R5 — Structured logging via a decorator

`observability` provides `configure_logging()`, `get_logger()` and an
async-aware `@logged` decorator. Per the constitution, the decorator is
preferred over scattering `log.info(...)` through business logic.

Every decorated call emits, at minimum: `event`, `chat_id`, `update_id`,
`duration_ms`. The decorator must not swallow or mask an exception — it logs and
re-raises.

### R6 — Dev scripts

Both are executable shell scripts (`chmod 755`), `set -euo pipefail`, resolving
the repo root so they work from any working directory, and preferring the
project-local `.venv` interpreter.

- **`scripts/test`** — the ground truth. Runs `pytest`, then `ruff check`, then
  `mypy`. Any non-zero exit fails the script.
- **`scripts/hooks`** — pre-commit. Resolves staged Python files via
  `git diff --cached --name-only --diff-filter=ACM -- '*.py'`. If none are
  staged it exits cleanly with a short message. Otherwise runs `ruff check` and
  `pytest` against that file list, and **always** runs `mypy` over all of
  `src/` per D4.

### R7 — Tests never touch the network

No test may require network access, a real bot token, or a real Gemini key.
The mock boundary is the Telegram `Bot` object. Telegram updates are constructed
from raw payload dicts through `Update.de_json(...)` with a mocked bot, so the
tests exercise the real parsing path rather than hand-built objects.

## Module layout

```
src/telegram_documentaries/
  __init__.py
  __main__.py        entry point: main() -> int
  config.py          Settings
  observability.py   configure_logging(), get_logger(), @logged
  contracts.py       InboundUpdate, from_telegram(), InvalidInboundUpdateError
  bot.py             build_application(), on_start(), on_error()
tests/unit/
  conftest.py        fake Telegram context fixture
  test_config.py
  test_observability.py
  test_contracts.py
  test_bot.py
scripts/test
scripts/hooks
```

**Naming rationale (deliberate divergence from the original proposal).** The
logging module is `observability.py`, **not** `logging.py`. Python 3 absolute
imports mean `logging.py` would still work, but it shadows a stdlib module name
for every future reader and importer inside the package. The one-word name avoids
that footgun permanently at zero cost.

## Divergences from project guidance, and why

1. **`tests/unit/` only, for this phase.** The `write-tests` skill describes a
   three-tier layout (`unit` / `integration` / `component`). Phase 1 has no
   dependency worth exercising for real, and TECH.md forbids tests that require
   network access or real API keys — which is what a real `integration` tier
   would mean here. Creating the directories empty would imply a convention the
   phase does not honour. The three-tier layout can be introduced later if a
   genuinely useful real-dependency test appears.
2. **`mypy` is not run over `tests/`.** Type-checking tests is not required by
   TECH.md, and the fake Telegram context is an intentionally loose test double
   that would need casts to satisfy strict mode.
3. **`observability.py` instead of `logging.py`** — see above.
4. **No `[project.scripts]`** — see Scope.

## README corrections required by this phase

Per TECH.md's README policy, documentation ships in the same change as
behaviour. Two corrections are needed:

1. **The "Use" section currently overclaims.** It instructs the user to send a
   portrait photo and promises that `/restart` wipes the session and temporary
   media. Neither exists in Phase 1. The section must state plainly that the
   only live behaviour is the `/start` echo, and that the pipeline and
   `/restart` arrive in later phases.
2. **The "Checks" wording must match real hooks scoping.** "Same checks, scoped
   to staged files" is inaccurate under D4 — `mypy` always runs over all of
   `src/`. The wording must be corrected.

Everything else in the README — setup, `.env`, `pip install -e ".[dev]"`,
`python -m telegram_documentaries`, and the Phase 1 status line — is already
correct. `pyproject.toml` must honour the `[dev]` extra the README assumes.

## Logging inventory for this phase

| Event | Level | Fields |
|-------|-------|--------|
| `settings_loaded` | INFO | `bot_username` — never a token |
| `settings_invalid` | CRITICAL | `fields` (names only) |
| `gateway_started` | INFO | `bot_username` |
| `start_command_received` | INFO | `chat_id`, `update_id` |
| `start_reply_sent` | INFO | `chat_id`, `update_id`, `duration_ms` |
| `inbound_update_invalid` | WARNING | `update_id`, `reason` |
| `inbound_update_ignored` | DEBUG | `update_id` |
| `handler_failed` | EXCEPTION | `chat_id`, `update_id` |