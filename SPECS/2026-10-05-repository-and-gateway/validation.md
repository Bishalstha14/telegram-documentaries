# Validation — Phase 1: Repository & gateway

Merge only when every item below passes. The verifier works these item by item,
against the **actual code and behaviour**, not against intent, and updates the
constitution and README with what was really shipped.

## A. Roadmap acceptance criteria

| # | Criterion | How to verify |
|---|-----------|---------------|
| A1 | `pyproject.toml` pins dependencies; `.venv` reproducible from it | Every runtime and dev dep is `==`-pinned. `pip install -e ".[dev]"` succeeds in a fresh venv; `pip list` matches the pins. |
| A2 | `.env.example` documents both secrets; `.env` is gitignored | `git check-ignore .env` succeeds. `git ls-files .env` is empty. `.env.example` is tracked and unchanged. |
| A3 | Settings load via `pydantic-settings` and fail loudly on a missing key | Run the bot with `.env` absent → exit `1`, one message naming the missing field. Run with `TELEGRAM_BOT_TOKEN=` → also exit `1`. No default, no partial start. |
| A4 | The bot long-polls, receives `/start`, and replies | **Manual smoke test, section E.** Echo reply is acceptable. |
| A5 | `scripts/test` and `scripts/hooks` exist and pass on an empty suite | `ls -l scripts/` shows mode `755` on both. `scripts/test` exits `0`. With nothing staged, `scripts/hooks` exits `0`. |
| A6 | `README.md` covers setup, run, and checks | Setup and run were already correct. The "Use" section now states only the `/start` echo is live; the "Checks" wording matches real hooks scoping. |

## B. Requirement coverage

- [ ] **R1** — `pyproject.toml` complete: pins, `[dev]` extra, pytest/ruff/mypy config.
- [ ] **R2.1** — both secrets are `SecretStr`; `repr`/`model_dump` render masked.
- [ ] **R2.2** — a field validator rejects empty **and** whitespace-only secrets.
- [ ] **R2.3** — **the validation error is never printed verbatim.** Confirmed
      against the live behaviour, not just the test: a missing key must surface
      field names only, with no sibling secret value anywhere in stdout, stderr
      or the log.
- [ ] **R2.4** — missing/blank key → one fatal line naming the field(s), exit `1`.
- [ ] **R3** — `InboundUpdate` is frozen, `extra="ignore"`, `chat_id` typed `int`.
      `from_telegram` returns `None` for a message-less update and raises
      `InvalidInboundUpdateError` for a missing chat or non-integer `chat.id`.
- [ ] **R4.1** — the reply goes through `context.bot.send_message` with a typed
      integer `chat_id`, **not** `update.effective_message.reply_text`.
- [ ] **R4.2** — malformed inbound → warning logged, nothing sent, no raise.
- [ ] **R4.3** — an `error` handler logs escaping exceptions at `exception` level.
- [ ] **R4.4** — no token or settings value appears in any log record.
- [ ] **R5** — `@logged` emits `event`, `chat_id`, `update_id`, `duration_ms`;
      async-aware; logs and re-raises.
- [ ] **R6** — `scripts/test` runs pytest + ruff + mypy; `scripts/hooks` scopes
      ruff and pytest to staged files and always runs mypy over `src/`.
- [ ] **R7** — no test requires network access or a real API key.

## C. Constitution invariants

- [ ] **No secrets in logs.** Not in stdout, not in stderr, not in a log record —
      including on the failure paths (R2.3, R4.4).
- [ ] **Typed values at the boundary.** No raw Telegram payload reaches business
      logic. No raw dicts between modules (TECH.md).
- [ ] **No swallowed exceptions.** No `except: pass`, no bare `except`, no
      un-logged fallback (MISSION.md #4, TECH.md). Verify by inspection.
- [ ] **Nothing anticipates Phases 2–7.** No Gemini or ADK import, no session
      state, no `/restart` handler, no media handling, no audio, no unused
      config field or extension point.
- [ ] **`.env` stays ignored** and no secret is committed.
- [ ] **`scripts/test` passes on a clean tree.**

## D. Evidence to produce

1. `scripts/test` — full output, exit `0`.
2. `scripts/hooks` — with and without staged files, exit `0`.
3. The A3 failure run, showing the exact user-facing message with the secret
   redacted or replaced.
4. A `grep` proving zero `except: pass` / bare `except` in `src/`.
5. A `git status` showing only intended files.

## E. Manual smoke test (cannot be automated here)

This is the one criterion the suite cannot prove, and A4 depends on it.

1. `cp .env.example .env` and fill in a **real** `TELEGRAM_BOT_TOKEN`.
2. Run `python -m telegram_documentaries`.
3. Send `/start` to the bot in Telegram.
4. Confirm the bot replies with the greeting.
5. Confirm the log shows `start_command_received` and `start_reply_sent` with
   `chat_id` and `update_id`, and that **no token appears in the log**.
6. Send a plain text message. Confirm the bot does not reply and does not crash
   (expected in Phase 1 — out-of-order input handling is Phase 2).
7. `Ctrl-C` to stop.

If A4 cannot be run, the phase is **not** complete: "the bot long-polls,
receives `/start`, and replies" is an end-to-end transport claim.

## F. Spec-versus-reality reconciliation

- [x] Every requirement above is implemented, or the deviation is recorded here
      with its justification.
- [x] `ROADMAP.md` Phase 1 marked complete with delivery details; the Status
      table updated.
- [x] `TECH.md` updated if the implementation changed any stated technical
      requirement or policy.
- [x] `MISSION.md` updated only if product scope or behaviour changed.
- [x] `README.md` synchronised with shipped behaviour.
- [x] Deviations recorded in this spec's files.

## G. Verification outcome — 2026-10-05

**Verdict: Phase 1 complete.** Sections A–D and F pass; section E was completed
against the live bot.

| Section | Result |
|---------|--------|
| A — Roadmap acceptance criteria | **Pass.** All six criteria met. |
| B — Requirement coverage R1–R7 | **Pass.** All implemented. |
| C — Constitution invariants | **Pass.** No `except: pass`, no bare `except`, no raw payload past the boundary, nothing anticipating Phases 2–7. |
| D — Evidence | **Collected** below. |
| E — Manual smoke test | **Completed** against `@BishalTech_bot`. |
| F — Doc reconciliation | **Done.** `ROADMAP.md`, `README.md` and this file updated. |

**Evidence.** `scripts/test` → 107 passed, ruff clean, mypy strict clean, exit 0.
`scripts/hooks` → exit 0 both with and without staged files, mode 755.
The A3 failure run, with a sentinel secret substituted for the real one, exits 1
and reads:

```
Invalid configuration: missing or blank required setting(s): gemini_api_key.
Fill them in your .env file - see .env.example for the exact names.
| event=settings_invalid fields=gemini_api_key
```

The sentinel value appears nowhere in stdout or stderr — the R2.3 guard holds.
`grep` finds zero `except: pass` and zero bare `except` in `src/`.

**Section E, as run.** `python -m telegram_documentaries` against the real token
logged `settings_loaded` and `gateway_started`, then received `/start`
(`update_id=75407220`, `chat_id=8767055318`) and replied in 283.61 ms, logging
`start_command_received` and `start_reply_sent`. No token in any log. A plain
text message is ignored and does not crash, as Phase 1 requires — out-of-order
input handling is Phase 2.

### Deviations

1. **`InvalidInboundUpdate` → `InvalidInboundUpdateError`.** Renamed to satisfy
   ruff `N818`. `requirements.md` R3/R4.2 and the module layout were updated to
   the new name; `N818` remains enabled as the standing regression guard.
2. **A bug the spec did not anticipate.** `from_telegram` rejected a missing
   `chat` and a non-integer `chat.id`, but a chat object with **no `id`
   attribute at all** raised `AttributeError` from a bare `chat.id` access —
   which `on_start` does not catch, so it escaped the typed contract into the
   generic error handler. Fixed by reading `getattr(chat, "id", None)`, so an
   absent id falls into the existing `isinstance` check and is rejected as
   `InvalidInboundUpdateError` with no new branch. Covered by
   `test_from_telegram_raises_when_the_chat_has_no_id`, which drives a stub
   because python-telegram-bot's `Chat.__init__` requires `id` and would reject
   the payload before this contract saw it.
3. **Two ERROR records on a failed send.** The `@logged` decorator wraps the send,
   so a failure emits `start_reply_sent` from its re-raise path and then
   `handler_failed`. Both are logged and neither leaks a secret, so this satisfies
   the error policy; it is noisy but not incorrect. Accepted for now.
4. **`GatewayApplication` is public** in `bot.py`. Needed so `__main__`'s
   `post_init` hook type-checks under strict mypy. Keeping it private would force
   `Any` into `__main__`.
5. **`build_application` does not validate its token.** python-telegram-bot 22.8
   does not validate at build time; a blank token is impossible at the real entry
   point because `config.Settings` rejects it. Accepted as fail-fast at the
   boundary rather than defence in depth.
6. **`tests/unit/` only.** No `integration`/`component` tiers this phase, since
   TECH.md forbids tests needing network or real credentials. Can be introduced
   later if a genuinely useful real-dependency test appears.
7. **`mypy` is not run over `tests/`.** The fake Telegram context is an
   intentionally loose double that would need casts under strict mode.