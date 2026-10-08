"""RED: the `/start` gateway, the reply contract, and the failure paths (R4).

The two things this module is really guarding are both easy to regress:

* **R4.1** - the greeting leaves the process through
  `context.bot.send_message(chat_id=<int>, ...)`. Not
  `update.effective_message.reply_text(...)`, which would drag a library object
  back into the handler *and* push the mock boundary out to the network seam.
  The outbound `chat_id` is therefore asserted to be an `int`: a chat id that is
  sometimes a string and sometimes an integer is the defect that silently breaks
  session lookups from Phase 3 onwards.
* **R4.2 / R4.3** - a bad payload is loud in the logs and silent to the user,
  and an exception escaping a handler is logged with enough context to trace it.

Nothing here touches the network or a real credential: updates are built from
raw payload dicts through `Update.de_json(...)` with a mocked bot (R7), and the
token used to build an application is the fictional one from `conftest`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from conftest import FAKE_BOT_TOKEN, FakeContext, FakeTelegramBot
from pydantic import ValidationError
from telegram import Message
from telegram.error import NetworkError

from telegram_documentaries import bot
from telegram_documentaries.pipeline import WELCOME

# `conftest`'s doubles are intentionally loose and `tests/` sits outside mypy's
# scope, so fixtures and helpers here are deliberately unannotated.
LogRecords = Any
Context = Any
UpdateFactory = Any

#: Telegram chat ids are negative for groups. A positive private id would pass a
#: naive "is it truthy" check; this one also catches a sign flip.
CHAT_ID = -1001234567890

#: The correlation ids the happy-path tests expect to see in the logs.
UPDATE_ID = 5001


def _start_message(**overrides: Any) -> dict[str, Any]:
    """A `/start` message payload, shaped the way Telegram actually sends one."""
    message: dict[str, Any] = {
        "message_id": 17,
        "date": 1_700_000_000,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": 555, "is_bot": False, "first_name": "Ada"},
        "text": "/start",
        # Telegram always reports a command as a BOT_COMMAND entity at offset 0.
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
    }
    message.update(overrides)
    return message


@pytest.fixture
def start_update(make_update: UpdateFactory) -> UpdateFactory:
    """Build a real `/start` `Update`; override any part of the message payload."""

    def factory(update_id: int = UPDATE_ID, **overrides: Any) -> Any:
        return make_update(update_id=update_id, message=_start_message(**overrides))

    return factory


class ExplodingTelegramBot(FakeTelegramBot):
    """A bot whose outbound call fails, the way the Telegram API can fail."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def send_message(self, *, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        raise self.error


def _everything_visible(record: logging.LogRecord) -> str:
    """Everything about one record that a human could ever read.

    The message, the structured `extra` context, the traceback and the raw
    record attributes - so "the token is not logged" means it is not logged
    *anywhere*, not merely absent from the formatted line.
    """
    traceback = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
    return f"{record.getMessage()}\n{vars(record)!r}\n{traceback}"


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


async def test_start_command_sends_the_greeting_to_the_integer_chat_id(
    start_update: UpdateFactory,
    fake_context: Context,
    pipeline: Any,
) -> None:
    """R4.1: one outbound message, addressed to an `int` chat id.

    `isinstance(chat_id, int)` and the `bool` exclusion are the load-bearing
    assertions. A string chat id would still "work" against the Bot API and would
    be found again only when session state keys on it in Phase 3.
    """
    fake_context.application.pipeline = pipeline
    fake_context.application.pipeline = pipeline
    await bot.on_start(start_update(), fake_context)

    assert len(fake_context.bot.sent) == 1
    sent = fake_context.bot.sent[0]
    assert sent["chat_id"] == CHAT_ID
    assert isinstance(sent["chat_id"], int)
    assert not isinstance(sent["chat_id"], bool)


async def test_start_reply_text_is_the_documented_greeting(
    start_update: UpdateFactory,
    fake_context: Context,
    pipeline: Any,
) -> None:
    """One documented constant, sent verbatim - no per-call string building.

    Retargeted at the hub's text (D10): the greeting is now a state-machine
    outcome rather than a transport detail, so `bot` has no string of its own.
    """
    assert isinstance(WELCOME, str)
    assert WELCOME.strip()

    fake_context.application.pipeline = pipeline
    fake_context.application.pipeline = pipeline
    await bot.on_start(start_update(), fake_context)

    assert [sent["text"] for sent in fake_context.bot.sent] == [WELCOME]


async def test_the_greeting_is_not_sent_through_effective_message_reply_text(
    start_update: UpdateFactory,
    fake_context: Context,
    monkeypatch: pytest.MonkeyPatch,
    pipeline: Any,
) -> None:
    """R4.1's other half: `reply_text` must never be the path the greeting takes.

    `reply_text` is patched to fail loudly, so a regression cannot slip through
    while still producing a green happy-path test.
    """
    taken: list[str] = []

    async def _forbidden_reply_text(self: Any, *args: Any, **kwargs: Any) -> Any:
        taken.append(str(args[0] if args else kwargs))
        raise AssertionError("the greeting must travel via context.bot.send_message")

    monkeypatch.setattr(Message, "reply_text", _forbidden_reply_text)

    fake_context.application.pipeline = pipeline
    await bot.on_start(start_update(), fake_context)

    assert taken == []
    assert [sent["text"] for sent in fake_context.bot.sent] == [WELCOME]


async def test_two_chats_each_receive_their_own_greeting(
    make_update: UpdateFactory,
    fake_context: Context,
    pipeline: Any,
) -> None:
    """Isolation: a reply is addressed to the chat that asked, never a shared one."""
    first = make_update(update_id=6001, message=_start_message(chat={"id": 111, "type": "private"}))
    second = make_update(
        update_id=6002, message=_start_message(chat={"id": CHAT_ID, "type": "supergroup"})
    )

    fake_context.application.pipeline = pipeline
    await bot.on_start(first, fake_context)
    await bot.on_start(second, fake_context)

    assert [sent["chat_id"] for sent in fake_context.bot.sent] == [111, CHAT_ID]


async def test_start_command_is_logged_with_both_correlation_ids(
    start_update: UpdateFactory,
    fake_context: Context,
    app_records: LogRecords,
    pipeline: Any,
) -> None:
    """The acceptance criterion for the happy path in validation.md section E.

    The event is `start_received` (D10): the adapter now logs a uniform
    `<entry>_received` for all three entry points rather than a bespoke name
    per handler.
    """
    fake_context.application.pipeline = pipeline
    await bot.on_start(start_update(), fake_context)

    received = [
        app_records.extra_of(record)
        for record in app_records.at_level(logging.INFO)
        if app_records.extra_of(record).get("event") == "start_received"
    ]
    assert len(received) == 1
    assert received[0]["chat_id"] == CHAT_ID
    assert received[0]["update_id"] == UPDATE_ID


async def test_a_successful_reply_is_logged_with_its_duration(
    start_update: UpdateFactory,
    fake_context: Context,
    app_records: LogRecords,
    pipeline: Any,
) -> None:
    fake_context.application.pipeline = pipeline
    await bot.on_start(start_update(), fake_context)

    events = {
        app_records.extra_of(record).get("event"): app_records.extra_of(record)
        for record in app_records.records
    }
    reply = events["reply_sent"]
    assert reply["chat_id"] == CHAT_ID
    assert reply["update_id"] == UPDATE_ID
    assert reply["duration_ms"] >= 0


# --------------------------------------------------------------------------
# Malformed input -> warning logged, nothing sent, nothing raised (R4.2)
# --------------------------------------------------------------------------


async def test_malformed_inbound_update_logs_a_warning_and_sends_nothing(
    start_update: UpdateFactory,
    fake_context: Context,
    app_records: LogRecords,
) -> None:
    """A string `chat.id` is rejected at the edge and never answered."""
    update = start_update(update_id=5002, chat={"id": str(CHAT_ID), "type": "supergroup"})

    await bot.on_start(update, fake_context)  # must not raise

    assert fake_context.bot.sent == []
    warnings = app_records.at_level(logging.WARNING)
    assert len(warnings) == 1
    context = app_records.extra_of(warnings[0])
    assert context["event"] == "inbound_update_invalid"
    assert context["update_id"] == 5002
    assert "chat.id" in context["reason"]
    assert not app_records.at_level(logging.ERROR)


async def test_a_message_with_no_chat_is_rejected_and_sends_nothing(
    make_update: UpdateFactory,
    fake_context: Context,
    app_records: LogRecords,
) -> None:
    """The sibling malformed shape: a message with nowhere to reply to."""
    payload = _start_message()
    del payload["chat"]
    update = make_update(update_id=UPDATE_ID, message=payload)

    await bot.on_start(update, fake_context)

    assert fake_context.bot.sent == []
    warnings = app_records.at_level(logging.WARNING)
    assert len(warnings) == 1
    assert app_records.extra_of(warnings[0])["event"] == "inbound_update_invalid"


async def test_malformed_input_is_never_answered_with_a_guessed_chat_id(
    start_update: UpdateFactory,
    fake_context: Context,
) -> None:
    """There is no fallback chat id: no answer is better than a wrong one."""
    update = start_update(chat={"id": "not-a-number", "type": "supergroup"})

    await bot.on_start(update, fake_context)

    assert fake_context.bot.last_chat_id is None


async def test_an_unanticipated_malformed_shape_is_reported_not_dropped(
    make_update: UpdateFactory,
    fake_context: Context,
    app_records: LogRecords,
) -> None:
    """The category guard for MISSION.md #4: no input shape may vanish quietly.

    `on_start` only catches `InvalidInboundUpdateError`. A string `update_id` is
    rejected by the contract as a pydantic error instead, escapes the handler,
    and is caught by the registered `error` handler - which reports it with a
    traceback and refuses to trust the bad id as a correlation key.
    """
    update = make_update(update_id="not-a-number", message=_start_message())

    with pytest.raises(ValidationError) as excinfo:
        await bot.on_start(update, fake_context)
    assert fake_context.bot.sent == []

    fake_context.error = excinfo.value  # what python-telegram-bot hands to on_error
    await bot.on_error(update, fake_context)

    reported = [
        record
        for record in app_records.at_level(logging.ERROR)
        if app_records.extra_of(record).get("event") == "handler_failed"
    ]
    assert len(reported) == 1
    extra = app_records.extra_of(reported[0])
    assert extra["chat_id"] == CHAT_ID
    assert extra["update_id"] is None, "an id that is not an int must not be trusted as one"
    visible = _everything_visible(reported[0])
    assert "ValidationError" in visible
    assert "update_id" in visible, "the traceback must name the field that failed"


# --------------------------------------------------------------------------
# An update with no message -> a documented no-op
# --------------------------------------------------------------------------


async def test_missing_message_update_sends_nothing(
    make_update: UpdateFactory,
    fake_context: Context,
) -> None:
    """A poll, a reaction or an edited message has nothing to greet."""
    await bot.on_start(make_update(update_id=5003), fake_context)

    assert fake_context.bot.sent == []


async def test_missing_message_update_is_not_treated_as_malformed(
    make_update: UpdateFactory,
    fake_context: Context,
    app_records: LogRecords,
) -> None:
    """Silence in the logs too: "ignored" is a normal outcome, not a fault."""
    await bot.on_start(make_update(update_id=5003), fake_context)

    assert app_records.at_level(logging.WARNING) == []
    assert "inbound_update_ignored" in app_records.events()


# --------------------------------------------------------------------------
# A failure escaping a handler -> logged at exception level (R4.3)
# --------------------------------------------------------------------------


async def test_a_send_failure_reaches_the_error_handler_with_full_context(
    start_update: UpdateFactory,
    app_records: LogRecords,
    pipeline: Any,
) -> None:
    """R4.3 end to end: the error escapes the handler and is logged with its ids.

    Retrying or telling the user anything is Phase 7 work; the contract here is
    only that the failure is never silent.
    """
    failure = NetworkError("connection reset by peer")
    context = FakeContext(ExplodingTelegramBot(failure), pipeline=pipeline)
    update = start_update()

    with pytest.raises(NetworkError):
        await bot.on_start(update, context)

    context.error = failure  # what python-telegram-bot does before calling on_error
    await bot.on_error(update, context)

    reported = [
        record
        for record in app_records.at_level(logging.ERROR)
        if app_records.extra_of(record).get("event") == "handler_failed"
    ]
    assert len(reported) == 1
    extra = app_records.extra_of(reported[0])
    assert extra["chat_id"] == CHAT_ID
    assert extra["update_id"] == UPDATE_ID
    traceback = logging.Formatter().formatException(reported[0].exc_info)
    assert "NetworkError" in traceback
    assert "connection reset by peer" in traceback


async def test_handler_failure_is_logged_at_exception_level_with_context(
    start_update: UpdateFactory,
    fake_context: Context,
    app_records: LogRecords,
) -> None:
    """The `on_error` handler is a reporting sink, and never swallows quietly."""
    fake_context.error = NetworkError("upstream unavailable")

    await bot.on_error(start_update(), fake_context)

    failed = app_records.at_level(logging.ERROR)
    assert len(failed) == 1
    assert failed[0].levelname == "ERROR"
    assert failed[0].exc_info is not None, "an exception-level record must carry its traceback"
    extra = app_records.extra_of(failed[0])
    assert extra["event"] == "handler_failed"
    assert extra["chat_id"] == CHAT_ID
    assert extra["update_id"] == UPDATE_ID
    assert "upstream unavailable" in _everything_visible(failed[0])


async def test_a_failure_with_no_update_at_all_is_still_logged_loudly(
    fake_context: Context,
    app_records: LogRecords,
) -> None:
    """A polling failure arrives with `update=None`; it must not vanish."""
    fake_context.error = TimeoutError("getUpdates timed out")

    await bot.on_error(None, fake_context)

    failed = app_records.at_level(logging.ERROR)
    assert len(failed) == 1
    extra = app_records.extra_of(failed[0])
    assert extra["event"] == "handler_failed"
    assert extra["chat_id"] is None
    assert extra["update_id"] is None
    assert "getUpdates timed out" in _everything_visible(failed[0])


async def test_the_error_handler_logs_loudly_even_when_there_is_no_exception(
    fake_context: Context,
    app_records: LogRecords,
) -> None:
    """No exception means no traceback to attach - but never silence."""
    fake_context.error = None

    await bot.on_error(None, fake_context)

    failed = app_records.at_level(logging.ERROR)
    assert len(failed) == 1
    assert app_records.extra_of(failed[0])["event"] == "handler_failed"


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_three_handlers_are_registered_plus_one_error_handler(pipeline: Any) -> None:
    """R9.3: `/start`, `/restart`, one catch-all, and `on_error`.

    Three rather than one is the whole point of D10 - but three is also all.
    There is deliberately no per-phase handler, because there is no per-phase
    behaviour.
    """
    application = bot.build_application(FAKE_BOT_TOKEN, pipeline)

    handlers = [handler for group in application.handlers.values() for handler in group]
    assert len(handlers) == 3

    commands = {
        handler.commands
        for handler in handlers
        if getattr(handler, "commands", None) is not None
    }
    assert commands == {frozenset({"start"}), frozenset({"restart"})}

    catch_all = next(
        handler for handler in handlers if getattr(handler, "commands", None) is None
    )
    assert catch_all.callback is bot.on_message

    assert len(application.error_handlers) == 1
    assert next(iter(application.error_handlers)) is bot.on_error


def test_the_two_command_callbacks_are_the_documented_ones(pipeline: Any) -> None:
    application = bot.build_application(FAKE_BOT_TOKEN, pipeline)

    by_command: dict[frozenset[str], Any] = {}
    for group in application.handlers.values():
        for handler in group:
            commands = getattr(handler, "commands", None)
            if commands is not None:
                by_command[commands] = handler.callback

    assert by_command[frozenset({"start"})] is bot.on_start
    assert by_command[frozenset({"restart"})] is bot.on_restart


def test_the_message_handler_is_registered_on_non_commands(pipeline: Any) -> None:
    """R9.3: one handler covering text, photos and unsupported media alike."""
    application = bot.build_application(FAKE_BOT_TOKEN, pipeline)

    handlers = [handler for group in application.handlers.values() for handler in group]
    catch_all = next(h for h in handlers if getattr(h, "commands", None) is None)

    assert catch_all.callback is bot.on_message
    # `~filters.COMMAND` is what keeps /start and /restart on their own handlers,
    # so neither command can fall through to the decision table twice.
    assert catch_all.filters is not None


def test_the_pipeline_is_attached_to_the_application(pipeline: Any) -> None:
    """D10: the hub is injected, so `bot.py` never constructs one."""
    application = bot.build_application(FAKE_BOT_TOKEN, pipeline)

    assert application.pipeline is pipeline


def test_build_application_is_configured_for_long_polling(pipeline: Any) -> None:
    """An `Updater` is what makes `run_polling` possible; no webhook is set."""
    application = bot.build_application(FAKE_BOT_TOKEN, pipeline)

    assert application.updater is not None


# --------------------------------------------------------------------------
# R4.4 - no secret reaches a log record
# --------------------------------------------------------------------------


async def test_no_log_record_anywhere_contains_the_bot_token(
    start_update: UpdateFactory,
    make_update: UpdateFactory,
    app_records: LogRecords,
    pipeline: Any,
) -> None:
    """Every path - greeting, malformed, no message, failure - is token-free.

    Driven through the handler the application actually registered, with the
    token from a real `build_application` call, so the assertion covers the
    wiring and not just `on_start` in isolation.
    """
    application = bot.build_application(FAKE_BOT_TOKEN, pipeline)
    registered = next(
        handler
        for group in application.handlers.values()
        for handler in group
        if getattr(handler, "commands", None) == frozenset({"start"})
    )
    context = FakeContext(pipeline=pipeline)

    await registered.callback(start_update(), context)

    malformed = start_update(update_id=5002, chat={"id": "nope", "type": "private"})
    await registered.callback(malformed, context)

    await registered.callback(make_update(update_id=5003), context)

    failing = FakeContext(
        ExplodingTelegramBot(NetworkError("connection reset")), pipeline=pipeline
    )
    update = start_update()
    with pytest.raises(NetworkError):
        await registered.callback(update, failing)
    failing.error = NetworkError("connection reset")
    await next(iter(application.error_handlers))(update, failing)

    assert app_records.records, "the sweep must actually have produced log records"
    for record in app_records.records:
        assert FAKE_BOT_TOKEN not in _everything_visible(record)


def test_the_reply_text_constants_carry_no_credential() -> None:
    """Retargeted at the hub's strings (D10), plus the adapter's own fallback.

    Neither place can become somewhere a token hides: every string here is
    sent verbatim to the user, so a credential in any of them would be
    published straight into the chat.
    """
    from telegram_documentaries import pipeline as pipeline_module

    for text in (
        pipeline_module.WELCOME,
        pipeline_module.PHOTO_REQUEST,
        pipeline_module.SCRIPTED_NUDGE,
        pipeline_module.RESTARTED,
        pipeline_module.GENERIC_FAILURE,
        pipeline_module.SCRIPT_FAILED,
    ):
        assert re.search(r"\d{5,}:[\w-]{20,}", text) is None, text
