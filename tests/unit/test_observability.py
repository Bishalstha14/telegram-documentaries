"""RED: structured logging with key-value context (R5).

Every decorated call must emit `event`, `chat_id`, `update_id` and
`duration_ms`, must be async-aware, and must log-and-re-raise rather than
swallow.

Records are captured with a handler attached directly to the application
logger. `caplog` cannot be used here: `configure_logging()` sets
`propagate = False` (so the application never double-logs through the root
logger), which makes `caplog` visibility depend on test execution order.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from telegram_documentaries import observability

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}


class _SentinelError(Exception):
    """Raised by the doubles to prove the decorator re-raises."""


class _Recorder(logging.Handler):
    """Collects the records the application actually emits."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records() -> Iterator[_Recorder]:
    """Capture the application logger's output for the duration of a test."""
    recorder = _Recorder()
    app_logger = observability.get_logger()
    app_logger.addHandler(recorder)
    previous_level = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    try:
        yield recorder
    finally:
        app_logger.removeHandler(recorder)
        app_logger.setLevel(previous_level)


def _extra(record: logging.LogRecord) -> dict[str, Any]:
    """The `extra` context attached to a record, minus LogRecord's own fields."""
    return {key: value for key, value in vars(record).items() if key not in _RESERVED}


def _at_level(recorder: _Recorder, level: int) -> list[logging.LogRecord]:
    return [record for record in recorder.records if record.levelno == level]


# --- R5: the decorated call emits the required context ---------------------


def test_logged_emits_chat_id_update_id_and_duration_ms(records: _Recorder) -> None:
    @observability.logged("start_reply_sent")
    def handler(chat_id: int, update_id: int) -> str:
        return "done"

    assert handler(chat_id=42, update_id=1234) == "done"

    emitted = _at_level(records, logging.INFO)
    assert len(emitted) == 1
    context = _extra(emitted[0])
    assert context["event"] == "start_reply_sent"
    assert context["chat_id"] == 42
    assert context["update_id"] == 1234
    assert isinstance(context["duration_ms"], float)
    assert context["duration_ms"] >= 0.0


def test_logged_reads_the_correlation_ids_positionally_too(records: _Recorder) -> None:
    """A handler invoked as `handler(chat_id, update_id)` must still log both."""

    @observability.logged("positional_probe")
    def handler(chat_id: int, update_id: int) -> None:
        return None

    handler(11, 22)

    context = _extra(_at_level(records, logging.INFO)[0])
    assert context["chat_id"] == 11
    assert context["update_id"] == 22


def test_logged_reports_duration_as_a_number_not_a_string(records: _Recorder) -> None:
    @observability.logged("timing_probe")
    def handler() -> None:
        return None

    handler()

    context = _extra(_at_level(records, logging.INFO)[0])
    assert isinstance(context["duration_ms"], float)
    assert not isinstance(context["duration_ms"], str)


async def test_logged_awaits_the_wrapped_coroutine(records: _Recorder) -> None:
    calls: list[str] = []

    @observability.logged("async_probe")
    async def handler(chat_id: int, update_id: int) -> str:
        calls.append("before-await")
        await asyncio.sleep(0)
        calls.append("after-await")
        return "finished"

    assert await handler(chat_id=7, update_id=99) == "finished"

    # The coroutine ran to completion, not merely started.
    assert calls == ["before-await", "after-await"]

    context = _extra(_at_level(records, logging.INFO)[0])
    assert context["event"] == "async_probe"
    assert context["chat_id"] == 7
    assert context["update_id"] == 99
    assert isinstance(context["duration_ms"], float)


async def test_logged_measures_duration_across_the_await(records: _Recorder) -> None:
    @observability.logged("slow_async")
    async def handler() -> None:
        await asyncio.sleep(0.02)

    await handler()

    context = _extra(_at_level(records, logging.INFO)[0])
    assert context["duration_ms"] >= 15.0


# --- R5: logs and re-raises, never swallows -------------------------------


def test_logged_reraises_after_logging_an_exception(records: _Recorder) -> None:
    @observability.logged("failing_sync")
    def handler(chat_id: int, update_id: int) -> None:
        raise _SentinelError("boom")

    with pytest.raises(_SentinelError, match="boom"):
        handler(chat_id=5, update_id=6)

    failures = _at_level(records, logging.ERROR)
    assert failures, "a failing decorated call must log loudly"
    assert failures[-1].exc_info is not None
    assert failures[-1].exc_info[0] is _SentinelError


def test_a_failing_call_does_not_also_emit_a_success_record(records: _Recorder) -> None:
    @observability.logged("failing_only")
    def handler() -> None:
        raise _SentinelError

    with pytest.raises(_SentinelError):
        handler()

    assert _at_level(records, logging.INFO) == []
    assert len(_at_level(records, logging.ERROR)) == 1


async def test_logged_reraises_a_coroutine_exception(records: _Recorder) -> None:
    @observability.logged("failing_async")
    async def handler(chat_id: int, update_id: int) -> None:
        raise _SentinelError("async boom")

    with pytest.raises(_SentinelError, match="async boom"):
        await handler(chat_id=8, update_id=9)

    failures = _at_level(records, logging.ERROR)
    assert failures
    assert failures[-1].exc_info is not None
    # The failure record still carries the correlation context.
    context = _extra(failures[-1])
    assert context["chat_id"] == 8
    assert context["update_id"] == 9
    assert isinstance(context["duration_ms"], float)


# --- D8: static `extra` context, merged into every record ------------------


def test_logged_merges_static_extra_into_every_record(records: _Recorder) -> None:
    """D8: `stage` and `model` on every Gemini record, without a `log.info`.

    The static keys must appear *alongside* the computed correlation context, not
    instead of it - a record missing `chat_id` would be worse than no record.
    """

    @observability.logged(
        "gemini_call",
        extra={"stage": "bouncer", "model": "gemini-3.1-flash-lite"},
    )
    def handler(chat_id: int, update_id: int) -> str:
        return "done"

    assert handler(chat_id=1, update_id=2) == "done"

    emitted = _at_level(records, logging.INFO)
    assert len(emitted) == 1
    context = _extra(emitted[0])
    assert context["stage"] == "bouncer"
    assert context["model"] == "gemini-3.1-flash-lite"
    assert context["event"] == "gemini_call"
    assert context["chat_id"] == 1
    assert context["update_id"] == 2
    assert isinstance(context["duration_ms"], float)


def test_logged_merges_static_extra_onto_the_failure_record(records: _Recorder) -> None:
    """The failure record is the one that matters for diagnosing a Gemini outage."""

    @observability.logged("gemini_call", extra={"stage": "scripter"})
    def handler(chat_id: int, update_id: int) -> None:
        raise _SentinelError("boom")

    with pytest.raises(_SentinelError):
        handler(chat_id=3, update_id=4)

    failures = _at_level(records, logging.ERROR)
    assert len(failures) == 1
    context = _extra(failures[0])
    assert context["stage"] == "scripter"
    assert context["event"] == "gemini_call"
    assert context["chat_id"] == 3
    assert context["update_id"] == 4


async def test_logged_merges_static_extra_onto_an_async_record(records: _Recorder) -> None:
    @observability.logged("gemini_call", extra={"stage": "interviewer"})
    async def handler(chat_id: int, update_id: int) -> str:
        return "done"

    assert await handler(chat_id=4, update_id=5) == "done"

    context = _extra(_at_level(records, logging.INFO)[0])
    assert context["stage"] == "interviewer"
    assert context["chat_id"] == 4


@pytest.mark.parametrize(
    "reserved",
    ["message", "levelname", "msg", "name", "created", "exc_info", "levelno", "pathname"],
)
def test_logged_static_extra_cannot_overwrite_a_reserved_record_attribute(
    records: _Recorder,
    reserved: str,
) -> None:
    """A reserved key must be dropped, never merged.

    Merging one makes `logging.Logger.makeRecord` raise `KeyError` *while the
    record is being built*, so a single careless `extra={"message": ...}` would
    turn every call of the decorated function into a crash. The decorator's job
    is to make that mistake impossible, not merely visible.
    """

    @observability.logged("reserved_probe", extra={reserved: "HIJACKED"})
    def handler(chat_id: int, update_id: int) -> str:
        return "done"

    # Must not raise while the record is constructed.
    assert handler(chat_id=9, update_id=10) == "done"

    record = _at_level(records, logging.INFO)[0]
    assert getattr(record, reserved, None) != "HIJACKED"
    # The record itself is intact: message, level and logger all still correct.
    assert record.getMessage() == "reserved_probe"
    assert record.levelname == "INFO"
    assert record.name == observability.LOGGER_NAME


def test_logged_computed_context_wins_over_a_colliding_static_key(records: _Recorder) -> None:
    """`event` / `chat_id` / `update_id` / `duration_ms` are the contract.

    A static key with one of those names is dropped rather than allowed to
    replace a real correlation id with a literal.
    """

    @observability.logged(
        "shadow_probe",
        extra={"chat_id": "static", "update_id": "static", "event": "static"},
    )
    def handler(chat_id: int, update_id: int) -> None:
        return None

    handler(chat_id=5, update_id=6)

    context = _extra(_at_level(records, logging.INFO)[0])
    assert context["chat_id"] == 5
    assert context["update_id"] == 6
    assert context["event"] == "shadow_probe"
    assert isinstance(context["duration_ms"], float)


def test_logged_does_not_mutate_the_caller_supplied_extra_mapping(records: _Recorder) -> None:
    """The mapping belongs to the caller; the decorator only ever reads it."""

    supplied: dict[str, object] = {"stage": "bouncer"}
    before = dict(supplied)

    @observability.logged("mutation_probe", extra=supplied)
    def handler(chat_id: int, update_id: int) -> None:
        return None

    handler(chat_id=7, update_id=8)
    handler(chat_id=9, update_id=10)

    assert supplied == before


# --- R5: fieldless calls still emit a coherent record ---------------------


def test_logged_emits_null_context_when_chat_and_update_are_absent(
    records: _Recorder,
) -> None:
    @observability.logged("no_context")
    def handler() -> None:
        return None

    handler()

    context = _extra(_at_level(records, logging.INFO)[0])
    assert context["event"] == "no_context"
    assert context["chat_id"] is None
    assert context["update_id"] is None
    assert isinstance(context["duration_ms"], float)


# --- R5: configure_logging ------------------------------------------------


def test_configure_logging_is_idempotent() -> None:
    observability.configure_logging()
    handler_count = len(observability.get_logger().handlers)

    observability.configure_logging()
    observability.configure_logging()

    assert len(observability.get_logger().handlers) == handler_count
    assert handler_count >= 1


def test_configure_logging_does_not_leak_records_onto_the_root_logger() -> None:
    """Otherwise a message would be emitted twice in production."""
    observability.configure_logging()

    assert observability.get_logger().propagate is False


def test_configure_logging_renders_key_value_context_in_the_output() -> None:
    """The application's own handler must render extra context as key=value.

    The handler is observed by pointing it at a StringIO rather than by
    `capsys`/`capfd`: `configure_logging()` binds `sys.stdout` once, on the
    first call, so whichever stream object it captured is already stale by the
    time any later test asks pytest to capture output.
    """
    observability.configure_logging()
    handler = observability.get_logger().handlers[0]

    captured = io.StringIO()
    original_stream = handler.stream
    handler.stream = captured
    try:
        observability.get_logger("render.probe").info(
            "hello",
            extra={"event": "render_probe", "chat_id": 11, "update_id": 22},
        )
    finally:
        handler.stream = original_stream

    printed = captured.getvalue()
    assert "hello" in printed
    assert "event=render_probe" in printed
    assert "chat_id=11" in printed
    assert "update_id=22" in printed


def test_the_formatted_output_never_invents_context_for_a_bare_message() -> None:
    observability.configure_logging()
    handler = observability.get_logger().handlers[0]

    captured = io.StringIO()
    original_stream = handler.stream
    handler.stream = captured
    try:
        observability.get_logger("bare.probe").info("no context here")
    finally:
        handler.stream = original_stream

    printed = captured.getvalue().strip()
    assert printed == "no context here"


def test_get_logger_returns_a_child_of_the_application_logger() -> None:
    assert observability.get_logger().name == observability.LOGGER_NAME
    assert observability.get_logger("child").name == f"{observability.LOGGER_NAME}.child"
