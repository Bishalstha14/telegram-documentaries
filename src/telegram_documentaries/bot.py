"""The Telegram adapter: parse, call the hub, send (R9.3).

Three handlers and one error handler. That is the whole surface.

There is deliberately **no per-phase handler**. There is no per-phase behaviour -
the phase is a field on the session, and the table that reads it lives in
:mod:`telegram_documentaries.pipeline`. Each handler does exactly three things:
parse the update through :meth:`InboundUpdate.from_telegram`, hand it to
``pipeline.handle_*``, and send the single reply that comes back. That reply is
now one of two shapes: plain text, or a :class:`VoiceNote` the adapter uploads
as a voice note. One *reply* per update, always - never two sends. If the voice
upload fails, the note's own ``fallback_text`` is sent as the one reply instead;
the failed upload is not retried.

Four rules, in order of how much they cost to get wrong:

**The reply leaves through `context.bot.send_message`.**
``update.effective_message.reply_text(...)`` is shorter and wrong twice over. It
hands a live library object to the handler instead of the typed
:class:`~telegram_documentaries.contracts.InboundUpdate` - the raw-payload
leakage TECH.md forbids - and it hides the network call behind the message
object, so the only way to test the handler is to let it reach Telegram.

**A bad payload is loud in the logs and silent to the user.**
`InvalidInboundUpdateError` is caught, logged with the offending `update_id`,
and answered with nothing. No crash, no raise, and above all no invented default
chat id: a wrong answer is worse than no answer.

**A failed send never advances the session.**
The session is only ever mutated inside the hub, and the hub returns text rather
than sending. If the send itself raises, the decorator logs it and the error
handler reports it - and the conversation is left exactly where it was, on a
reply nobody received.

**An exception escaping a handler is never silent.**
``on_error`` reports it at ``exception`` level with the `chat_id` and
`update_id` needed to find the request in Telegram's own logs.

Two smaller decisions worth stating:

* `@logged` decorates the *send*, not the whole handler. The decorator always
  stamps `chat_id`, `update_id` and `duration_ms`, and timing the network call is
  the number actually worth having.
* The `TypeAlias` block exists only because strict `mypy` refuses a bare
  `Application`. python-telegram-bot's generics are six deep; the exact shape the
  builder produces is spelled out once, here, instead of in every signature.
"""

from __future__ import annotations

import io
from typing import TypeAlias

from telegram import Bot, File, Update
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ExtBot,
    JobQueue,
    MessageHandler,
    filters,
)

from telegram_documentaries import observability
from telegram_documentaries.contracts import (
    InboundUpdate,
    InvalidInboundUpdateError,
    PhotoAttachment,
    VoiceNote,
)
from telegram_documentaries.pipeline import ConversationPipeline

__all__ = [
    "GatewayApplication",
    "TelegramPhotoFetcher",
    "build_application",
    "on_error",
    "on_message",
    "on_restart",
    "on_start",
]

logger = observability.get_logger("bot")

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


class TelegramPhotoFetcher:
    """The real implementation of the hub's `PhotoFetcher` port (R9.2).

    The only place in the codebase that talks to Telegram's file API. The hub
    sees a `PhotoAttachment` and gets bytes back; how those bytes travel is not
    its business, which is what makes its decision table testable offline.
    """

    def __init__(self, client: Bot) -> None:
        self._bot = client

    async def fetch(self, attachment: PhotoAttachment) -> bytes:
        """Download `attachment` and hand back its bytes.

        Returns:
            The photo's bytes. Telegram serves `photo` sizes as JPEG.

        Raises:
            Whatever `python-telegram-bot` raises on a failed download. The hub
            catches it at its boundary and answers with a short line (R9.5);
            this method does not translate the error, because a translated error
            would be one more place a message can be written and get out of step
            with the others.
        """
        telegram_file: File = await self._bot.get_file(attachment.file_id)
        return bytes(await telegram_file.download_as_bytearray())


@observability.logged("reply_sent")
async def _send(chat_id: int, update_id: int, context: _Context, text: str) -> None:
    """Send one reply and time the call.

    The parameter names are the point: `observability.logged` reads `chat_id` and
    `update_id` off the call by name, so every `reply_sent` record carries both
    correlation ids plus `duration_ms`, with no extra wiring.

    If the send raises, the decorator logs the traceback and re-raises; the
    application's `error` handler then reports `handler_failed`. Note that a
    failed send therefore also produces an ERROR-level `reply_sent` record - the
    decorator is a fixed contract, and `handler_failed` is what says the send
    actually failed.
    """
    await context.bot.send_message(chat_id=chat_id, text=text)


@observability.logged("voice_note_sent")
async def _send_voice(
    chat_id: int, update_id: int, context: _Context, note: VoiceNote
) -> None:
    """Upload one voice note and time the call (R4.2).

    The parameter names are the point, exactly as for :func:`_send`:
    `observability.logged` reads `chat_id` and `update_id` off the call by name,
    so every `voice_note_sent` record carries both correlation ids plus
    `duration_ms`.

    The bytes are wrapped in `io.BytesIO` because Telegram rejects a bare
    `bytes` for a filename-bearing upload. The note is **bare** - no caption, no
    duplicated text (D1). If the upload raises, the decorator logs the traceback
    and re-raises; :func:`_dispatch` catches that and sends the note's own
    `fallback_text`, so the narration still arrives (R4.3).
    """
    await context.bot.send_voice(chat_id=chat_id, voice=io.BytesIO(note.data))


async def _dispatch(
    update: Update, context: _Context, *, entry: str
) -> None:
    """Parse, call the hub, send. The only shape any handler has (R9.3)."""
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

    pipeline = _pipeline(context)
    if pipeline is None:
        # No pipeline injected: `build_application` is the only constructor and
        # it always injects one, so this means the application was assembled by
        # hand. Said loudly rather than answered with a crash mid-handler.
        logger.error(
            "pipeline_missing",
            extra={
                "event": "pipeline_missing",
                "chat_id": inbound.chat_id,
                "update_id": inbound.update_id,
                "handler": entry,
            },
        )
        return

    logger.info(
        f"{entry}_received",
        extra={
            "event": f"{entry}_received",
            "chat_id": inbound.chat_id,
            "update_id": inbound.update_id,
        },
    )

    if entry == "start":
        reply = await pipeline.handle_start(inbound)
    elif entry == "restart":
        reply = await pipeline.handle_restart(inbound)
    else:
        reply = await pipeline.handle_message(inbound)

    # One reply per update, in one of two shapes (R4.1). A `VoiceNote` is
    # uploaded; if that upload fails the note's own text is sent instead - the
    # same single reply, delivered over the other transport (R4.3, D9/D10). The
    # failed upload is never retried; a different transport is used.
    if isinstance(reply, VoiceNote):
        try:
            await _send_voice(
                chat_id=inbound.chat_id,
                update_id=inbound.update_id,
                context=context,
                note=reply,
            )
        except Exception as exc:
            logger.warning(
                "voice_note_send_failed",
                extra={
                    "event": "voice_note_send_failed",
                    "chat_id": inbound.chat_id,
                    "update_id": inbound.update_id,
                    # Class-name only: `str(exc)` may carry the payload and is
                    # never rendered into a record (R4.3).
                    "error_type": type(exc).__name__,
                },
            )
            await _send(
                chat_id=inbound.chat_id,
                update_id=inbound.update_id,
                context=context,
                text=reply.fallback_text,
            )
    else:
        await _send(
            chat_id=inbound.chat_id, update_id=inbound.update_id, context=context, text=reply
        )


def _pipeline(context: _Context) -> ConversationPipeline | None:
    """Read the injected hub out of the application (D10).

    Kept in one place so the lookup has a single type annotation, and so a
    missing pipeline is one check rather than three.
    """
    application = context.application
    return getattr(application, "pipeline", None)


async def on_start(update: Update, context: _Context) -> None:
    """Handle `/start`: purge and ask for a portrait photo.

    Args:
        update: The raw Telegram update. Untrusted until `from_telegram` says so.
        context: The callback context. Only `context.bot` and the injected
            pipeline are read.

    Returns:
        Nothing, and nothing is raised. A payload that cannot be trusted produces
        a warning and no reply rather than a crash (R4.2).
    """
    await _dispatch(update, context, entry="start")


async def on_restart(update: Update, context: _Context) -> None:
    """Handle `/restart`: wipe the session and the saved photo (D4)."""
    await _dispatch(update, context, entry="restart")


async def on_message(update: Update, context: _Context) -> None:
    """Handle everything that is not a command: text, photos, other media.

    Registered on `~filters.COMMAND`, so one handler covers every payload the
    decision table has a row for (R9.3). There is no per-phase handler because
    there is no per-phase behaviour - the phase is a field the hub reads.
    """
    await _dispatch(update, context, entry="message")


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


def build_application(
    token: str, pipeline: ConversationPipeline
) -> GatewayApplication:
    """Build the long-polling application with its three handlers (D10).

    Args:
        token: The bot token. Already validated as non-blank by `Settings`; the
            value itself is never logged (R4.4).
        pipeline: The hub. Injected rather than constructed here, so `bot.py`
            depends on the domain rather than building it, and so a test can
            pass a pipeline wired to fakes.

    Returns:
        An `Application` carrying an `Updater`, so `run_polling` works.

    Note:
        Registration order is load-bearing. The two `CommandHandler`s are added
        first, so `/start` and `/restart` are matched by them; the catch-all is
        registered on `~filters.COMMAND`, which excludes both. One decision
        table, no handler that has to know about phases.
    """
    application = Application.builder().token(token).build()
    # Attached to the application rather than closed over: the handlers receive
    # it through `context.application`, which keeps every callback a plain
    # module-level function that a test can call directly.
    application.pipeline = pipeline  # type: ignore[attr-defined]
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("restart", on_restart))
    application.add_handler(MessageHandler(~filters.COMMAND, on_message))
    application.add_error_handler(on_error)
    return application
