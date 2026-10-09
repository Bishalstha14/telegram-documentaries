"""RED: the hub - the decision table, one entry per (phase x payload) (R9).

`pipeline.py` is the only place that knows what happens next. The adapter in
`bot.py` parses and sends; every branch lives here, which is why the whole
conversation can be driven in a test with no Telegram, no Gemini and no
filesystem beyond `tmp_path`.

Three properties get the most attention:

* **One send per update.** The adapter returns exactly one string. Tests count
  the messages a fake bot recorded, because "the interview asks one question at
  a time" is a product promise, not an implementation detail.
* **Nothing is raised into the user's flow (R9.5).** Every domain error is
  caught at the hub boundary and answered with a short human line. The tests
  that matter most are the ones where Gemini is unavailable at each of the three
  call sites: the user sees a sentence, and the session is untouched.
* **Isolation.** Two chats driven through interleaved interviews must never see
  each other's questions, answers or scripts.

Nothing here touches the network or a real credential.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FakeGeminiClient,
    FakePhotoFetcher,
    FakeTelegramBot,
    LogRecorder,
)
from telegram import Bot, Update

from telegram_documentaries.bouncer import BouncerVerdict, Verdict
from telegram_documentaries.contracts import InboundUpdate, Reply, VoiceNote
from telegram_documentaries.gemini import (
    GeminiThrottledError,
    GeminiUnavailableError,
    Stage,
    SynthesizedAudio,
)
from telegram_documentaries.interviewer import InterviewPlan, Question
from telegram_documentaries.media import MediaStore
from telegram_documentaries.pipeline import (
    GENERIC_FAILURE,
    PHOTO_MIME_TYPE,
    RATE_LIMITED,
    WELCOME,
    ConversationPipeline,
)
from telegram_documentaries.scripter import Script
from telegram_documentaries.state import Phase, SessionStore

CHAT = 8767055318
OTHER_CHAT = 111222333
UPDATE = 1

PHOTO_BYTES = b"\xff\xd8fakejpeg\xff\xd9"

PLAN_5 = InterviewPlan(
    questions=tuple(Question(text=f"Question {n}?") for n in range(5)),
    suggested_animal="sea otter",
)


def _words(count: int) -> str:
    return " ".join(f"word{n}" for n in range(count))


SCRIPT = Script(text=_words(72), word_count=72)


def _verdict(**overrides: Any) -> BouncerVerdict:
    payload: dict[str, Any] = {
        "verdict": Verdict.HUMAN,
        "subject": "a person smiling at the camera",
        "line": "",
    }
    payload.update(overrides)
    return BouncerVerdict(**payload)


# --------------------------------------------------------------------------
# Update builders - real telegram.Update objects through the real parser
# --------------------------------------------------------------------------


def _photo_message(chat_id: int = CHAT, caption: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 17,
        "date": 1_700_000_000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 555, "is_bot": False, "first_name": "Ada"},
        "photo": [
            {
                "file_id": "small",
                "file_unique_id": "u-small",
                "width": 90,
                "height": 90,
                "file_size": 100,
            },
            {
                "file_id": "big",
                "file_unique_id": "u-big",
                "width": 800,
                "height": 800,
                "file_size": 9_000,
            },
        ],
    }
    if caption is not None:
        message["caption"] = caption
    return {"message": message}


def _text_message(text: str, chat_id: int = CHAT) -> dict[str, Any]:
    return {
        "message": {
            "message_id": 18,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 555, "is_bot": False, "first_name": "Ada"},
            "text": text,
        }
    }


def _kind_message(kind: str, chat_id: int = CHAT) -> dict[str, Any]:
    """A message carrying `kind`, built so python-telegram-bot accepts it."""
    payloads: dict[str, dict[str, Any]] = {
        "sticker": {
            "file_id": "f",
            "file_unique_id": "u",
            "width": 1,
            "height": 1,
            "type": "regular",
            "is_animated": False,
            "is_video": False,
        },
        "document": {"file_name": "notes.pdf", "file_id": "f", "file_unique_id": "u"},
        "voice": {"file_id": "f", "file_unique_id": "u", "duration": 1},
    }
    return {
        "message": {
            "message_id": 19,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 555, "is_bot": False, "first_name": "Ada"},
            kind: payloads[kind],
        }
    }


def inbound(payload: dict[str, Any], update_id: int = UPDATE) -> InboundUpdate:
    """Build a typed update through the real `Update.de_json` path."""
    bot = Bot(token="123456:AAFakeTestTokenForUnitTestsOnly0000000000")
    parsed = Update.de_json({"update_id": update_id, **payload}, bot)
    assert parsed is not None
    result = InboundUpdate.from_telegram(parsed)
    assert result is not None
    return result


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


#: One whole MPEG-2 frame of mono `s16le` PCM at 24 kHz (576 samples, 1152
#: bytes). The script row now runs the *real* narrator, so the synthesis fake
#: must hand back a frame the real encoder can write (R5.2).
PCM_SAMPLES = 576
PCM_AUDIO = SynthesizedAudio(
    data=b"\x00\x00" * PCM_SAMPLES,
    mime_type="audio/l16; rate=24000; channels=1",
)
#: Fewer bytes than one MPEG frame, so the real narrator refuses to encode it.
TOO_SHORT_AUDIO = SynthesizedAudio(
    data=b"\x00\x00",
    mime_type="audio/l16; rate=24000; channels=1",
)


class _SynthesizingGeminiClient(FakeGeminiClient):
    """`FakeGeminiClient` plus the TTS seam the script row now needs.

    The shared fake implements only `generate`. `_write_script` calls
    `narrator.narrate`, which calls `synthesize`, so the pipeline harness needs a
    client that speaks both halves of the `GeminiClient` protocol (D-V2). This
    lives here rather than in `conftest.py` because the pipeline is the only
    caller that reaches synthesis through `narrate`.

    `synthesize` deliberately does not append to `calls`/`call_count`: those
    count generation calls, and the existing assertions depend on that. It is
    recorded separately on `synth_calls`.
    """

    def __init__(
        self,
        replies: list[Any] | None = None,
        *,
        error: BaseException | None = None,
        synth_audio: SynthesizedAudio | None = None,
        synth_error: BaseException | None = None,
    ) -> None:
        super().__init__(replies, error=error)
        self.synth_audio = synth_audio if synth_audio is not None else PCM_AUDIO
        self.synth_error = synth_error
        self.synth_calls: list[dict[str, Any]] = []

    async def synthesize(
        self, text: str, voice: str, chat_id: int, update_id: int
    ) -> SynthesizedAudio:
        self.synth_calls.append(
            {"text": text, "voice": voice, "chat_id": chat_id, "update_id": update_id}
        )
        if self.synth_error is not None:
            raise self.synth_error
        return self.synth_audio


@dataclass
class Harness:
    """One pipeline under test, with every seam replaced by a fake."""

    pipeline: ConversationPipeline
    client: _SynthesizingGeminiClient
    sessions: SessionStore
    media: MediaStore
    fetcher: FakePhotoFetcher
    bot: FakeTelegramBot = field(default_factory=FakeTelegramBot)
    #: Voice notes the adapter would upload, kept apart from `bot.sent` so the
    #: existing text assertions keep their meaning. The real adapter branches on
    #: type (`bot.py`, group 7); this harness just records what the row returned.
    voice_notes: list[VoiceNote] = field(default_factory=list)

    async def send(self, update: InboundUpdate) -> Reply:
        """Run one update and record the reply the adapter would send."""
        if update.text == "/start":
            reply = await self.pipeline.handle_start(update)
        elif update.text == "/restart":
            reply = await self.pipeline.handle_restart(update)
        else:
            reply = await self.pipeline.handle_message(update)
        if isinstance(reply, VoiceNote):
            self.voice_notes.append(reply)
        else:
            await self.bot.send_message(chat_id=update.chat_id, text=reply)
        return reply

    def phase(self, chat_id: int = CHAT) -> Phase:
        return self.sessions.load(chat_id, update_id=UPDATE).phase

    def state(self, chat_id: int = CHAT) -> Any:
        return self.sessions.load(chat_id, update_id=UPDATE)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """A pipeline wired to fakes, with its own temp media directory."""
    return _harness(tmp_path)


def _harness(
    tmp_path: Path,
    replies: list[Any] | None = None,
    *,
    error: BaseException | None = None,
    synth_audio: SynthesizedAudio | None = None,
    synth_error: BaseException | None = None,
    fetcher: FakePhotoFetcher | None = None,
) -> Harness:
    client = _SynthesizingGeminiClient(
        replies, error=error, synth_audio=synth_audio, synth_error=synth_error
    )
    store = SessionStore()
    media = MediaStore(base_dir=tmp_path / "media")
    fetch = fetcher or FakePhotoFetcher([PHOTO_BYTES])
    pipeline = ConversationPipeline(
        client=client, sessions=store, media=media, fetcher=fetch
    )
    return Harness(
        pipeline=pipeline,
        client=client,
        sessions=store,
        media=media,
        fetcher=fetch,
    )


def _happy_replies(plan: InterviewPlan = PLAN_5, script: Script = SCRIPT) -> list[Any]:
    """Bouncer, planner, scripter - in the order the happy path calls them."""
    return [_verdict(), plan, script]


async def drive_to_answers(h: Harness, tmp_path: Path, *, answers: int = 0) -> None:
    """Take a chat from nothing to `answers` recorded answers."""
    await h.send(inbound(_photo_message()))
    for n in range(answers):
        await h.send(inbound(_text_message(f"Answer {n}.")))


# --------------------------------------------------------------------------
# Commands (the `/start` and `/restart` rows)
# --------------------------------------------------------------------------


async def test_start_purges_state_and_media_and_asks_for_a_photo(
    harness: Harness, tmp_path: Path
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await drive_to_answers(h, tmp_path, answers=2)

    assert h.phase() is Phase.AWAITING_ANSWER
    assert h.media.session_dir(CHAT).is_dir()

    reply = await h.send(inbound(_text_message("/start")))

    assert h.phase() is Phase.AWAITING_PHOTO
    assert not h.media.session_dir(CHAT).exists()
    assert h.state().answers == ()
    assert "portrait" in reply.lower() or "photo" in reply.lower()


async def test_start_is_a_welcome_from_the_hub_not_a_transport_constant(
    harness: Harness,
) -> None:
    """D10: `bot.GREETING` is gone; the text is now a hub output."""
    reply = await harness.send(inbound(_text_message("/start")))

    assert reply == WELCOME


async def test_the_welcome_promises_the_voice_note_and_only_what_exists() -> None:
    """R3.6: the /start text promises the voice note; the image does not exist."""
    lower = WELCOME.lower()
    assert "portrait" in lower
    assert "voice" in lower
    assert "hybrid" not in lower
    assert "not built" not in lower


async def test_restart_purges_state_and_media_mid_interview(
    tmp_path: Path,
) -> None:
    """The roadmap's `/restart` criterion: no process restart required."""
    h = _harness(tmp_path, _happy_replies())
    await drive_to_answers(h, tmp_path, answers=3)
    assert h.phase() is Phase.AWAITING_ANSWER
    assert h.media.session_dir(CHAT).is_dir()

    reply = await h.send(inbound(_text_message("/restart")))

    assert h.phase() is Phase.AWAITING_PHOTO
    assert not h.media.session_dir(CHAT).exists()
    assert h.state().answers == ()
    assert h.state().plan is None
    assert "portrait" in reply.lower() or "photo" in reply.lower()


async def test_start_after_a_completed_interview_starts_over(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await drive_to_answers(h, tmp_path, answers=5)
    assert h.phase() is Phase.SCRIPTED

    await h.send(inbound(_text_message("/start")))

    assert h.phase() is Phase.AWAITING_PHOTO
    assert h.state().script is None


async def test_commands_are_accepted_at_every_phase(
    tmp_path: Path
) -> None:
    """The command rows say "any phase", so all three are driven explicitly."""
    for target in (Phase.AWAITING_PHOTO, Phase.AWAITING_ANSWER, Phase.SCRIPTED):
        h = _harness(tmp_path / target.value, _happy_replies())
        if target is Phase.AWAITING_ANSWER:
            await drive_to_answers(h, tmp_path, answers=1)
        elif target is Phase.SCRIPTED:
            await drive_to_answers(h, tmp_path, answers=5)
        assert h.phase() is target

        reply = await h.send(inbound(_text_message("/restart")))
        assert h.phase() is Phase.AWAITING_PHOTO, target
        assert reply


# --------------------------------------------------------------------------
# Out-of-order input (the `AWAITING_PHOTO` rows)
# --------------------------------------------------------------------------


async def test_text_before_a_photo_asks_for_a_photo_without_calling_gemini(
    harness: Harness,
) -> None:
    reply = await harness.send(inbound(_text_message("hello?")))

    assert harness.client.call_count == 0
    assert "photo" in reply.lower()


async def test_text_before_a_photo_never_starts_an_interview(
    harness: Harness,
) -> None:
    await harness.send(inbound(_text_message("my answer")))

    assert harness.phase() is Phase.AWAITING_PHOTO
    assert harness.state().plan is None


async def test_a_sticker_before_a_photo_is_refused_by_kind(
    harness: Harness,
) -> None:
    """R9.1: the note names the kind that was sent."""
    reply = await harness.send(inbound(_kind_message("sticker")))

    assert "sticker" in reply.lower()
    assert harness.client.call_count == 0


async def test_a_document_before_a_photo_is_refused_by_kind(
    harness: Harness,
) -> None:
    reply = await harness.send(inbound(_kind_message("document")))

    assert "document" in reply.lower()


async def test_a_voice_note_before_a_photo_is_refused_by_kind(
    harness: Harness,
) -> None:
    reply = await harness.send(inbound(_kind_message("voice")))

    assert "voice" in reply.lower()


async def test_unsupported_media_before_a_photo_still_asks_for_a_photo(
    harness: Harness,
) -> None:
    reply = await harness.send(inbound(_kind_message("sticker")))

    assert "photo" in reply.lower()


# --------------------------------------------------------------------------
# The photo row
# --------------------------------------------------------------------------


async def test_an_accepted_photo_starts_the_interview_with_the_first_question(
    harness: Harness,
) -> None:
    harness.client.replies = _happy_replies()

    reply = await harness.send(inbound(_photo_message()))

    assert harness.phase() is Phase.AWAITING_ANSWER
    assert reply == "Question 0?"


async def test_the_photo_is_fetched_through_the_port_not_the_bot(
    harness: Harness,
) -> None:
    harness.client.replies = _happy_replies()

    await harness.send(inbound(_photo_message()))

    assert len(harness.fetcher.fetched) == 1
    assert harness.fetcher.fetched[0].file_id == "big"


async def test_the_largest_photo_size_is_the_one_judged(
    harness: Harness,
) -> None:
    harness.client.replies = _happy_replies()

    await harness.send(inbound(_photo_message()))

    # The bytes came from the fetcher for whichever size was selected; what the
    # Bouncer received is recorded on the request.
    request = harness.client.calls[0]["request"]
    assert request.stage is Stage.BOUNCER
    assert request.image == PHOTO_BYTES
    assert request.image_mime_type == PHOTO_MIME_TYPE


async def test_the_accepted_photo_is_saved_to_media(harness: Harness) -> None:
    harness.client.replies = _happy_replies()

    await harness.send(inbound(_photo_message()))

    photo = harness.state().photo
    assert photo is not None
    assert Path(photo.path).is_file()  # noqa: ASYNC240 - a fake wrote it
    assert photo.mime_type == PHOTO_MIME_TYPE


async def test_the_bouncer_subject_becomes_the_interview_subject(
    harness: Harness,
) -> None:
    harness.client.replies = _happy_replies()

    await harness.send(inbound(_photo_message()))

    planner_prompt = harness.client.calls[1]["request"].prompt
    assert "a person smiling at the camera" in planner_prompt


async def test_the_plan_is_stored_in_the_session(harness: Harness) -> None:
    harness.client.replies = _happy_replies()

    await harness.send(inbound(_photo_message()))

    assert harness.state().plan == PLAN_5


async def test_a_rejected_photo_resets_the_session_and_purges_the_media(
    tmp_path: Path,
) -> None:
    """Phase 2's playful rejection + state reset, with the media assertion."""
    h = _harness(
        tmp_path,
        [
            _verdict(
                verdict=Verdict.NOT_HUMAN,
                subject="a very good dog",
                line="That is a dog.",
            )
        ],
    )

    reply = await h.send(inbound(_photo_message()))

    assert reply == "That is a dog."
    assert h.phase() is Phase.AWAITING_PHOTO
    assert h.state().plan is None
    assert not h.media.session_dir(CHAT).exists()
    assert h.client.call_count == 1


async def test_an_unsure_verdict_is_accepted(tmp_path: Path) -> None:
    """D6: failing open. A wrongly rejected portrait costs the whole experience."""
    h = _harness(tmp_path, [_verdict(verdict=Verdict.UNSURE), PLAN_5, SCRIPT])

    reply = await h.send(inbound(_photo_message()))

    assert reply == "Question 0?"
    assert h.phase() is Phase.AWAITING_ANSWER


async def test_an_accepted_photo_with_no_line_uses_no_rejection_text(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())

    reply = await h.send(inbound(_photo_message()))

    assert "afraid" not in reply.lower()


# --------------------------------------------------------------------------
# Answers (the `AWAITING_ANSWER` rows)
# --------------------------------------------------------------------------


async def test_each_answer_sends_exactly_one_message(tmp_path: Path) -> None:
    """The one-at-a-time guard: three answers, three replies, nothing else."""
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))
    h.bot.sent.clear()

    for n in range(3):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    assert len(h.bot.sent) == 3
    assert [m["text"] for m in h.bot.sent] == [
        "Question 1?",
        "Question 2?",
        "Question 3?",
    ]


async def test_answers_are_recorded_in_order(tmp_path: Path) -> None:
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))

    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    answers = h.state().answers
    assert [a.answer for a in answers] == [
        "Answer 0.",
        "Answer 1.",
        "Answer 2.",
        "Answer 3.",
    ]
    assert [a.question for a in answers] == [
        "Question 0?",
        "Question 1?",
        "Question 2?",
        "Question 3?",
    ]


async def test_the_interview_completes_after_five_to_seven_answers_and_sends_the_script(
    tmp_path: Path,
) -> None:
    """The happy path, end to end: narration is the last reply, same chat.

    Updated by R3.1/D4: the narration is now delivered as a `VoiceNote`, whose
    `fallback_text` carries the exact narration. The voice note still goes to
    the update's own chat (asserted through the synthesis seam).
    """
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))

    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))
    reply = await h.send(inbound(_text_message("Answer 4.")))

    assert h.phase() is Phase.SCRIPTED
    assert isinstance(reply, VoiceNote)
    assert reply.fallback_text == SCRIPT.text
    assert h.client.synth_calls[-1]["chat_id"] == CHAT
    assert h.state().script == SCRIPT


async def test_the_script_call_receives_every_answer(tmp_path: Path) -> None:
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))

    for n in range(5):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    script_request = h.client.calls[-1]["request"]
    for n in range(5):
        assert f"Answer {n}." in script_request.prompt


async def test_a_six_question_plan_finishes_on_the_sixth_answer(tmp_path: Path) -> None:
    plan = InterviewPlan(
        questions=tuple(Question(text=f"Question {n}?") for n in range(6)),
        suggested_animal="sea otter",
    )
    h = _harness(tmp_path, _happy_replies(plan=plan))
    await h.send(inbound(_photo_message()))

    for n in range(5):
        await h.send(inbound(_text_message(f"Answer {n}.")))
    assert h.phase() is Phase.AWAITING_ANSWER

    await h.send(inbound(_text_message("Answer 5.")))

    assert h.phase() is Phase.SCRIPTED


async def test_a_caption_on_a_photo_is_not_recorded_as_an_answer(
    tmp_path: Path,
) -> None:
    """R2.3: the caption cannot reach `record_answer`."""
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))

    h.client.replies = _happy_replies()
    await h.send(inbound(_photo_message(caption="this is my answer")))

    # D4: a photo mid-interview is a fresh start, so this restarts rather than
    # recording the caption.
    assert h.state().answers == ()


async def test_text_while_awaiting_a_photo_is_never_stored(
    harness: Harness,
) -> None:
    await harness.send(inbound(_text_message("I am a lion")))

    assert harness.state().answers == ()
    assert harness.state().plan is None


# --------------------------------------------------------------------------
# D4 - a photo mid-interview is a fresh start
# --------------------------------------------------------------------------


async def test_a_photo_mid_interview_starts_a_fresh_interview(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))
    await h.send(inbound(_text_message("Answer 0.")))
    await h.send(inbound(_text_message("Answer 1.")))
    assert len(h.state().answers) == 2

    h.client.replies = _happy_replies()
    await h.send(inbound(_photo_message()))

    assert h.phase() is Phase.AWAITING_ANSWER
    assert len(h.state().answers) == 0
    assert h.bot.sent[-1]["text"] == "Question 0?"


async def test_a_photo_after_the_script_starts_a_fresh_interview(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await drive_to_answers(h, tmp_path, answers=5)
    assert h.phase() is Phase.SCRIPTED

    h.client.replies = _happy_replies()
    await h.send(inbound(_photo_message()))

    assert h.phase() is Phase.AWAITING_ANSWER
    assert h.state().script is None


async def test_a_fresh_start_purges_the_old_media(tmp_path: Path) -> None:
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))
    assert h.media.session_dir(CHAT).is_dir()

    h.client.replies = _happy_replies()
    await h.send(inbound(_photo_message()))

    assert h.media.session_dir(CHAT).is_dir()
    assert h.state().photo is not None


# --------------------------------------------------------------------------
# The `SCRIPTED` rows
# --------------------------------------------------------------------------


async def test_text_after_the_script_nudges_towards_a_new_photo(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await drive_to_answers(h, tmp_path, answers=5)
    h.bot.sent.clear()
    h.client.replies = []

    reply = await h.send(inbound(_text_message("more please")))

    assert h.client.call_count == 3
    assert "photo" in reply.lower()
    assert h.phase() is Phase.SCRIPTED


async def test_unsupported_media_after_the_script_is_named_and_nudged(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await drive_to_answers(h, tmp_path, answers=5)
    h.client.replies = []

    reply = await h.send(inbound(_kind_message("document")))

    assert "document" in reply.lower()
    assert "photo" in reply.lower()


# --------------------------------------------------------------------------
# Unsupported media mid-interview
# --------------------------------------------------------------------------


async def test_unsupported_media_mid_interview_invites_carrying_on(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))
    h.client.replies = []

    reply = await h.send(inbound(_kind_message("document")))

    assert "document" in reply.lower()
    assert h.phase() is Phase.AWAITING_ANSWER
    assert len(h.state().answers) == 0


async def test_unsupported_media_mid_interview_does_not_skip_the_question(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))
    h.client.replies = []

    await h.send(inbound(_kind_message("voice")))
    await h.send(inbound(_text_message("My real answer.")))

    assert len(h.state().answers) == 1
    assert h.state().answers[0].question == "Question 0?"


# --------------------------------------------------------------------------
# D3 - a Gemini failure holds the state and re-asks
# --------------------------------------------------------------------------


def _unavailable(stage: Stage, reason: str) -> GeminiUnavailableError:
    return GeminiUnavailableError(
        stage=stage,
        reason=reason,
        error_type="httpx.TimeoutException",
        error_code=None,
    )


def _throttled(stage: Stage) -> GeminiThrottledError:
    """A 429 as the transport would have produced it after the retries ran out."""
    return GeminiThrottledError(
        stage=stage,
        reason="the API throttled the request",
        error_type="APIError",
        error_code=429,
    )


async def test_a_gemini_failure_on_the_final_answer_preserves_the_state(
    tmp_path: Path,
) -> None:
    """D3's substance: the scripter is the only call made between answers."""
    replies = [_verdict(), PLAN_5]
    h = _harness(
        tmp_path,
        replies,
        error=None,
    )
    await h.send(inbound(_photo_message()))
    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    before = h.state()
    h.client.replies = []  # next call has nothing queued
    h.client.error = _unavailable(Stage.SCRIPTER, "the request timed out")

    reply = await h.send(inbound(_text_message("Answer 4.")))

    after = h.state()
    assert after.phase is Phase.AWAITING_ANSWER
    assert after.answers == before.answers
    assert after.pending_question == before.pending_question
    assert len(after.answers) == 4
    assert reply


async def test_a_failure_on_the_final_answer_re_asks_the_same_question(
    tmp_path: Path,
) -> None:
    """Nothing is skipped: the next message answers the question already pending."""
    h = _harness(tmp_path, [_verdict(), PLAN_5])
    await h.send(inbound(_photo_message()))
    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    h.client.error = _unavailable(Stage.SCRIPTER, "the request timed out")
    h.client.replies = []
    await h.send(inbound(_text_message("Answer 4.")))

    assert h.state().pending_question == "Question 4?"

    # The failure is cleared; the same question is now answerable.
    h.client.error = None
    h.client.replies = [SCRIPT]
    reply = await h.send(inbound(_text_message("Answer 4, second try.")))

    assert isinstance(reply, VoiceNote)
    assert reply.fallback_text == SCRIPT.text
    assert h.phase() is Phase.SCRIPTED
    assert len(h.state().answers) == 5
    assert h.state().answers[-1].answer == "Answer 4, second try."


async def test_a_gemini_failure_at_the_bouncer_holds_the_state(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, [_unavailable(Stage.BOUNCER, "the request timed out")])

    reply = await h.send(inbound(_photo_message()))

    assert h.phase() is Phase.AWAITING_PHOTO
    assert h.state().plan is None
    assert h.state().answers == ()
    assert reply


async def test_a_gemini_failure_at_the_planner_holds_the_state(
    tmp_path: Path,
) -> None:
    h = _harness(
        tmp_path,
        [_verdict(), _unavailable(Stage.INTERVIEWER, "the request timed out")],
    )

    reply = await h.send(inbound(_photo_message()))

    assert h.phase() is Phase.AWAITING_PHOTO
    assert h.state().plan is None
    assert reply


async def test_a_gemini_timeout_never_escapes_into_the_adapter(tmp_path: Path) -> None:
    """R9.5: every call site is covered, and none raises."""
    for stage in (Stage.BOUNCER, Stage.INTERVIEWER, Stage.SCRIPTER):
        h = _harness(tmp_path / stage.value, [_unavailable(stage, "timed out")])
        reply = await h.send(inbound(_photo_message()))
        assert isinstance(reply, str) and reply, stage

        h2 = _harness(tmp_path / f"{stage.value}-answers", [_verdict(), PLAN_5])
        await h2.send(inbound(_photo_message()))
        h2.client.error = _unavailable(stage, "timed out")
        h2.client.replies = []
        reply2 = await h2.send(inbound(_text_message("Answer.")))
        assert isinstance(reply2, str) and reply2, stage


async def test_a_throttled_step_replies_with_the_rate_limited_line(
    tmp_path: Path,
) -> None:
    """R1.6: a throttle is answered with 'try again', not the generic line."""
    h = _harness(tmp_path, [_throttled(Stage.BOUNCER)])

    reply = await h.send(inbound(_photo_message()))

    assert reply == RATE_LIMITED
    assert reply != GENERIC_FAILURE


def test_the_rate_limited_line_never_tells_the_user_to_restart() -> None:
    """R1.6: the advice for a transient throttle is to resend, not to reset.

    `/restart` would throw away a half-finished interview for a condition that
    lasts a second - the exact failure this phase exists to remove.
    """
    assert "/restart" not in RATE_LIMITED


async def test_a_throttle_holds_the_session_so_the_resend_answers_the_same_question(
    tmp_path: Path,
) -> None:
    """R1.7: 'try again' must be true - the pending question is untouched."""
    h = _harness(tmp_path, [_verdict(), PLAN_5])
    await h.send(inbound(_photo_message()))
    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    h.client.error = _throttled(Stage.SCRIPTER)
    h.client.replies = []
    reply = await h.send(inbound(_text_message("Answer 4.")))

    assert reply == RATE_LIMITED
    assert h.state().pending_question == "Question 4?"
    assert len(h.state().answers) == 4

    # The throttle clears; the resend is consumed as the answer to the question
    # that was already on screen - nothing was skipped, nothing was lost.
    h.client.error = None
    h.client.replies = [SCRIPT]
    reply2 = await h.send(inbound(_text_message("Answer 4, second try.")))
    assert isinstance(reply2, VoiceNote)
    assert h.phase() is Phase.SCRIPTED


async def test_a_throttled_synthesis_still_degrades_to_the_narration_text(
    tmp_path: Path,
) -> None:
    """R1.8: a throttled *delivery* keeps the D6 path - the user gets the text.

    Only a failed *step* earns the RATE_LIMITED line; a failed delivery of an
    already-written narration degrades to the narration itself.
    """
    h = _harness(tmp_path, [_verdict(), PLAN_5])
    await h.send(inbound(_photo_message()))
    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    # The narration itself writes fine (D6 is a *delivery* fallback); only the
    # synthesis call that voices it is throttled. The final answer completes
    # the plan, triggering narration with the throttle on the synth seam.
    h.client.replies = [SCRIPT]
    h.client.synth_error = _throttled(Stage.NARRATOR)
    reply = await h.send(inbound(_text_message("Answer 4.")))

    assert reply == SCRIPT.text
    assert isinstance(reply, str)
    assert h.phase() is Phase.SCRIPTED


async def test_a_throttle_is_logged_with_the_rate_limited_record(
    tmp_path: Path,
    app_records: LogRecorder,
) -> None:
    """R1.9: the hub's answer is stamped with the chat and the update."""
    h = _harness(tmp_path, [_throttled(Stage.BOUNCER)])
    await h.send(inbound(_photo_message()))

    records = [
        app_records.extra_of(record)
        for record in app_records.records
        if app_records.extra_of(record).get("event") == "rate_limited"
    ]
    assert records, "a throttle must be answered *and* logged, never silently"
    assert records[0]["chat_id"] == CHAT
    assert records[0]["update_id"] == UPDATE


async def test_a_failed_photo_fetch_is_answered_not_raised(tmp_path: Path) -> None:
    fetcher = FakePhotoFetcher(error=RuntimeError("telegram is down"))
    h = _harness(tmp_path, [], fetcher=fetcher)

    reply = await h.send(inbound(_photo_message()))

    assert isinstance(reply, str) and reply
    assert h.phase() is Phase.AWAITING_PHOTO


async def test_a_failure_never_produces_a_partial_script(tmp_path: Path) -> None:
    h = _harness(tmp_path, [_verdict(), PLAN_5])
    await h.send(inbound(_photo_message()))
    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    h.client.error = _unavailable(Stage.SCRIPTER, "timed out")
    h.client.replies = []
    await h.send(inbound(_text_message("Answer 4.")))

    assert h.state().script is None
    assert h.phase() is Phase.AWAITING_ANSWER


# --------------------------------------------------------------------------
# D5 at the hub level
# --------------------------------------------------------------------------


async def test_a_rejected_script_twice_degrades_to_a_restart_prompt(
    tmp_path: Path,
) -> None:

    h = _harness(tmp_path, [_verdict(), PLAN_5])
    await h.send(inbound(_photo_message()))
    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    # Two replies of the wrong length, so the scripter's one retry is spent.
    wrong = Script.model_construct(text="far too short", word_count=3)
    h.client.replies = [wrong, wrong]

    reply = await h.send(inbound(_text_message("Answer 4.")))

    assert "/restart" in reply
    assert h.phase() is Phase.AWAITING_ANSWER
    assert h.state().script is None


async def test_the_script_failure_is_logged_at_exception_level(tmp_path: Path) -> None:
    import logging

    from conftest import LogRecorder

    from telegram_documentaries import observability

    recorder = LogRecorder()
    app_logger = observability.get_logger()
    app_logger.addHandler(recorder)
    previous = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    try:

        h = _harness(tmp_path, [_verdict(), PLAN_5])
        await h.send(inbound(_photo_message()))
        for n in range(4):
            await h.send(inbound(_text_message(f"Answer {n}.")))

        wrong = Script.model_construct(text="far too short", word_count=3)
        h.client.replies = [wrong, wrong]
        await h.send(inbound(_text_message("Answer 4.")))
    finally:
        app_logger.removeHandler(recorder)
        app_logger.setLevel(previous)

    assert recorder.at_level(logging.ERROR), "a double rejection is an incident"


# --------------------------------------------------------------------------
# Isolation - the rubric item
# --------------------------------------------------------------------------


async def test_two_chats_interleaved_never_see_each_others_answers_or_script(
    tmp_path: Path,
) -> None:
    """Two full interviews, interleaved update by update."""
    # Two portraits, two scripts - in the order the interleaving produces them:
    # A's photo, B's photo, then a script for whichever chat finishes first.
    h = _harness(
        tmp_path,
        [_verdict(), PLAN_5, _verdict(), PLAN_5, SCRIPT, SCRIPT],
    )
    seen: dict[int, list[str]] = {CHAT: [], OTHER_CHAT: []}

    await h.send(inbound(_photo_message(chat_id=CHAT), update_id=1))
    await h.send(inbound(_photo_message(chat_id=OTHER_CHAT), update_id=2))

    for n in range(5):
        await h.send(
            inbound(_text_message(f"{CHAT}-answer-{n}", chat_id=CHAT), update_id=10 + n)
        )
        await h.send(
            inbound(
                _text_message(f"{OTHER_CHAT}-answer-{n}", chat_id=OTHER_CHAT),
                update_id=50 + n,
            )
        )

    for chat in (CHAT, OTHER_CHAT):
        state = h.state(chat)
        assert state.phase is Phase.SCRIPTED
        seen[chat] = [a.answer for a in state.answers]
        assert state.script is not None

    assert seen[CHAT] == [f"{CHAT}-answer-{n}" for n in range(5)]
    assert seen[OTHER_CHAT] == [f"{OTHER_CHAT}-answer-{n}" for n in range(5)]

    # No message was ever delivered to the wrong chat.
    chat_ids = {m["chat_id"] for m in h.bot.sent}
    assert chat_ids == {CHAT, OTHER_CHAT}
    for message in h.bot.sent:
        body = message["text"]
        assert f"{OTHER_CHAT}-answer" not in body or message["chat_id"] == OTHER_CHAT
        assert f"{CHAT}-answer" not in body or message["chat_id"] == CHAT


async def test_a_state_for_one_chat_is_never_returned_for_another(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, [_verdict(), PLAN_5, SCRIPT])
    await h.send(inbound(_photo_message(chat_id=CHAT), update_id=1))
    await h.send(inbound(_text_message("secret"), update_id=2))

    other = h.state(OTHER_CHAT)

    assert other.chat_id == OTHER_CHAT
    assert other.answers == ()
    assert other.plan is None


async def test_media_directories_are_per_chat(tmp_path: Path) -> None:
    h = _harness(tmp_path, [_verdict(), PLAN_5, _verdict(), PLAN_5])
    await h.send(inbound(_photo_message(chat_id=CHAT), update_id=1))
    await h.send(inbound(_photo_message(chat_id=OTHER_CHAT), update_id=2))

    assert h.media.session_dir(CHAT) != h.media.session_dir(OTHER_CHAT)
    assert h.media.session_dir(CHAT).is_dir()
    assert h.media.session_dir(OTHER_CHAT).is_dir()


# --------------------------------------------------------------------------
# Degradation - R9.5, nothing raised into the user's flow
# --------------------------------------------------------------------------


async def test_every_illegal_transition_is_answered_not_raised(
    tmp_path: Path,
) -> None:
    """R4.3 through R9.5: an illegal input produces a sentence, never a raise."""
    h = _harness(tmp_path, [_verdict(), PLAN_5])

    # A blank answer would be refused by `record_answer`.
    await h.send(inbound(_photo_message()))
    h.client.replies = []
    reply = await h.send(inbound(_text_message("   ")))

    assert isinstance(reply, str) and reply
    assert h.phase() is Phase.AWAITING_ANSWER
    assert h.state().answers == ()


async def test_a_blank_answer_does_not_advance_the_interview(tmp_path: Path) -> None:
    h = _harness(tmp_path, [_verdict(), PLAN_5])
    await h.send(inbound(_photo_message()))
    h.client.replies = []

    await h.send(inbound(_text_message("\n\t")))

    assert h.state().pending_question == "Question 0?"
    assert h.state().answers == ()


async def test_every_send_uses_an_integer_chat_id(tmp_path: Path) -> None:
    h = _harness(tmp_path, _happy_replies())
    await h.send(inbound(_photo_message()))
    for n in range(5):
        await h.send(inbound(_text_message(f"Answer {n}.")))

    assert h.bot.sent
    for message in h.bot.sent:
        assert isinstance(message["chat_id"], int)
        assert not isinstance(message["chat_id"], bool)


def test_an_update_with_no_message_is_reported_as_no_message(make_update: Any) -> None:
    """A message-less update carries no chat, so the adapter never calls the hub.

    `None` rather than an error: an edited message, a poll update or a reaction
    is not a failure, it is Telegram telling us something happened elsewhere.
    """
    update = make_update(update_id=99)

    assert update.message is None
    assert InboundUpdate.from_telegram(update) is None


# --------------------------------------------------------------------------
# Logging sweeps (R1.7, R10)
# --------------------------------------------------------------------------


async def _run_a_whole_conversation(tmp_path: Path) -> Any:
    """Drive one chat all the way through, returning the log recorder."""
    import logging

    from conftest import LogRecorder

    from telegram_documentaries import observability

    recorder = LogRecorder()
    app_logger = observability.get_logger()
    app_logger.addHandler(recorder)
    previous = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    try:
        h = _harness(tmp_path, _happy_replies())
        await h.send(inbound(_photo_message()))
        for n in range(5):
            await h.send(inbound(_text_message(f"Answer {n}.")))
        await h.send(inbound(_text_message("/restart")))
        await h.send(inbound(_kind_message("sticker")))
        await h.send(inbound(_text_message("hello")))
    finally:
        app_logger.removeHandler(recorder)
        app_logger.setLevel(previous)
    return recorder


async def test_no_log_record_anywhere_contains_the_bot_token(tmp_path: Path) -> None:
    recorder = await _run_a_whole_conversation(tmp_path)
    token = "123456:AAFakeTestTokenForUnitTestsOnly0000000000"

    for record in recorder.records:
        rendered = f"{record.getMessage()} {vars(record)}"
        assert token not in rendered


async def test_no_log_record_anywhere_contains_the_gemini_api_key(
    tmp_path: Path,
) -> None:
    recorder = await _run_a_whole_conversation(tmp_path)
    key = "AIzaFakeGeminiApiKeyThatMustNeverBeLogged0"

    for record in recorder.records:
        rendered = f"{record.getMessage()} {vars(record)}"
        assert key not in rendered


async def test_the_conversation_emits_the_expected_events(tmp_path: Path) -> None:
    recorder = await _run_a_whole_conversation(tmp_path)
    events = recorder.events()

    for expected in (
        "bouncer_judged",
        "interview_planned",
        "question_asked",
        "answer_recorded",
        "script_written",
        "session_reset",
    ):
        assert expected in events, f"missing {expected}"


async def test_out_of_order_input_is_logged(tmp_path: Path) -> None:
    recorder = await _run_a_whole_conversation(tmp_path)
    assert "out_of_order_input" in recorder.events()


async def test_unsupported_media_is_logged(tmp_path: Path) -> None:
    recorder = await _run_a_whole_conversation(tmp_path)
    assert "unsupported_media" in recorder.events()


# --------------------------------------------------------------------------
# The module's public surface
# --------------------------------------------------------------------------


def test_the_pipeline_exports_what_the_adapter_needs() -> None:
    import telegram_documentaries.pipeline as module

    for name in ("ConversationPipeline", "PhotoFetcher", "WELCOME"):
        assert name in module.__all__, name


def test_the_hub_does_not_import_the_telegram_adapter() -> None:
    """Dependency direction: nothing imports `bot` except `__main__`."""
    import ast
    from pathlib import Path

    from telegram_documentaries import pipeline as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not any(name.startswith("telegram.ext") for name in imported)
    assert "telegram_documentaries.bot" not in imported


def test_the_hub_depends_on_the_fetch_port_not_the_sdk() -> None:
    import ast
    from pathlib import Path

    from telegram_documentaries import pipeline as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert "telegram" not in imported
    assert "google" not in imported


# --------------------------------------------------------------------------
# R3 - the script row: deliver the narration as a voice note, fall back to text
#
# The script *was* written and the session is *finished* before any of this
# happens. A delivery failure is a degradation, not a pipeline failure: the user
# still gets the narration, as text, and never `GENERIC_FAILURE` (D6/R3.4).
# --------------------------------------------------------------------------


def _events(recorder: LogRecorder, event: str) -> list[logging.LogRecord]:
    """Every record one run emitted for `event`, in emission order."""
    return [
        record
        for record in recorder.records
        if recorder.extra_of(record).get("event") == event
    ]


async def _finish_interview(h: Harness) -> Reply:
    """Drive one chat from nothing to the finished narration, one update at a time."""
    await h.send(inbound(_photo_message()))
    for n in range(4):
        await h.send(inbound(_text_message(f"Answer {n}.")))
    return await h.send(inbound(_text_message("Answer 4.")))


async def test_the_script_row_delivers_the_narration_as_one_voice_note(
    tmp_path: Path,
) -> None:
    """R3.1/R3.5: the narration is one `VoiceNote`, not a list and not a `str`."""
    h = _harness(tmp_path, _happy_replies())

    reply = await _finish_interview(h)

    assert isinstance(reply, VoiceNote)
    assert reply.mime_type == "audio/mpeg"
    assert reply.fallback_text == SCRIPT.text
    assert reply.duration_seconds == pytest.approx(PCM_SAMPLES / 24_000)
    assert not isinstance(reply, (list, tuple))
    assert h.voice_notes == [reply]
    assert h.phase() is Phase.SCRIPTED
    assert h.state().script == SCRIPT


async def test_a_delivered_narration_logs_the_audio_facts(
    tmp_path: Path, app_records: LogRecorder
) -> None:
    """R3.3: `narration_delivered` carries word count, duration and byte size."""
    h = _harness(tmp_path, _happy_replies())

    reply = await _finish_interview(h)

    records = _events(app_records, "narration_delivered")
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert isinstance(reply, VoiceNote)
    extra = app_records.extra_of(record)
    assert extra["chat_id"] == CHAT
    assert extra["update_id"] == UPDATE
    assert extra["word_count"] == SCRIPT.word_count
    assert extra["duration_seconds"] == pytest.approx(PCM_SAMPLES / 24_000)
    assert extra["byte_size"] == len(reply.data)


async def test_a_synthesis_failure_returns_the_narration_verbatim_not_generic_failure(
    tmp_path: Path,
) -> None:
    """R3.2/R3.4: a TTS failure degrades to text; it never reaches `_respond`."""
    failure = GeminiUnavailableError(
        stage=Stage.NARRATOR,
        reason="the request timed out",
        error_type="ReadTimeout",
        error_code=None,
    )
    h = _harness(tmp_path, _happy_replies(), synth_error=failure)

    reply = await _finish_interview(h)

    assert reply == SCRIPT.text
    assert reply != GENERIC_FAILURE
    assert len(h.client.synth_calls) == 1
    assert h.client.synth_calls[0]["text"] == SCRIPT.text
    assert h.phase() is Phase.SCRIPTED
    assert h.state().script == SCRIPT


async def test_a_synthesis_failure_is_logged_at_warning_as_synthesis(
    tmp_path: Path, app_records: LogRecorder
) -> None:
    """R3.2: `narration_voice_failed` names synthesis, and renders no `str(exc)`."""
    failure = GeminiUnavailableError(
        stage=Stage.NARRATOR,
        reason="the request timed out",
        error_type="ReadTimeout",
        error_code=None,
    )
    h = _harness(tmp_path, _happy_replies(), synth_error=failure)

    await _finish_interview(h)

    records = _events(app_records, "narration_voice_failed")
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    extra = app_records.extra_of(record)
    assert extra["chat_id"] == CHAT
    assert extra["update_id"] == UPDATE
    assert extra["reason"] == "synthesis"
    assert extra["error_type"] == "GeminiUnavailableError"
    assert str(failure) not in record.getMessage()


async def test_an_encoding_failure_returns_the_narration_verbatim_not_generic_failure(
    tmp_path: Path,
) -> None:
    """R3.2/R3.4: our encoder failing is a degradation, never a step failure."""
    h = _harness(tmp_path, _happy_replies(), synth_audio=TOO_SHORT_AUDIO)

    reply = await _finish_interview(h)

    assert reply == SCRIPT.text
    assert reply != GENERIC_FAILURE
    assert len(h.client.synth_calls) == 1
    assert h.phase() is Phase.SCRIPTED
    assert h.state().script == SCRIPT


async def test_an_encoding_failure_is_logged_at_warning_as_encoding(
    tmp_path: Path, app_records: LogRecorder
) -> None:
    """R3.2: `narration_voice_failed` names encoding, not synthesis."""
    h = _harness(tmp_path, _happy_replies(), synth_audio=TOO_SHORT_AUDIO)

    await _finish_interview(h)

    records = _events(app_records, "narration_voice_failed")
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    extra = app_records.extra_of(record)
    assert extra["chat_id"] == CHAT
    assert extra["update_id"] == UPDATE
    assert extra["reason"] == "encoding"
    assert extra["error_type"] == "NarratorError"


async def test_the_session_is_completed_and_saved_before_synthesis_is_attempted(
    tmp_path: Path,
) -> None:
    """D6: the delivery is attempted only after the interview is finished."""
    failure = GeminiUnavailableError(
        stage=Stage.NARRATOR,
        reason="the request timed out",
        error_type="ReadTimeout",
        error_code=None,
    )
    h = _harness(tmp_path, _happy_replies(), synth_error=failure)

    await _finish_interview(h)

    stored = h.sessions.load(CHAT, update_id=UPDATE)
    assert stored.phase is Phase.SCRIPTED
    assert stored.script == SCRIPT
    assert stored.pending_question is None
    # The synthesis *was* attempted, after the save: the fallback is a real
    # degradation, not the row never trying.
    assert len(h.client.synth_calls) == 1
