"""The Bouncer: the vision gate at the front of the pipeline (R6).

The Bouncer is the only stage whose input is wholly unfiltered - the user's photo
and nothing else - so it is written around two containment rules.

**The verdict is an enum, never a parsed string.** The reply is validated against
the :class:`BouncerVerdict` schema by :mod:`gemini`, then compared as a
:class:`Verdict`. Matching free text would mean a model rewording its answer
would silently change the behaviour of the gate (R6.1).

**The model's cheeky line is contained.** Its output is bounded to 300 characters
by the schema, and :data:`BOUNCER_REJECTION_FALLBACK` is substituted whenever the
line is blank or over-long. The photo is the user's one unfiltered input, and it
must not be able to put arbitrary text in front of them (R6.2).

**`UNSURE` is accepted** (D6). Failing open is deliberate: a wrongly rejected
portrait costs the user the entire experience, while a wrongly accepted one just
means a slightly odd interview.

This module judges and returns. It does not reset the session, does not purge
media and does not reply - that is the hub's decision table (R6.4), and keeping
the responsibilities apart is what makes both testable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from telegram_documentaries import observability
from telegram_documentaries.gemini import GeminiClient, GeminiRequest, Stage

logger = observability.get_logger(__name__)

__all__ = [
    "BOUNCER_REJECTION_FALLBACK",
    "BouncerVerdict",
    "Verdict",
    "judge",
]

#: Used whenever the model's line is unusable. Local, fixed, and within
#: Telegram's limit, so a rejection always says *something* (R6.2).
BOUNCER_REJECTION_FALLBACK = (
    "I am afraid that is not a person. Send me a portrait photo of a human "
    "and I will tell your story."
)

#: R6.1. `subject` is what the model says it saw.
_SUBJECT_MAX = 120
#: R6.2. The cheeky line must fit comfortably in one Telegram message.
_LINE_MAX = 300


def _non_blank(value: str) -> str:
    """Reject whitespace-only text; `min_length=1` alone accepts `'   '`."""
    if not value.strip():
        raise ValueError("must contain non-whitespace characters")
    return value


class Verdict(StrEnum):
    """What the Bouncer decided. The load-bearing part of R6.1."""

    HUMAN = "HUMAN"
    NOT_HUMAN = "NOT_HUMAN"
    UNSURE = "UNSURE"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BouncerVerdict(_Frozen):
    """The Bouncer's typed reply.

    `line` carries the model's cheeky rejection, but it is *never* trusted
    directly: :func:`judge` substitutes :data:`BOUNCER_REJECTION_FALLBACK` unless
    the line is present and within bounds (R6.2).
    """

    verdict: Verdict
    subject: Annotated[
        str, Field(min_length=1, max_length=_SUBJECT_MAX), AfterValidator(_non_blank)
    ]
    #: The cheeky rejection. Deliberately *not* non-blank: R6.2 handles a blank
    #: line by substituting the local fallback, so the schema must let one
    #: through for `judge` to act on.
    line: Annotated[str, Field(max_length=_LINE_MAX)] = ""

    @property
    def accepted(self) -> bool:
        """`HUMAN` and `UNSURE` both continue to the interview (R6.3).

        Written as a property rather than left to the caller, so the D6 decision
        has exactly one home and cannot drift between the hub and the tests.
        """
        return self.verdict in {Verdict.HUMAN, Verdict.UNSURE}


#: The Bouncer's persona and its hard rules. The allowed values are named in the
#: prompt so the reply is a choice among three tokens rather than a coin toss.
SYSTEM_INSTRUCTION = (
    "You are the doorman at a nature documentary studio. You look at one photo "
    "and decide whether it shows a human being. Reply with JSON only, no "
    "preamble and no markdown, of the form "
    '{"verdict": "...", "subject": "...", "line": "..."} where "verdict" is '
    "exactly one of HUMAN, NOT_HUMAN, UNSURE. "
    "Use HUMAN only when a person is clearly present, NOT_HUMAN when it is "
    "clearly something else, and UNSURE when the photo is too dark, too blurry, "
    "too cropped or genuinely ambiguous. "
    'In "subject", describe in at most 120 characters what you actually see. '
    'In "line", if and only if the verdict is not HUMAN, write one cheeky, kind, '
    "under-300-character sentence addressing the subject - as if you were a "
    "polite but unmistakably unimpressed doorman. Never be cruel, never use "
    "slurs, never make assumptions about the person. If the verdict is HUMAN, "
    "leave \"line\" as an empty string."
)


def _line_for(verdict: BouncerVerdict) -> str:
    """R6.2: use the model's line only when it is sane, else the local fallback.

    The schema already bounds `line` to 300 characters, so the length check here
    is defence in depth rather than dead code: a future bound change cannot turn
    unbounded model output into a rejection message.
    """
    line = verdict.line
    if not line.strip():
        return BOUNCER_REJECTION_FALLBACK
    if len(line) > _LINE_MAX:
        return BOUNCER_REJECTION_FALLBACK
    return line


async def judge(
    client: GeminiClient,
    *,
    image: bytes,
    mime_type: str,
    chat_id: int,
    update_id: int,
) -> BouncerVerdict:
    """Decide whether this photo is a person.

    Args:
        client: The injected Gemini seam. Never the SDK itself (R1.2).
        image: The portrait bytes.
        mime_type: What those bytes claim to be.
        chat_id: Telegram chat id, for log correlation only.
        update_id: Telegram update id, for log correlation only.

    Returns:
        The typed verdict, with `line` normalised onto a sane value (R6.2).

    Raises:
        GeminiUnavailableError: Gemini could not answer. The caller keeps the
            session as it was (R6.5).
        GeminiResponseError: A reply arrived and could not be trusted. Logged at
            `exception` by the boundary, and re-raised rather than swallowed
            (R6.5).

    Note:
        This function does not reset state and does not purge media. A rejected
        photo is the hub's decision to make (R6.4), so `judge` stays pure
        apart from its one Gemini call.
    """
    request = GeminiRequest(
        stage=Stage.BOUNCER,
        system_instruction=SYSTEM_INSTRUCTION,
        prompt=(
            "Here is the photo. Decide whether it shows a human being and reply "
            "with the JSON object described in your instructions."
        ),
        image=image,
        image_mime_type=_as_image_mime(mime_type),
    )

    verdict = await client.generate(
        request, BouncerVerdict, chat_id=chat_id, update_id=update_id
    )

    if verdict.verdict is Verdict.UNSURE:
        # D6 accepted, but the uncertainty is recorded so a pattern of UNSURE
        # replies is visible in the logs rather than invisible behind success.
        logger.warning(
            "bouncer_unsure",
            extra={
                "event": "bouncer_unsure",
                "chat_id": chat_id,
                "update_id": update_id,
                "subject": verdict.subject,
            },
        )

    logger.info(
        "bouncer_judged",
        extra={
            "event": "bouncer_judged",
            "chat_id": chat_id,
            "update_id": update_id,
            "verdict": verdict.verdict.value,
            "subject": verdict.subject,
            "accepted": verdict.accepted,
        },
    )

    # `line` is re-emitted through `_line_for` so what the caller sees is always
    # usable, whatever the model returned inside the bounds.
    return verdict.model_copy(update={"line": _line_for(verdict)})


def _as_image_mime(mime_type: str) -> str:
    """Narrow a `str` onto the request's `ImageMimeType` literal.

    The media store has already admitted only `image/jpeg` and `image/png`
    (R5.2), so anything else reaching here is a programming error rather than
    user input - and it is reported as one instead of being coerced.
    """
    if mime_type in {"image/jpeg", "image/png"}:
        return mime_type
    raise ValueError(f"unsupported photo type for the Bouncer: {mime_type}")
