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
      `InvalidInboundUpdate` for a missing chat or non-integer `chat.id`.
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

- [ ] Every requirement above is implemented, or the deviation is recorded here
      with its justification.
- [ ] `ROADMAP.md` Phase 1 marked complete with delivery details; the Status
      table updated.
- [ ] `TECH.md` updated if the implementation changed any stated technical
      requirement or policy.
- [ ] `MISSION.md` updated only if product scope or behaviour changed.
- [ ] `README.md` synchronised with shipped behaviour.
- [ ] Deviations recorded in this spec's files.