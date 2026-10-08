"""RED: the Bouncer - the vision gate (R6).

The Bouncer is the only stage whose input is wholly unfiltered: the user's photo,
and nothing else. Two things follow, and both are pinned here.

* **The reply is a typed enum, never a parsed string.** `Verdict` is a
  `StrEnum`, so "is this a human?" is answered by comparing an enum member, not
  by matching free text that a model could reword at any moment (R6.1).
* **The model's cheeky line is contained.** Its output is length-bounded and
  falls back to a local constant when blank or over-long, so a photo cannot put
  arbitrary text in front of the user (R6.2).

`UNSURE` is *accepted* (D6). Failing open is deliberate: a wrongly rejected
portrait costs the user the whole experience, while a wrongly accepted one just
means a slightly odd interview.

Nothing here touches the network. The seam is `GeminiClient.generate`.
"""

from __future__ import annotations

import pytest
from conftest import FakeGeminiClient
from pydantic import ValidationError

from telegram_documentaries.bouncer import (
    BOUNCER_REJECTION_FALLBACK,
    BouncerVerdict,
    Verdict,
    judge,
)
from telegram_documentaries.gemini import (
    GeminiRequest,
    GeminiResponseError,
    GeminiUnavailableError,
    Stage,
)

CHAT = 8767055318
UPDATE = 1
PHOTO = b"\xff\xd8\xff\xe0" + b"portrait-bytes" * 64
MIME = "image/jpeg"


def _verdict(verdict: Verdict, *, subject: str = "a human", line: str = "") -> BouncerVerdict:
    return BouncerVerdict(verdict=verdict, subject=subject, line=line)


@pytest.fixture
def human() -> FakeGeminiClient:
    return FakeGeminiClient([_verdict(Verdict.HUMAN, subject="a person smiling")])


@pytest.fixture
def not_human() -> FakeGeminiClient:
    return FakeGeminiClient(
        [_verdict(Verdict.NOT_HUMAN, subject="a very smug cat", line="That is a cat.")]
    )


# --------------------------------------------------------------------------
# R6.1 - typed, not parsed
# --------------------------------------------------------------------------


async def test_judge_returns_a_typed_verdict_and_never_a_parsed_string(
    human: FakeGeminiClient,
) -> None:
    """The roadmap's criterion, asserted as a Verdict instance."""
    result = await judge(
        human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert isinstance(result, BouncerVerdict)
    assert isinstance(result.verdict, Verdict)
    assert result.verdict is Verdict.HUMAN


def test_the_verdict_enum_has_exactly_the_three_declared_members() -> None:
    assert [v.name for v in Verdict] == ["HUMAN", "NOT_HUMAN", "UNSURE"]


def test_the_verdict_is_a_string_enum() -> None:
    assert Verdict.HUMAN == "HUMAN"
    assert isinstance(Verdict.UNSURE, str)


def test_the_verdict_model_is_frozen() -> None:
    verdict = _verdict(Verdict.HUMAN)

    with pytest.raises(ValidationError):
        verdict.subject = "something else"  # type: ignore[misc]


async def test_a_non_human_verdict_is_recognised_as_not_human(
    not_human: FakeGeminiClient,
) -> None:
    result = await judge(
        not_human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.verdict is Verdict.NOT_HUMAN


async def test_an_unsure_verdict_is_returned_as_unsure() -> None:
    fake = FakeGeminiClient([_verdict(Verdict.UNSURE, subject="something ambiguous")])

    result = await judge(
        fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.verdict is Verdict.UNSURE


# --------------------------------------------------------------------------
# What gets sent
# --------------------------------------------------------------------------


async def test_judge_passes_the_photo_bytes_to_gemini(human: FakeGeminiClient) -> None:
    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    assert human.calls[0]["request"].image == PHOTO


async def test_judge_declares_the_photo_mime_type(human: FakeGeminiClient) -> None:
    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    assert human.calls[0]["request"].image_mime_type == MIME


async def test_judge_uses_the_bouncer_stage(human: FakeGeminiClient) -> None:
    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    assert human.calls[0]["request"].stage.value == "bouncer"


async def test_judge_asks_for_the_bouncer_schema(human: FakeGeminiClient) -> None:
    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    assert human.calls[0]["response_schema"] is BouncerVerdict


async def test_judge_carries_a_system_instruction(human: FakeGeminiClient) -> None:
    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    request: GeminiRequest = human.calls[0]["request"]
    assert request.system_instruction.strip()
    assert len(request.system_instruction) > 50


async def test_the_system_instruction_names_the_enum_the_model_must_use(
    human: FakeGeminiClient,
) -> None:
    """A prompt that omits the allowed values is a coin toss, not a gate."""
    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    instruction: str = human.calls[0]["request"].system_instruction
    for member in Verdict:
        assert member.value in instruction


async def test_judge_includes_the_model_subject_in_the_verdict(
    human: FakeGeminiClient,
) -> None:
    result = await judge(
        human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.subject == "a person smiling"


# --------------------------------------------------------------------------
# Acceptance (R6.3)
# --------------------------------------------------------------------------


async def test_a_human_verdict_is_accepted(human: FakeGeminiClient) -> None:
    result = await judge(
        human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.verdict in {Verdict.HUMAN, Verdict.UNSURE}


async def test_an_unsure_verdict_is_accepted_and_logged_as_bouncer_unsure(
    app_records,
) -> None:
    """D6: unsure fails open, and the uncertainty is recorded anyway."""
    fake = FakeGeminiClient([_verdict(Verdict.UNSURE, subject="unclear at this range")])

    await judge(fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    events = app_records.events()
    assert "bouncer_unsure" in events
    record = next(r for r in app_records.records if r.message == "bouncer_unsure")
    assert record.chat_id == CHAT
    assert record.update_id == UPDATE
    assert record.subject == "unclear at this range"


async def test_a_human_verdict_is_not_logged_as_unsure(
    app_records,
) -> None:
    fake = FakeGeminiClient([_verdict(Verdict.HUMAN)])

    await judge(fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    assert "bouncer_unsure" not in app_records.events()


# --------------------------------------------------------------------------
# R6.2 - the cheeky line is contained
# --------------------------------------------------------------------------


async def test_a_non_human_verdict_carries_the_models_cheeky_line(
    not_human: FakeGeminiClient,
) -> None:
    result = await judge(
        not_human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.line == "That is a cat."


async def test_a_blank_line_falls_back_to_the_local_rejection() -> None:
    fake = FakeGeminiClient([_verdict(Verdict.NOT_HUMAN, line="   ")])

    result = await judge(
        fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.line == BOUNCER_REJECTION_FALLBACK


async def test_a_missing_line_falls_back_too() -> None:
    fake = FakeGeminiClient([_verdict(Verdict.NOT_HUMAN, line="")])

    result = await judge(
        fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.line == BOUNCER_REJECTION_FALLBACK


async def test_an_oversized_line_falls_back_to_the_local_rejection() -> None:
    """Defence in depth: an essay is discarded even if the schema bound loosens.

    `model_construct` bypasses validation on purpose. The schema already refuses
    a line over 300 characters, so the only way an over-long line could ever
    reach `judge` is a future change to that bound - and this is the test that
    would catch the substitution being lost along with it.
    """
    over_long = BouncerVerdict.model_construct(
        verdict=Verdict.NOT_HUMAN, subject="a cat", line="x" * 301
    )
    fake = FakeGeminiClient([over_long])

    result = await judge(
        fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.line == BOUNCER_REJECTION_FALLBACK


def test_the_schema_itself_refuses_a_line_over_the_bound() -> None:
    """The primary guard: an over-long line never validates."""
    with pytest.raises(ValidationError):
        BouncerVerdict(
            verdict=Verdict.NOT_HUMAN, subject="a cat", line="x" * 301
        )


async def test_a_line_exactly_at_the_bound_is_accepted() -> None:
    fake = FakeGeminiClient([_verdict(Verdict.NOT_HUMAN, line="x" * 300)])

    result = await judge(
        fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    assert result.line == "x" * 300


async def test_the_fallback_is_a_local_constant_and_not_model_output() -> None:
    assert isinstance(BOUNCER_REJECTION_FALLBACK, str)
    assert 0 < len(BOUNCER_REJECTION_FALLBACK) <= 300


async def test_the_fallback_is_not_empty_or_blank() -> None:
    assert BOUNCER_REJECTION_FALLBACK.strip()
    assert BOUNCER_REJECTION_FALLBACK.strip() == BOUNCER_REJECTION_FALLBACK


# --------------------------------------------------------------------------
# The stage is pure apart from the Gemini call (R6.4 / R6.5)
# --------------------------------------------------------------------------


async def test_a_non_human_verdict_leaves_the_session_untouched() -> None:
    """The stage does not reset; the hub does (R6.4). `judge` only judges."""
    fake = FakeGeminiClient([_verdict(Verdict.NOT_HUMAN, line="A dog.")])

    result = await judge(
        fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
    )

    # The only outputs are the verdict and its two fields. No state, no store,
    # no media - so resetting cannot be happening here.
    assert set(BouncerVerdict.model_fields) == {"verdict", "subject", "line"}
    assert result.verdict is Verdict.NOT_HUMAN


def test_the_bouncer_does_not_import_the_state_or_media_modules() -> None:
    """The stage must stay free of the hub's responsibilities."""
    import ast
    from pathlib import Path

    from telegram_documentaries import bouncer as bouncer_module

    tree = ast.parse(Path(bouncer_module.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "state" not in imported
    assert "media" not in imported


async def test_a_gemini_unavailable_error_leaves_the_session_untouched() -> None:
    """R6.5: a transport failure does not become a rejection."""
    fake = FakeGeminiClient(
        error=GeminiUnavailableError(
            stage=Stage.BOUNCER,
            reason="the request timed out",
            error_type="httpx.TimeoutException",
            error_code=None,
        )
    )

    with pytest.raises(GeminiUnavailableError):
        await judge(fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 1


async def test_a_gemini_response_error_is_not_swallowed() -> None:
    """R6.5: the Bouncer lets it through rather than turning it into a verdict.

    The `exception`-level log belongs to `gemini.py`, which raises the error in
    the first place and is already covered there. What the Bouncer must not do
    is catch it and fall back to some verdict, because that would turn "the
    model misbehaved" into "this is not a person" - a rejection the user could
    not distinguish from a real one.
    """
    fake = FakeGeminiClient(
        error=GeminiResponseError(
            stage=Stage.BOUNCER,
            reason="the reply does not satisfy the response schema",
            chat_id=CHAT,
            update_id=UPDATE,
        )
    )

    with pytest.raises(GeminiResponseError):
        await judge(fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    assert fake.call_count == 1


async def test_a_failed_judge_returns_no_verdict_at_all() -> None:
    """A failure must not look like any verdict, including UNSURE."""
    fake = FakeGeminiClient(
        error=GeminiUnavailableError(
            stage=Stage.BOUNCER,
            reason="a transport failure",
            error_type="httpx.ConnectError",
            error_code=None,
        )
    )

    result = None
    with pytest.raises(GeminiUnavailableError):
        result = await judge(
            fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE
        )

    assert result is None


async def test_a_failure_does_not_produce_a_verdict() -> None:
    """No verdict means no accidental acceptance by falling through."""
    fake = FakeGeminiClient(
        error=GeminiUnavailableError(
            stage=Stage.BOUNCER,
            reason="a transport failure",
            error_type="httpx.ConnectError",
            error_code=None,
        )
    )

    with pytest.raises(GeminiUnavailableError):
        await judge(fake, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)


async def test_the_bouncer_sends_no_image_of_its_own_making(
    human: FakeGeminiClient,
) -> None:
    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    request: GeminiRequest = human.calls[0]["request"]
    assert request.image is not None


async def test_no_prompt_leaks_the_api_key(
    human: FakeGeminiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R10: a stage never sees a credential, so it cannot echo one."""
    monkeypatch.setenv("GEMINI_API_KEY", "should-never-appear-anywhere")

    await judge(human, image=PHOTO, mime_type=MIME, chat_id=CHAT, update_id=UPDATE)

    request: GeminiRequest = human.calls[0]["request"]
    assert "should-never-appear-anywhere" not in request.system_instruction
    assert "should-never-appear-anywhere" not in request.prompt
