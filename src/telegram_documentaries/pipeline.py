"""The hub: one decision table, one reply per update (R9.1).

Everything the bot does is a row here. `bot.py` parses a Telegram update into an
:class:`~telegram_documentaries.contracts.InboundUpdate`, hands it over, and
sends back the single string this module returns - so the branch lives in the
domain layer, where it can be tested with no Telegram, no Gemini and no network
(R9.2).

Four rules govern the table:

**One send per update (R9.3).** `handle_*` returns one `str`, always. The
adapter sends it and nothing else.

**Nothing is raised into the user's flow (R9.5).** Every domain error - Gemini,
state transition, media - is caught at this boundary, logged, and answered with
a short human line. The user never sees a traceback, and never sees nothing.

**A photo arriving mid-interview is a fresh start (D4).** The three phases are
the three things a person can be doing; re-sending a selfie means "again", not
"no".

**A failed call holds the state (D3).** The answer is recorded only if the step
that consumes it succeeds, so a Gemini failure mid-interview leaves the session
byte-identical and the same question pending. Throwing away five good answers
because of a transient outage would be the worst available outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from telegram_documentaries import bouncer, interviewer, media, scripter, state
from telegram_documentaries.contracts import (
    InboundUpdate,
    PhotoAttachment,
    UnsupportedAttachment,
)
from telegram_documentaries.gemini import (
    GeminiClient,
    GeminiError,
)
from telegram_documentaries.observability import get_logger

if TYPE_CHECKING:
    pass

__all__ = [
    "PHOTO_MIME_TYPE",
    "WELCOME",
    "ConversationPipeline",
    "PhotoFetcher",
]

logger = get_logger(__name__)

#: Telegram's Bot API serves `photo` sizes as JPEG, always. Named rather than
#: sniffed: the bytes come from a fixed endpoint with a fixed type, and a magic
#: number check would be a second opinion about something already known.
PHOTO_MIME_TYPE = "image/jpeg"


#: The `/start` output (D10). Replaces `bot.GREETING`, whose claim that the
#: pipeline "is not built yet" is no longer true - and it must promise only what
#: exists, so the hybrid image and the voice note are not mentioned.
WELCOME = (
    "Welcome to Telegram Documentaries.\n\n"
    "Send me a portrait photo of a person and I will interview them for a "
    "wildlife documentary: five to seven questions, one at a time. When the "
    "interview is finished the narration arrives as a message.\n\n"
    "Send /restart at any time to start over."
)

PHOTO_REQUEST = (
    "I need a portrait photo of a person to begin. Send me one and I will "
    "introduce them to the studio."
)

SCRIPTED_NUDGE = (
    "That story is finished. Send me a new portrait photo and I will make "
    "another one, or /restart to begin again."
)

RESTARTED = (
    "All wiped - the session and the saved photo both. Send me a portrait "
    "photo of a person to begin again."
)

SCRIPT_FAILED = (
    "I could not write the narration just now, so nothing has been sent. "
    "Send /restart to begin again."
)

GENERIC_FAILURE = (
    "Something went wrong on my side. Send /restart to begin again."
)

CARRY_ON = (
    "I cannot use {kind} as an answer - carry on with the question above, or "
    "send /restart to start over."
)

UNSUPPORTED_AWAITING_PHOTO = (
    "That was {kind}, not a portrait photo. Send me a photo of a person and "
    "I will begin."
)

UNSUPPORTED_SCRIPTED = (
    "That was {kind}, and that story is already finished. Send me a new "
    "portrait photo and I will make another one."
)


@runtime_checkable
class PhotoFetcher(Protocol):
    """The port that gets photo bytes out of Telegram (R9.2).

    One method, `async`, taking an attachment and returning bytes. `bot.py`
    implements it with `get_file` and `download_as_bytearray`; tests implement
    it with a list. This is the only thing that makes the table testable with
    zero network access, and the reason the table can live in the domain layer
    instead of inside a Telegram handler.
    """

    async def fetch(self, attachment: PhotoAttachment) -> bytes: ...


class ConversationPipeline:
    """The decision table, and nothing else (R9.1).

    Args:
        client: The injected Gemini seam shared by all three stages.
        sessions: The session store. The only thing that touches session state.
        media: Where a judged portrait is written for the length of one run.
        fetcher: The photo port. Never `telegram.Bot` (R9.2).

    Note:
        One instance serves every chat. `SessionStore` keys by `chat_id` and
        takes no lock, which is correct because python-telegram-bot delivers
        one chat's updates sequentially (R3.4).
    """

    def __init__(
        self,
        *,
        client: GeminiClient,
        sessions: state.SessionStore,
        media: media.MediaStore,
        fetcher: PhotoFetcher,
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._media = media
        self._fetcher = fetcher

    # -- commands -----------------------------------------------------------

    async def handle_start(self, update: InboundUpdate) -> str:
        """`/start`: purge state and media, then ask for a portrait photo.

        The only row that applies at every phase, and the only one that discards
        a running interview.
        """
        return await self._command(update, reason="start")

    async def handle_restart(self, update: InboundUpdate) -> str:
        """`/restart`: purge state and media, from any phase (D4)."""
        return await self._command(update, reason="restart")

    async def handle_message(self, update: InboundUpdate) -> str:
        """Every other update: text, photo, or something unusable (R9.1)."""
        return await self._respond(update)

    # -- command rows -------------------------------------------------------

    async def _command(self, update: InboundUpdate, *, reason: str) -> str:
        chat_id, update_id = update.chat_id, update.update_id

        try:
            self._sessions.purge(chat_id, update_id=update_id)
            self._media.purge(chat_id=chat_id, update_id=update_id)
        except media.MediaError:
            # A purge failure must not stop the conversation: the session is
            # already gone, and the directory is removed next time (R5.3).
            logger.warning(
                "session_reset_degraded",
                extra={
                    "event": "session_reset_degraded",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "reason": reason,
                },
            )

        logger.info(
            "session_reset",
            extra={
                "event": "session_reset",
                "chat_id": chat_id,
                "update_id": update_id,
                "reason": reason,
            },
        )
        return WELCOME if reason == "start" else RESTARTED

    # -- the table ----------------------------------------------------------

    async def _respond(self, update: InboundUpdate) -> str:
        chat_id, update_id = update.chat_id, update.update_id

        try:
            current = self._sessions.fresh_or_load(chat_id, update_id=update_id)
            return await self._dispatch(update, current)
        except scripter.ScriptRejectedError:
            # D5: the corrective retry is spent, so say so plainly and point at
            # /restart. Not a generic failure - the user did nothing wrong, and
            # a vague apology would not tell them how to continue.
            logger.exception(
                "pipeline_step_failed",
                extra={
                    "event": "pipeline_step_failed",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "error_type": "ScriptRejectedError",
                },
            )
            return SCRIPT_FAILED
        except (GeminiError, state.SessionError, media.MediaError) as exc:
            # R9.5: the boundary. Every domain failure becomes one short human
            # line, and `exc` is logged as a typed object - never rendered into
            # the reply, and never rendered into the record with `str(exc)`.
            logger.exception(
                "pipeline_step_failed",
                extra={
                    "event": "pipeline_step_failed",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "stage": getattr(getattr(exc, "stage", None), "value", None),
                    "error_type": type(exc).__name__,
                },
            )
            return GENERIC_FAILURE

    async def _dispatch(
        self, update: InboundUpdate, current: state.SessionState
    ) -> str:
        if update.attachment is None:
            text = update.text or ""
            if not text.strip():
                return self._out_of_order(update, current)
            if current.phase is state.Phase.AWAITING_PHOTO:
                return self._out_of_order(update, current)
            if current.phase is state.Phase.SCRIPTED:
                return SCRIPTED_NUDGE
            return await self._record_answer(update, current)

        if isinstance(update.attachment, PhotoAttachment):
            return await self._handle_photo(update, update.attachment, current)

        # Everything above has returned, so what is left is the other arm of
        # `InboundAttachment` - mypy narrows it here rather than a runtime check
        # doing so. Deliberately no `else` and no fallback: adding a third arm
        # to the union becomes a "missing return" at type-check time rather than
        # a silent no-op at run time.
        return self._unsupported(update, update.attachment, current)

    # -- rows ---------------------------------------------------------------

    def _out_of_order(self, update: InboundUpdate, current: state.SessionState) -> str:
        """Text where a photo was expected: ask again, and call it what it is."""
        logger.info(
            "out_of_order_input",
            extra={
                "event": "out_of_order_input",
                "chat_id": update.chat_id,
                "update_id": update.update_id,
                "phase": current.phase.value,
                "payload": "text",
            },
        )
        return PHOTO_REQUEST

    def _unsupported(
        self,
        update: InboundUpdate,
        attachment: UnsupportedAttachment,
        current: state.SessionState,
    ) -> str:
        """Name the kind that arrived, then say what is wanted instead (R9.1)."""
        kind = attachment.media_kind.value

        logger.info(
            "unsupported_media",
            extra={
                "event": "unsupported_media",
                "chat_id": update.chat_id,
                "update_id": update.update_id,
                "media_kind": kind,
                "phase": current.phase.value,
            },
        )

        if current.phase is state.Phase.AWAITING_PHOTO:
            return UNSUPPORTED_AWAITING_PHOTO.format(kind=kind)
        if current.phase is state.Phase.SCRIPTED:
            return UNSUPPORTED_SCRIPTED.format(kind=kind)
        return CARRY_ON.format(kind=kind)

    async def _handle_photo(
        self,
        update: InboundUpdate,
        attachment: PhotoAttachment,
        current: state.SessionState,
    ) -> str:
        """The photo row: fetch, store, judge, then either reject or interview."""
        chat_id, update_id = update.chat_id, update.update_id

        # D4: a photo anywhere but `AWAITING_PHOTO` means "again". The old run's
        # media goes first so the new one cannot be mistaken for it.
        if current.phase is not state.Phase.AWAITING_PHOTO:
            self._media.purge(chat_id=chat_id, update_id=update_id)
            current = state.reset_session(current, chat_id=chat_id, update_id=update_id)

        try:
            data = await self._fetcher.fetch(attachment)
        except Exception:
            logger.exception(
                "photo_fetch_failed",
                extra={
                    "event": "photo_fetch_failed",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "error_type": "fetch",
                },
            )
            return GENERIC_FAILURE

        stored = self._media.save_photo(
            chat_id=chat_id,
            data=data,
            mime_type=PHOTO_MIME_TYPE,
            declared_byte_size=attachment.file_size,
        )
        current = current.model_copy(update={"photo": stored})

        verdict = await bouncer.judge(
            self._client,
            image=data,
            mime_type=stored.mime_type,
            chat_id=chat_id,
            update_id=update_id,
        )

        if not verdict.accepted:
            logger.info(
                "bouncer_rejected",
                extra={
                    "event": "bouncer_rejected",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "subject": verdict.subject,
                },
            )
            self._reset(chat_id=chat_id, update_id=update_id, reason="bouncer_rejected")
            return verdict.line or bouncer.BOUNCER_REJECTION_FALLBACK

        plan = await interviewer.plan(
            self._client,
            subject=verdict.subject,
            chat_id=chat_id,
            update_id=update_id,
        )
        begun = state.begin_interview(
            current, plan=plan, chat_id=chat_id, update_id=update_id
        )
        self._sessions.save(begun, update_id=update_id)

        first = interviewer.next_question(begun)
        if first is None:
            # Unreachable: `InterviewPlan` requires 5-7 questions, so there is
            # always a first. Answered rather than asserted - an empty plan is a
            # broken reply, and a broken reply gets the failure line (R9.5).
            logger.warning(
                "interview_plan_empty",
                extra={
                    "event": "interview_plan_empty",
                    "chat_id": chat_id,
                    "update_id": update_id,
                },
            )
            return GENERIC_FAILURE
        self._asked(
            chat_id=chat_id,
            update_id=update_id,
            position=1,
            total=len(plan.questions),
        )
        return first

    async def _record_answer(
        self, update: InboundUpdate, current: state.SessionState
    ) -> str:
        """Record one answer, then either ask the next question or write the script.

        The new state is *not* saved until the step that consumes it succeeds.
        That is what makes D3 work: if the scripter fails, the session still
        holds the previous answers and the same pending question, so the next
        message is consumed as the answer to the question already on screen.
        """
        chat_id, update_id = update.chat_id, update.update_id
        answer_text = (update.text or "").strip()
        answered = state.record_answer(
            current, answer=answer_text, chat_id=chat_id, update_id=update_id
        )

        if answered.pending_question is not None:
            self._sessions.save(answered, update_id=update_id)
            total = len(answered.plan.questions) if answered.plan else 0
            position = len(answered.answers) + 1
            logger.info(
                "answer_recorded",
                extra={
                    "event": "answer_recorded",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "position": position,
                    "total": total,
                },
            )
            return answered.pending_question

        return await self._write_script(update, answered)

    async def _write_script(
        self, update: InboundUpdate, answered: state.SessionState
    ) -> str:
        chat_id, update_id = update.chat_id, update.update_id
        plan = answered.plan
        if plan is None:
            # Unreachable: only a state with a plan can have exhausted it.
            logger.warning(
                "interview_plan_empty",
                extra={
                    "event": "interview_plan_empty",
                    "chat_id": chat_id,
                    "update_id": update_id,
                },
            )
            return GENERIC_FAILURE

        script = await scripter.write(
            self._client,
            plan=plan,
            answers=answered.answers,
            chat_id=chat_id,
            update_id=update_id,
        )
        finished = state.complete_interview(
            answered, script=script, chat_id=chat_id, update_id=update_id
        )
        self._sessions.save(finished, update_id=update_id)

        logger.info(
            "script_delivered",
            extra={
                "event": "script_delivered",
                "chat_id": chat_id,
                "update_id": update_id,
                "word_count": script.word_count,
            },
        )
        return script.text

    # -- helpers ------------------------------------------------------------

    def _reset(self, *, chat_id: int, update_id: int, reason: str) -> None:
        """Discard everything for one chat: state and media alike (R6.4)."""
        self._sessions.purge(chat_id, update_id=update_id)
        self._media.purge(chat_id=chat_id, update_id=update_id)
        logger.info(
            "session_reset",
            extra={
                "event": "session_reset",
                "chat_id": chat_id,
                "update_id": update_id,
                "reason": reason,
            },
        )

    @staticmethod
    def _asked(*, chat_id: int, update_id: int, position: int, total: int) -> None:
        logger.info(
            "question_asked",
            extra={
                "event": "question_asked",
                "chat_id": chat_id,
                "update_id": update_id,
                "position": position,
                "total": total,
            },
        )
