# Plan — Phase 1: Repository & gateway

Red/Green TDD throughout (TECH.md). Every task group writes its tests first,
runs them, and **watches them fail for the right reason** before any production
code is written. Checks are run through `scripts/test` and `scripts/hooks` only.

## Task group 1 — Skeleton and green gates

1. Create `scripts/test` (pytest → ruff → mypy, `set -euo pipefail`, `chmod 755`).
2. Create `pyproject.toml`: setuptools `src/` layout, `requires-python >=3.11`,
   `==` pinned runtime deps, `[dev]` extra, pytest/ruff/mypy config
   (`asyncio_mode = "auto"`, mypy `strict`, `files = ["src"]`).
3. Create the empty `src/telegram_documentaries/__init__.py`.
4. Create `tests/unit/` with one placeholder test so pytest has a collection root.
5. Run `scripts/test`. **Expected: green on a near-empty suite.** This is
   ROADMAP acceptance criterion "scripts/test and scripts/hooks exist and pass
   on an empty suite" — prove it before writing anything real.

## Task group 2 — `Settings` fails loudly and never leaks  (RED → GREEN)

RED — write these first, run, confirm each fails because the behaviour does not
exist yet:

- `test_settings_loads_both_secrets_from_the_environment`
- `test_settings_missing_key_raises_a_validation_error`
- `test_settings_blank_key_is_rejected_as_missing` (empty **and** whitespace)
- `test_settings_repr_and_dump_never_expose_secret_values`
- `test_settings_validation_error_text_never_contains_the_secret_value` — the
  R2.3 guard. Build a config with one real and one missing key and assert the
  secret never appears in `str(exc)`. **This test is the mechanism-level
  prevention for the R2 leak and must exist.**

GREEN — implement `config.Settings`: both fields `SecretStr` (R2.1), the
non-blank validator (R2.2), `extra="ignore"`.

## Task group 3 — `observability`  (RED → GREEN)

RED:

- `test_logged_emits_chat_id_update_id_and_duration_ms`
- `test_logged_awaits_the_wrapped_coroutine`
- `test_logged_reraises_after_logging_an_exception`
- `test_configure_logging_is_idempotent`

GREEN — implement `configure_logging()`, `get_logger()`, `@logged`.

## Task group 4 — Typed inbound contract  (RED → GREEN)

RED:

- `test_from_telegram_parses_a_well_formed_start_command`
- `test_from_telegram_returns_none_when_the_update_has_no_message`
- `test_from_telegram_raises_when_the_message_has_no_chat`
- `test_from_telegram_raises_when_chat_id_is_not_an_integer` — must **reject**,
  never coerce
- `test_inbound_update_is_frozen`
- `test_from_telegram_ignores_unknown_extra_fields`

GREEN — implement `contracts.py`.

## Task group 5 — Bot wiring and the `/start` reply  (RED → GREEN)

RED — construct updates from raw payload dicts via `Update.de_json(...)` with a
mocked bot, per R7:

- `test_start_command_sends_the_greeting_to_the_integer_chat_id` — asserts the
  outbound `chat_id` is an `int`, guarding the str/int class of bug
- `test_start_reply_text_is_the_documented_greeting`
- `test_malformed_inbound_update_logs_a_warning_and_sends_nothing`
- `test_missing_message_update_sends_nothing`
- `test_handler_failure_is_logged_at_exception_level_with_context`
- `test_build_application_registers_exactly_one_start_handler`

GREEN — implement `bot.py`. Use the `conftest.py` fake-context fixture.

## Task group 6 — Entry point and the friendly fatal path  (RED → GREEN)

RED:

- `test_main_returns_zero_when_settings_are_valid` — builder mocked; asserts
  long polling is started
- `test_main_returns_one_when_a_secret_is_missing`
- `test_main_returns_one_when_a_secret_is_blank`
- `test_main_names_only_the_missing_field_in_its_message` — the R2.3/R5
  user-facing guard; assert the message names the field and that the secret
  value is absent
- `test_main_never_prints_a_secret_to_stdout`

GREEN — implement `__main__.py`: `main() -> int`, `raise SystemExit(main())`.

## Task group 7 — `scripts/hooks`

1. Create `scripts/hooks` (`chmod 755`), staged-file resolution per R6.
2. Verify the empty-staging path exits `0` with a short message.
3. Verify the staged path runs ruff and pytest on exactly the staged files.
4. Verify `mypy` runs over all of `src/` regardless of what is staged (D4).

## Task group 8 — Documentation sync

1. README "Use" section: state that only the `/start` echo is live in Phase 1;
   the pipeline and `/restart` arrive in later phases.
2. README "Checks" section: correct the scoping wording to match D4.
3. Confirm the README's `pip install -e ".[dev]"` actually works from a clean
   environment.
4. Run `scripts/test` one final time; confirm green.