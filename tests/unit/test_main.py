"""RED: the entry point, and the friendly fatal path.

The module under test is `src/telegram_documentaries/__main__.py`. Three
requirements drive everything here:

* **R2.4 / D5** - a missing or blank secret produces *one* clear message naming
  the offending field, points at `.env.example`, and exits `1`. No default, no
  fallback, and above all no partial start.
* **R2.3** - the raw pydantic `ValidationError` is never printed. Its text
  embeds the *sibling* secret inside `input_value`, so only field names are ever
  read out of it. This is the highest-risk behaviour in the phase, and most of
  this module exists to keep it true.
* **R4.4** - `settings_loaded` and `gateway_started` are logged, and no token or
  settings *value* ever reaches a log record, a stream, or a traceback.

No test here touches the network or a real credential. The Telegram application
is a fake, the environment is emptied by the autouse isolation fixture, and the
working directory is a temporary one - so the repository's real `.env`, which
holds real credentials, is never read.
"""

from __future__ import annotations

import importlib
import io
import logging
import os
import runpy
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeTelegramBot
from pydantic import ValidationError

from telegram_documentaries import config, contracts, observability

# Distinctive markers. If any of these reaches stdout, stderr, a log record or a
# traceback, a secret has leaked. Not plausible real credentials.
TOKEN = "123456:SUPERSECRET-TOKEN-VALUE"
GEMINI_KEY = "AIzaSUPERSECRET-GEMINI-KEY-VALUE"

# `conftest`'s doubles are intentionally loose, and `tests/` sits outside mypy's
# scope, so fixtures and helpers here are deliberately unannotated.
LogRecords = Any
Coroutine = Any
Module = Any
Environment = Any
Completed = Any

#: The field names a `Settings` failure is allowed to name, and nothing else.
EXPECTED_FIELDS = frozenset({"telegram_bot_token", "gemini_api_key"})


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may read the real `.env` or the developer's shell environment.

    `Settings` resolves `env_file=".env"` against the *working directory*, so
    moving to a temporary one is what puts the repository's real `.env` - real
    credentials - genuinely out of reach, rather than merely irrelevant.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _pristine_application_logger() -> Iterator[None]:
    """Undo `main()`'s logging configuration after every test.

    `main()` calls `configure_logging()`, which attaches a stdout handler bound
    to whatever `sys.stdout` is at that moment. Under pytest that stream stops
    working once the test ends, so a handler left behind would make every later
    log record fail. Restoring the logger keeps `main()`'s behaviour under test
    while keeping the suite order-independent.
    """
    app_logger = observability.get_logger()
    handlers = list(app_logger.handlers)
    level = app_logger.level
    propagate = app_logger.propagate
    try:
        yield
    finally:
        app_logger.handlers = handlers
        app_logger.setLevel(level)
        app_logger.propagate = propagate


@pytest.fixture
def entry() -> Module:
    """The module under test, imported under its real package path."""
    return importlib.import_module("telegram_documentaries.__main__")


@pytest.fixture
def valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class FakeApplication:
    """Stands in for `telegram.ext.Application`. Records; never polls."""

    def __init__(self, token: str, bot_username: str = "phase_one_test_bot") -> None:
        self.token = token
        self.bot = FakeTelegramBot(username=bot_username)
        self.post_init: Coroutine = None
        self.polling_started = False
        self.polling_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def run_polling(self, *args: Any, **kwargs: Any) -> None:
        """Stand in for `Application.run_polling`, which blocks until stopped."""
        self.polling_calls.append((args, kwargs))
        self.polling_started = True


class FakeGateway:
    """Records what `build_application` was asked for, so a test can assert on it.

    `latest` is a property rather than a stored value, because the interesting
    assertion is often "nothing was built at all", and a stored value would have
    to be snapshotted before `main()` ran.
    """

    def __init__(self) -> None:
        self.built: list[FakeApplication] = []
        self.tokens: list[str] = []
        self.pipelines: list[Any] = []

    def build(self, token: str, pipeline: Any) -> FakeApplication:
        self.tokens.append(token)
        self.pipelines.append(pipeline)
        application = FakeApplication(token)
        self.built.append(application)
        return application

    @property
    def latest(self) -> FakeApplication | None:
        """The most recently built application, or `None` if none was built."""
        return self.built[-1] if self.built else None


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """Replace the real `build_application`, so no network call is possible.

    Patched on the `bot` module rather than on `__main__`, because that is the
    seam `__main__` calls through - and it keeps working when `__main__` is
    re-executed by `runpy`.
    """
    fake_gateway = FakeGateway()

    def fake_build_application(token: str, pipeline: Any) -> FakeApplication:
        return fake_gateway.build(token, pipeline)

    monkeypatch.setattr("telegram_documentaries.bot.build_application", fake_build_application)
    return fake_gateway


def _everything_visible(record: logging.LogRecord) -> str:
    """Everything about one record a human could read: message, structured
    context, traceback and raw attributes - so "not logged" means *nowhere*."""
    traceback = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
    return f"{record.getMessage()}\n{vars(record)!r}\n{traceback}"


def _infos_named(records: LogRecords, event: str) -> list[dict[str, Any]]:
    """The `extra` context of every INFO record for one event."""
    return [
        records.extra_of(record)
        for record in records.at_level(logging.INFO)
        if records.extra_of(record).get("event") == event
    ]


def _secret_markers() -> tuple[str, ...]:
    """Every fragment that must never appear: whole values and slices.

    `str(ValidationError)` truncates its ``input_value``, so a whole-value check
    can pass while a real slice of the secret is on screen.
    """
    return (
        TOKEN,
        GEMINI_KEY,
        "SUPERSECRET",
        TOKEN[-10:],
        GEMINI_KEY[-10:],
        TOKEN.split(":")[1][:12],
    )


# --------------------------------------------------------------------------
# Happy path: valid settings -> long polling (R2.4 "no partial start")
# --------------------------------------------------------------------------


def test_main_returns_zero_when_settings_are_valid(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
) -> None:
    """Exit `0`, and the gateway actually runs - not merely gets built."""
    assert entry.main() == 0

    application = gateway.latest
    assert application.polling_started is True
    assert application.polling_calls == [((), {})]


def test_main_passes_the_configured_token_to_the_gateway(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
) -> None:
    """The real token reaches the builder, deliberately read from settings."""
    entry.main()

    assert gateway.tokens == [TOKEN]
    assert gateway.latest.token == TOKEN


def test_main_builds_and_injects_a_pipeline(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
) -> None:
    """D10: the hub is assembled here and handed to the adapter, not built in it.

    Asserted from `main()` rather than from `_build_pipeline`, because the
    wiring is the thing that could silently break - `bot.py` growing a
    `ConversationPipeline(...)` of its own would leave every test passing while
    production used a second, unconfigured hub.
    """
    from telegram_documentaries.pipeline import ConversationPipeline

    entry.main()

    assert len(gateway.pipelines) == 1
    pipeline = gateway.pipelines[0]
    assert isinstance(pipeline, ConversationPipeline)


def test_main_registers_a_post_init_hook_before_polling(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
) -> None:
    """The hook must be attached to the application, not merely defined.

    `Application.run_polling` invokes `post_init` after initialisation, which is
    the first moment the bot username exists. A hook that is never assigned
    would make `gateway_started` silently unloggable.
    """
    entry.main()

    assert gateway.latest.post_init is not None


def test_main_logs_settings_loaded_at_info_without_any_secret(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """R4.4: the load is announced exactly once, carrying no secret.

    The bot username cannot appear on this record: it is only known once
    Telegram has answered `getMe`, which happens later. `gateway_started` is
    where it belongs.
    """
    entry.main()

    loaded = _infos_named(app_records, "settings_loaded")
    assert len(loaded) == 1
    for marker in _secret_markers():
        assert marker not in app_records.rendered()


async def test_main_logs_gateway_started_with_the_bot_username(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """The username is safe to log, and is available only after init."""
    entry.main()
    application = gateway.latest

    await application.post_init(application)

    started = _infos_named(app_records, "gateway_started")
    assert len(started) == 1
    assert started[0]["bot_username"] == "phase_one_test_bot"
    assert TOKEN not in app_records.rendered()


# --------------------------------------------------------------------------
# Fatal path: a missing secret (R2.4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("missing", "kept"),
    [
        ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN"),
        ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"),
        ("GEMINI_API_KEY", None),
        ("TELEGRAM_BOT_TOKEN", None),
    ],
    ids=["missing-gemini-key", "missing-token", "missing-both-1", "missing-both-2"],
)
def test_main_returns_one_when_a_secret_is_missing(
    entry: Module,
    monkeypatch: pytest.MonkeyPatch,
    gateway: FakeGateway,
    missing: str,
    kept: str | None,
) -> None:
    """Either secret alone, or both at once, is fatal with exit code `1`."""
    assert missing  # documents which variable the case removes
    if kept is not None:
        monkeypatch.setenv(kept, TOKEN if kept == "TELEGRAM_BOT_TOKEN" else GEMINI_KEY)

    assert entry.main() == 1


# --------------------------------------------------------------------------
# Fatal path: a blank secret (R2.2 as the user experiences it)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"])
@pytest.mark.parametrize("blank", ["", "   ", " \t\n "])
def test_main_returns_one_when_a_secret_is_blank(
    entry: Module,
    monkeypatch: pytest.MonkeyPatch,
    valid_environment: None,
    gateway: FakeGateway,
    field: str,
    blank: str,
) -> None:
    """A blank value is the likeliest real mistake, and is caught at load."""
    monkeypatch.setenv(field, blank)

    assert entry.main() == 1


# --------------------------------------------------------------------------
# One clear message, naming only the offending field (R2.4, R2.3)
# --------------------------------------------------------------------------


def test_main_names_only_the_missing_field_in_its_message(
    entry: Module,
    monkeypatch: pytest.MonkeyPatch,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """The message is diagnostic: the broken field, and where to fix it."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)  # valid sibling
    # GEMINI_API_KEY stays unset.

    assert entry.main() == 1

    criticals = app_records.at_level(logging.CRITICAL)
    assert len(criticals) == 1, "R2.4 wants a single fatal line, not a paragraph"
    context = app_records.extra_of(criticals[0])
    assert context["event"] == "settings_invalid"
    assert context["fields"] == "gemini_api_key"

    message = criticals[0].getMessage()
    assert "gemini_api_key" in message
    assert "telegram_bot_token" not in message, "the healthy field is not the problem"
    assert ".env.example" in message, "the user must be told where to look"


def test_main_names_both_fields_when_both_secrets_are_missing(
    entry: Module,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """Two broken fields are reported as two, not as a generic failure."""
    assert entry.main() == 1

    criticals = app_records.at_level(logging.CRITICAL)
    assert len(criticals) == 1
    assert set(app_records.extra_of(criticals[0])["fields"].split(", ")) == EXPECTED_FIELDS
    message = criticals[0].getMessage()
    for field in EXPECTED_FIELDS:
        assert field in message


def test_main_reports_a_blank_secret_under_the_same_single_message(
    entry: Module,
    monkeypatch: pytest.MonkeyPatch,
    valid_environment: None,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """A blank value is named like a missing one - the user learns one rule."""
    monkeypatch.setenv("GEMINI_API_KEY", "   ")

    assert entry.main() == 1

    criticals = app_records.at_level(logging.CRITICAL)
    assert len(criticals) == 1
    assert app_records.extra_of(criticals[0])["fields"] == "gemini_api_key"
    assert ".env.example" in criticals[0].getMessage()


def test_main_emits_no_other_diagnosis_alongside_the_fatal_line(
    entry: Module,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """One clear message means one: no second opinion from another handler."""
    entry.main()

    assert app_records.at_level(logging.WARNING) == []
    assert app_records.at_level(logging.ERROR) == []
    assert len(app_records.at_level(logging.CRITICAL)) == 1


def test_main_never_prints_a_secret_to_stdout(
    entry: Module,
    monkeypatch: pytest.MonkeyPatch,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """The mechanism-level prevention, with the premise asserted first.

    A valid token sits next to the missing key, so pydantic's own error text
    *does* carry the token. If that ever stops being true this test fails
    loudly instead of becoming vacuously safe.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)

    with pytest.raises(ValidationError) as excinfo:
        config.Settings(_env_file=None)
    # Premise: the raw error object carries the sibling secret. This is exactly
    # why nothing downstream may render it.
    assert TOKEN in str(excinfo.value.errors())

    assert entry.main() == 1

    rendered = app_records.rendered()
    for marker in _secret_markers():
        assert marker not in rendered
        for record in app_records.records:
            assert marker not in _everything_visible(record)
    # The raw error text itself never surfaces, in any form.
    assert "input_value" not in rendered
    assert "validation error for" not in rendered


def test_main_never_surfaces_the_validation_error_on_a_stream(
    entry: Module,
    monkeypatch: pytest.MonkeyPatch,
    gateway: FakeGateway,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whatever reaches stdout or stderr on the fatal path is leak-free.

    The application handler's stream is observed through a `StringIO` rather
    than through `capsys` alone: `configure_logging()` binds `sys.stdout` once,
    on the first call, so if an earlier test module already configured logging
    the bound stream is not the one `capsys` is capturing. Retargeting the
    handler makes this independent of test order. (Same approach, and same
    reason, as `test_observability.py`.)
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)

    observability.configure_logging()
    handler = observability.get_logger().handlers[0]
    captured = io.StringIO()
    original_stream = handler.stream
    handler.stream = captured
    try:
        entry.main()
    finally:
        handler.stream = original_stream

    written = captured.getvalue()
    streams = capsys.readouterr()
    written += streams.out + streams.err
    assert "gemini_api_key" in written, "the fatal message must actually reach the user"
    assert ".env.example" in written
    for marker in _secret_markers():
        assert marker not in written


def test_main_never_attaches_the_validation_error_to_a_log_record(
    entry: Module,
    gateway: FakeGateway,
    app_records: LogRecords,
) -> None:
    """No record may carry the leaky `exc_info` of the settings failure.

    `logger.exception` / `exc_info=...` is the reflex when a fatal error must be
    "chained for a developer". Here it is precisely the leak: the formatted
    traceback of a `ValidationError` renders its `input_value`.
    """
    entry.main()

    for record in app_records.records:
        assert record.exc_info is None


def test_main_chains_the_original_validation_error_for_a_developer(
    entry: Module,
) -> None:
    """D5: chained for a developer, absent from the user-facing message.

    The chain is deliberately not *rendered* (R2.3 forbids it), so it is what a
    debugger inspects. Asserting the mechanism exists keeps a future refactor
    from dropping it silently.
    """
    with pytest.raises(entry.ConfigurationError) as excinfo:
        entry.load_settings()

    cause = excinfo.value.__cause__
    assert isinstance(cause, ValidationError)
    assert contracts.validation_error_fields(cause) == ("telegram_bot_token", "gemini_api_key")
    # The wrapper's own text is safe even though its cause is not.
    for marker in _secret_markers():
        assert marker not in str(excinfo.value)


def test_load_settings_returns_valid_settings_unchanged(
    entry: Module,
    valid_environment: None,
) -> None:
    """The happy path of the helper `main()` is built on."""
    settings = entry.load_settings()

    assert settings.telegram_bot_token.get_secret_value() == TOKEN


# --------------------------------------------------------------------------
# No partial start (D5)
# --------------------------------------------------------------------------


def test_main_does_not_start_the_bot_when_configuration_is_invalid(
    entry: Module,
    gateway: FakeGateway,
) -> None:
    """Bad configuration must not reach the network, even to say hello."""
    assert entry.main() == 1

    assert gateway.built == []
    assert gateway.tokens == []


def test_main_returns_an_integer_exit_code_and_nothing_else(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
) -> None:
    """`main() -> int` is the contract the module-scope `SystemExit` relies on."""
    result = entry.main()

    assert result == 0
    assert isinstance(result, int)
    assert not isinstance(result, bool)


# --------------------------------------------------------------------------
# Module scope: `python -m telegram_documentaries` (R2.4's exit code)
# --------------------------------------------------------------------------


def test_module_scope_exits_with_the_code_main_returned(
    entry: Module,
    valid_environment: None,
    gateway: FakeGateway,
) -> None:
    """`raise SystemExit(main())` really is at module scope.

    `runpy` executes the module with `__name__ == "__main__"`, exactly as
    `python -m` does, with `build_application` already faked - so the happy path
    is proven end to end without a network call.
    """
    sys.modules.pop("telegram_documentaries.__main__", None)
    try:
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("telegram_documentaries.__main__", run_name="__main__")
    finally:
        importlib.import_module("telegram_documentaries.__main__")

    assert excinfo.value.code == 0
    assert gateway.latest.polling_started is True


def test_python_dash_m_exits_one_with_a_single_actionable_message(tmp_path: Path) -> None:
    """The real entry point, in a real process, with nothing configured.

    This is validation.md A3. A subprocess is the only honest way to test it:
    the exit code is the process's own, and no credential can leak in from the
    developer's shell because both variables are removed from the child's
    environment. It exits before any network call, so it cannot hang.
    """
    completed = _run_module(tmp_path, _environment_without_secrets())

    assert completed.returncode == 1, completed.stdout + completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one message, got {lines}"
    for field in EXPECTED_FIELDS:
        assert field in lines[0]
    assert ".env.example" in lines[0]
    assert "validation error for" not in lines[0]
    assert "input_value" not in lines[0]


def test_python_dash_m_exits_one_and_leaks_nothing_when_one_secret_is_set(
    tmp_path: Path,
) -> None:
    """The realistic mistake: one key filled in, the other forgotten.

    With a real-looking token present, pydantic's own error text would carry it.
    Asserting on the child's real stdout is the strongest available proof that
    the R2.3 handling holds outside the test process too.
    """
    environment = _environment_without_secrets()
    environment["TELEGRAM_BOT_TOKEN"] = TOKEN

    completed = _run_module(tmp_path, environment)

    assert completed.returncode == 1
    written = completed.stdout + completed.stderr
    assert "gemini_api_key" in written
    for marker in _secret_markers():
        assert marker not in written


def _environment_without_secrets() -> Environment:
    """The developer's environment, minus both configuration variables."""
    environment = dict(os.environ)
    environment.pop("TELEGRAM_BOT_TOKEN", None)
    environment.pop("GEMINI_API_KEY", None)
    return environment


def _run_module(cwd: Path, environment: Environment) -> Completed:
    """Run `python -m telegram_documentaries` from `cwd`, with no network."""
    return subprocess.run(
        [sys.executable, "-m", "telegram_documentaries"],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
