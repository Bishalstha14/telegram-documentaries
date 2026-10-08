"""The Interviewer: one plan call, then questions asked from local state (R7).

The roadmap promises "5-7 questions, one at a time, one Gemini call". Only the
first of those is a prompt's doing; the other two are structural here:

* **One call, total (R7.1/D2).** :func:`plan` is the only Gemini call this
  module makes. Everything after it is an index walk, so a whole interview costs
  one request whatever the answer count. The test suite drives a fake client
  through seven answers and then asserts it was used exactly once - without that,
  "one call" would be a comment rather than a cost guarantee.
* **One at a time (R7.3).** :func:`next_question` is a pure read over the plan
  and the answers already given. It takes no client, so an accidental model call
  during questioning is not possible.

The 5-7 rule lives on :class:`InterviewPlan` itself, so a reply carrying 3 or 9
questions fails R1.4.5 like any other off-schema reply. There is no string
counting anywhere in the hub.

The suggested animal is *not* used here. The Scripter is given the plan and the
accumulated answers and judges it itself (R7.4) - one fewer call, and it sees the
raw material rather than a lossy summary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from telegram_documentaries import observability
from telegram_documentaries.gemini import GeminiClient, GeminiRequest, Stage

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from telegram_documentaries.state import SessionState

__all__ = [
    "MAX_ANIMAL_LENGTH",
    "MAX_QUESTION_LENGTH",
    "InterviewPlan",
    "Question",
    "next_question",
    "plan",
]

#: A question must fit one Telegram message with room for the prompt above it.
MAX_QUESTION_LENGTH = 280
#: The Bouncer's subject feeds this, and is itself capped at 120 (R6.1).
MAX_ANIMAL_LENGTH = 60


def _non_blank(value: str) -> str:
    """Reject whitespace-only text; `min_length=1` alone accepts `'   '`."""
    if not value.strip():
        raise ValueError("must contain non-whitespace characters")
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class Question(_Frozen):
    """One interview question, asked verbatim."""

    text: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_LENGTH),
                    AfterValidator(_non_blank)]


class InterviewPlan(_Frozen):
    """The questions and the animal, decided in a single Gemini call (R7.2).

    The 5-7 rule is a *field* constraint, not a hub check, so an off-schema
    reply is rejected by the same path as every other malformed reply (R1.4.5).
    That is what keeps one rejection mechanism rather than two.
    """

    questions: tuple[Question, ...] = Field(min_length=5, max_length=7)
    suggested_animal: Annotated[
        str, Field(min_length=1, max_length=MAX_ANIMAL_LENGTH), AfterValidator(_non_blank)
    ]

    @field_validator("questions", mode="before")
    @classmethod
    def _accept_json_arrays(cls, value: object) -> object:
        """Accept what `json.loads` produces: an array is a `list`, never a tuple.

        Strict mode is right to reject a list for a `tuple[...]` field - it is
        what keeps this plan immutable - but a Gemini reply crosses the boundary
        as JSON, so the array arrives as a list and would otherwise be discarded
        as off-schema (R1.4.5), taking every well-formed interview with it.
        Coercing *before* validation keeps the field declared as
        `tuple[Question, ...]`, so `plan.questions.append(...)` still raises, the
        5-7 rule stays a field constraint, and `extra="forbid"` plus the
        non-blank check still run over each question afterwards.

        `list[Question]` would accept the reply too, and would be the wrong fix:
        it silences the error by letting a mutable list stand where the frozen
        model promises a tuple, voiding `_Frozen`'s guarantee while every
        existing test keeps passing.
        """
        if isinstance(value, list):
            return tuple(value)
        return value


logger = observability.get_logger(__name__)

SYSTEM_INSTRUCTION = (
    "You write the interview for a comedic wildlife documentary about a person. "
    "You are given a brief description of the subject. Write the questions the "
    "interviewer should ask them, one at a time, to find out what kind of animal "
    "they are. "
    "Reply with JSON only, no preamble and no markdown, of the form "
    '{"questions": [{"text": "..."}], "suggested_animal": "..."} where '
    '"questions" holds between 5 and 7 entries inclusive, each question is at '
    "most 280 characters, and each question is a real question you would ask a "
    "stranger. "
    "Ask open questions about behaviour, habits and temperament rather than "
    "yes-or-no questions. Do not ask for a name, age or occupation. "
    '"suggested_animal" is the animal you currently think fits them best, at '
    "most 60 characters."
)


async def plan(
    client: GeminiClient,
    *,
    subject: str,
    chat_id: int,
    update_id: int,
) -> InterviewPlan:
    """Build the interview plan. The Interviewer's only model call (R7.1).

    Args:
        client: The injected Gemini seam. Never the SDK itself (R1.2).
        subject: What the Bouncer said it saw, so the questions relate to the
            photo rather than being generic.
        chat_id: Telegram chat id, for log correlation only.
        update_id: Telegram update id, for log correlation only.

    Returns:
        A validated plan carrying 5-7 questions and a suggested animal.

    Raises:
        GeminiUnavailableError: Gemini could not answer.
        GeminiResponseError: A reply arrived but did not satisfy the schema -
            including one carrying 3 or 9 questions.

    Note:
        The `subject` is validated here rather than at the call site, so an
        empty Bouncer subject is refused before a request is built.
    """
    request = GeminiRequest(
        stage=Stage.INTERVIEWER,
        system_instruction=SYSTEM_INSTRUCTION,
        prompt=_prompt_for(subject),
    )

    planned = await client.generate(
        request, InterviewPlan, chat_id=chat_id, update_id=update_id
    )

    logger.info(
        "interview_planned",
        extra={
            "event": "interview_planned",
            "chat_id": chat_id,
            "update_id": update_id,
            "question_count": len(planned.questions),
            "suggested_animal": planned.suggested_animal,
        },
    )
    return planned


class _TurnPrompt(_Frozen):
    """The per-turn prompt, with the subject validated by the model itself.

    `GeminiRequest.prompt` only enforces `min_length=1`, which a whitespace-only
    Bouncer subject would satisfy - and a blank subject would make every plan
    generic, which is exactly what R7.1 exists to prevent. Giving the subject its
    own validated field reuses the same validator as every other non-blank string
    here, instead of hand-rolling a check that could drift from the rest.
    """

    subject: Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]


def _prompt_for(subject: str) -> str:
    """Build the per-turn prompt, refusing a blank subject before any request.

    Raising a `ValidationError` rather than a bare `ValueError` keeps one error
    type for "you gave me something unusable" across the whole codebase.
    """
    validated = _TurnPrompt(subject=subject)
    return (
        f"The Bouncer saw: {validated.subject}\n\n"
        "Write the interview for this person."
    )


def next_question(state_now: SessionState) -> str | None:
    """The question to ask now, or `None` once the plan is exhausted (R7.3).

    Pure and client-free by construction: it reads the pending question from
    state, which :func:`telegram_documentaries.state.record_answer` advances. No
    Gemini call happens between answers, so "one at a time" is a property of this
    function rather than of a prompt's good manners.

    Args:
        state_now: The current session state.

    Returns:
        The pending question, or `None` when every question has been answered.
    """
    return state_now.pending_question
