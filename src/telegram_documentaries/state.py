"""The session store and the declared state machine (R3, R4).

State is what turns five independent Gemini calls into a conversation. Two
properties matter more than the rest, and both are the point of this module:

* **Isolation.** One user's answers must never reach another's dossier. The
  store is keyed by ``chat_id`` and exposes no access to its backing dict, so
  callers cannot reach around it (R3.2).
* **The transition table is the contract.** R4.2 declares it here, so the table
  is enforced rather than documented. A row cannot be listed and left
  unimplemented, and an undeclared pair cannot be taken.

Because :class:`SessionState` is frozen, every transition is a function that
returns a *new* state. A failure part-way through therefore cannot leave a state
half-mutated - which is what makes the illegal-transition guard meaningful
rather than decorative.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
)

from telegram_documentaries import observability
from telegram_documentaries.interviewer import InterviewPlan, Question

logger = observability.get_logger(__name__)

__all__ = [
    "LEGAL_TRANSITIONS",
    "SESSION_VERSION",
    "Answer",
    "InterviewPlan",
    "Phase",
    "Question",
    "Script",
    "SessionError",
    "SessionState",
    "SessionStore",
    "SessionTransitionError",
    "SessionVersionError",
    "StoredPhoto",
    "begin_interview",
    "complete_interview",
    "record_answer",
    "reset_session",
]

#: The schema version. Bumping this is the only supported way to change the shape:
#: states written at any other version are discarded rather than migrated (R3.3).
SESSION_VERSION = 1


class Phase(StrEnum):
    """Where a conversation stands. Exactly three members, declared by R4.1."""

    AWAITING_PHOTO = "AWAITING_PHOTO"
    AWAITING_ANSWER = "AWAITING_ANSWER"
    SCRIPTED = "SCRIPTED"


#: The one legal-transition table (R4.2). `SCRIPTED` is terminal: it is left only
#: for `AWAITING_PHOTO`, by `/start`, `/restart` or a new photo. Every pair not
#: listed here raises `SessionTransitionError` (R4.3).
LEGAL_TRANSITIONS: frozenset[tuple[Phase, Phase]] = frozenset(
    {
        (Phase.AWAITING_PHOTO, Phase.AWAITING_PHOTO),
        (Phase.AWAITING_PHOTO, Phase.AWAITING_ANSWER),
        (Phase.AWAITING_ANSWER, Phase.AWAITING_ANSWER),
        (Phase.AWAITING_ANSWER, Phase.SCRIPTED),
        # "any → AWAITING_PHOTO" in R4.2 is a full row, not just the two
        # obvious cases: a photo arriving mid-interview purges and restarts too,
        # which is how a user abandons a stale interview by sending a new one.
        (Phase.AWAITING_ANSWER, Phase.AWAITING_PHOTO),
        (Phase.SCRIPTED, Phase.AWAITING_PHOTO),
    }
)

_PLAN_QUESTION_BOUNDS = (5, 7)


class _Frozen(BaseModel):
    """Base for every state value: frozen, strict, and hostile to extra keys."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


def _non_blank(value: str) -> str:
    """Reject whitespace-only text.

    `min_length=1` alone accepts `"   "`, which would let a blank question, a
    blank answer or an empty narration through as if it were content. The
    project's own config guard has the same shape and the same reason: a
    whitespace-only value is missing, not present.
    """
    if not value.strip():
        raise ValueError("must contain non-whitespace characters")
    return value


class Answer(_Frozen):
    """One answered question.

    The question is stored alongside the answer, and `record_answer` insists it
    matches the question actually asked - so a dossier can never contain a
    question the user was never asked (R3.5).
    """

    question: Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]
    answer: Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]


class Script(_Frozen):
    """The finished narration and its measured length."""

    text: Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]
    word_count: int = Field(ge=1)


class StoredPhoto(_Frozen):
    """Where a saved portrait lives, and what it is.

    Shared with `media.py` so the media store's opinion and the session's opinion
    of a photo are the same typed value (R5.2).
    """

    path: str = Field(min_length=1)
    byte_size: int = Field(ge=1)
    mime_type: str = Field(min_length=1)


class SessionState(_Frozen):
    """Everything one chat's conversation consists of (R3.1).

    Frozen, so "updating" state means producing a new instance and a failed
    transition cannot leave a half-changed session behind.
    """

    version: int = SESSION_VERSION
    chat_id: StrictInt
    # Defaults, so a new session needs no special case anywhere (R3.2).
    phase: Phase = Phase.AWAITING_PHOTO
    photo: StoredPhoto | None = None
    plan: InterviewPlan | None = None
    pending_question: str | None = None
    answers: tuple[Answer, ...] = ()
    script: Script | None = None


class SessionError(Exception):
    """Base for both state failures, so one `except` can catch the pair."""


class SessionTransitionError(SessionError):
    """An illegal transition was attempted (R4.3).

    Carries the from-phase, to-phase and triggering event so the caller can log
    all three without re-deriving them. Never coerced into a legal transition
    and never swallowed.

    An unknown or version-mismatched `chat_id` is *not* this error - that is
    R3.2/R3.3, a fresh session, not a broken one.
    """

    def __init__(
        self,
        *,
        from_phase: Phase,
        to_phase: Phase,
        chat_id: int,
        event: str = "unknown",
    ) -> None:
        self.from_phase = from_phase
        self.to_phase = to_phase
        self.chat_id = chat_id
        self.event = event
        super().__init__(
            f"{event} is not legal from {from_phase.value} to {to_phase.value}"
            f" (chat_id={chat_id})"
        )


class SessionVersionError(SessionError):
    """Stored state carries a version this build cannot read (R3.3).

    Discarded, never migrated. No migration code ships in this phase and none is
    stubbed; the mechanism is what stops a future schema change corrupting a
    live session.
    """

    def __init__(self, *, found_version: int, chat_id: int) -> None:
        self.found_version = found_version
        self.expected_version = SESSION_VERSION
        self.chat_id = chat_id
        super().__init__(
            f"session for chat_id={chat_id} has version {found_version},"
            f" this build speaks {SESSION_VERSION}"
        )


class SessionStore:
    """The only thing that touches the session dict (R3.2).

    In-memory, keyed by `chat_id`, exposing `load`, `save` and `purge` and no
    access to its backing mapping, so no module can bypass the version check or
    reach another chat's state.

    **Single-threaded by design (R3.4).** No locks are taken. python-telegram-bot
    delivers one chat's updates sequentially under default settings, which is
    sufficient today; Phase 7 is where this is revisited if
    `max_concurrent_updates` is ever raised. Recorded so the absence of locking
    is not read as an oversight.
    """

    def __init__(self) -> None:
        self._states: dict[int, SessionState] = {}

    def load(self, chat_id: int, *, update_id: int) -> SessionState:
        """Return the stored state, or a fresh one if the chat is unknown.

        Args:
            chat_id: Telegram chat id.
            update_id: Telegram update id, for log correlation only.

        Returns:
            The stored `SessionState`, or a fresh `AWAITING_PHOTO` one, so a
            brand-new user needs no special case (R3.2).

        Raises:
            SessionVersionError: The stored state is from another schema version.
                The caller is expected to fall back to a fresh session (R3.3).
        """
        stored = self._states.get(chat_id)
        if stored is None:
            return fresh_state(chat_id)
        if stored.version != SESSION_VERSION:
            logger.warning(
                "session_version_discarded",
                extra={
                    "event": "session_version_discarded",
                    "chat_id": chat_id,
                    "found_version": stored.version,
                    "expected_version": SESSION_VERSION,
                    "update_id": update_id,
                },
            )
            raise SessionVersionError(found_version=stored.version, chat_id=chat_id)
        return stored

    def fresh_or_load(self, chat_id: int, *, update_id: int) -> SessionState:
        """`load`, but a version mismatch yields a fresh state instead of raising.

        This is the hub's R3.3 path: an unreadable session is treated exactly
        like an unknown `chat_id`, because that is what it is.
        """
        try:
            return self.load(chat_id, update_id=update_id)
        except SessionVersionError:
            return fresh_state(chat_id)

    def save(self, to_save: SessionState, *, update_id: int) -> None:
        """Store a state for its own `chat_id`.

        Args:
            to_save: The state to store. Its `chat_id` is the key, so a state
                can never be filed under another chat's id.
            update_id: Telegram update id, for log correlation only.
        """
        self._states[to_save.chat_id] = to_save

    def purge(self, chat_id: int, *, update_id: int) -> None:
        """Forget one chat entirely. Other chats are untouched.

        Args:
            chat_id: The chat to forget.
            update_id: Telegram update id, for log correlation only.
        """
        self._states.pop(chat_id, None)


def fresh_state(chat_id: int) -> SessionState:
    """A new session awaiting a photo."""
    return SessionState(chat_id=chat_id, phase=Phase.AWAITING_PHOTO)


def _guard(
    state_now: SessionState,
    to_phase: Phase,
    chat_id: int,
    event: str,
) -> None:
    """Raise unless `to_phase` is declared reachable from the current phase."""
    if (state_now.phase, to_phase) in LEGAL_TRANSITIONS:
        return
    logger.warning(
        "transition_rejected",
        extra={
            "event": "transition_rejected",
            "chat_id": chat_id,
            "from_phase": state_now.phase.value,
            "to_phase": to_phase.value,
            "trigger": event,
        },
    )
    raise SessionTransitionError(
        from_phase=state_now.phase, to_phase=to_phase, chat_id=chat_id, event=event
    )


def begin_interview(
    state_now: SessionState,
    *,
    plan: InterviewPlan,
    chat_id: int,
    update_id: int,
    event: str = "bouncer_accepted",
) -> SessionState:
    """`AWAITING_PHOTO` → `AWAITING_ANSWER`, with the plan and first question.

    Args:
        state_now: The current state. Its `photo` must already be set, since the
            Bouncer judged it.
        plan: The interview plan, with 5-7 questions.
        chat_id: Telegram chat id, for the guard's error.
        update_id: Telegram update id, for log correlation only.
        event: What triggered this, reported on rejection.

    Returns:
        A new state awaiting the first answer.

    Raises:
        SessionTransitionError: The Bouncer accepted from a phase that cannot
            begin an interview - notably `SCRIPTED`, which is terminal (R4.3).
    """
    _guard(state_now, Phase.AWAITING_ANSWER, chat_id, event)
    return state_now.model_copy(
        update={
            "phase": Phase.AWAITING_ANSWER,
            "plan": plan,
            "pending_question": plan.questions[0].text,
        }
    )


def record_answer(
    state_now: SessionState,
    *,
    answer: str,
    chat_id: int,
    update_id: int,
    question: str | None = None,
    event: str = "answer_received",
) -> SessionState:
    """Append one answer and move to the next question (R3.5).

    Args:
        state_now: A state in `AWAITING_ANSWER`.
        answer: The user's reply. Must be non-blank.
        chat_id: Telegram chat id, for the guard's error.
        update_id: Telegram update id, for log correlation only.
        question: The question this answers. Defaults to the pending question;
            passing anything else is rejected, so the recorded question is always
            the one actually asked.
        event: What triggered this, reported on rejection.

    Returns:
        A new state with the answer appended and the next question pending, or no
        question pending once the plan is exhausted.

    Raises:
        SessionTransitionError: No question was pending, the supplied question
            does not match it, the answer is blank, or the plan is already
            exhausted (R4.3).
    """
    _guard(state_now, Phase.AWAITING_ANSWER, chat_id, event)

    pending = state_now.pending_question
    if pending is None:
        raise SessionTransitionError(
            from_phase=state_now.phase,
            to_phase=Phase.AWAITING_ANSWER,
            chat_id=chat_id,
            event=f"{event}:plan_exhausted",
        )
    if question is not None and question != pending:
        raise SessionTransitionError(
            from_phase=state_now.phase,
            to_phase=Phase.AWAITING_ANSWER,
            chat_id=chat_id,
            event=f"{event}:question_mismatch",
        )
    if not answer.strip():
        raise SessionTransitionError(
            from_phase=state_now.phase,
            to_phase=Phase.AWAITING_ANSWER,
            chat_id=chat_id,
            event=f"{event}:blank_answer",
        )

    plan = state_now.plan
    if plan is None:
        raise SessionTransitionError(
            from_phase=state_now.phase,
            to_phase=Phase.AWAITING_ANSWER,
            chat_id=chat_id,
            event=f"{event}:no_plan",
        )

    # The plan's questions in order, minus those already answered. `pending` is
    # always the first of these - answering a question already asked is
    # rejected above - so the next question is simply the second.
    asked = {a.question for a in state_now.answers}
    remaining = [q.text for q in plan.questions if q.text not in asked]
    if not remaining or remaining[0] != pending:
        # The pending question is not the next unanswered one, which would mean
        # the stored state and its plan disagree. Refused rather than guessed at.
        raise SessionTransitionError(
            from_phase=state_now.phase,
            to_phase=Phase.AWAITING_ANSWER,
            chat_id=chat_id,
            event=f"{event}:plan_disagrees_with_state",
        )

    return state_now.model_copy(
        update={
            "answers": (*state_now.answers, Answer(question=pending, answer=answer)),
            "pending_question": remaining[1] if len(remaining) > 1 else None,
        }
    )


def complete_interview(
    state_now: SessionState,
    *,
    script: Script,
    chat_id: int,
    update_id: int,
    event: str = "script_ready",
) -> SessionState:
    """`AWAITING_ANSWER` → `SCRIPTED`, storing the finished narration.

    Args:
        state_now: A state in `AWAITING_ANSWER`.
        script: The narration to store.
        chat_id: Telegram chat id, for the guard's error.
        update_id: Telegram update id, for log correlation only.
        event: What triggered this, reported on rejection.

    Returns:
        A new terminal state.

    Raises:
        SessionTransitionError: The phase is wrong, or questions are still
            pending - completing early would silently truncate the dossier
            (R4.3).
    """
    _guard(state_now, Phase.SCRIPTED, chat_id, event)
    if state_now.pending_question is not None:
        raise SessionTransitionError(
            from_phase=state_now.phase,
            to_phase=Phase.SCRIPTED,
            chat_id=chat_id,
            event=f"{event}:answers_incomplete",
        )
    return state_now.model_copy(update={"phase": Phase.SCRIPTED, "script": script})


def reset_session(
    state_now: SessionState,
    *,
    chat_id: int,
    update_id: int,
    event: str = "restart",
) -> SessionState:
    """Any phase → a fresh `AWAITING_PHOTO`, discarding everything.

    The only way out of `SCRIPTED`, and the path `/start` and `/restart` both
    take. Answers and any saved script are dropped rather than kept for a
    re-run: a new run must not inherit a previous one's dossier.
    """
    return fresh_state(chat_id)
