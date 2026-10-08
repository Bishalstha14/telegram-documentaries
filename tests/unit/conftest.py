"""Shared test doubles for the unit tier.

Two things every gateway test needs, kept here so the individual test modules
describe only their own behaviour:

* `make_update` - build a real `telegram.Update` from a raw payload dict via
  `Update.de_json(...)`, so tests exercise python-telegram-bot's actual parsing
  path instead of hand-built objects (R7).
* `FakeTelegramBot` / `fake_context` - the mock boundary at the network seam
  (R4.1, R7). Nothing here touches the network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import Bot, Update

from telegram_documentaries import observability

#: `LogRecord`'s own attributes, so a test can read back only the `extra` context.
_RESERVED_RECORD_ATTRS = (
    frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
)

#: Shape-valid but fictional. `Bot.__init__` performs no network call and no
#: token validation in python-telegram-bot 22.x, so this never leaves the process.
FAKE_BOT_TOKEN = "123456:AAFakeTestTokenForUnitTestsOnly0000000000"

#: The Telegram `Bot` object is the mock boundary (R7). One instance is shared by
#: every test that needs to parse a payload; it holds no per-test state.
_parsing_bot = Bot(token=FAKE_BOT_TOKEN)

UpdateFactory = Callable[..., Update]


@pytest.fixture
def make_update() -> UpdateFactory:
    """Return a factory turning a raw Telegram payload into an `Update`."""

    def factory(update_id: int = 4242, **payload: Any) -> Update:
        return Update.de_json({"update_id": update_id, **payload}, _parsing_bot)

    return factory


# --------------------------------------------------------------------------
# Fake Telegram context
# --------------------------------------------------------------------------


class FakeTelegramBot:
    """Records outbound `send_message` calls instead of performing them."""

    def __init__(self, username: str = "phase_one_test_bot") -> None:
        self.username = username
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, *, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        """Stand in for `telegram.Bot.send_message`."""
        message = {"chat_id": chat_id, "text": text, **kwargs}
        self.sent.append(message)
        return {"message_id": len(self.sent), **message}

    @property
    def last_chat_id(self) -> Any:
        """The chat id of the last outbound message, or None if nothing was sent."""
        return self.sent[-1]["chat_id"] if self.sent else None


class FakeContext:
    """The subset of `telegram.ext.CallbackContext` the handlers read.

    `application` carries the injected pipeline (D10). The handlers reach it
    through `context.application.pipeline`, exactly as they do in production,
    so a test exercises the same lookup rather than a shortcut around it.
    """

    def __init__(
        self,
        bot: FakeTelegramBot | None = None,
        *,
        pipeline: Any | None = None,
    ) -> None:
        self.bot = bot if bot is not None else FakeTelegramBot()
        self.error: BaseException | None = None
        self.application = SimpleNamespace(pipeline=pipeline)


@pytest.fixture
def fake_context() -> FakeContext:
    return FakeContext()


# --------------------------------------------------------------------------
# Log capture
#
# `caplog` cannot be used: `configure_logging()` sets `propagate = False` so the
# application never double-logs through the root logger, which would make
# visibility depend on test execution order. Records are therefore captured by a
# handler attached directly to the application logger.
# --------------------------------------------------------------------------


class LogRecorder(logging.Handler):
    """Collects the records the application actually emits."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def at_level(self, level: int) -> list[logging.LogRecord]:
        return [record for record in self.records if record.levelno == level]

    def events(self) -> list[str]:
        """The `event=` value of every record, in emission order."""
        return [self.extra_of(record).get("event") for record in self.records]

    @staticmethod
    def extra_of(record: logging.LogRecord) -> dict[str, Any]:
        """The `extra` context of one record, minus `LogRecord`'s own fields."""
        return {
            key: value
            for key, value in vars(record).items()
            if key not in _RESERVED_RECORD_ATTRS
        }

    def rendered(self) -> str:
        """Everything the records would show a human, as one blob of text."""
        return "\n".join(record.getMessage() for record in self.records)


@pytest.fixture
def app_records() -> Iterator[LogRecorder]:
    """Capture the application logger's output for the duration of one test."""
    recorder = LogRecorder()
    app_logger = observability.get_logger()
    app_logger.addHandler(recorder)
    previous_level = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    try:
        yield recorder
    finally:
        app_logger.removeHandler(recorder)
        app_logger.setLevel(previous_level)


# --------------------------------------------------------------------------
# Fake Gemini client
#
# The mock boundary for every stage: `generate` is the whole seam, so a fake
# that records its calls and returns a canned reply means no test ever opens a
# socket or spends a real API call. Stages receive this instead of
# `GenAiGeminiClient`.
# --------------------------------------------------------------------------


class FakeGeminiClient:
    """Records every `generate` call and returns whatever the test queued up.

    Args:
        replies: Replies to hand back, in order. A `BaseModel` is returned as
            is; anything else is returned as given, so a test can queue an error
            to raise or an off-schema value to reject.
        error: If set, every call raises this. Used for transport-failure paths.
    """

    def __init__(
        self,
        replies: list[Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def generate(
        self,
        request: Any,
        response_schema: type[Any],
        chat_id: int,
        update_id: int,
    ) -> Any:
        self.calls.append(
            {
                "request": request,
                "response_schema": response_schema,
                "chat_id": chat_id,
                "update_id": update_id,
            }
        )
        if self.error is not None:
            raise self.error
        if not self.replies:
            raise AssertionError(
                f"FakeGeminiClient ran out of replies after {len(self.calls)} call(s)"
            )
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture
def fake_gemini() -> FakeGeminiClient:
    """An empty fake; queue replies per test."""
    return FakeGeminiClient()



# --------------------------------------------------------------------------
# The photo fetch port (R9.2)
#
# The hub depends on this Protocol rather than on python-telegram-bot, which is
# what lets the whole decision table run with zero network access.
# --------------------------------------------------------------------------


class FakePhotoFetcher:
    """Returns queued bytes, or raises the queued failure.

    Args:
        data: Bytes to hand back, in order. When the queue empties the default
            blob is returned indefinitely, because a person can send a photo
            many times in one conversation and running out would be the fake
            failing rather than the code under test.
        error: If set, every fetch raises this. Used for the transport-failure
            path through the decision table.
    """

    DEFAULT = b"\xff\xd8fakejpeg\xff\xd9"

    def __init__(
        self,
        data: list[bytes] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.data = list(data or [])
        self.error = error
        self.fetched: list[Any] = []

    async def fetch(self, attachment: Any) -> bytes:
        self.fetched.append(attachment)
        if self.error is not None:
            raise self.error
        return self.data.pop(0) if self.data else self.DEFAULT


@pytest.fixture
def fake_fetcher() -> FakePhotoFetcher:
    """A fake fetcher with one queued photograph."""
    return FakePhotoFetcher()


@pytest.fixture
def pipeline(tmp_path: Path) -> Any:
    """A real `ConversationPipeline` wired entirely to fakes (R10).

    Real, not a stub: the adapter's contract is "call the hub and send what it
    returns", and a stub would assert only that the adapter calls a stub.
    """
    from telegram_documentaries.media import MediaStore
    from telegram_documentaries.pipeline import ConversationPipeline
    from telegram_documentaries.state import SessionStore

    return ConversationPipeline(
        client=FakeGeminiClient(),
        sessions=SessionStore(),
        media=MediaStore(base_dir=Path(tmp_path) / "media"),
        fetcher=FakePhotoFetcher(),
    )
