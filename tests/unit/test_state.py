"""RED: the session store and the declared state machine (R3, R4).

State is what turns five independent Gemini calls into a conversation. Two
properties matter more than the rest, and both are pinned here:

* **Isolation.** One user's answers must never reach another's dossier. The
  store is keyed by ``chat_id`` and exposes no access to its backing dict, so
  the tests drive ``purge`` and ``load`` and assert on what survives - not on
  internals.
* **The transition table is the contract.** R4.2 declares it in the module, so
  the table itself is tested: every row it lists is accepted, and every pair it
  omits is refused. A row cannot be declared and quietly left unimplemented,
  and an extra row cannot be added without the suite noticing.

Nothing here touches the network, the filesystem, or a real credential.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from pydantic import ValidationError

from telegram_documentaries import state
from telegram_documentaries.state import (
    LEGAL_TRANSITIONS,
    Answer,
    InterviewPlan,
    Phase,
    Question,
    Script,
    SessionState,
    SessionStore,
    SessionTransitionError,
    SessionVersionError,
    StoredPhoto,
    begin_interview,
    complete_interview,
    record_answer,
    reset_session,
)

CHAT = 8767055318
OTHER_CHAT = 111111111
UPDATE = 1

FIVE_QUESTIONS = (
    "What is your name?",
    "Where do you roam?",
    "Your finest meal?",
    "Who are your rivals?",
    "What are your strengths?",
)
SCRIPT = Script(text="A regal lion doc.", word_count=12)
THEIR_QUESTIONS = tuple(f"Their question {n}?" for n in range(5))


def _photo() -> StoredPhoto:
    """A stand-in for a saved photo. Never opened - the media store owns paths."""
    return StoredPhoto(path="/photos/some/chat/photo.jpg", byte_size=1024, mime_type="image/jpeg")


def _plan(*questions: str) -> InterviewPlan:
    return InterviewPlan(
        questions=tuple(Question(text=q) for q in (questions or FIVE_QUESTIONS)),
        suggested_animal="lion",
    )


def _exhausted(*questions: str, chat_id: int = CHAT) -> SessionState:
    """Mid-interview with every question answered and the script not yet stored."""
    state_now = _awaiting_answer(*(questions or FIVE_QUESTIONS), chat_id=chat_id)
    while state_now.pending_question is not None:
        state_now = record_answer(state_now, answer="a", chat_id=chat_id, update_id=UPDATE)
    return state_now


def _finished(*questions: str, chat_id: int = CHAT) -> SessionState:
    """A state whose whole plan has been answered and whose script is stored."""
    return complete_interview(
        _exhausted(*questions, chat_id=chat_id),
        script=SCRIPT,
        chat_id=chat_id,
        update_id=UPDATE,
    )


def _awaiting_answer(*questions: str, chat_id: int = CHAT) -> SessionState:
    """A state already mid-interview, awaiting the first answer."""
    plan = _plan(*(questions or FIVE_QUESTIONS))
    return begin_interview(
        SessionState(chat_id=chat_id, photo=_photo()),
        plan=plan,
        chat_id=chat_id,
        update_id=UPDATE,
    )


# --------------------------------------------------------------------------
# R3.1 - the state model
# --------------------------------------------------------------------------


def test_a_new_chat_loads_a_fresh_awaiting_photo_state() -> None:
    store = SessionStore()

    loaded = store.load(CHAT, update_id=UPDATE)

    assert loaded.phase is Phase.AWAITING_PHOTO
    assert loaded.chat_id == CHAT
    assert loaded.answers == ()
    assert loaded.script is None
    assert loaded.plan is None
    assert loaded.photo is None


def test_a_fresh_state_carries_the_current_version() -> None:
    assert SessionStore().load(CHAT, update_id=UPDATE).version == state.SESSION_VERSION


def test_session_state_is_frozen() -> None:
    loaded = SessionStore().load(CHAT, update_id=UPDATE)

    with pytest.raises(ValidationError):
        loaded.phase = Phase.SCRIPTED  # type: ignore[misc]


def test_an_answer_is_frozen_too() -> None:
    answer = Answer(question="What is your name?", answer="Bishal")

    with pytest.raises(ValidationError):
        answer.answer = "Someone else"  # type: ignore[misc]


def test_chat_id_must_be_a_strict_int() -> None:
    with pytest.raises(ValidationError):
        SessionState(chat_id="8767055318")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# R3.2 - the store owns its dict
# --------------------------------------------------------------------------


def test_save_then_load_round_trips_the_state() -> None:
    store = SessionStore()
    original = _awaiting_answer()

    store.save(original, update_id=UPDATE)

    assert store.load(CHAT, update_id=UPDATE) == original


def test_load_of_an_unknown_chat_does_not_raise() -> None:
    assert SessionStore().load(424242, update_id=UPDATE).phase is Phase.AWAITING_PHOTO


def test_purge_removes_the_chat_and_leaves_others_untouched() -> None:
    """The isolation guard: one user's /restart must not wipe another's interview."""
    store = SessionStore()
    mine = _awaiting_answer(chat_id=CHAT)
    theirs = _awaiting_answer(*THEIR_QUESTIONS, chat_id=OTHER_CHAT)
    store.save(mine, update_id=UPDATE)
    store.save(theirs, update_id=UPDATE)

    store.purge(CHAT, update_id=UPDATE)

    assert store.load(CHAT, update_id=UPDATE).phase is Phase.AWAITING_PHOTO
    assert store.load(OTHER_CHAT, update_id=UPDATE) == theirs


def test_purging_an_unknown_chat_is_not_an_error() -> None:
    SessionStore().purge(999999, update_id=UPDATE)


def test_the_store_exposes_no_way_to_reach_its_dict() -> None:
    """R3.2: no module can bypass the store's rules by touching the dict."""
    store = SessionStore()

    assert not [name for name in dir(store) if name.startswith("_dict")]
    assert not any(
        isinstance(getattr(store, name, None), dict)
        for name in dir(store)
        if not name.startswith("_")
    )


# --------------------------------------------------------------------------
# R3.3 - a mismatched version is discarded, never migrated
# --------------------------------------------------------------------------


def test_a_mismatched_version_is_discarded_rather_than_loaded() -> None:
    store = SessionStore()
    store.save(_awaiting_answer(), update_id=UPDATE)

    # Reach past the model to simulate state written by a future schema.
    store._states[CHAT] = _awaiting_answer().model_copy(
        update={"version": 99}
    )

    with pytest.raises(SessionVersionError) as caught:
        store.load(CHAT, update_id=UPDATE)

    assert caught.value.found_version == 99
    assert caught.value.expected_version == state.SESSION_VERSION


def test_the_hub_fallback_for_a_bad_version_is_a_fresh_state() -> None:
    """R3.3: the caller treats it exactly like an unknown chat_id."""
    store = SessionStore()
    store._states[CHAT] = _awaiting_answer().model_copy(
        update={"version": 99}
    )

    recovered = store.fresh_or_load(CHAT, update_id=UPDATE)

    assert recovered.phase is Phase.AWAITING_PHOTO
    assert recovered.chat_id == CHAT


def test_fresh_or_load_returns_the_real_state_when_the_version_matches() -> None:
    store = SessionStore()
    real = _awaiting_answer()
    store.save(real, update_id=UPDATE)

    assert store.fresh_or_load(CHAT, update_id=UPDATE) == real


def test_a_version_error_names_both_versions_in_its_message() -> None:
    message = str(SessionVersionError(found_version=99, chat_id=CHAT))

    assert "99" in message
    assert str(state.SESSION_VERSION) in message


# --------------------------------------------------------------------------
# R4.1 - the phases
# --------------------------------------------------------------------------


def test_phase_has_exactly_the_three_declared_members() -> None:
    assert [p.name for p in Phase] == ["AWAITING_PHOTO", "AWAITING_ANSWER", "SCRIPTED"]


def test_phase_is_a_string_enum() -> None:
    assert Phase.AWAITING_PHOTO == "AWAITING_PHOTO"
    assert isinstance(Phase.SCRIPTED, str)


# --------------------------------------------------------------------------
# R4.4 / R3.5 - the transitions
# --------------------------------------------------------------------------


def test_begin_interview_moves_to_awaiting_answer() -> None:
    started = _awaiting_answer()

    assert started.phase is Phase.AWAITING_ANSWER
    assert started.plan is not None
    assert started.pending_question == FIVE_QUESTIONS[0]
    assert started.answers == ()


def test_begin_interview_keeps_the_photo_that_was_judged() -> None:
    assert _awaiting_answer().photo == _photo()


def test_begin_interview_from_scripted_is_illegal() -> None:
    """SCRIPTED is terminal: only /start, /restart or a new photo leaves it."""
    scripted = _finished()

    with pytest.raises(SessionTransitionError):
        begin_interview(scripted, plan=_plan(), chat_id=CHAT, update_id=UPDATE)


def test_record_answer_appends_the_asked_question_and_the_answer() -> None:
    started = _awaiting_answer()

    after = record_answer(started, answer="Bishal", chat_id=CHAT, update_id=UPDATE)

    assert after.answers == (Answer(question=FIVE_QUESTIONS[0], answer="Bishal"),)
    assert after.pending_question == FIVE_QUESTIONS[1]
    assert after.phase is Phase.AWAITING_ANSWER


def test_record_answer_rejects_an_answer_to_a_different_question() -> None:
    """R3.5: a dossier can never contain a question the user was not asked."""
    with pytest.raises(SessionTransitionError):
        record_answer(
            _awaiting_answer(),
            answer="Bishal",
            question="What is your favourite planet?",
            chat_id=CHAT,
            update_id=UPDATE,
        )


def test_record_answer_on_an_empty_answer_is_rejected() -> None:
    with pytest.raises(SessionTransitionError):
        record_answer(_awaiting_answer(), answer="   ", chat_id=CHAT, update_id=UPDATE)


def test_record_answer_when_no_question_is_pending_is_illegal() -> None:
    fresh = SessionState(chat_id=CHAT)

    with pytest.raises(SessionTransitionError):
        record_answer(fresh, answer="Bishal", chat_id=CHAT, update_id=UPDATE)


def test_answers_accumulate_one_per_turn() -> None:
    state_now = _awaiting_answer(*FIVE_QUESTIONS)
    replies = ("Bishal", "Kathmandu", "Momo")
    for answer in replies:
        state_now = record_answer(state_now, answer=answer, chat_id=CHAT, update_id=UPDATE)

    assert [a.answer for a in state_now.answers] == list(replies)
    assert [a.question for a in state_now.answers] == list(FIVE_QUESTIONS[:3])


def test_record_answer_rejects_the_final_answer_beyond_the_plan() -> None:
    """A 6th answer against a 5-question plan has no question to answer."""
    state_now = _awaiting_answer(*FIVE_QUESTIONS)
    for answer in ("a", "b", "c", "d", "e"):
        state_now = record_answer(state_now, answer=answer, chat_id=CHAT, update_id=UPDATE)

    assert state_now.pending_question is None

    with pytest.raises(SessionTransitionError):
        record_answer(state_now, answer="f", chat_id=CHAT, update_id=UPDATE)


def test_the_last_answer_leaves_no_question_pending() -> None:
    state_now = _awaiting_answer(*FIVE_QUESTIONS)
    for answer in ("a", "b", "c", "d", "e"):
        state_now = record_answer(state_now, answer=answer, chat_id=CHAT, update_id=UPDATE)

    assert state_now.pending_question is None
    assert len(state_now.answers) == 5


def test_complete_interview_moves_to_scripted_and_stores_the_script() -> None:
    done = _finished()

    assert done.phase is Phase.SCRIPTED
    assert done.script == SCRIPT


def test_complete_interview_with_questions_still_pending_is_illegal() -> None:
    with pytest.raises(SessionTransitionError):
        complete_interview(
            _awaiting_answer(*FIVE_QUESTIONS),
            script=SCRIPT,
            chat_id=CHAT,
            update_id=UPDATE,
        )


def test_an_illegal_transition_reports_from_to_and_event() -> None:
    with pytest.raises(SessionTransitionError) as caught:
        complete_interview(
            SessionState(chat_id=CHAT),
            script=SCRIPT,
            chat_id=CHAT,
            update_id=UPDATE,
            event="script_ready",
        )

    assert caught.value.from_phase is Phase.AWAITING_PHOTO
    assert caught.value.to_phase is Phase.SCRIPTED
    assert caught.value.event == "script_ready"
    assert caught.value.chat_id == CHAT


def test_an_illegal_transition_mutates_nothing() -> None:
    fresh = SessionState(chat_id=CHAT)

    with pytest.raises(SessionTransitionError):
        complete_interview(fresh, script=SCRIPT, chat_id=CHAT, update_id=UPDATE)

    assert fresh == SessionState(chat_id=CHAT)
    assert fresh.phase is Phase.AWAITING_PHOTO
    assert fresh.script is None


def test_reset_session_returns_a_fresh_awaiting_photo_state() -> None:
    scripted = _finished()

    fresh = reset_session(scripted, chat_id=CHAT, update_id=UPDATE)

    assert fresh.phase is Phase.AWAITING_PHOTO
    assert fresh.answers == ()
    assert fresh.plan is None
    assert fresh.script is None
    assert fresh.chat_id == CHAT


def test_a_transition_returns_a_new_state_and_leaves_the_old_one_alone() -> None:
    original = _awaiting_answer()

    after = record_answer(original, answer="Bishal", chat_id=CHAT, update_id=UPDATE)

    assert after is not original
    assert original.answers == ()
    assert len(after.answers) == 1


# --------------------------------------------------------------------------
# R4.2 - the declared table is the contract
# --------------------------------------------------------------------------


def test_removing_a_row_from_the_table_is_caught_by_the_behaviour_tests() -> None:
    """The point of driving the table from the behaviour.

    Sheds one declared row and confirms the walk fails, so the pair test cannot
    quietly become a tautology that passes whatever the table says.
    """
    import telegram_documentaries.state as state_module

    original = state_module.LEGAL_TRANSITIONS
    try:
        state_module.LEGAL_TRANSITIONS = frozenset(
            original - {(Phase.SCRIPTED, Phase.AWAITING_PHOTO)}
        )

        # The reset from SCRIPTED still works, so the walk disagrees with the
        # shrunken table and the assertion fails.
        assert (Phase.SCRIPTED, Phase.AWAITING_PHOTO) not in state_module.LEGAL_TRANSITIONS
        assert _transition_reaches(Phase.SCRIPTED, Phase.AWAITING_PHOTO) is True
    finally:
        state_module.LEGAL_TRANSITIONS = original

    assert _transition_reaches(Phase.SCRIPTED, Phase.AWAITING_PHOTO) is True
    assert (Phase.SCRIPTED, Phase.AWAITING_PHOTO) in state_module.LEGAL_TRANSITIONS


def test_a_transition_that_is_not_declared_is_refused() -> None:
    """The table is exhaustive, not advisory."""
    assert (Phase.SCRIPTED, Phase.AWAITING_ANSWER) not in LEGAL_TRANSITIONS

    with pytest.raises(SessionTransitionError):
        begin_interview(
            _finished(), plan=_plan(), chat_id=CHAT, update_id=UPDATE
        )


def test_the_legal_transition_table_is_exactly_the_declared_one() -> None:
    assert frozenset(
        {
            (Phase.AWAITING_PHOTO, Phase.AWAITING_PHOTO),
            (Phase.AWAITING_PHOTO, Phase.AWAITING_ANSWER),
            (Phase.AWAITING_ANSWER, Phase.AWAITING_ANSWER),
            (Phase.AWAITING_ANSWER, Phase.SCRIPTED),
            (Phase.AWAITING_ANSWER, Phase.AWAITING_PHOTO),
            (Phase.SCRIPTED, Phase.AWAITING_PHOTO),
        }
    ) == LEGAL_TRANSITIONS


@pytest.mark.parametrize(
    ("from_phase", "to_phase"),
    sorted(
        ((f, t) for f in Phase for t in Phase),
        key=lambda pair: (pair[0].value, pair[1].value),
    ),
)
def test_the_table_agrees_with_the_behaviour_for_every_pair(
    from_phase: Phase, to_phase: Phase
) -> None:
    """Drive the real transitions and compare against the declared table.

    Declared is not the same as implemented. This walks every phase pair and
    asserts the table's opinion matches what the functions actually do, so a row
    cannot be listed and left unimplemented.
    """
    declared = (from_phase, to_phase) in LEGAL_TRANSITIONS

    reached = _transition_reaches(from_phase, to_phase)

    assert declared is reached


def _transition_reaches(from_phase: Phase, to_phase: Phase) -> bool:
    """Attempt the real transitions out of `from_phase`; report whether `to_phase` is reachable.

    Each `from_phase` is reached by driving the actual functions, not by
    hand-building a state, so the check exercises the same code path production
    does. A phase pair counts as reachable only if some legal function moves a
    real state exactly there.
    """
    candidates: list[SessionState] = []

    if from_phase is Phase.AWAITING_PHOTO:
        candidates.append(SessionState(chat_id=CHAT))
    elif from_phase is Phase.AWAITING_ANSWER:
        # Both positions in the interview, because `SCRIPTED` is only reachable
        # once the plan is exhausted. Probing only the start would report the
        # declared (AWAITING_ANSWER, SCRIPTED) row as unimplemented.
        candidates.append(_awaiting_answer())
        candidates.append(_exhausted())
    else:
        candidates.append(_finished())

    for candidate in candidates:
        for produced in _attempt_all(candidate):
            if produced.phase is to_phase:
                return True
    return False


def _attempt_all(from_state: SessionState) -> list[SessionState]:
    """Every state the transition functions will produce from `from_state`.

    Breadth-first to a depth of one transition, starting from `from_state` itself
    - seeding with the start state is what lets `complete_interview` be tried
    against it, rather than only against the states an earlier call produced.
    """
    produced: list[SessionState] = []

    def attempt(candidate: SessionState) -> None:
        with contextlib.suppress(SessionTransitionError):
            produced.append(
                begin_interview(candidate, plan=_plan(), chat_id=CHAT, update_id=UPDATE)
            )
        with contextlib.suppress(SessionTransitionError):
            produced.append(record_answer(candidate, answer="a", chat_id=CHAT, update_id=UPDATE))
        with contextlib.suppress(SessionTransitionError):
            produced.append(
                complete_interview(candidate, script=SCRIPT, chat_id=CHAT, update_id=UPDATE)
            )
        produced.append(reset_session(candidate, chat_id=CHAT, update_id=UPDATE))

    attempt(from_state)

    return produced


def test_scripted_has_no_outgoing_transition_except_a_reset() -> None:
    outgoing = {
        to for frm, to in LEGAL_TRANSITIONS if frm is Phase.SCRIPTED
    }

    assert outgoing == {Phase.AWAITING_PHOTO}


def test_an_unknown_phase_is_not_constructible() -> None:
    with pytest.raises(ValueError):
        Phase("SOMETHING_ELSE")


# --------------------------------------------------------------------------
# Answers must stay bounded (R3.5 / R7.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [5, 6, 7])
def test_a_plan_of_five_to_seven_questions_is_accepted(count: int) -> None:
    plan = _plan(*(f"Question {n}?" for n in range(count)))

    assert len(plan.questions) == count


@pytest.mark.parametrize("count", [0, 1, 4, 8])
def test_a_plan_outside_five_to_seven_is_rejected(count: int) -> None:
    questions = tuple(f"Question {n}?" for n in range(count))
    with pytest.raises(ValidationError):
        InterviewPlan(
            questions=tuple(Question(text=q) for q in questions),
            suggested_animal="lion",
        )


def test_a_script_must_hold_narration_text() -> None:
    with pytest.raises(ValidationError):
        Script(text="   ", word_count=2)


def test_the_dossier_is_bounded_by_construction() -> None:
    """R3.5: no separate cap is needed because the plan is bounded."""
    plan = _plan(*(f"Question {n}?" for n in range(7)))
    state_now = _awaiting_answer(*(f"Question {n}?" for n in range(7)))

    assert len(plan.questions) == 7
    assert state_now.plan == plan


def test_state_fields_are_all_declared() -> None:
    """R3.1 names every field; a rename should fail here rather than silently."""
    assert set(SessionState.model_fields) == {
        "version",
        "chat_id",
        "phase",
        "photo",
        "plan",
        "pending_question",
        "answers",
        "script",
    }


def test_no_module_reaches_past_the_store_for_state() -> None:
    """R3.2 at the codebase level: state.py is the only owner of the dict."""
    import ast
    from pathlib import Path

    from telegram_documentaries import state as state_module

    package = Path(state_module.__file__).parent
    offenders = [
        module.name
        for module in package.glob("*.py")
        if module.name != "state.py"
        and "_states" in ast.dump(ast.parse(module.read_text()))
    ]

    assert offenders == []


def test_state_exports_everything_the_hub_needs() -> None:
    for name in (
        "Phase",
        "SessionState",
        "Answer",
        "StoredPhoto",
        "SessionStore",
        "LEGAL_TRANSITIONS",
        "begin_interview",
        "record_answer",
        "complete_interview",
        "reset_session",
        "SessionTransitionError",
        "SessionVersionError",
    ):
        assert name in state.__all__, name


def test_the_transition_error_is_not_swallowed_as_a_version_error() -> None:
    """R4.3: an illegal transition is not the same thing as an unknown chat."""
    assert not issubclass(SessionTransitionError, SessionVersionError)
    assert not issubclass(SessionVersionError, SessionTransitionError)


def test_a_both_are_catchable_as_one_state_error() -> None:
    with pytest.raises(state.SessionError):
        raise SessionTransitionError(
            from_phase=Phase.SCRIPTED, to_phase=Phase.AWAITING_ANSWER, chat_id=CHAT
        )


def test_state_takes_no_dependency_it_does_not_need() -> None:
    """The state machine must not import the SDK or the transport."""
    import ast
    from pathlib import Path

    from telegram_documentaries import state as state_module

    tree = ast.parse(Path(state_module.__file__).read_text())
    imported: dict[str, Any] = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    } | {
        node.names[0].name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
    }

    assert "httpx" not in imported
    assert "telegram" not in imported
