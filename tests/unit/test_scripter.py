"""RED: the Scripter - narration validated locally, one corrective retry (R8).

The Scripter is where the product is actually delivered, and where the
temptation to cheat is strongest: if the model returns 47 words, padding with a
sentence would produce something that looks fine and is not what was asked for.

So the two things pinned hardest here are:

* **The word count is computed locally (R8.1).** `str.split()` over what came
  back. No model-reported count is read, so a model that confidently says
  "87 words" over a 47-word reply is still rejected.
* **No coercion, ever (R8.3).** After one corrective retry the failure is
  logged and raised. The test drives two failures and asserts the bot produced
  *no script at all* - not a padded one, not a truncated one, not a locally
  written one. A fallback narration would be a lie dressed as the product.

Both boundaries are inclusive: exactly 60 and exactly 90 words are accepted, 59
and 91 are not.

Nothing here touches the network or a real credential.
"""

from __future__ import annotations

import logging

import pytest
from conftest import FakeGeminiClient
from pydantic import ValidationError

from telegram_documentaries import scripter
from telegram_documentaries.gemini import Stage
from telegram_documentaries.interviewer import InterviewPlan, Question
from telegram_documentaries.scripter import (
    MAX_WORDS,
    MIN_WORDS,
    Script,
    ScriptRejectedError,
    write,
)
from telegram_documentaries.state import Answer

CHAT = 8767055318
UPDATE = 1

ANIMAL = "sea otter"


def _words(count: int) -> str:
    return " ".join(f"word{n}" for n in range(count))


SIXTY = _words(60)
NINETY = _words(90)


def _plan() -> InterviewPlan:
    return InterviewPlan(
        questions=tuple(Question(text=f"Question {n}?") for n in range(5)),
        suggested_animal=ANIMAL,
    )


def _answers() -> tuple[Answer, ...]:
    return tuple(
        Answer(question=f"Question {n}?", answer=f"Answer {n}.") for n in range(5)
    )


def _script(text_or_count: str | int) -> Script:
    """A reply to hand the Scripter: either explicit text or a word count.

    Counting by hand rather than calling `_words` in every test keeps the
    intent readable - `_script(59)` says "a reply that is 59 words", which is
    exactly what the boundary test is about.
    """
    text = _words(text_or_count) if isinstance(text_or_count, int) else text_or_count
    return Script(text=text, word_count=len(text.split()))


@pytest.fixture
def fake() -> FakeGeminiClient:
    return FakeGeminiClient([_script(SIXTY)])


# --------------------------------------------------------------------------
# R8.1 - the count is local
# --------------------------------------------------------------------------


async def test_write_returns_a_script_with_a_locally_counted_word_count(
    fake: FakeGeminiClient,
) -> None:
    result = await write(
        fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE
    )

    assert isinstance(result, Script)
    assert result.word_count == 60
    assert result.word_count == len(result.text.split())


async def test_a_model_reported_word_count_is_ignored(
    fake: FakeGeminiClient,
) -> None:
    """The model's own count is never trusted, only `str.split()` over its text."""
    overclaim = Script.model_construct(text=_words(47), word_count=90)
    fake = FakeGeminiClient([overclaim, overclaim])

    with pytest.raises(ScriptRejectedError) as caught:
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert caught.value.actual == 47
    assert caught.value.actual != overclaim.word_count


async def test_write_accepts_exactly_sixty_words() -> None:
    fake = FakeGeminiClient([_script(SIXTY)])

    result = await write(
        fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE
    )

    assert result.word_count == MIN_WORDS == 60


async def test_write_accepts_exactly_ninety_words() -> None:
    fake = FakeGeminiClient([_script(NINETY)])

    result = await write(
        fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE
    )

    assert result.word_count == MAX_WORDS == 90


async def test_write_rejects_fifty_nine_words() -> None:
    """59 misses the inclusive lower bound, and still misses after the retry."""
    fake = FakeGeminiClient([_script(59), _script(59)])

    with pytest.raises(ScriptRejectedError) as caught:
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert caught.value.actual == 59
    assert caught.value.minimum == 60
    assert caught.value.maximum == 90
    assert fake.call_count == 2


async def test_write_rejects_ninety_one_words() -> None:
    fake = FakeGeminiClient([_script(91), _script(91)])

    with pytest.raises(ScriptRejectedError) as caught:
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert caught.value.actual == 91


def _unvalidated(text: str, count: int) -> Script:
    """A reply handed straight over, bypassing `Script`'s own validators.

    This restates what the boundary already guarantees so the stage's own check
    can be exercised on its own. Defence in depth, not a loophole: if the stage
    relied on the boundary, an empty narration reaching it would be a bug that
    only showed up in production.
    """
    return Script.model_construct(text=text, word_count=count)


async def test_write_rejects_an_empty_reply() -> None:
    """R8.1: an empty narration is not a narration, wherever it comes from."""
    fake = FakeGeminiClient([_unvalidated("", 0), _unvalidated("", 0)])

    with pytest.raises(ScriptRejectedError) as caught:
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert caught.value.actual == 0


async def test_write_rejects_a_whitespace_only_reply() -> None:
    blank = "   \n\t  "
    fake = FakeGeminiClient([_unvalidated(blank, 0), _unvalidated(blank, 0)])

    with pytest.raises(ScriptRejectedError):
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)


async def test_write_rejects_text_longer_than_the_telegram_message_limit() -> None:
    """4096 is Telegram's limit; longer than that cannot be sent at all.

    The count is deliberately in range: a 5000-word reply is not a length miss,
    it is undeliverable, and this is the check that tells the two apart.
    """
    over = " ".join(["x"] * 5000)
    fake = FakeGeminiClient([_unvalidated(over, 5000), _unvalidated(over, 5000)])

    with pytest.raises(ScriptRejectedError) as caught:
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert caught.value.actual == 5000
    assert len(over) > 4096


def test_the_word_bounds_are_the_declared_ones() -> None:
    assert (MIN_WORDS, MAX_WORDS) == (60, 90)


def test_the_script_model_is_frozen() -> None:
    script = _script(SIXTY)

    with pytest.raises(ValidationError):
        script.text = "something else"  # type: ignore[misc]


def test_a_script_with_a_blank_text_is_not_constructible() -> None:
    with pytest.raises(ValidationError):
        Script(text="   ", word_count=60)


def test_a_script_with_a_non_positive_count_is_not_constructible() -> None:
    with pytest.raises(ValidationError):
        Script(text=SIXTY, word_count=0)


# --------------------------------------------------------------------------
# R8.3 - exactly one corrective retry
# --------------------------------------------------------------------------


async def test_write_retries_once_and_reports_the_actual_count() -> None:
    """The second prompt carries the count that was rejected (D5)."""
    fake = FakeGeminiClient([_script(47), _script(SIXTY)])

    result = await write(
        fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE
    )

    assert result.word_count == 60
    assert fake.call_count == 2
    assert "47" in fake.calls[1]["request"].prompt


async def test_the_retry_restates_the_required_length() -> None:
    fake = FakeGeminiClient([_script(47), _script(SIXTY)])

    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    second_prompt: str = fake.calls[1]["request"].prompt
    assert "60" in second_prompt and "90" in second_prompt


async def test_a_success_needs_no_retry() -> None:
    fake = FakeGeminiClient([_script(SIXTY)])

    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 1


async def test_write_gives_up_after_one_retry() -> None:
    """No third attempt (R8.3)."""
    fake = FakeGeminiClient([_script(47), _script(51)])

    with pytest.raises(ScriptRejectedError):
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 2


async def test_a_second_failure_logs_at_exception_level(
    app_records,
) -> None:
    fake = FakeGeminiClient([_script(47), _script(51)])

    with pytest.raises(ScriptRejectedError):
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    errors = app_records.at_level(logging.ERROR)
    assert len(errors) == 1, "exactly one incident, after the retry, not before"


async def test_the_exception_log_reports_both_counts(app_records) -> None:
    fake = FakeGeminiClient([_script(47), _script(51)])

    with pytest.raises(ScriptRejectedError):
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    incident = app_records.at_level(logging.ERROR)[0]
    assert app_records.extra_of(incident)["first_count"] == 47
    assert app_records.extra_of(incident)["second_count"] == 51


async def test_write_never_pads_truncates_or_falls_back_to_local_text() -> None:
    """The no-coercion guard: after two failures there is no script at all.

    Asserted by returning `None` on failure. A padded or locally written
    narration would mean the bot delivered prose the model never produced - a
    lie dressed as the product (R8.3).
    """
    fake = FakeGeminiClient([_script(47), _script(51)])
    delivered: Script | None = None

    with pytest.raises(ScriptRejectedError):
        delivered = await write(
            fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE
        )

    # `delivered` is assigned only if write returns; it raised, so nothing was.
    assert delivered is None
    assert fake.call_count == 2


async def test_no_fallback_text_is_composed_locally() -> None:
    """There is no narration string in this module to fall back to (R8.3).

    Scans module-level constants: the only prose is the system instruction,
    which is *sent* rather than *delivered*. Anything else - a canned opening,
    a default script - would be a fallback, and a fallback would be a lie
    dressed as the product.
    """
    from telegram_documentaries import scripter as module

    prose = [
        (name, value)
        for name, value in vars(module).items()
        if isinstance(value, str)
        and not name.startswith("_")
        and value.count(" ") > 12
    ]

    assert [name for name, _ in prose] == ["SYSTEM_INSTRUCTION"]


# --------------------------------------------------------------------------
# R8.2 - what gets sent
# --------------------------------------------------------------------------


async def test_write_passes_the_plan_and_the_answers_to_gemini(
    fake: FakeGeminiClient,
) -> None:
    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    request = fake.calls[0]["request"]
    for answer in _answers():
        assert answer.answer in request.prompt


async def test_write_passes_the_suggested_animal_through(
    fake: FakeGeminiClient,
) -> None:
    """R7.4: this is the one place the suggestion is actually used."""
    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert ANIMAL in fake.calls[0]["request"].prompt


async def test_write_uses_the_scripter_stage(fake: FakeGeminiClient) -> None:
    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert fake.calls[0]["request"].stage is Stage.SCRIPTER


async def test_write_asks_for_a_schema_with_a_text_field_and_no_count(
    fake: FakeGeminiClient,
) -> None:
    """R8.1: the model is never asked for a word count it would have to guess.

    A schema carrying `word_count` would invite the model to report a number it
    did not produce, and that number would then sit in the same object as a
    locally computed one.
    """
    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    schema = fake.calls[0]["response_schema"]
    assert set(schema.model_fields) == {"text"}


async def test_write_sends_a_system_instruction_in_the_documentary_voice(
    fake: FakeGeminiClient,
) -> None:
    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    instruction = fake.calls[0]["request"].system_instruction
    assert instruction.strip()
    assert "60" in instruction and "90" in instruction


async def test_write_instructs_a_single_paragraph_with_no_sign_off(
    fake: FakeGeminiClient,
) -> None:
    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    instruction = fake.calls[0]["request"].system_instruction.lower()
    assert "preamble" in instruction
    assert "sign-off" in instruction or "sign off" in instruction


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


async def test_a_gemini_unavailable_error_is_not_retried() -> None:
    """Transport failures are Phase 7's business, not this phase's (R8.3)."""
    from telegram_documentaries.gemini import GeminiUnavailableError

    fake = FakeGeminiClient(
        error=GeminiUnavailableError(
            stage=Stage.SCRIPTER,
            reason="the request timed out",
            error_type="httpx.TimeoutException",
            error_code=None,
        )
    )

    with pytest.raises(GeminiUnavailableError):
        await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 1


async def test_no_script_is_delivered_when_the_call_fails() -> None:
    from telegram_documentaries.gemini import GeminiUnavailableError

    fake = FakeGeminiClient(
        error=GeminiUnavailableError(
            stage=Stage.SCRIPTER,
            reason="a transport failure",
            error_type="httpx.ConnectError",
            error_code=None,
        )
    )
    delivered: Script | None = None

    with pytest.raises(GeminiUnavailableError):
        delivered = await write(
            fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE
        )

    assert delivered is None


async def test_the_first_failure_is_not_logged_at_exception_level(app_records) -> None:
    """A retryable count miss is not an incident; a second one is (R8.3)."""
    fake = FakeGeminiClient([_script(47), _script(SIXTY)])

    await write(fake, plan=_plan(), answers=_answers(), chat_id=CHAT, update_id=UPDATE)

    assert app_records.at_level(logging.ERROR) == []


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


def test_the_script_rejection_carries_both_bounds_and_the_actual() -> None:
    error = ScriptRejectedError(actual=47)

    assert error.actual == 47
    assert error.minimum == 60
    assert error.maximum == 90


def test_the_script_rejection_message_names_the_actual_count() -> None:
    assert "47" in str(ScriptRejectedError(actual=47))


def test_the_rejection_message_does_not_contain_the_narration() -> None:
    """R8.4: what failed is a count, not the prose itself."""
    error = ScriptRejectedError(actual=47)

    assert "word0" not in str(error)


def test_scripter_exports_what_the_hub_needs() -> None:
    for name in ("Script", "write", "ScriptRejectedError", "MIN_WORDS", "MAX_WORDS"):
        assert name in scripter.__all__, name


def test_the_scripter_does_not_import_the_sdk() -> None:
    import ast
    from pathlib import Path

    from telegram_documentaries import scripter as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert "google" not in imported


def test_the_scripter_never_sends_a_message_itself() -> None:
    """R8.5: delivered as one text message by the adapter, not by the stage."""
    import ast
    from pathlib import Path

    from telegram_documentaries import scripter as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "telegram" not in imported
    assert "bot" not in imported
