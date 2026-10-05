"""The typed contract at the Telegram boundary.

Nothing that arrives from Telegram reaches application logic directly. Every
update is parsed into an :class:`InboundUpdate` first, and that model is frozen,
ignores unknown fields, and - this is the load-bearing part - **refuses to
coerce**.

Why coercion is treated as a defect rather than a convenience
-------------------------------------------------------------
python-telegram-bot performs no validation on ``chat.id``; it hands back
whatever JSON contained. Pydantic's default (lax) validation would turn
``"-1001234567890"`` into the integer ``-1001234567890`` and look like it
succeeded. A chat id that is sometimes an ``int`` and sometimes a ``str`` is
precisely the defect that silently breaks session lookups once persistent state
lands. So a payload arriving at an illegal state is rejected explicitly, here,
at the edge - never silently repaired.

Two failure modes, deliberately distinct:

* **No message** (``edited_message``, a poll, a reaction) - a normal Phase 1
  no-op. Logged at debug and reported as ``None``; the caller returns early.
* **A malformed message** (no chat, or ``chat.id`` not an integer) - an
  ``InvalidInboundUpdateError`` is raised. The caller decides how to tell the
  user; it must never invent a default chat id to keep going.

Why ``validation_error_fields`` lives here
------------------------------------------
Any pydantic ``ValidationError`` renders the offending ``input_value``, so
printing ``str(exc)`` leaks whatever was in the input - including a sibling
secret. That is true of a missing ``.env`` key and equally true of an
off-schema Gemini reply, which is why this module owns the one safe reader and
both callers reuse it (D7).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError
from telegram import Update

from telegram_documentaries import observability

__all__ = ["InboundUpdate", "InvalidInboundUpdateError", "validation_error_fields"]

logger = observability.get_logger("contracts")


def validation_error_fields(exc: ValidationError) -> tuple[str, ...]:
    """Return only the offending field *names*, de-duplicated, in order.

    Never return ``str(exc)`` to a caller, a log or stdout. For a missing key
    pydantic renders the whole input mapping, which includes the sibling
    secret's value::

        1 validation error for Settings
        gemini_api_key
          Field required [type=missing,
          input_value={'telegram_bot_token': 'SUPERSECRET'}, input_type=dict]

    ``ValidationError.errors()`` is the only safe source: it carries the same
    field locations, and this function reads nothing else from it.

    Args:
        exc: The validation failure to summarise. Only field *locations* are read.

    Returns:
        The offending field names, in report order and without duplicates. An
        error with no location contributes nothing - a payload fragment must never
        stand in for a field name.
    """
    names: list[str] = []
    for error in exc.errors():
        location = error.get("loc", ())
        if location:
            names.append(str(location[0]))
    return tuple(dict.fromkeys(names))


class InvalidInboundUpdateError(ValueError):
    """An inbound update cannot be trusted and has been rejected.

    A :class:`ValueError` so that broad ``except ValueError`` handling elsewhere
    can never mistake it for a routine, ignorable failure. ``update_id`` and a
    value-free ``reason`` are exposed for logging; the identifier doubles as the
    correlation key for the offending request.
    """

    def __init__(self, reason: str, *, update_id: int) -> None:
        super().__init__(f"update {update_id}: {reason}")
        self.reason = reason
        self.update_id = update_id


class InboundUpdate(BaseModel):
    """A Telegram update: validated, typed and immutable.

    ``StrictInt`` rather than ``int``: in lax mode pydantic would accept the
    string ``"42"``. ``StrictInt`` keeps the promise that a value annotated
    ``int`` really *is* an ``int`` however the model was constructed - the second
    line of defence behind the explicit check in :meth:`from_telegram`.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: Telegram update identifier, and the log correlation key.
    update_id: StrictInt
    #: Telegram chat identifier. Typed ``int``, never ``str`` - see module docs.
    chat_id: StrictInt
    #: Message text, when the message carries any.
    text: str | None = None

    @classmethod
    def from_telegram(cls, update: Update) -> InboundUpdate | None:
        """Parse one Telegram update, or report that it carries no message.

        Args:
            update: A real ``telegram.Update``, as parsed by python-telegram-bot.

        Returns:
            The typed update, or ``None`` when there is no message to act on.

        Raises:
            InvalidInboundUpdateError: The message exists but cannot be trusted - no
                chat, or a ``chat.id`` that is absent or is not an integer. A
                missing id is rejected through this same error rather than
                surfacing as an ``AttributeError``.
        """
        message = update.message
        if message is None:
            logger.debug(
                "inbound_update_ignored",
                extra={
                    "event": "inbound_update_ignored",
                    "update_id": update.update_id,
                    "reason": "no message in update",
                },
            )
            return None

        chat = message.chat
        if chat is None:
            raise InvalidInboundUpdateError(
                "message has no chat to reply to", update_id=update.update_id
            )

        # Widened to `object` deliberately: python-telegram-bot annotates
        # `Chat.id` as `int` but does not enforce it, so the type checker must not
        # be allowed to conclude that the checks below are dead code.
        #
        # `getattr` with a `None` default, not a plain attribute access: a chat
        # object missing `id` outright would otherwise raise `AttributeError`,
        # which escapes this module's contract and lands in the generic error
        # handler. `None` falls through to the check below, so an absent id is
        # rejected through the same typed error as a wrongly-typed one.
        raw_chat_id: object = getattr(chat, "id", None)
        if isinstance(raw_chat_id, bool) or not isinstance(raw_chat_id, int):
            raise InvalidInboundUpdateError(
                f"chat.id must be an int, got {type(raw_chat_id).__name__}",
                update_id=update.update_id,
            )

        return cls(update_id=update.update_id, chat_id=raw_chat_id, text=_text_of(message))


def _text_of(message: object) -> str | None:
    """The message text, normalised to ``str | None``.

    A missing text and an explicit ``None`` both mean "no text". Anything that
    is not a string is reported as no text rather than coerced, and the model
    validates whatever is left.
    """
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else None
