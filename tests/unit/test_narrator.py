"""RED: the Narrator - synthesize, encode, hand back a sendable voice note (R2).

The Narrator is two boundaries in one small function, and the tests pin the line
between them:

* **Gemini's half is not ours to swallow (D6).** A synthesis failure propagates
  untouched. Whether to fall back to text is the pipeline's decision, one level
  up; catching it here would hide a delivery problem behind a stage failure.
* **Our half is named as ours (R2.5).** Once the audio *arrived*, a failure to
  encode it is `NarratorError` - deliberately not a `GeminiError`, so the caller
  can report which half gave way. No exception's `str()` is ever rendered.

The sample rate is read from the validated `SynthesizedAudio`, never assumed
(R2.4): the test encodes at 48 kHz and decodes the MP3 frame header back to
prove the encoder was told *that* rate, not a hardcoded 24000.

`lameenc` is real and local (R5.2) - no mock of the encoder, no network, no
credential. The synthesis seam is a fake client that returns a canned
`SynthesizedAudio` or raises a queued error.
"""

from __future__ import annotations

from typing import Any

import pytest

from telegram_documentaries import contracts, narrator
from telegram_documentaries.contracts import VoiceNote
from telegram_documentaries.gemini import (
    GeminiError,
    GeminiUnavailableError,
    Stage,
    SynthesizedAudio,
)
from telegram_documentaries.narrator import DEFAULT_VOICE, NarratorError, narrate

CHAT = 8767055318
UPDATE = 1
NARRATION = (
    "A sea otter, poised at the water's edge, regards the camera with the "
    "weary composure of middle management on a Monday."
)

#: One whole MPEG-2 frame of mono `s16le` PCM at 24 kHz (576 samples, 1152
#: bytes), so the encoder always has a complete frame to write (R5.2).
SAMPLES_PER_FRAME_24K = 576

#: MPEG sample-rate tables, indexed by the header's version and rate bits. Used
#: to read back what the encoder was actually told (R2.4).
_MPEG1_RATES = (44100, 48000, 32000)
_MPEG2_RATES = (22050, 24000, 16000)
_MPEG25_RATES = (11025, 12000, 8000)


def _audio(*, sample_rate: int = 24_000, samples: int = SAMPLES_PER_FRAME_24K) -> SynthesizedAudio:
    """Validated PCM at a chosen rate and length."""
    return SynthesizedAudio(
        data=b"\x00\x00" * samples,
        mime_type=f"audio/l16; rate={sample_rate}; channels=1",
    )


def _mp3_sample_rate(data: bytes) -> int:
    """The sample rate encoded in the first MPEG frame's header.

    The rate the encoder was told is written into the frame, so reading it back
    is how the test proves `set_in_sample_rate` came from the validated audio
    rather than from a literal 24000 (R2.4).
    """
    offset = 0
    if data.startswith(b"ID3"):
        size = (
            (data[6] & 0x7F) << 21
            | (data[7] & 0x7F) << 14
            | (data[8] & 0x7F) << 7
            | (data[9] & 0x7F)
        )
        offset = 10 + size
    header = data[offset : offset + 4]
    version = (header[1] >> 3) & 0b11
    rate_index = (header[2] >> 2) & 0b11
    table = {0b11: _MPEG1_RATES, 0b10: _MPEG2_RATES, 0b00: _MPEG25_RATES}[version]
    return table[rate_index]


class _FakeSynthClient:
    """The synthesis seam: returns a canned `SynthesizedAudio`, or raises.

    Records every call so the test can assert *what* the Narrator asked for, not
    just what came back. The fake implements `synthesize` only; the Narrator
    sees exactly one method of the Gemini seam.
    """

    def __init__(
        self,
        *,
        audio: SynthesizedAudio | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._audio = audio
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def synthesize(
        self,
        text: str,
        voice: str,
        chat_id: int,
        update_id: int,
    ) -> SynthesizedAudio:
        self.calls.append(
            {"text": text, "voice": voice, "chat_id": chat_id, "update_id": update_id}
        )
        if self._error is not None:
            raise self._error
        assert self._audio is not None, "the fake was asked to return nothing"
        return self._audio


# --------------------------------------------------------------------------
# Happy path (R2.3, R2.4, R2.6)
# --------------------------------------------------------------------------


async def test_narrate_returns_a_validated_sendable_voice_note() -> None:
    """The whole point: PCM in, a note the adapter can already upload out."""
    client = _FakeSynthClient(audio=_audio())

    note = await narrate(client, NARRATION, chat_id=CHAT, update_id=UPDATE)

    assert isinstance(note, VoiceNote)
    assert note.mime_type == "audio/mpeg"
    assert contracts.has_mp3_header(note.data)
    assert note.duration_seconds == pytest.approx(SAMPLES_PER_FRAME_24K / 24_000)
    assert note.fallback_text == NARRATION


async def test_narrate_asks_the_client_for_the_narration_and_voice() -> None:
    """R2.3: the narration and the chosen voice go to the one seam, correlated."""
    client = _FakeSynthClient(audio=_audio())

    await narrate(client, NARRATION, chat_id=CHAT, update_id=UPDATE, voice="Fenrir")

    assert client.calls == [
        {
            "text": NARRATION,
            "voice": "Fenrir",
            "chat_id": CHAT,
            "update_id": UPDATE,
        }
    ]


def test_the_default_voice_is_kore() -> None:
    """D2: the verified voice, as a named constant, one line to change."""
    assert DEFAULT_VOICE == "Kore"


async def test_narrate_defaults_to_the_kore_voice() -> None:
    client = _FakeSynthClient(audio=_audio())

    await narrate(client, NARRATION, chat_id=CHAT, update_id=UPDATE)

    assert client.calls[0]["voice"] == DEFAULT_VOICE


def test_the_encoder_settings_are_named_constants() -> None:
    """R2.4: bit rate, quality and channel count are named, not buried numbers."""
    assert narrator.MP3_BIT_RATE_KBPS == 64
    assert narrator.MP3_QUALITY == 2
    assert narrator.MP3_CHANNELS == 1


async def test_encoding_uses_the_validated_sample_rate_not_a_hardcoded_24000() -> None:
    """R2.4: the rate comes from `SynthesizedAudio`, proven via the MP3 header.

    The fixture speaks at 48 kHz. The frame header must read back as 48 kHz, and
    the duration must be the 48 kHz one - if the encoder had been handed 24000,
    both would disagree.
    """
    client = _FakeSynthClient(audio=_audio(sample_rate=48_000, samples=1152))

    note = await narrate(client, NARRATION, chat_id=CHAT, update_id=UPDATE)

    assert _mp3_sample_rate(note.data) == 48_000
    assert note.duration_seconds == pytest.approx(1152 / 48_000)


# --------------------------------------------------------------------------
# Gemini's half propagates; ours is `NarratorError` (D6, R2.5)
# --------------------------------------------------------------------------


async def test_a_gemini_failure_propagates_unchanged() -> None:
    """D6: whether to degrade to text is the pipeline's decision, not ours."""
    failure = GeminiUnavailableError(
        stage=Stage.NARRATOR,
        reason="the request timed out",
        error_type="ReadTimeout",
        error_code=None,
    )
    client = _FakeSynthClient(error=failure)

    with pytest.raises(GeminiUnavailableError) as caught:
        await narrate(client, NARRATION, chat_id=CHAT, update_id=UPDATE)

    assert caught.value is failure


def test_narrator_error_is_not_a_gemini_error() -> None:
    """R2.5: two boundaries, two failure classes, so the caller can tell them apart."""
    assert not issubclass(NarratorError, GeminiError)


async def test_an_encoder_failure_raises_narrator_error_without_rendering_the_cause() -> None:
    """R2.5: our failure is named as ours, and no `str(exc)` is rendered.

    123 Hz is a value the audio contract accepts (`rate` is any positive int) and
    the real encoder refuses, so the failure is genuine rather than mocked. Only
    the exception's class name reaches the message; the original is chained
    ``from None``.
    """
    client = _FakeSynthClient(audio=_audio(sample_rate=123))

    with pytest.raises(NarratorError) as caught:
        await narrate(client, NARRATION, chat_id=CHAT, update_id=UPDATE)

    error = caught.value
    assert error.error_type == "RuntimeError"
    assert "LAME encoding failed" not in str(error)
    assert error.__cause__ is None


# --------------------------------------------------------------------------
# Too-short PCM is refused, never encoded into an empty-sounding note (R2.5)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("samples", [1, SAMPLES_PER_FRAME_24K - 1])
async def test_pcm_shorter_than_one_frame_raises_narrator_error(samples: int) -> None:
    """`lameenc` will wrap a single sample in a valid-looking frame of silence."""
    client = _FakeSynthClient(audio=_audio(samples=samples))

    with pytest.raises(NarratorError):
        await narrate(client, NARRATION, chat_id=CHAT, update_id=UPDATE)


async def test_the_one_frame_boundary_is_derived_from_the_validated_rate() -> None:
    """At 48 kHz a whole MPEG-1 frame is 1152 samples, not 576 (R2.4, R2.5)."""
    too_short = _FakeSynthClient(audio=_audio(sample_rate=48_000, samples=1151))
    just_enough = _FakeSynthClient(audio=_audio(sample_rate=48_000, samples=1152))

    with pytest.raises(NarratorError):
        await narrate(too_short, NARRATION, chat_id=CHAT, update_id=UPDATE)

    note = await narrate(just_enough, NARRATION, chat_id=CHAT, update_id=UPDATE)
    assert contracts.has_mp3_header(note.data)
