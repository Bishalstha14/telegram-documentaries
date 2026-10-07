"""The Scripter: write the narration, then validate it locally (R8).

Two rules govern this module, and the second is the one that matters.

**The word count is computed here, not read from the model (R8.1).**
:func:`str.split` over whatever came back. A model that confidently reports
"87 words" over a 47-word reply is still rejected, because only the text is
counted.

**Nothing is coerced into being good enough (R8.3).** One corrective retry, which
restates the required length and reports the count just received. A second
failure is logged at `exception` and raised. No third attempt, no padding, no
truncation, no locally composed narration - a fallback would be a lie dressed as
the product.

Both bounds are inclusive: exactly 60 and exactly 90 words are accepted.

What this does *not* do, stated honestly (R8.4): validation covers structural
degeneracy only - empty, whitespace-only, wrong count, over Telegram's message
limit. Semantic off-topicness is not detected, because detecting it means either
a regex over model prose or a second model call to grade 80 words. The
corrective retry is the accepted mitigation. Recording that is more honest than
claiming the roadmap's "degenerate output is detected" is fully met.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

from telegram_documentaries import observability
from telegram_documentaries.gemini import (
    GeminiClient,
    GeminiRequest,
    Stage,
)
from telegram_documentaries.interviewer import InterviewPlan
from telegram_documentaries.state import Answer, Script

logger = observability.get_logger(__name__)

__all__ = [
    "MAX_TELEGRAM_MESSAGE",
    "MAX_WORDS",
    "MIN_WORDS",
    "Narration",
    "Script",
    "ScriptRejectedError",
    "write",
]

# `Script` is re-exported, not defined here: `state.SessionState` stores one, so
# the session and the stage must share a single definition - two would let the
# validators drift and the count that passed validation disagree with the count
# the session holds. See `state.Script`.


#: Telegram's message limit (R8.1). Anything longer cannot be delivered at all.
MAX_TELEGRAM_MESSAGE = 4096

#: The inclusive word bounds (R8.1).
MIN_WORDS = 60
MAX_WORDS = 90


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain non-whitespace characters")
    return value


class ScriptRejectedError(Exception):
    """The narration was the wrong length, or unusable (R8.1).

    Carries the actual count and both bounds so the corrective retry can report
    precisely what was wrong - a retry that says "try again, longer" is not
    corrective, it is a coin toss.

    Note:
        This carries a *count*, never the prose. A rejection log naming 47 is
        useful; a rejection log printing the narration is noise, and in a
        conversation about a real person it is also a leak.
    """

    def __init__(self, *, actual: int) -> None:
        self.actual = actual
        self.minimum = MIN_WORDS
        self.maximum = MAX_WORDS
        super().__init__(
            f"the narration is {actual} words; {MIN_WORDS}-{MAX_WORDS} required"
        )


SYSTEM_INSTRUCTION = (
    "You write the narration for a comedic wildlife documentary about a real "
    "person, in the voice of a British natural-history documentary - warm, "
    "grandiose, mildly absurd, never unkind. "
    "Reply with JSON only, no preamble and no markdown, of the form "
    '{"text": "..."} where "text" is a single paragraph of between 60 and 90 '
    "words inclusive, counted as whitespace-separated words. "
    "There must be no preamble, no sign-off, no heading and no emoji: only the "
    "narration, because it is sent to the viewer verbatim. "
    "The number of words matters exactly. Under 60 or over 90 is a rejected "
    "reply and will be sent back to you with the count you produced. "
    "Weave in what they actually said, and make the animal do the work of the "
    "joke rather than insulting them."
)


def _count(text: str) -> int:
    """The only word count in this module (R8.1).

    Whitespace splitting, no model-reported figure. Deliberately not a regular
    expression and not a second model call - counting words is not a problem
    that needs either.
    """
    return len(text.split())


def _reject(actual: int) -> ScriptRejectedError:
    """Build the rejection, and log it when it is final (R8.3)."""
    return ScriptRejectedError(actual=actual)


async def write(
    client: GeminiClient,
    *,
    plan: InterviewPlan,
    answers: tuple[Answer, ...],
    chat_id: int,
    update_id: int,
) -> Script:
    """Write the narration, validating it and retrying once (R8.2/R8.3).

    Args:
        client: The injected Gemini seam. Never the SDK itself (R1.2).
        plan: The interview plan, including `suggested_animal`.
        answers: The accumulated `Answer` models. The raw material - no
            summarisation call stands between them and this (R7.4).
        chat_id: Telegram chat id, for log correlation only.
        update_id: Telegram update id, for log correlation only.

    Returns:
        The validated script. Only what the model returned is ever returned;
        this module adds nothing around it (R8.2).

    Raises:
        ScriptRejectedError: Two consecutive replies were the wrong length. The
            message carries both counts.
        GeminiUnavailableError: Gemini could not answer. Not retried here -
            transport failures are Phase 7's concern, and retrying a timeout
            inside one handler would hold the chat open for nothing (R8.3).

    Note:
        Returning means success and raising means nothing at all was produced.
        There is no path on which a partially correct or locally composed
        narration reaches the caller.
    """
    prompt = _prompt_for(plan, answers)

    try:
        return await _call(client, prompt, chat_id=chat_id, update_id=update_id)
    except ScriptRejectedError as first:
        # One corrective retry (D5), and only one. A second failure raises out
        # of _call here and propagates: no third attempt, no padding.
        #
        # GeminiUnavailableError is deliberately absent from this handler. A
        # transport failure retried inside one handler holds the chat open for
        # nothing, and Phase 7 owns recovery.
        logger.info(
            "script length missed; correcting once",
            extra={
                "chat_id": chat_id,
                "update_id": update_id,
                "stage": Stage.SCRIPTER,
                "rejected_count": first.actual,
            },
        )
        try:
            return await _call(
                client,
                _retry_prompt(prompt, rejected=first.actual),
                chat_id=chat_id,
                update_id=update_id,
            )
        except ScriptRejectedError as second:
            # Two in a row is not a count miss any more, it is a failure worth
            # an operator's attention - hence `exception`, with both counts so
            # the drift is diagnosable from the log alone.
            logger.exception(
                "script rejected twice; no narration produced",
                extra={
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "stage": Stage.SCRIPTER,
                    "first_count": first.actual,
                    "second_count": second.actual,
                },
            )
            raise


async def _call(
    client: GeminiClient,
    prompt: str,
    *,
    chat_id: int,
    update_id: int,
) -> Script:
    """One model call, then local validation (R8.1)."""
    request = GeminiRequest(
        stage=Stage.SCRIPTER,
        system_instruction=SYSTEM_INSTRUCTION,
        prompt=prompt,
    )

    # The stage's schema requires only `text`; `word_count` is added here from
    # the local count, so a model-supplied count can never reach the validator.
    raw = await client.generate(
        request, Narration, chat_id=chat_id, update_id=update_id
    )
    try:
        script = _validated(raw.text)
    except ScriptRejectedError as rejected:
        # Whether this is retryable is decided by `write`; logging here is
        # unconditional so the count is never lost. `write` raises again after
        # the retry, which is where the second one becomes an incident.
        logger.info(
            "script_rejected_retryable",
            extra={
                "event": "script_rejected_retryable",
                "chat_id": chat_id,
                "update_id": update_id,
                "stage": Stage.SCRIPTER,
                "actual": rejected.actual,
            },
        )
        raise
    else:
        logger.info(
            "script_written",
            extra={
                "event": "script_written",
                "chat_id": chat_id,
                "update_id": update_id,
                "stage": Stage.SCRIPTER,
                "word_count": script.word_count,
            },
        )
        return script


def _validated(text: str) -> Script:
    """Count locally, then accept or reject (R8.1)."""
    actual = _count(text)
    if not text.strip():
        raise _reject(0)
    if actual < MIN_WORDS or actual > MAX_WORDS:
        raise _reject(actual)
    if len(text) > MAX_TELEGRAM_MESSAGE:
        raise _reject(actual)
    return Script(text=text, word_count=actual)


class Narration(BaseModel):
    """What the model is asked for: narration only, no count field (R8.1).

    Asking for a `word_count` would invite the model to report a number it did
    not produce, and then hand that number to the validator as if it were a
    measurement. The count is computed from `text` instead.

    Note:
        Deliberately *not* `Script`. `Script` requires a `word_count`, so using
        it as the response schema would make the model invent one, and then a
        model-supplied figure would sit in the same object as a locally
        computed one. These stay separate objects: the model owns `text`, this
        module owns the count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: Annotated[str, AfterValidator(_non_blank)]


def _prompt_for(plan: InterviewPlan, answers: tuple[Answer, ...]) -> str:
    """The single per-run prompt: the dossier, in full (R8.2)."""
    lines = [
        f"Suggested animal: {plan.suggested_animal}",
        "",
        "The interview:",
    ]
    lines.extend(f"Q: {answer.question}\nA: {answer.answer}" for answer in answers)
    lines.append("")
    lines.append("Write the narration now.")
    return "\n".join(lines)


def _retry_prompt(previous: str, *, rejected: int) -> str:
    """The corrective prompt: same material, plus the count that failed (D5).

    Restating the bounds *and* reporting the count turns a retry into a
    correction. A retry that only says "try again, longer" is not corrective,
    it is a coin toss - the model has no way to know which way it missed.
    """
    return (
        f"{previous}\n\n"
        f"That reply was {rejected} words. The requirement is between "
        f"{MIN_WORDS} and {MAX_WORDS} words inclusive. Rewrite it to land "
        "inside that range, counting whitespace-separated words yourself."
    )
