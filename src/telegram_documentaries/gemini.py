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
from typing import Literal, Protocol, TypeVar, cast, runtime_checkable

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from telegram_documentaries import observability
from telegram_documentaries.contracts import validation_error_fields

__all__ = [
    "GEMINI_TIMEOUT_MS",
    "MODEL_ID",
    "GeminiClient",
    "GeminiError",
    "GeminiRequest",
    "GeminiResponseError",
    "GeminiTransport",
    "GeminiUnavailableError",
    "GenAiGeminiClient",
    "ImageMimeType",
    "Stage",
]

logger = observability.get_logger("gemini")

#: R1.8: one model id for all three stages, and the only one named in `src/`.
#: It is deliberately *not* a `Settings` field - it needs to be bounded and
#: shared, not tunable (D9).
MODEL_ID = "gemini-3.1-flash-lite"

#: R1.5: every call is bounded at 20 seconds through `types.HttpOptions`, never
#: at the SDK's own default, which is far longer than a user will wait.
GEMINI_TIMEOUT_MS = 20_000

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


class Stage(StrEnum):
    """Which pipeline stage a call belongs to. The log correlation field."""

    BOUNCER = "bouncer"
    INTERVIEWER = "interviewer"
    SCRIPTER = "scripter"


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
    """The one method every stage sees (R1.2).

    A `Protocol`, not a class, so a test injects a fake and never touches a
    socket. `runtime_checkable` makes `isinstance` a real guard: a change to this
    signature that `GenAiGeminiClient` does not follow is a test failure.

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
        response_schema=response_schema,
    )

    try:
        raw = await transport.generate_content(
            model=MODEL_ID,
            contents=_contents_of(request),
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


class GenAiGeminiClient:
    """The production :class:`GeminiClient`, over `google.genai` (R1.2).

    The only importer of `google.genai` in the project. It owns one `genai.Client`
    for the process lifetime and one 20-second timeout.

    Args:
        api_key: The Gemini key, already validated as non-blank by `Settings`. It
            is handed straight to the SDK and then dropped: this object keeps no
            reference to it, so no `repr`, log record or traceback can carry it.
        timeout_ms: Per-call bound in milliseconds (R1.5).
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
            GeminiResponseError: A reply arrived and was rejected under R1.4.
        """
        call = _STAGE_CALLS[request.stage]
        return await call(self._transport, request, response_schema, chat_id, update_id)


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# The error taxonomy, and the records that describe it.
# --------------------------------------------------------------------------


def _from_api_error(
    stage: Stage,
    exc: genai_errors.APIError,
    chat_id: int | None,
    update_id: int | None,
) -> GeminiError:
    """Classify an `APIError` into unavailable or rejected (R1.6).

    Only the class name and `code` are read. `exc.details` - the raw response
    body - and `exc.message` are never touched (R1.7).
    """
    code = _code_of(exc)
    if (
        isinstance(exc, genai_errors.ServerError)
        or code == _THROTTLED_CODE
        or (code is not None and code >= 500)
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
