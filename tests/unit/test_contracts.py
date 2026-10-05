"""RED: the typed inbound contract at the Telegram boundary (R3).

Every value that crosses into the application passes through
`InboundUpdate.from_telegram`. The contract it enforces is that a malformed
payload is **rejected explicitly and never coerced** into something that looks
well-formed - a string `chat_id` that pydantic would happily turn into an int is
the exact defect this module exists to prevent.

Updates are built from raw payload dicts through `Update.de_json(...)` (R7), so
these tests exercise python-telegram-bot's real parsing path.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError
from telegram import Update

from telegram_documentaries import contracts
from telegram_documentaries.contracts import InboundUpdate, InvalidInboundUpdateError

#: `LogRecorder` from conftest; unannotated here because `tests/` is deliberately
#: outside mypy's scope (a loose test double would need casts under strict mode).
LogRecords = Any


def _message_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal, well-formed private-chat message payload."""
    chat: dict[str, Any] = {"id": -1001234567890, "type": "supergroup"}
    payload: dict[str, Any] = {
        "message_id": 17,
        "date": 1_700_000_000,
        "chat": chat,
        "from": {"id": 555, "is_bot": False, "first_name": "Ada"},
        "text": "/start",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_from_telegram_parses_a_well_formed_start_command(make_update: Any) -> None:
    update = make_update(update_id=9001, message=_message_payload())

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.update_id == 9001
    assert inbound.chat_id == -1001234567890
    assert inbound.text == "/start"


def test_from_telegram_keeps_a_negative_supergroup_chat_id(make_update: Any) -> None:
    """Telegram chat ids are negative for groups; a `str` there breaks Phase 3."""
    update = make_update(
        message=_message_payload(
            chat={"id": -1009876543210, "type": "supergroup"}
        )
    )

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.chat_id == -1009876543210
    assert isinstance(inbound.chat_id, int)


def test_from_telegram_returns_none_when_the_message_has_no_text(make_update: Any) -> None:
    """A photo with no caption is still a message; `text` is simply absent."""
    payload = _message_payload()
    del payload["text"]
    update = make_update(message=payload)

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.text is None


# --------------------------------------------------------------------------
# No message -> None, never an exception.
# --------------------------------------------------------------------------


def test_from_telegram_returns_none_when_the_update_has_no_message(make_update: Any) -> None:
    update = make_update(update_id=9002)

    assert InboundUpdate.from_telegram(update) is None


def test_from_telegram_returns_none_for_an_edited_message(make_update: Any) -> None:
    """Out-of-order input is Phase 2's problem; here it is simply ignored."""
    update = make_update(update_id=9003, edited_message=_message_payload())

    assert InboundUpdate.from_telegram(update) is None


def test_from_telegram_returns_none_for_a_poll_update(make_update: Any) -> None:
    poll = {
        "id": "pol-1",
        "question": "Cat or dog?",
        "options": [],
        "total_voter_count": 0,
        "is_closed": True,
        "is_anonymous": True,
        "type": "regular",
        "allows_multiple_answers": False,
        "allows_revoting": False,
        "members_only": False,
    }
    update = make_update(update_id=9004, poll=poll)

    assert InboundUpdate.from_telegram(update) is None


def test_a_message_less_update_is_ignored_at_debug_level(
    make_update: Any,
    app_records: LogRecords,
) -> None:
    update = make_update(update_id=9005)

    assert InboundUpdate.from_telegram(update) is None

    debug_records = app_records.at_level(logging.DEBUG)
    assert debug_records, "a message-less update must be logged, not silently dropped"
    context = app_records.extra_of(debug_records[-1])
    assert context["event"] == "inbound_update_ignored"
    assert context["update_id"] == 9005


# --------------------------------------------------------------------------
# Malformed input -> rejected explicitly, NEVER coerced.
# --------------------------------------------------------------------------


def test_from_telegram_raises_when_the_message_has_no_chat(make_update: Any) -> None:
    payload = _message_payload()
    del payload["chat"]
    update = make_update(update_id=9006, message=payload)

    with pytest.raises(InvalidInboundUpdateError) as excinfo:
        InboundUpdate.from_telegram(update)

    assert "9006" in str(excinfo.value)


def test_from_telegram_raises_when_chat_id_is_not_an_integer(make_update: Any) -> None:
    """The load-bearing case: python-telegram-bot does **not** validate `chat.id`.

    It hands back whatever JSON contained, so a string id reaches us verbatim.
    Lax pydantic validation would coerce `"-1001234567890"` into an int and hide
    the defect; this contract must reject it instead.
    """
    update = make_update(
        update_id=9007,
        message=_message_payload(chat={"id": "-1001234567890", "type": "supergroup"}),
    )
    assert isinstance(update.message.chat.id, str)  # premise: PTB passed it through

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_from_telegram_raises_when_chat_id_is_a_float(make_update: Any) -> None:
    update = make_update(
        update_id=9008,
        message=_message_payload(chat={"id": 1234.5, "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_from_telegram_raises_when_chat_id_is_a_boolean(make_update: Any) -> None:
    """`bool` subclasses `int`, so an `isinstance` check alone would let it through."""
    update = make_update(
        update_id=9009,
        message=_message_payload(chat={"id": True, "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_from_telegram_raises_when_chat_id_is_null(make_update: Any) -> None:
    update = make_update(
        update_id=9010,
        message=_message_payload(chat={"id": None, "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_a_rejected_chat_id_is_never_silently_replaced_by_a_default(make_update: Any) -> None:
    """There is no fallback: a malformed chat id yields no `InboundUpdate` at all."""
    update = make_update(
        update_id=9011,
        message=_message_payload(chat={"id": "nope", "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError) as excinfo:
        InboundUpdate.from_telegram(update)

    # The reason names the offending field and says nothing about a chosen value.
    assert "chat.id" in str(excinfo.value)
    assert "9011" in str(excinfo.value)


def test_from_telegram_raises_a_domain_error_not_a_pydantic_error(make_update: Any) -> None:
    """Callers catch `InvalidInboundUpdateError`; a `ValidationError` would escape them."""
    update = make_update(
        update_id=9012,
        message=_message_payload(chat={"id": "x", "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError) as excinfo:
        InboundUpdate.from_telegram(update)

    assert not isinstance(excinfo.value, ValidationError)


# --------------------------------------------------------------------------
# The model itself refuses to coerce, even when constructed directly.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chat_id", ["-1001234567890", 1234.5, True, None])
def test_inbound_update_refuses_to_coerce_a_non_integer_chat_id(chat_id: Any) -> None:
    """Second line of defence: `from_telegram` is not the only way in."""
    with pytest.raises(ValidationError):
        InboundUpdate(update_id=1, chat_id=chat_id, text=None)


def test_inbound_update_accepts_a_plain_integer_chat_id() -> None:
    inbound = InboundUpdate(update_id=1, chat_id=-100, text="/start")

    assert inbound.chat_id == -100
    assert isinstance(inbound.chat_id, int)


# --------------------------------------------------------------------------
# Model shape
# --------------------------------------------------------------------------


def test_inbound_update_is_frozen() -> None:
    """A contract value that mutated mid-flight would poison every later stage."""
    inbound = InboundUpdate(update_id=1, chat_id=42, text="/start")

    with pytest.raises(ValidationError):
        inbound.chat_id = 99  # type: ignore[misc]


def test_inbound_update_ignores_unknown_extra_fields() -> None:
    """Telegram adds fields without warning; they must not break parsing."""
    payload = _message_payload()
    payload["some_future_telegram_field"] = {"nested": True}
    update = Update.de_json({"update_id": 9013, "message": payload}, None)

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.chat_id == -1001234567890
    assert not hasattr(inbound, "some_future_telegram_field")


def test_inbound_update_requires_a_chat_id() -> None:
    with pytest.raises(ValidationError):
        InboundUpdate(update_id=1, text="/start")


def test_invalid_inbound_update_is_a_value_error() -> None:
    """So generic `except ValueError` handling can never swallow it silently."""
    assert issubclass(InvalidInboundUpdateError, ValueError)


def test_contracts_module_exports_only_the_public_surface() -> None:
    assert set(contracts.__all__) == {
        "InboundUpdate",
        "InvalidInboundUpdateError",
    }
