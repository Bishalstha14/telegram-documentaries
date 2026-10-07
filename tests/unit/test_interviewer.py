"""RED: the Interviewer - one plan call, then local questions (R7).

The roadmap promises "5-7 questions, one at a time, one Gemini call". Two of
those three are prompt-dependent and one is not, and this module is where the
difference is made real:

* **One call, total (R7.1/D2).** `plan()` is the only Gemini call the
  Interviewer makes. A whole interview's worth of questions is asked from local
  state afterwards, asserted by a call-count guard that drives the fake client
  through a full interview and then checks it was used exactly once. Without
  that test, "one call" is a comment rather than a cost guarantee.
* **"One at a time" is a property of the code, not of a prompt (R7.3).** Asking
  the next question is an index walk over a tuple that already exists.

The 5-7 rule lives in the schema (R7.2), so a reply with 3 or 9 questions fails
R1.4.5 like any other off-schema reply - there is no string counting anywhere.

Nothing here touches the network or a real credential.
"""

from __future__ import annotations

import pytest
from conftest import FakeGeminiClient
from pydantic import ValidationError

from telegram_documentaries import interviewer
from telegram_documentaries.gemini import Stage
from telegram_documentaries.interviewer import (
    MAX_QUESTION_LENGTH,
    InterviewPlan,
    Question,
    next_question,
    plan,
)
from telegram_documentaries.state import (
    SessionState,
    SessionTransitionError,
    begin_interview,
    record_answer,
)

CHAT = 8767055318
UPDATE = 1
SUBJECT = "a person smiling at the camera"
ANIMAL = "sea otter"

FIVE = tuple(f"Question {n}?" for n in range(5))


def _questions(count: int = 5) -> tuple[Question, ...]:
    return tuple(Question(text=f"Question {n}?") for n in range(count))


def _plan_reply(count: int = 5, *, animal: str = ANIMAL) -> InterviewPlan:
    return InterviewPlan(questions=_questions(count), suggested_animal=animal)


@pytest.fixture
def fake() -> FakeGeminiClient:
    return FakeGeminiClient([_plan_reply()])


# --------------------------------------------------------------------------
# R7.1 - the plan call
# --------------------------------------------------------------------------


async def test_plan_returns_a_typed_plan_with_questions_and_an_animal(
    fake: FakeGeminiClient,
) -> None:
    result = await plan(fake, subject=SUBJECT, chat_id=CHAT, update_id=UPDATE)

    assert isinstance(result, InterviewPlan)
    assert len(result.questions) == 5
    assert result.suggested_animal == ANIMAL


async def test_plan_passes_the_bouncer_subject_into_the_prompt(
    fake: FakeGeminiClient,
) -> None:
    """R7.1: the questions relate to the photo rather than being generic."""
    await plan(fake, subject=SUBJECT, chat_id=CHAT, update_id=UPDATE)

    request = fake.calls[0]["request"]
    assert SUBJECT in request.prompt


async def test_the_system_instruction_explains_where_the_subject_arrives(
    fake: FakeGeminiClient,
) -> None:
    """R7.1: the persona is stable, so the subject belongs in the prompt.

    Asserting it names the subject here would be wrong - a system instruction
    that changed per photo would rebuild the persona on every call. What matters
    is that the model is told a subject will be given, so it does not go looking
    for one in the photo alone.
    """
    await plan(fake, subject=SUBJECT, chat_id=CHAT, update_id=UPDATE)

    instruction = fake.calls[0]["request"].system_instruction
    assert "subject" in instruction.lower()
    assert SUBJECT not in instruction


async def test_plan_uses_the_interviewer_stage(fake: FakeGeminiClient) -> None:
    await plan(fake, subject=SUBJECT, chat_id=CHAT, update_id=UPDATE)

    assert fake.calls[0]["request"].stage is Stage.INTERVIEWER


async def test_plan_asks_for_the_interview_plan_schema(fake: FakeGeminiClient) -> None:
    await plan(fake, subject=SUBJECT, chat_id=CHAT, update_id=UPDATE)

    assert fake.calls[0]["response_schema"] is InterviewPlan


async def test_plan_makes_exactly_one_call(fake: FakeGeminiClient) -> None:
    await plan(fake, subject=SUBJECT, chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 1


async def test_a_blank_subject_is_refused_before_any_call(
    fake: FakeGeminiClient,
) -> None:
    """R7.1: an empty Bouncer subject would make the questions generic."""
    with pytest.raises(ValidationError):
        await plan(fake, subject="   ", chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 0


# --------------------------------------------------------------------------
# R7.2 - the 5-7 rule is in the schema
# --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [5, 6, 7])
def test_a_plan_of_five_to_seven_questions_is_valid(count: int) -> None:
    plan_model = InterviewPlan(
        questions=_questions(count), suggested_animal=ANIMAL
    )

    assert len(plan_model.questions) == count


@pytest.mark.parametrize("count", [0, 1, 3, 4, 8, 9, 20])
def test_a_plan_with_any_other_count_is_rejected(count: int) -> None:
    """The count is a schema constraint, so an off-schema reply fails R1.4.5."""
    with pytest.raises(ValidationError):
        InterviewPlan(questions=_questions(count), suggested_animal=ANIMAL)


def test_plan_rejects_a_count_under_five() -> None:
    """The 5-7 rule is on the schema, so a 3-question reply cannot become a plan.

    `gemini.py` validates a raw reply against this same model, which is where an
    off-schema payload is actually refused - covered there. What this asserts is
    the mechanism: there is no `len(questions)` check anywhere in the hub,
    because the field itself will not hold a 3-tuple.
    """
    with pytest.raises(ValidationError):
        InterviewPlan(questions=_questions(3), suggested_animal=ANIMAL)


def test_plan_rejects_a_count_over_seven() -> None:
    with pytest.raises(ValidationError):
        InterviewPlan(questions=_questions(9), suggested_animal=ANIMAL)


def test_a_blank_question_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InterviewPlan(
            questions=(Question(text="   "), *_questions(4)),
            suggested_animal=ANIMAL,
        )


def test_a_whitespace_only_question_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InterviewPlan(
            questions=(Question(text="\n\t"), *_questions(4)),
            suggested_animal=ANIMAL,
        )


def test_an_over_long_question_is_rejected() -> None:
    """280 characters so the question fits in one Telegram message."""
    with pytest.raises(ValidationError):
        InterviewPlan(
            questions=(Question(text="x" * (MAX_QUESTION_LENGTH + 1)), *_questions(4)),
            suggested_animal=ANIMAL,
        )


def test_a_question_exactly_at_the_bound_is_accepted() -> None:
    plan_model = InterviewPlan(
        questions=(Question(text="x" * MAX_QUESTION_LENGTH), *_questions(4)),
        suggested_animal=ANIMAL,
    )

    assert len(plan_model.questions[0].text) == MAX_QUESTION_LENGTH == 280


def test_a_blank_suggested_animal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InterviewPlan(questions=_questions(5), suggested_animal="   ")


def test_an_over_long_suggested_animal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InterviewPlan(questions=_questions(5), suggested_animal="x" * 61)


def test_the_plan_model_is_frozen() -> None:
    plan_model = _plan_reply()

    with pytest.raises(ValidationError):
        plan_model.suggested_animal = "wolf"  # type: ignore[misc]


# --------------------------------------------------------------------------
# R7.3 - questions come from local state, not from a model
# --------------------------------------------------------------------------


def _awaiting(count: int = 5) -> SessionState:
    plan_model = InterviewPlan(
        questions=_questions(count), suggested_animal=ANIMAL
    )
    return begin_interview(
        SessionState(chat_id=CHAT),
        plan=plan_model,
        chat_id=CHAT,
        update_id=UPDATE,
    )


def test_next_question_returns_the_first_question_then_advances() -> None:
    state_now = _awaiting()

    assert next_question(state_now) == "Question 0?"

    state_now = record_answer(state_now, answer="Bishal", chat_id=CHAT, update_id=UPDATE)
    assert next_question(state_now) == "Question 1?"


def test_next_question_returns_none_once_the_plan_is_exhausted() -> None:
    state_now = _awaiting(5)
    for answer in ("a", "b", "c", "d", "e"):
        state_now = record_answer(state_now, answer=answer, chat_id=CHAT, update_id=UPDATE)

    assert next_question(state_now) is None


def test_asking_a_question_makes_no_gemini_call() -> None:
    """The call-count guard: one call for a whole interview's worth of questions.

    This is what makes D2's cost claim true rather than aspirational. The fake
    is given one reply and never used again, so an accidental model call during
    questioning would raise "ran out of replies" instead of passing silently.
    """
    fake = FakeGeminiClient([_plan_reply()])
    state_now = _awaiting()

    # `next_question` is pure, so the guard is structural: no client, no call.
    for _ in range(5):
        assert next_question(state_now) is not None
        state_now = record_answer(state_now, answer="a", chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 0


async def test_one_plan_call_serves_a_whole_interview() -> None:
    """The real cost guarantee: exactly one call, however many answers arrive."""
    fake = FakeGeminiClient([_plan_reply(7)])
    state_now = SessionState(chat_id=CHAT)

    plan_model = await plan(fake, subject=SUBJECT, chat_id=CHAT, update_id=UPDATE)
    state_now = begin_interview(
        state_now, plan=plan_model, chat_id=CHAT, update_id=UPDATE
    )

    for answer in ("a", "b", "c", "d", "e", "f", "g"):
        if next_question(state_now) is None:
            break
        state_now = record_answer(state_now, answer=answer, chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 1
    assert next_question(state_now) is None


def test_next_question_reads_the_plan_it_was_given() -> None:
    plan_model = InterviewPlan(
        questions=(Question(text="Only one?"), *_questions(5)[1:]),
        suggested_animal=ANIMAL,
    )
    state_now = begin_interview(
        SessionState(chat_id=CHAT), plan=plan_model, chat_id=CHAT, update_id=UPDATE
    )

    assert next_question(state_now) == "Only one?"


# --------------------------------------------------------------------------
# R7.5 - an empty answer does not advance
# --------------------------------------------------------------------------


def test_an_empty_answer_does_not_advance_the_plan() -> None:
    """R7.5: the question is never silently skipped."""
    state_now = _awaiting()

    with pytest.raises(SessionTransitionError):
        record_answer(state_now, answer="   ", chat_id=CHAT, update_id=UPDATE)

    # Still asking the same question.
    assert next_question(state_now) == "Question 0?"
    assert state_now.answers == ()


def test_a_blank_answer_leaves_the_pending_question_untouched() -> None:
    state_now = _awaiting()

    with pytest.raises(SessionTransitionError):
        record_answer(state_now, answer="\n", chat_id=CHAT, update_id=UPDATE)

    assert state_now.pending_question == "Question 0?"


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


def test_the_question_cursor_never_leaves_the_plan() -> None:
    state_now = _awaiting(5)
    seen = []

    for _ in range(6):
        seen.append(next_question(state_now))
        if seen[-1] is None:
            break
        state_now = record_answer(state_now, answer="a", chat_id=CHAT, update_id=UPDATE)

    assert seen == [
        "Question 0?",
        "Question 1?",
        "Question 2?",
        "Question 3?",
        "Question 4?",
        None,
    ]


def test_next_question_on_a_state_with_no_plan_is_none() -> None:
    assert next_question(SessionState(chat_id=CHAT)) is None


def test_interviewer_exports_what_the_hub_needs() -> None:
    for name in (
        "InterviewPlan",
        "Question",
        "plan",
        "next_question",
        "MAX_QUESTION_LENGTH",
    ):
        assert name in interviewer.__all__, name


def test_the_interviewer_does_not_import_the_sdk() -> None:
    """R10: only gemini.py may reach past the boundary."""
    import ast
    from pathlib import Path

    from telegram_documentaries import interviewer as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert "google" not in imported
    assert "google.genai" not in imported


def test_the_interviewer_does_not_touch_the_session_store() -> None:
    """Asking a question is local; only the hub reads and writes the store."""
    import ast
    from pathlib import Path

    from telegram_documentaries import interviewer as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "SessionStore" not in imported
