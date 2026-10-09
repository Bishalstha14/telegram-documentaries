"""The Narrator: turn synthesized speech into a sendable voice note (R2).

Three rules govern this module.

**Not an agent, a direct function (MISSION).** :func:`narrate` is one plain
coroutine - synthesize, encode, validate, return. There is no state, no
interview and no second model call here: the narration already exists, and this
only makes it audible.

**The sample rate is read, never assumed (R2.4).** Gemini returned ``audio/l16``
at a rate it declared, and :class:`SynthesizedAudio` already parsed and validated
that rate. The encoder is told *that* value, so a model that speaks at a
different rate later is encoded correctly rather than silently sped up or slowed
down.

**A failure here is ours, and is named as ours (R2.5).** A Gemini failure
propagates untouched: whether to degrade to text is the pipeline's decision, one
level up (D6). But if the audio arrived and *we* could not encode it, that is
:class:`NarratorError` - deliberately not a :class:`GeminiError`, so the caller
can tell which half gave way. No exception's ``str()`` is ever rendered:
anything wrapped whose message could carry a payload is chained ``from None``.
"""

from __future__ import annotations

import lameenc  # type: ignore[import-not-found]

from telegram_documentaries.contracts import VoiceNote, has_mp3_header
from telegram_documentaries.gemini import GeminiClient, SynthesizedAudio, VoiceName

__all__ = [
    "DEFAULT_VOICE",
    "MP3_BIT_RATE_KBPS",
    "MP3_CHANNELS",
    "MP3_QUALITY",
    "NarratorError",
    "narrate",
]

#: The chosen voice (D2): the one verified in this repository, named here behind
#: the `VoiceName` literal so a typo is a `mypy` failure and a change of voice is
#: one line.
DEFAULT_VOICE: VoiceName = "Kore"

#: The encoder settings (R2.4), named rather than buried in the call. 64 kbps
#: mono is ample for a single voice at 60-90 words, and quality 2 is lame's
#: near-best setting at a cost that does not matter at this length.
MP3_BIT_RATE_KBPS = 64
MP3_QUALITY = 2
MP3_CHANNELS = 1

#: Samples in one MPEG audio frame. The MPEG version the rate selects decides
#: the length: MPEG-1 (>= 32 kHz) carries 1152 samples per frame, MPEG-2/2.5
#: (< 32 kHz) carries 576. Read from the validated rate, so "shorter than a
#: frame" is judged against the format actually being written.
_MPEG1_MIN_SAMPLE_RATE = 32_000
_MPEG1_FRAME_SAMPLES = 1152
_MPEG2_FRAME_SAMPLES = 576

#: Two bytes per mono `s16le` sample.
_BYTES_PER_SAMPLE = 2


class NarratorError(Exception):
    """The audio arrived but could not be turned into a sendable note (R2.5).

    Deliberately *not* a :class:`~telegram_documentaries.gemini.GeminiError`:
    Gemini returned usable PCM, so this is our half of the boundary that failed,
    and the caller can report which half gave way. The message names the reason
    and, when there is one, the underlying exception's *class name* - never
    ``str(exc)``, which for a pydantic failure would render the PCM bytes.
    """

    def __init__(self, *, reason: str, error_type: str | None = None) -> None:
        detail = f"{reason}; error_type={error_type}" if error_type else reason
        super().__init__(f"narrator: {detail}")
        self.reason = reason
        self.error_type = error_type


async def narrate(
    client: GeminiClient,
    text: str,
    *,
    chat_id: int,
    update_id: int,
    voice: VoiceName = DEFAULT_VOICE,
) -> VoiceNote:
    """Synthesize `text`, encode it to MP3, and return a validated note (R2.3).

    Args:
        client: The injected Gemini seam. Never the SDK itself (R1.2).
        text: The narration to read aloud. Carried unchanged as the note's
            `fallback_text`, so the narration survives a later delivery failure
            (D9).
        chat_id: Telegram chat id, for log correlation only.
        update_id: Telegram update id, for log correlation only.
        voice: The voice to read it in. Defaults to the verified `Kore` (D2).

    Returns:
        A `VoiceNote` whose bytes are proven to open as an MP3 and whose
        `duration_seconds` is the one the validated audio already derived.

    Raises:
        GeminiError: Gemini could not be reached or did not return usable audio.
            Propagated untouched: whether to degrade to text is the pipeline's
            decision, one level up (D6).
        NarratorError: The audio arrived but our encoder could not use it, or it
            was too short to be one MPEG frame.
    """
    audio = await client.synthesize(text, voice, chat_id=chat_id, update_id=update_id)
    return VoiceNote(
        data=_encode(audio),
        mime_type="audio/mpeg",
        duration_seconds=audio.duration_seconds,
        fallback_text=text,
    )


def _encode(audio: SynthesizedAudio) -> bytes:
    """Encode validated PCM to MP3, or raise `NarratorError` (R2.4-R2.6).

    The sample rate is taken from `audio`, never a literal: Gemini declared the
    rate it spoke at and :class:`SynthesizedAudio` validated it, so encoding at
    anything else would play the note at the wrong speed (R2.4).

    A payload shorter than one MPEG frame is refused up front. `lameenc` will
    otherwise wrap a single sample in a valid-looking frame of silence, turning a
    broken synthesis into a note that says nothing (R2.5).
    """
    if len(audio.data) < _minimum_pcm_bytes(audio.sample_rate):
        raise NarratorError(reason="the audio is shorter than one MPEG frame")

    try:
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(MP3_BIT_RATE_KBPS)
        encoder.set_in_sample_rate(audio.sample_rate)
        encoder.set_channels(MP3_CHANNELS)
        encoder.set_quality(MP3_QUALITY)
        encoded: bytes = encoder.encode(audio.data) + encoder.flush()
    except Exception as exc:
        # Never `str(exc)`: a lame error can carry the PCM, and a pydantic one
        # can render it as `input_value`. Class name only, and `from None` so no
        # formatter can reach the original either.
        raise NarratorError(
            reason="the MP3 encoder failed", error_type=type(exc).__name__
        ) from None

    if not has_mp3_header(encoded):
        # Reuse the one shared header check rather than restating it: bytes we
        # do not recognise as an MP3 must not reach the adapter (R2.6).
        raise NarratorError(reason="the encoder produced no MP3 header")

    return encoded


def _minimum_pcm_bytes(sample_rate: int) -> int:
    """The smallest whole-frame PCM payload for `sample_rate`, in bytes (R2.5).

    One mono sample is two bytes of `s16le`; the frame length in samples depends
    on the MPEG version the rate selects.
    """
    frame_samples = (
        _MPEG1_FRAME_SAMPLES
        if sample_rate >= _MPEG1_MIN_SAMPLE_RATE
        else _MPEG2_FRAME_SAMPLES
    )
    return frame_samples * _BYTES_PER_SAMPLE
