"""The Gemini boundary: the one place in this codebase that talks to Gemini.

Three rules govern everything here, in order of importance.

**No stage imports `google.genai`, and no stage sees a raw response object.**
A stage builds a :class:`GeminiRequest` and receives a validated Pydantic model
or one of two typed errors. That is the whole contract (R1).

**A malformed or truncated reply is rejected explicitly, never coerced.**
Truncated JSON is a *broken* reply, not a short answer, and completing it would
be inventing content the model never produced. Every rejection raises
:class:`GeminiResponseError`; nothing is retried, padded or partially filled
(R1.4).

**No ``str(exc)`` is ever logged, at any level.**
``google.genai.errors.APIError.__str__`` interpolates ``self.details`` - the raw
HTTP response body - into its own message. A traceback of any failed call would
therefore print the upstream response verbatim. Records carry the exception's
*class name* and ``code``; exception messages are assembled from the stage, a
fixed reason and field *names*; and the leaky originals are chained with
``from None`` so the stdlib formatter cannot reach them. This is the same leak
class Phase 1 neutralised for pydantic's ``ValidationError``, in a different
library, with the same mechanism (R1.7).

Recorded divergence (D1): TECH.md names Google ADK's ``Runner`` as the agent
framework, and this module reaches ``google.genai`` directly instead. The Runner
and its session service would duplicate the state driver this project is
required to build anyway, and mocking an ADK agent means faking deep inside
``BaseLlm`` rather than at a seam. One ``generate_content`` call behind one
protocol is the simplest thing that is still a real boundary.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, TypeVar, cast, runtime_checkable

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from telegram_documentaries import observability
from telegram_documentaries.contracts import validation_error_fields

__all__ = [
    "GEMINI_TIMEOUT_MS",
    "MODEL_ID",
    "TTS_MODEL_ID",
    "TTS_TIMEOUT_MS",
    "GeminiClient",
    "GeminiError",
    "GeminiRequest",
    "GeminiResponseError",
    "GeminiThrottledError",
    "GeminiTransport",
    "GeminiUnavailableError",
    "GenAiGeminiClient",
    "ImageMimeType",
    "Stage",
    "SynthesisRequest",
    "SynthesizedAudio",
    "VoiceName",
]

logger = observability.get_logger("gemini")

#: R1.8: one model id for all three stages, and the only one named in `src/`.
#: It is deliberately *not* a `Settings` field - it needs to be bounded and
#: shared, not tunable (D9).
MODEL_ID = "gemini-3.1-flash-lite"

#: R1.4/A.1: the speech model, distinct from the text model. It returns raw PCM
#: (`audio/l16`) and nothing else - every non-default `response_mime_type`
#: returns HTTP 400 - so it is named once here rather than inline at the call.
TTS_MODEL_ID = "gemini-3.1-flash-tts-preview"

#: R1.5: the short text calls - Bouncer, Interviewer and Scripter - are bounded
#: at 20 seconds through the client-level `types.HttpOptions`, never at the SDK's
#: own default, which is far longer than a user will wait. This is the *shared*
#: budget for those calls; it is deliberately not raised to cover synthesis (see
#: :data:`TTS_TIMEOUT_MS`), because a hung Bouncer must not stall a conversation.
GEMINI_TIMEOUT_MS = 20_000

#: R1.5: speech synthesis gets its own, longer budget, applied per request on
#: the synthesis config (`GenerateContentConfig.http_options`) so it overrides
#: the client-level bound above.
#:
#: Why the two budgets differ - do not merge them back into one. The 20 s figure
#: was chosen when the slowest call was the Scripter at ~1.2 s. Synthesis is a
#: fundamentally longer call: it generates ~40 s of audio and measured 18-20 s
#: live. Sharing the 20 s ceiling made every voice note a coin flip, and a
#: timeout here loses the voice note the feature exists to deliver. Live log:
#:
#:     gemini_call_failed | stage=narrator error_type=ReadTimeout
#:       model=gemini-3.1-flash-tts-preview update_id=75407294
#:     event=gemini_call duration_ms=20010.806 stage=narrator
#:
#: 60 s clears the measured worst case with wide margin while still bounding the
#: call. The Gemini API rejects any deadline below 10 s, so this is well clear of
#: the floor; it is kept at or above that floor by a test.
TTS_TIMEOUT_MS = 60_000

#: R1.3: replies are asked for as JSON against a schema, never as prose to be
#: parsed by hand.
_JSON_MIME_TYPE = "application/json"

#: R1.6: throttling is an environmental condition, so it preserves a session
#: just as a 5xx does, rather than being treated as our own defect.
_THROTTLED_CODE = 429

_SchemaT = TypeVar("_SchemaT", bound=BaseModel)

#: A closed set of the `image/*` types Telegram can hand us, so a wrong or
#: attacker-shaped value cannot reach the SDK as a free string (R1.1).
ImageMimeType = Literal["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"]

#: The three TTS voices verified in this repository, and no others (D2). A
#: closed set, so a typo is a `mypy` failure rather than a runtime surprise -
#: exactly the boundary `ImageMimeType` draws for images.
VoiceName = Literal["Kore", "Fenrir", "Charon"]


class Stage(StrEnum):
    """Which pipeline stage a call belongs to. The log correlation field."""

    BOUNCER = "bouncer"
    INTERVIEWER = "interviewer"
    SCRIPTER = "scripter"
    NARRATOR = "narrator"


class GeminiRequest(BaseModel):
    """One call's worth of input, typed and frozen (R1.1).

    `extra="forbid"` because a misspelled field on a request is a defect that
    would otherwise be silently dropped - and a silently dropped
    `system_instruction` produces a reply nobody can debug.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    #: The stage's persona and hard rules.
    system_instruction: str = Field(min_length=1)
    #: This turn's instruction and inputs.
    prompt: str = Field(min_length=1)
    #: The portrait, for the Bouncer only. Bytes, never a path or a `FileId`.
    image: bytes | None = None
    image_mime_type: ImageMimeType = "image/jpeg"


class SynthesisRequest(BaseModel):
    """One TTS call's worth of input, typed and frozen (R1.2).

    Deliberately *not* :class:`GeminiRequest`. That model's
    `system_instruction` is mandatory, and a speech request has no system
    instruction to give it - using it here would mean fabricating one. Its shape
    genuinely differs, so it gets its own model.

    `extra="forbid"` for the same reason as :class:`GeminiRequest`: a misspelled
    field on the way in would otherwise be silently dropped and produce audio
    nobody can explain.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The narration to read aloud.
    text: str = Field(min_length=1)
    #: The voice to read it in. A closed set (D2).
    voice: VoiceName


def _require_even_length(data: bytes) -> bytes:
    """Reject a PCM payload that is not whole little-endian `s16le` frames (R1.3).

    A frame is two bytes, so an odd byte count can only be a truncation;
    downstream arithmetic would otherwise invent a half-sample.
    """
    if len(data) % 2:
        raise ValueError("must be a whole number of s16le frames")
    return data


class _AudioMime(BaseModel):
    """The typed shape of the TTS model's MIME declaration (R1.3).

    The model returns exactly ``audio/l16; rate=24000; channels=1``. The
    parameters arrive as strings, so a value that is not an integer
    (``rate=fast``) is refused here rather than reaching the arithmetic that
    derives `duration_seconds`. `channels` is pinned to 1 because the PCM
    downstream is mono: a stereo payload would otherwise be timed as though it
    were not, and the note would play at the wrong speed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    media_type: Literal["audio/l16"]
    rate: int = Field(gt=0)
    channels: Annotated[int, Field(ge=1, le=1)]


def _parse_audio_mime(mime_type: str) -> _AudioMime:
    """Parse a MIME declaration into typed parameters, without a regex (R1.3).

    The string is split on ``;``; the leading segment is the media type and each
    later segment is split on ``=`` into a parameter. The resulting mapping is
    validated by :class:`_AudioMime`, so an unexpected media type, a
    non-integer rate or a channel count other than 1 is a ``ValidationError`` -
    never a value that slips downstream and is mis-timed.

    Args:
        mime_type: The declaration as the SDK reported it, e.g.
            ``audio/l16; rate=24000; channels=1``.

    Returns:
        The validated declaration.

    Raises:
        pydantic.ValidationError: The declaration is not ``audio/l16``, is
            missing a parameter, or carries one of the wrong type or value.
    """
    segments = [segment.strip() for segment in mime_type.split(";")]
    mapping: dict[str, str] = {"media_type": segments[0]} if segments else {}
    for segment in segments[1:]:
        key, separator, value = segment.partition("=")
        if separator:
            mapping[key.strip()] = value.strip()
    return _AudioMime.model_validate(mapping)


class SynthesizedAudio(BaseModel):
    """The validated PCM a TTS call returned (R1.3).

    The **only** place the raw audio payload is read. ``mime_type`` is parsed
    into ``sample_rate`` and ``channels``; ``duration_seconds`` is derived from
    the byte length; and ``data`` is proven non-empty and a whole number of
    frames. Downstream code therefore never re-parses the wire format, and a
    malformed payload is refused here rather than encoded into a misleading
    note.

    Note:
        ``sample_rate``, ``channels`` and ``duration_seconds`` are derived in
        the validator from ``data`` and ``mime_type``. The defaulted field
        declarations exist so the constructor reads
        ``SynthesizedAudio(data=..., mime_type=...)`` - no caller computes them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Raw little-endian `s16le` PCM: non-empty, and a whole number of frames.
    data: Annotated[bytes, Field(min_length=1), AfterValidator(_require_even_length)]
    #: The wire declaration, kept for traceability.
    mime_type: str = Field(min_length=1)
    #: Parsed from the declaration's rate, in hertz.
    sample_rate: int = 0
    #: Parsed from the declaration's channels; always 1.
    channels: int = 0
    #: Playback length, derived from the byte count and the parsed rate.
    duration_seconds: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _from_wire(cls, values: Any) -> Any:
        """Derive the parsed fields from `mime_type` before field validation.

        A missing or wrongly-typed ``mime_type``/``data`` is left for field
        validation to report, rather than raising an unrelated error here.
        """
        if not isinstance(values, dict):
            return values
        mime_type = values.get("mime_type")
        data = values.get("data")
        if not isinstance(mime_type, str) or not isinstance(data, (bytes, bytearray)):
            return values
        mime = _parse_audio_mime(mime_type)
        return {
            **values,
            "sample_rate": mime.rate,
            "channels": mime.channels,
            # samples = len(data) / 2; frames = samples / channels; seconds = frames / rate.
            "duration_seconds": len(data) / (2 * mime.channels * mime.rate),
        }


class GeminiError(Exception):
    """Base class for every Gemini failure.

    Catching this means "Gemini did not give us something usable"; catching one
    of the two subclasses distinguishes *why*, which is what decides whether a
    session is preserved or the failure is escalated (R1.6).
    """


class GeminiUnavailableError(GeminiError):
    """Gemini could not be reached, or was too busy or too throttled to answer.

    An environmental failure, not a bad reply: the user's session is intact and
    worth keeping, so the caller degrades gently and lets them continue (R1.6).

    The message is assembled from the stage, a fixed reason, the exception's
    *class name* and its status code. It never contains ``str(exc)``, which for
    `google.genai` would carry the raw response body (R1.7).
    """

    def __init__(
        self,
        *,
        stage: Stage,
        reason: str,
        error_type: str,
        error_code: int | None,
    ) -> None:
        super().__init__(
            f"{stage.value}: Gemini unavailable ({reason}); "
            f"error_type={error_type} error_code={error_code}"
        )
        self.stage = stage
        self.reason = reason
        self.error_type = error_type
        self.error_code = error_code


class GeminiThrottledError(GeminiUnavailableError):
    """The API returned ``429``: a transient throttle, not an outage (R1.1).

    A **subclass** of :class:`GeminiUnavailableError`, so every existing
    ``except GeminiUnavailableError`` / ``except GeminiError`` that keeps a
    session for an environmental failure stays correct - including the
    narrator's degrade-to-text path. It exists only so a caller that cares can
    tell "we were throttled, try again shortly" apart from "the service is
    down" and reach for different words (D4).

    Inherits the assembled, leak-free message from its parent; it never carries
    ``str(exc)`` (R1.7).
    """


class GeminiResponseError(GeminiError):
    """A reply arrived and was rejected under R1.4.

    Our defect, so it is logged loudly - but the user still gets a plain apology
    rather than a stack trace. `fields` holds the offending field *names* from
    `ValidationError.errors()`, never the reply body (R1.4.5).

    When no field location can be extracted the message says so explicitly: an
    error that reports nothing is worse than one that admits it knows nothing.
    """

    def __init__(
        self,
        *,
        stage: Stage,
        reason: str,
        fields: tuple[str, ...] = (),
        chat_id: int | None = None,
        update_id: int | None = None,
    ) -> None:
        detail = (
            f"offending fields: {', '.join(fields)}"
            if fields
            else "no field names reported"
        )
        super().__init__(f"{stage.value}: Gemini reply rejected ({reason}); {detail}")
        self.stage = stage
        self.reason = reason
        self.fields = fields
        self.chat_id = chat_id
        self.update_id = update_id


@runtime_checkable
class GeminiClient(Protocol):
    """The two methods the application sees: text generation and speech (D-V2).

    `generate` is schema-bound JSON; `synthesize` is audio, so it cannot travel
    through `generate`'s `response_schema` without lying about its shape. They
    share the injection seam and the transport - one object, two methods.

    A `Protocol`, not a class, so a test injects a fake and never touches a
    socket. `runtime_checkable` makes `isinstance` a real guard: a change to
    either signature that `GenAiGeminiClient` does not follow is a test failure.

    `chat_id` and `update_id` are carried purely for logging correlation - they
    are read by name by the `@logged` decoration on the implementation.
    """

    async def generate(
        self,
        request: GeminiRequest,
        response_schema: type[_SchemaT],
        chat_id: int,
        update_id: int,
    ) -> _SchemaT: ...

    async def synthesize(
        self,
        text: str,
        voice: VoiceName,
        chat_id: int,
        update_id: int,
    ) -> SynthesizedAudio: ...


class GeminiTransport(Protocol):
    """The HTTP call itself - the seam tests substitute (R10).

    `GenAiGeminiClient` speaks to `google.genai` only through this one method, so
    a fake implements one coroutine and no socket is ever opened. Production
    passes `genai.Client`, whose `aio.models.generate_content` satisfies it.
    """

    async def generate_content(
        self,
        *,
        model: str,
        contents: types.Content,
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse: ...


class _GenAiTransport:
    """Adapts `genai.Client` to :class:`GeminiTransport`.

    The SDK does not put the async call on the client. It lives at
    `client.aio.models.generate_content`, and `Client` itself has no
    `generate_content` attribute at all. Verified against the installed version
    rather than assumed.

    Without this, production would pass a `genai.Client` straight into
    `GeminiTransport`, the structural check would be satisfied on paper, and the
    first real Gemini call would raise `AttributeError`. The fake transport in
    the tests could not catch it, because a fake can be written to match any
    signature - only the concrete client reveals the mismatch. This adapter is
    the fix, and it is why `scripts/test` runs mypy as well as pytest.

    Args:
        client: The configured SDK client.
    """

    def __init__(self, client: genai.Client) -> None:
        self._client = client

    async def generate_content(
        self,
        *,
        model: str,
        contents: types.Content,
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        return await self._client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )


async def _call_transport(
    transport: GeminiTransport,
    *,
    model: str,
    contents: types.Content,
    config: types.GenerateContentConfig,
    stage: Stage,
    chat_id: int,
    update_id: int,
) -> types.GenerateContentResponse:
    """Make the HTTP call, mapping every failure onto the taxonomy (R1.6).

    The one try/except in the project that knows how ``google.genai`` fails,
    shared by :func:`_generate_content` and :func:`_synthesize` so the
    classification is written once. Nothing here renders ``str(exc)``; the
    leaky originals are chained with ``from None``.
    """
    try:
        return await transport.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
    except httpx.TimeoutException as exc:
        raise _unavailable(stage, "the request timed out", exc, chat_id, update_id) from None
    except httpx.HTTPError as exc:
        raise _unavailable(stage, "a transport failure", exc, chat_id, update_id) from None
    except genai_errors.APIError as exc:
        raise _from_api_error(stage, exc, chat_id, update_id) from None
    except genai_errors.UnknownApiResponseError as exc:
        # The SDK received something it could not read: a reply we cannot use,
        # not an outage. `str(exc)` is not rendered.
        raise _reject(
            stage,
            f"the SDK could not read the reply ({type(exc).__name__})",
            chat_id=chat_id,
            update_id=update_id,
        ) from None
    except Exception as exc:
        # Final catch-all, and it has to be one: `google.genai` reads the 200
        # body *inside* `generate_content` - `json.loads`, then its own
        # converters, then pydantic - so a body its parser cannot turn into a
        # `GenerateContentResponse` raises before `_typed_reply` ever sees an
        # object. Those failures are none of the four types above: with
        # google-genai 2.28.0 the escaping set is `ValidationError`,
        # `JSONDecodeError`, `TypeError` and `AttributeError`, and the first
        # carries the raw body in `str(exc)` as `input_value=...`.
        #
        # `_reject`, not `_unavailable`: a 200 that the gateway or the SDK
        # cannot read is a reply we were handed and could not use - a defect at
        # the reply end, like `UnknownApiResponseError` - rather than a network,
        # quota or 5xx outage, so it must be the loud class and must not
        # advertise the session as worth retrying (R1.6).
        #
        # Class name only, never `str(exc)`/`repr(exc)`, and `from None` so the
        # `@logged` decorator's `exc_info` traceback cannot reach the original
        # either (R1.7, and this function's own invariant above).
        raise _reject(
            stage,
            f"the SDK could not read the reply ({type(exc).__name__})",
            chat_id=chat_id,
            update_id=update_id,
        ) from None


async def _generate_content(
    transport: GeminiTransport,
    request: GeminiRequest,
    response_schema: type[_SchemaT],
    chat_id: int,
    update_id: int,
) -> _SchemaT:
    """Make the call, map every failure onto the taxonomy, parse strictly.

    This is the function `@logged` wraps, so it must never let an exception whose
    `str()` is unsafe escape to the decorator's `exc_info` rendering.
    """
    stage = request.stage
    config = types.GenerateContentConfig(
        system_instruction=request.system_instruction,
        response_mime_type=_JSON_MIME_TYPE,
        # The single place a response schema is serialised for the wire; the
        # helper's docstring explains why the strip is not optional.
        response_schema=_strip_additional_properties(response_schema.model_json_schema()),
    )
    raw = await _call_transport(
        transport,
        model=MODEL_ID,
        contents=_contents_of(request),
        config=config,
        stage=stage,
        chat_id=chat_id,
        update_id=update_id,
    )
    return _typed_reply(stage, raw, response_schema, chat_id, update_id)


class _StageCall(Protocol):
    """One stage's decorated entry point, bound to its own static `extra`."""

    def __call__(
        self,
        transport: GeminiTransport,
        request: GeminiRequest,
        response_schema: type[_SchemaT],
        chat_id: int,
        update_id: int,
    ) -> Awaitable[_SchemaT]: ...


def _stage_call(stage: Stage) -> _StageCall:
    """Decorate the call for one stage, with `stage` as *static* context.

    D8 gives `@logged` a static `extra` so a stage never has to force a
    `log.info` into business logic just to stamp its identity onto the record.
    Binding one wrapper per stage is what lets `stage` be genuinely static while
    the same single implementation serves all three (R1.8).
    """
    decorated = observability.logged(
        "gemini_call",
        extra={"stage": stage.value, "model": MODEL_ID},
    )(_generate_content)
    return cast("_StageCall", decorated)


_STAGE_CALLS: dict[Stage, _StageCall] = {stage: _stage_call(stage) for stage in Stage}


async def _synthesize(
    transport: GeminiTransport,
    text: str,
    voice: VoiceName,
    chat_id: int,
    update_id: int,
) -> SynthesizedAudio:
    """Synthesize `text` in `voice`, or raise one of the two typed errors (R1.4).

    This is the function `@logged` wraps, so it must never let an exception whose
    `str()` is unsafe escape to the decorator's `exc_info` rendering. It reuses
    :func:`_call_transport`, so it inherits the same error classification as
    :func:`_generate_content` rather than re-deriving it.
    """
    stage = Stage.NARRATOR
    request = SynthesisRequest(text=text, voice=voice)
    raw = await _call_transport(
        transport,
        model=TTS_MODEL_ID,
        contents=_synthesis_contents(request),
        config=_synthesis_config(request),
        stage=stage,
        chat_id=chat_id,
        update_id=update_id,
    )
    return _synthesized_audio(stage, raw, chat_id, update_id)


class _SynthesizeCall(Protocol):
    """The narrator's decorated entry point, bound to the TTS stage and model."""

    def __call__(
        self,
        transport: GeminiTransport,
        text: str,
        voice: VoiceName,
        chat_id: int,
        update_id: int,
    ) -> Awaitable[SynthesizedAudio]: ...


def _synthesize_call() -> _SynthesizeCall:
    """Decorate synthesis with the narrator stage and TTS model as static context.

    The counterpart of :func:`_stage_call` for the one call that is not
    schema-bound. It binds the same ``gemini_call`` event with the same record
    shape - `event`, `chat_id`, `update_id`, `duration_ms` and the static
    `stage`/`model` - so a narrator record is indistinguishable in form from a
    `generate` record (R1.5).
    """
    decorated = observability.logged(
        "gemini_call",
        extra={"stage": Stage.NARRATOR.value, "model": TTS_MODEL_ID},
    )(_synthesize)
    return cast("_SynthesizeCall", decorated)


_SYNTHESIZE_CALL: _SynthesizeCall = _synthesize_call()


class GenAiGeminiClient:
    """The production :class:`GeminiClient`, over `google.genai` (R1.2).

    The only importer of `google.genai` in the project. It owns one `genai.Client`
    for the process lifetime. The client-level timeout is the shared 20-second
    budget for the short text calls; synthesis overrides it per request with the
    longer :data:`TTS_TIMEOUT_MS`, because it measured 18-20 s live and would
    otherwise flip a coin against 20 s (see `_synthesis_config`).

    Args:
        api_key: The Gemini key, already validated as non-blank by `Settings`. It
            is handed straight to the SDK and then dropped: this object keeps no
            reference to it, so no `repr`, log record or traceback can carry it.
        timeout_ms: The client-level bound in milliseconds for the short text
            calls (R1.5). Synthesis is *not* bounded by this value - it carries
            :data:`TTS_TIMEOUT_MS` on its own request config.
        transport: Substitutes the HTTP call. Production leaves it unset, which
            builds the real `genai.Client`; a test passes a fake so the network
            seam is the only thing mocked (R10).

    Note:
        `http_options` is public so the applied timeout is observable rather than
        merely asserted in a comment - it is the difference between a bounded
        call and a hopeful one.
    """

    def __init__(
        self,
        api_key: str,
        timeout_ms: int = GEMINI_TIMEOUT_MS,
        *,
        transport: GeminiTransport | None = None,
    ) -> None:
        self.http_options = types.HttpOptions(timeout=timeout_ms)
        self._transport: GeminiTransport = (
            transport
            if transport is not None
            else _GenAiTransport(genai.Client(api_key=api_key, http_options=self.http_options))
        )

    async def generate(
        self,
        request: GeminiRequest,
        response_schema: type[_SchemaT],
        chat_id: int,
        update_id: int,
    ) -> _SchemaT:
        """Return the validated reply, or raise one of the two typed errors.

        Args:
            request: The typed request for this turn.
            response_schema: The stage's Pydantic model. Also sent to the API as
                `response_schema`, and returned as the validated instance.
            chat_id: Telegram chat id, for log correlation only.
            update_id: Telegram update id, for log correlation only.

        Returns:
            An instance of `response_schema`.

        Raises:
            GeminiUnavailableError: The call did not produce a usable answer for
                environmental reasons - a timeout, a transport failure, a 5xx or
                a 429.
            GeminiResponseError: A reply arrived and was rejected under R1.4 -
                or it arrived as a 200 body the SDK itself could not read.
        """
        call = _STAGE_CALLS[request.stage]
        return await call(self._transport, request, response_schema, chat_id, update_id)

    async def synthesize(
        self,
        text: str,
        voice: VoiceName,
        chat_id: int,
        update_id: int,
    ) -> SynthesizedAudio:
        """Return the validated PCM, or raise one of the two typed errors (R1.4).

        Args:
            text: The narration to read aloud.
            voice: The voice to read it in, from the verified set (D2).
            chat_id: Telegram chat id, for log correlation only.
            update_id: Telegram update id, for log correlation only.

        Returns:
            The validated raw audio, with its sample rate and duration parsed
            and derived.

        Raises:
            GeminiUnavailableError: The call did not produce a usable answer for
                environmental reasons - a timeout, a transport failure, a 5xx or
                a 429.
            GeminiResponseError: A reply arrived and could not be used - no
                candidates, no content, no audio part, or a malformed payload.
        """
        return await _SYNTHESIZE_CALL(self._transport, text, voice, chat_id, update_id)


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------


#: The schema keys the Gemini API's OpenAPI parser does not recognise, in both
#: spellings: Pydantic emits the camelCase one for ``extra="forbid"``, and the
#: SDK's own standardiser understands the snakeCase one. Whichever arrives,
#: the wire must end up with neither.
_REJECTED_SCHEMA_KEYS = frozenset({"additionalProperties", "additional_properties"})


def _strip_additional_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Serialised schema without ``additionalProperties`` - a key the API rejects.

    Every reply schema in this project declares ``extra="forbid"`` (R7/R8: an
    off-schema payload is rejected, never coerced), and Pydantic serialises that
    as ``additionalProperties: false`` on the top-level object *and* on every
    nested object - including each entry under ``$defs``, where
    ``InterviewPlan`` nests ``Question``. The ``google-genai`` SDK (v2.28.0)
    then dumps its ``Schema`` model by field name, so the key crosses the wire
    snake-cased as ``additional_properties``, which the API's OpenAPI parser
    does not recognise. Every call then dies with::

        400 INVALID_ARGUMENT. Invalid JSON payload received. Unknown name
        "additional_properties" at 'generation_config.response_schema':
        Cannot find field.

    The key is therefore dropped from what we *ask* the API for - and only
    from that. What we *accept* is unchanged: the reply is still validated by
    the original model, whose ``extra="forbid"`` keeps rejecting fields we did
    not ask for. Deleting ``extra="forbid"`` from the models instead would
    trade a request we cannot send for a reply we no longer screen, which is
    why the strip lives here, at the single place the wire schema is built.

    Pure: a fresh structure is returned and `schema` is never modified.

    Args:
        schema: A serialised JSON schema, as `BaseModel.model_json_schema()`
            produces it.

    Returns:
        A new schema of the same shape, with both spellings of the key removed
        from every object at any depth, in any dict or list.
    """

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: strip(value)
                for key, value in node.items()
                if key not in _REJECTED_SCHEMA_KEYS
            }
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    # `strip` walks arbitrary JSON, hence `Any`; the value at this level is
    # always the dict that was handed in.
    return cast(dict[str, Any], strip(schema))


def _contents_of(request: GeminiRequest) -> types.Content:
    """Build the single user turn: the portrait (if any) plus the prompt.

    The image always precedes the prompt, so "is there a person in *this* photo?"
    is unambiguous.
    """
    parts: list[types.Part] = []
    if request.image is not None:
        parts.append(
            types.Part.from_bytes(data=request.image, mime_type=request.image_mime_type)
        )
    parts.append(types.Part.from_text(text=request.prompt))
    return types.Content(role="user", parts=parts)


def _synthesis_contents(request: SynthesisRequest) -> types.Content:
    """Build the single user turn: the narration to read aloud, and nothing else."""
    return types.Content(role="user", parts=[types.Part.from_text(text=request.text)])


def _synthesis_config(request: SynthesisRequest) -> types.GenerateContentConfig:
    """The TTS request config: text and audio out, a named voice (R1.7).

    Deliberately carries **no** ``response_mime_type`` and **no**
    ``response_schema``. Every non-default mime type was verified to return HTTP
    400 ``INVALID_ARGUMENT``, so the model's own ``audio/l16`` is the only
    working format; and audio is not JSON, so :func:`_typed_reply` is not on this
    path and :class:`SynthesizedAudio`'s own validation is (R1.3).

    It **does** carry its own ``http_options`` with :data:`TTS_TIMEOUT_MS`, and
    that is load-bearing: the client-level :data:`GEMINI_TIMEOUT_MS` is the
    shared 20 s budget for the short text calls, and synthesis measured 18-20 s
    live. The per-request field genuinely overrides the client value (verified
    against the live API), so a synthesis call gets the longer budget without
    raising the ceiling for a Bouncer that might hang. See :data:`TTS_TIMEOUT_MS`
    for the measurement and why the two must not be merged.
    """
    return types.GenerateContentConfig(
        response_modalities=["TEXT", "AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=request.voice)
            )
        ),
        http_options=types.HttpOptions(timeout=TTS_TIMEOUT_MS),
    )


# --------------------------------------------------------------------------
# Reply parsing, in the order R1.4 specifies.
# --------------------------------------------------------------------------


def _typed_reply(
    stage: Stage,
    raw: types.GenerateContentResponse,
    response_schema: type[_SchemaT],
    chat_id: int,
    update_id: int,
) -> _SchemaT:
    """Validate the reply against the schema, or reject it - never coerce it."""
    text = _text_of(stage, _candidate_of(stage, raw, chat_id, update_id), chat_id, update_id)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # Chained, as R1.4.4 requires, and safe: `JSONDecodeError`'s own text is
        # "Expecting value: line 1 column 1 (char 0)" - a position, not the body.
        raise _reject(
            stage,
            f"the reply is not valid JSON ({type(exc).__name__})",
            chat_id=chat_id,
            update_id=update_id,
        ) from exc

    try:
        return response_schema.model_validate(payload)
    except ValidationError as exc:
        # `str(exc)` renders the offending payload, which is the model's own
        # words and may carry anything. Field locations only, chained with
        # `from None` so no formatter can reach the raw error.
        raise _reject(
            stage,
            "the reply does not satisfy the response schema",
            fields=validation_error_fields(exc),
            chat_id=chat_id,
            update_id=update_id,
        ) from None


def _candidate_of(
    stage: Stage,
    raw: types.GenerateContentResponse,
    chat_id: int,
    update_id: int,
) -> types.Candidate:
    """R1.4.1 and R1.4.2: exactly one complete candidate.

    A `MAX_TOKENS` finish is rejected *before* any parsing is attempted: the text
    of a truncated reply is a prefix of valid JSON, and completing it would be
    inventing content the model never produced.
    """
    # `getattr` on a response object yields `Any`; without this the `or ()`
    # fallback stays `Any` and the return below would smuggle an untyped value
    # past a typed signature.
    candidates: Sequence[types.Candidate] = getattr(raw, "candidates", None) or ()
    if not candidates:
        raise _reject(
            stage,
            "the reply carried no candidates",
            chat_id=chat_id,
            update_id=update_id,
        )

    candidate = candidates[0]
    if _is_max_tokens(getattr(candidate, "finish_reason", None)):
        raise _reject(
            stage,
            f"the reply was truncated (finish_reason {types.FinishReason.MAX_TOKENS.value})",
            chat_id=chat_id,
            update_id=update_id,
        )
    return candidate


def _text_of(
    stage: Stage,
    candidate: types.Candidate,
    chat_id: int,
    update_id: int,
) -> str:
    """R1.4.3: text-only replies, all the way through.

    Inline data, a function call or a blank part is a defect rather than a
    variation, and is reported as such instead of being skipped over and joined
    around.
    """
    parts = getattr(getattr(candidate, "content", None), "parts", None) or ()
    if not parts:
        raise _reject(
            stage,
            "the reply carried no parts",
            chat_id=chat_id,
            update_id=update_id,
        )

    chunks: list[str] = []
    for part in parts:
        text = _text_part_of(part)
        if text is None:
            raise _reject(
                stage,
                "a part carries something other than text",
                chat_id=chat_id,
                update_id=update_id,
            )
        chunks.append(text)
    return "".join(chunks)


def _text_part_of(part: object) -> str | None:
    """One part's text, or `None` when it is not a usable text part.

    `text` alone is not enough to decide: a part built from inline data carries
    `text` as `None` *or* as an empty string depending on how it was
    constructed, so the non-text payloads are checked explicitly.
    """
    if getattr(part, "inline_data", None) is not None:
        return None
    if getattr(part, "function_call", None) is not None:
        return None
    text = getattr(part, "text", None)
    return text if isinstance(text, str) and text.strip() else None


def _is_max_tokens(finish_reason: object) -> bool:
    """True for the `MAX_TOKENS` enum member or its plain string form."""
    return getattr(finish_reason, "value", finish_reason) == types.FinishReason.MAX_TOKENS.value


def _synthesized_audio(
    stage: Stage,
    raw: types.GenerateContentResponse,
    chat_id: int,
    update_id: int,
) -> SynthesizedAudio:
    """Validate the one audio part, or reject the reply (R1.6).

    The rejections are explicit and ordered: no candidates, no content, no audio
    part, then the payload's own validation (mime, rate, channels, data length).
    The payload is never rendered - a malformed one is reported as a fixed
    reason chosen from the field names the validator reported.
    """
    candidate = _candidate_of(stage, raw, chat_id, update_id)
    part = _audio_part_of(stage, candidate, chat_id, update_id)
    blob = getattr(part, "inline_data", None)
    data = getattr(blob, "data", None)
    mime_type = getattr(blob, "mime_type", None)

    try:
        return SynthesizedAudio(data=data or b"", mime_type=mime_type or "")
    except ValidationError as exc:
        raise _reject(
            stage,
            _audio_payload_reason(exc),
            chat_id=chat_id,
            update_id=update_id,
        ) from None


def _audio_part_of(
    stage: Stage,
    candidate: types.Candidate,
    chat_id: int,
    update_id: int,
) -> types.Part:
    """R1.6: the one inline-data audio part, or a fixed-reason rejection.

    A candidate with no content and a candidate whose parts carry text are
    distinct defects and are named as such, rather than collapsed into "no
    audio".
    """
    content = getattr(candidate, "content", None)
    if content is None:
        raise _reject(
            stage,
            "the reply carried no content",
            chat_id=chat_id,
            update_id=update_id,
        )
    parts = getattr(content, "parts", None) or ()
    for part in parts:
        if getattr(part, "inline_data", None) is not None:
            return cast("types.Part", part)
    raise _reject(
        stage,
        "the reply carried no audio part",
        chat_id=chat_id,
        update_id=update_id,
    )


def _audio_payload_reason(exc: ValidationError) -> str:
    """A fixed reason for a malformed audio payload, from field names only.

    ``str(exc)`` would render ``input_value`` - the audio bytes or the
    narration - so only the offending field *names* are read. The checks are
    ordered so the most specific defect is reported first.
    """
    fields = set(validation_error_fields(exc))
    for field, reason in (
        ("data", "the audio payload is empty or not whole s16le frames"),
        ("media_type", "the audio payload is not audio/l16"),
        ("rate", "the audio payload declares a non-integer sample rate"),
        ("channels", "the audio payload is not mono"),
        ("mime_type", "the audio payload declares an unexpected mime type"),
    ):
        if field in fields:
            return reason
    return "the audio payload is not usable audio"


# --------------------------------------------------------------------------
# The error taxonomy, and the records that describe it.
# --------------------------------------------------------------------------


def _from_api_error(
    stage: Stage,
    exc: genai_errors.APIError,
    chat_id: int | None,
    update_id: int | None,
) -> GeminiError:
    """Classify an `APIError` into throttled, unavailable or rejected (R1.6).

    Only the class name and `code` are read. `exc.details` - the raw response
    body - and `exc.message` are never touched (R1.7).

    The ``429`` branch is split out *ahead* of the server branch (R1.1/D4): a
    throttle is a strict refinement of "unavailable", so it must be recognised
    before the broader ``ServerError``/5xx test can swallow it.
    """
    code = _code_of(exc)
    if code == _THROTTLED_CODE:
        return _throttled(stage, exc, chat_id, update_id, code=code)
    if isinstance(exc, genai_errors.ServerError) or (
        code is not None and code >= 500
    ):
        return _unavailable(
            stage,
            "the API reported a server-side failure",
            exc,
            chat_id,
            update_id,
            code=code,
        )
    return _reject(
        stage,
        f"the API rejected the request ({type(exc).__name__}, error_code={code})",
        chat_id=chat_id,
        update_id=update_id,
    )


def _code_of(exc: object) -> int | None:
    """The exception's HTTP status code, or `None` when it is not an integer.

    `APIError.__init__` falls back to reading `code` out of the response body,
    which makes the attribute `Any` - and `bool` is an `int`, so both need
    rejecting before the value reaches a log record.
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _unavailable(
    stage: Stage,
    reason: str,
    exc: BaseException,
    chat_id: int | None,
    update_id: int | None,
    *,
    code: int | None = None,
) -> GeminiUnavailableError:
    """Build and log an environmental failure, reading only `type(exc)`."""
    error_type = type(exc).__name__
    error_code = code if code is not None else _code_of(exc)
    logger.error(
        "gemini_call_failed",
        extra={
            "event": "gemini_call_failed",
            "stage": stage.value,
            "model": MODEL_ID,
            "chat_id": chat_id,
            "update_id": update_id,
            "error_type": error_type,
            "error_code": error_code,
        },
    )
    return GeminiUnavailableError(
        stage=stage,
        reason=reason,
        error_type=error_type,
        error_code=error_code,
    )


def _throttled(
    stage: Stage,
    exc: BaseException,
    chat_id: int | None,
    update_id: int | None,
    *,
    code: int | None = None,
) -> GeminiThrottledError:
    """Build and log a throttle, reading only `type(exc)` and `code` (R1.9).

    The record is ``gemini_throttled`` at ERROR - the attempts are spent and the
    user is about to be told to try again shortly - and carries the class name,
    stage and code, never ``str(exc)``.
    """
    error_type = type(exc).__name__
    error_code = code if code is not None else _code_of(exc)
    logger.error(
        "gemini_throttled",
        extra={
            "event": "gemini_throttled",
            "stage": stage.value,
            "model": MODEL_ID,
            "chat_id": chat_id,
            "update_id": update_id,
            "error_type": error_type,
            "error_code": error_code,
        },
    )
    return GeminiThrottledError(
        stage=stage,
        reason="the API throttled the request",
        error_type=error_type,
        error_code=error_code,
    )


def _reject(
    stage: Stage,
    reason: str,
    *,
    fields: tuple[str, ...] = (),
    chat_id: int | None = None,
    update_id: int | None = None,
) -> GeminiResponseError:
    """Build and log a rejected reply.

    `fields` are field *names*. Neither the reply body nor a rendered
    `ValidationError` ever reaches this record.
    """
    logger.error(
        "gemini_reply_rejected",
        extra={
            "event": "gemini_reply_rejected",
            "stage": stage.value,
            "reason": reason,
            "fields": fields,
            "chat_id": chat_id,
            "update_id": update_id,
        },
    )
    return GeminiResponseError(
        stage=stage,
        reason=reason,
        fields=fields,
        chat_id=chat_id,
        update_id=update_id,
    )
