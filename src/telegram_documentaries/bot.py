"""The `/start` gateway: the bot's only behaviour in Phase 1.

Everything here exists to honour three rules, in order of importance.

**The reply leaves through `context.bot.send_message`.**
`update.effective_message.reply_text(...)` would be shorter, and it is the wrong
call twice over. It hands a live library object to the handler instead of the
typed :class:`~telegram_documentaries.contracts.InboundUpdate`, which is exactly
the raw-payload leakage TECH.md forbids; and it hides the network call behind the
message object, so the only way to test the handler is to let it reach Telegram.
Sending through the `Bot` puts the mock boundary at the network seam, and the
handler becomes testable with zero network access (R7).

**A bad payload is loud in the logs and silent to the user.**
`InvalidInboundUpdateError` is caught, logged with the offending `update_id`,
and answered with nothing. No crash, no raise, and above all no invented default
chat id to "keep going" - a wrong answer is worse than no answer.

**An exception escaping a handler is never silent.**
An `error` handler reports it at `exception` level with the `chat_id` and
`update_id` needed to find the request in Telegram's own logs. There is no
per-stage user-facing degradation here; that is Phase 7 (R4.3).

Two smaller decisions worth stating, because neither is obvious:

* `@logged` decorates the *send*, not the whole handler. The decorator always
  stamps `chat_id`, `update_id` and `duration_ms`, and timing the network call is
  the number actually worth having. The separate `start_command_received` record
  is emitted the instant the payload is validated, before any I/O.
* The `TypeAlias` block below exists only because strict `mypy` refuses a bare
  `Application`. python-telegram-bot's generics are six deep; the exact shape the
  builder produces is spelled out once, here, instead of in every signature.
"""

from __future__ import annotations

from typing import TypeAlias

from telegram import Update
from telegram.ext import Application, CallbackContext, CommandHandler, ExtBot, JobQueue

from telegram_documentaries import observability
from telegram_documentaries.contracts import InboundUpdate, InvalidInboundUpdateError

__all__ = ["GREETING", "GatewayApplication", "build_application", "on_error", "on_start"]

logger = observability.get_logger("bot")

#: The single documented reply to `/start`. It must not overclaim: as of Phase 1
#: the pipeline does not exist, and a greeting that promises a documentary would
#: be a lie the user finds out about by sending a photo.
GREETING = (
    "Hello. The Telegram Documentaries bot is alive and talking to Telegram.\n\n"
    "Right now that is all it does. The portrait-photo pipeline - the interview, "
    "the hybrid animal portrait, the narration and the voice note - is not built "
    "yet and arrives in a later phase."
)

# See the module docstring for why these exist.
_Bot: TypeAlias = ExtBot[None]
_Context: TypeAlias = CallbackContext[_Bot, dict[str, object], dict[str, object], dict[str, object]]

#: Public: `__main__` registers a `post_init` hook, and a hook typed against a
#: bare `Application` would not satisfy strict `mypy`.
GatewayApplication: TypeAlias = Application[
    _Bot,
    _Context,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    JobQueue[_Context],
]


@observability.logged("start_reply_sent")
async def _send_greeting(chat_id: int, update_id: int, context: _Context) -> None:
    """Send :data:`GREETING` to `chat_id` and time the call.

    The parameter names are the point: `observability.logged` reads `chat_id` and
    `update_id` off the call by name, so every `start_reply_sent` record carries
    both correlation ids plus `duration_ms`, with no extra wiring.

    If the send raises, the decorator logs the traceback and re-raises; the
    application's `error` handler then reports `handler_failed`. Note that this
    means a failed send also produces an ERROR-level `start_reply_sent` record -
    the decorator is fixed contract, and `handler_failed` is what says the send
    actually failed.
    """
    await context.bot.send_message(chat_id=chat_id, text=GREETING)


async def on_start(update: Update, context: _Context) -> None:
    """Greet the user who sent `/start`.

    Args:
        update: The raw Telegram update. Untrusted until `from_telegram` says so.
        context: The python-telegram-bot callback context. Only `context.bot` is
            read, because that is the network seam.

    Returns:
        Nothing, and nothing is raised. A payload that cannot be trusted produces
        a warning and no reply rather than a crash (R4.2).
    """
    try:
        inbound = InboundUpdate.from_telegram(update)
    except InvalidInboundUpdateError as exc:
        logger.warning(
            "inbound_update_invalid",
            extra={
                "event": "inbound_update_invalid",
                "update_id": exc.update_id,
                # `exc.reason` is built from field names and type names only, so
                # it cannot carry payload values into the log.
                "reason": exc.reason,
            },
        )
        return

    if inbound is None:
        # An update with no message (a poll, a reaction, an edited message).
        # `inbound_update_ignored` is already logged at DEBUG by the contract.
        return

    logger.info(
        "start_command_received",
        extra={
            "event": "start_command_received",
            "chat_id": inbound.chat_id,
            "update_id": inbound.update_id,
        },
    )
    await _send_greeting(chat_id=inbound.chat_id, update_id=inbound.update_id, context=context)


async def on_error(update: object, context: _Context) -> None:
    """Report an exception that escaped any handler (R4.3).

    Args:
        update: The update being handled, or `None` for a failure that happened
            during polling itself. Typed as `object` because that is all
            python-telegram-bot guarantees.
        context: Carries the exception on `context.error`.

    Note:
        `exc_info` is passed explicitly rather than relying on the ambient
        `sys.exc_info()`: this handler is invoked by python-telegram-bot's error
        loop, not from inside an `except` block, so there would be no traceback
        to pick up. `logger.exception` still means "error level, with the
        traceback attached", which is what "logged at exception level" asks for.
    """
    error = context.error
    chat_id, update_id = _correlation_ids(update)
    extra = {"event": "handler_failed", "chat_id": chat_id, "update_id": update_id}

    if error is None:
        # python-telegram-bot always sets `context.error` before calling here. If
        # that ever stops being true, say so loudly rather than returning quietly.
        logger.error("handler_failed", extra=extra)
        return

    logger.exception("handler_failed", exc_info=error, extra=extra)


def _correlation_ids(update: object) -> tuple[int | None, int | None]:
    """Best-effort `(chat_id, update_id)` for a failure log. Never raises.

    Used only to label a record on a path that has *already* failed, so a value
    that cannot be trusted is reported as `None` - not guessed at, not coerced,
    and never used to address a reply.
    """
    if not isinstance(update, Update):
        return None, None

    # Widened to `object` deliberately: python-telegram-bot annotates both ids as
    # `int` but does not enforce it, and `warn_unreachable` must not be allowed to
    # decide these checks are dead code. See `contracts.from_telegram`.
    chat = update.effective_chat
    raw_chat_id: object = chat.id if chat is not None else None
    raw_update_id: object = update.update_id

    chat_id = raw_chat_id if isinstance(raw_chat_id, int) else None
    update_id = raw_update_id if isinstance(raw_update_id, int) else None
    return chat_id, update_id


def build_application(token: str) -> GatewayApplication:
    """Build the long-polling application with its single `/start` handler.

    Args:
        token: The bot token. Already validated as non-blank by `Settings`; the
            value itself is never logged (R4.4).

    Returns:
        An `Application` carrying an `Updater`, so `run_polling` works. Exactly
        one handler is registered - `/start` - plus one `error` handler. There is
        deliberately no catch-all for other messages: out-of-order input handling
        is Phase 2 (R4).
    """
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", on_start))
    application.add_error_handler(on_error)
    return application
