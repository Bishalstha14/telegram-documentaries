"""RED: the Gemini boundary - the highest-risk module in the phase (R1).

Every reply arrives from outside the process, so this module is where "reject
explicitly, never coerce" is either true or merely claimed. The tests below pin
six things, one behaviour each:

* **Schema-first replies (R1.3).** Every call asks for
  ``application/json`` and hands over the caller's Pydantic model, so structure
  comes from a schema and never from string matching.
* **A schema the API can parse.** The serialised schema crosses without
  ``additionalProperties`` - the key Pydantic emits for ``extra="forbid"`` and
  the SDK snake-cases into a name the Gemini API rejects with a 400.
* **Explicit rejection (R1.4).** Each of the five rejection cases - no
  candidates, ``MAX_TOKENS``, a non-text part, unparseable JSON, an off-schema
  payload - raises ``GeminiResponseError``. A truncated reply is a broken reply,
  so the partial JSON is never parsed.
* **Two error classes (R1.6).** Timeouts, transport errors, 5xx and 429 are
  *unavailable*; a reply we cannot use is a *response* error. Keeping them apart
  is what lets the caller preserve a session for one and escalate the other.
* **No leak (R1.7).** ``google.genai.errors.APIError.__str__`` embeds the raw
  HTTP response body in ``self.details``. Every failure path is therefore swept
  with sentinels: the API key and the response body must appear in no log record
  and in no exception message.
* **A bounded call (R1.5, R1.8).** The timeout is applied to the constructed
  client, and the model id is the one constant every stage shares.

Nothing here touches the network or a real credential. The mock boundary is
``generate_content`` itself - the HTTP call - and ``GenAiGeminiClient`` accepts
an injected transport for exactly that reason, so a test never reaches a socket.
"""

from __future__ import annotations

import inspect
import json
import logging
from contextlib import suppress
from typing import Any

import httpx
import pytest
from google import genai
from google.genai import _api_client, _common, errors, models, types
from pydantic import BaseModel, ConfigDict, ValidationError

from telegram_documentaries import gemini
from telegram_documentaries.bouncer import BouncerVerdict
from telegram_documentaries.gemini import (
    GeminiRequest,
    GeminiResponseError,
    GeminiUnavailableError,
    GenAiGeminiClient,
    Stage,
)
from telegram_documentaries.interviewer import InterviewPlan
from telegram_documentaries.scripter import Narration

#: `LogRecorder` from conftest; unannotated because `tests/` is deliberately
#: outside mypy's scope (a loose test double would need casts under strict mode).
LogRecords = Any

#: Not plausible credentials. If either reaches a log record, a traceback or an
#: exception message, a secret has leaked.
API_KEY = "AIzaSUPERSECRET-GEMINI-KEY-VALUE"

#: The raw HTTP body the SDK hides inside `APIError.details`. Truncation is not
#: a defence here: the sweep asserts on this exact string, which is what the SDK
#: interpolates verbatim into `str(exc)`.
RESPONSE_BODY_SENTINEL = "RAW-RESPONSE-BODY-SENTINEL-7f3a9c"

PORTRAIT = b"\xff\xd8\xff\xe0" + b"portrait-bytes" + b"\xff\xd9"


class Verdict(BaseModel):
    """A stand-in for a stage's reply schema."""

    verdict: str
    subject: str


# --------------------------------------------------------------------------
# The transport double: the only seam, at the network call itself.
# --------------------------------------------------------------------------


class RecordedCall:
    """One captured `generate_content` invocation."""

    def __init__(self, *, model: str, contents: types.Content, config: Any) -> None:
        self.model = model
        self.contents = contents
        self.config = config

    @property
    def parts(self) -> list[types.Part]:
        return list(self.contents.parts or [])

    def text_parts(self) -> list[str]:
        return [part.text for part in self.parts if isinstance(part.text, str)]

    def image_parts(self) -> list[types.Blob]:
        return [part.inline_data for part in self.parts if part.inline_data is not None]


class FakeTransport:
    """Returns a scripted reply, or raises a scripted error.

    Records every call so a test can assert *what was sent*, not just what came
    back - the schema-first and inline-data guards need the request side.
    """

    def __init__(self, reply: Any = None, error: BaseException | None = None) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[RecordedCall] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: types.Content,
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        self.calls.append(RecordedCall(model=model, contents=contents, config=config))
        if self._error is not None:
            raise self._error
        assert self._reply is not None, "FakeTransport needs a reply or an error"
        return self._reply


def _reply(
    text: str | None = None,
    *,
    parts: list[types.Part] | None = None,
    finish_reason: types.FinishReason = types.FinishReason.STOP,
    candidates: list[Any] | None = None,
) -> types.GenerateContentResponse:
    """Build a real `GenerateContentResponse`, shaped however a test needs."""
    if candidates is None:
        resolved = (
            parts
            if parts is not None
            else [types.Part(text=text)]
        )
        candidates = [
            types.Candidate(
                content=types.Content(role="model", parts=resolved),
                finish_reason=finish_reason,
            )
        ]
    return types.GenerateContentResponse(
        candidates=candidates,
        model_version=gemini.MODEL_ID,
    )


def _request(
    stage: Stage = Stage.BOUNCER,
    *,
    prompt: str = "Is there a person in this photo?",
    image: bytes | None = None,
    image_mime_type: str = "image/jpeg",
) -> GeminiRequest:
    return GeminiRequest(
        stage=stage,
        system_instruction="You are the Bouncer.",
        prompt=prompt,
        image=image,
        image_mime_type=image_mime_type,
    )


def _client(transport: Any, *, api_key: str = API_KEY) -> GenAiGeminiClient:
    return GenAiGeminiClient(api_key=api_key, transport=transport)


async def _generate(
    client: GenAiGeminiClient,
    request: GeminiRequest | None = None,
    *,
    chat_id: int = -1001234567890,
    update_id: int = 4242,
) -> Verdict:
    """Drive the protocol method the way a stage would."""
    reply = await client.generate(
        request if request is not None else _request(),
        Verdict,
        chat_id,
        update_id,
    )
    assert isinstance(reply, Verdict), "generate must return the caller's schema type"
    return reply


# ==========================================================================
# Happy path
# ==========================================================================


async def test_generate_returns_the_typed_model_when_the_reply_validates() -> None:
    """R1.3: the return type is the caller's schema, not a dict."""
    transport = FakeTransport(_reply(json.dumps({"verdict": "HUMAN", "subject": "a woman"})))

    verdict = await _generate(_client(transport))

    assert verdict.verdict == "HUMAN"
    assert verdict.subject == "a woman"
    assert isinstance(verdict, Verdict)


async def test_an_interviewer_reply_json_array_arrives_as_a_typed_plan() -> None:
    """The one reply shape the Interviewer ever receives, end to end.

    `json.loads` yields a *list* for `questions`, and strict mode rejects a
    list for a `tuple[...]` field - so before the fix this perfectly
    well-formed reply was discarded at the schema as though it were off-schema,
    and the interview died on its first question. The body here is byte-for-
    byte what a real reply looks like: a JSON array of question objects.
    """
    reply_body = json.dumps(
        {
            "questions": [{"text": f"Question {n}?"} for n in range(5)],
            "suggested_animal": "sea otter",
        }
    )
    transport = FakeTransport(_reply(reply_body))

    reply = await _client(transport).generate(
        _request(stage=Stage.INTERVIEWER), InterviewPlan, -1, 4242
    )

    assert isinstance(reply, InterviewPlan)
    assert len(reply.questions) == 5
    assert reply.questions[0].text == "Question 0?"
    assert reply.suggested_animal == "sea otter"


async def test_generate_requests_json_with_the_stage_schema() -> None:
    """The schema-first guard (R1.3).

    If this regresses, structure starts coming from string matching - which
    TECH.md discourages and which silently re-opens every parsing failure mode.

    The schema crosses *serialised*, never as the model class: handing over the
    class makes Pydantic emit ``additionalProperties``, which the SDK
    snake-cases into a key the Gemini API rejects with a 400 (see
    :func:`telegram_documentaries.gemini._strip_additional_properties`). So
    this pins both halves of the guard - it is a dict, *and* that dict still
    describes this stage's model rather than arriving gutted.
    """
    transport = FakeTransport(_reply(json.dumps({"verdict": "HUMAN", "subject": "a man"})))

    await _generate(_client(transport))

    config = transport.calls[0].config
    assert config.response_mime_type == "application/json"
    assert config.system_instruction == "You are the Bouncer."
    schema = config.response_schema
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"verdict", "subject"}
    assert schema["required"] == ["verdict", "subject"]


async def test_generate_sends_the_image_bytes_as_inline_data() -> None:
    """R1.1: the portrait crosses the boundary as bytes, never as a path."""
    transport = FakeTransport(_reply(json.dumps({"verdict": "HUMAN", "subject": "a man"})))

    await _generate(_client(transport), _request(image=PORTRAIT))

    blobs = transport.calls[0].image_parts()
    assert len(blobs) == 1
    assert blobs[0].data == PORTRAIT
    assert blobs[0].mime_type == "image/jpeg"
    # The bytes really are inline, not a path handed to the model: the prompt is
    # the only text in the turn, and no filesystem location appears anywhere.
    assert transport.calls[0].text_parts() == ["Is there a person in this photo?"]


async def test_generate_sends_no_image_part_when_there_is_no_portrait() -> None:
    """The Interviewer and Scripter calls are text-only."""
    transport = FakeTransport(_reply(json.dumps({"verdict": "HUMAN", "subject": "a man"})))

    await _generate(_client(transport), _request(stage=Stage.INTERVIEWER))

    assert transport.calls[0].image_parts() == []
    assert transport.calls[0].contents.parts is not None
    assert len(transport.calls[0].parts) == 1


async def test_generate_sends_the_prompt_alongside_the_image() -> None:
    transport = FakeTransport(_reply(json.dumps({"verdict": "HUMAN", "subject": "a man"})))

    await _generate(_client(transport), _request(image=PORTRAIT))

    assert transport.calls[0].text_parts() == ["Is there a person in this photo?"]


async def test_generate_uses_the_single_model_constant() -> None:
    """R1.8: one model id for all three stages."""
    transport = FakeTransport(_reply(json.dumps({"verdict": "HUMAN", "subject": "a man"})))

    await _generate(_client(transport), _request(stage=Stage.SCRIPTER))

    assert transport.calls[0].model == gemini.MODEL_ID == "gemini-3.1-flash-lite"


async def test_gemini_request_is_frozen() -> None:
    """A request that mutated in flight would change what a stage asked for."""
    request = _request()

    with pytest.raises(ValidationError):
        request.prompt = "something else"  # type: ignore[misc]


async def test_gemini_request_refuses_an_unknown_image_mime_type() -> None:
    """R1.1: a closed set of `image/*` values, not a free string."""
    with pytest.raises(ValidationError):
        _request(image=PORTRAIT, image_mime_type="application/pdf")


async def test_gemini_request_rejects_an_unknown_stage() -> None:
    with pytest.raises(ValidationError):
        GeminiRequest(
            stage="narrator",  # type: ignore[arg-type]
            system_instruction="Speak it.",
            prompt="Read this aloud.",
        )


def test_the_stage_enum_has_exactly_the_three_stages_of_this_phase() -> None:
    assert [stage.value for stage in Stage] == ["bouncer", "interviewer", "scripter"]


# ==========================================================================
# What we send - the schema must carry no key the Gemini API rejects.
# ==========================================================================
#
# Regression lock for a whole class of 400s, not one instance. Every reply
# schema in this project declares `extra="forbid"` (R7/R8), so Pydantic
# serialises each of them - top level and nested - with `additionalProperties`,
# and google-genai 2.28.0 dumps that key snake-cased onto the wire:
#
#   400 INVALID_ARGUMENT. Invalid JSON payload received. Unknown name
#   "additional_properties" at 'generation_config.response_schema':
#   Cannot find field.
#
# The blast radius was all three stage schemas: BouncerVerdict (photo intake,
# the reported instance), InterviewPlan (with Question nested under $defs) and
# Narration. The strip lives at the one construction site in `gemini.py`; the
# tests below pin the helper's contract, the three real schemas at the config
# boundary, and the SDK's own conversion of that config into the request body.

_OFFENDING_KEYS = ("additionalProperties", "additional_properties")


def _occurrences_of(node: Any, names: tuple[str, ...] = _OFFENDING_KEYS) -> list[str]:
    """Every occurrence of `names` anywhere in a serialised schema, at any depth."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in names:
                found.append(key)
            found.extend(_occurrences_of(value, names))
    elif isinstance(node, list):
        for item in node:
            found.extend(_occurrences_of(item, names))
    return found


def test_the_schema_strip_removes_additional_properties_from_the_top_level() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"name": {"type": "string"}},
    }

    stripped = gemini._strip_additional_properties(schema)

    assert "additionalProperties" not in stripped
    assert stripped["type"] == "object"
    assert stripped["properties"] == {"name": {"type": "string"}}
    assert "additionalProperties" in schema, "the helper must not mutate its input"


def test_the_schema_strip_covers_nested_objects_and_defs_using_the_real_interview_plan() -> None:
    """`InterviewPlan` nests `Question` under `$defs`, each object carrying the key.

    The real model is the fixture so the shape under test is the one that
    actually reaches the API, not a hand-written imitation of it.
    """
    schema = InterviewPlan.model_json_schema()

    # Premise: the real schema really does carry the key, at both levels. If
    # this fails, the fixture no longer reproduces the bug and the assertions
    # below would be testing on a ghost.
    assert "additionalProperties" in schema
    assert "additionalProperties" in schema["$defs"]["Question"]

    stripped = gemini._strip_additional_properties(schema)

    assert _occurrences_of(stripped) == []
    # Only the key goes: the defs themselves, and the `$ref` into them, stay
    # for the SDK to inline later.
    assert "Question" in stripped["$defs"]
    assert stripped["properties"]["questions"]["items"]["$ref"] == "#/$defs/Question"
    # Pure: the input is untouched and a fresh structure comes back.
    assert stripped is not schema
    assert "additionalProperties" in schema
    assert "additionalProperties" in schema["$defs"]["Question"]


def test_the_schema_strip_leaves_everything_else_untouched() -> None:
    """One key removed; every surviving key and value identical to the original."""
    schema = InterviewPlan.model_json_schema()

    stripped = gemini._strip_additional_properties(schema)

    assert set(schema) - set(stripped) == {"additionalProperties"}
    # Every surviving top-level value is identical to the original's - except
    # `$defs`, whose whole point is that the nested key hides inside it, so it
    # is compared separately below.
    for key, value in stripped.items():
        if key != "$defs":
            assert value == schema[key]
    assert set(stripped["$defs"]) == set(schema["$defs"])

    question = stripped["$defs"]["Question"]
    original_question = schema["$defs"]["Question"]
    assert set(original_question) - set(question) == {"additionalProperties"}
    assert question["type"] == original_question["type"] == "object"
    assert question["required"] == original_question["required"] == ["text"]
    assert question["properties"] == original_question["properties"]
    assert question["description"] == original_question["description"]


def test_the_schema_strip_removes_the_snake_case_spelling_and_descends_into_lists() -> None:
    """Both spellings, and objects reachable only through a list.

    Pydantic emits the camelCase spelling, but the SDK's own standardiser also
    understands the snake one - either spelling on the way in must yield
    neither on the way out, however deeply it is buried.
    """
    schema = {
        "type": "object",
        "additional_properties": {"type": "string"},
        "anyOf": [
            {"type": "object", "additionalProperties": False},
            {"type": "null"},
            [{"nested": {"additionalProperties": True}}],
        ],
    }

    stripped = gemini._strip_additional_properties(schema)

    assert _occurrences_of(stripped) == []
    assert stripped["type"] == "object"
    assert stripped["anyOf"] == [{"type": "object"}, {"type": "null"}, [{"nested": {}}]]


_REAL_SCHEMA_CASES = [
    pytest.param(
        Stage.BOUNCER,
        BouncerVerdict,
        {"verdict": "HUMAN", "subject": "a woman", "line": "Not a dog."},
        id="bouncer_verdict",
    ),
    pytest.param(
        Stage.INTERVIEWER,
        InterviewPlan,
        {
            "questions": [{"text": f"Question {n}?"} for n in range(5)],
            "suggested_animal": "sea otter",
        },
        id="interview_plan",
    ),
    pytest.param(
        Stage.SCRIPTER,
        Narration,
        {"text": "The otter takes to the water."},
        id="narration",
    ),
]


@pytest.mark.parametrize(("stage", "schema", "payload"), _REAL_SCHEMA_CASES)
async def test_no_real_response_schema_reaches_the_wire_with_a_key_the_api_rejects(
    stage: Stage,
    schema: type[BaseModel],
    payload: dict[str, Any],
) -> None:
    """The regression lock for the whole blast radius, at both observable layers.

    Layer one is the config handed to the SDK; layer two is the SDK's own
    conversion of that config into the request body - the step where the key
    used to reappear snake-cased and the API answered 400. A regression on
    either side of that boundary fails here rather than on the first live photo.
    """
    transport = FakeTransport(_reply(json.dumps(payload)))
    with suppress(GeminiResponseError):
        # Only what we *send* is under test: the config is recorded on the
        # transport before any reply arrives, and whether the reply itself
        # parses is asserted by the R1.4 section below and by each stage's own
        # tests.
        await _client(transport).generate(_request(stage=stage), schema, -1, 4242)

    assert transport.calls, "the config must have been built and handed to the transport"
    config = transport.calls[0].config

    # Layer 1: what we hand the SDK.
    sent = config.response_schema
    assert isinstance(sent, dict), "the schema must cross serialised, never as the model class"
    assert _occurrences_of(sent) == []
    assert sent["type"] == "object" and sent["properties"], "a real schema, not an empty shell"

    # Layer 2: what the SDK would put on the wire. This mirrors the exact
    # conversion `google.genai.models` performs before its POST (the step that
    # used to emit `additional_properties`). The functions are private to the
    # SDK, but the dependency is pinned exactly, so an upgrade that moves them
    # fails here rather than in production - the same trade as the flat-
    # `generate_content` guard below.
    api_client = _api_client.BaseApiClient(api_key="not-a-real-key", vertexai=False)
    wire = _common.convert_to_dict(models._GenerateContentConfig_to_mldev(api_client, config))
    rendered = json.dumps(wire)
    assert '"additional_properties"' not in rendered
    assert '"additionalProperties"' not in rendered


_VALID_PAYLOADS: dict[type[BaseModel], dict[str, Any]] = {
    BouncerVerdict: {"verdict": "HUMAN", "subject": "a woman", "line": "Not a dog."},
    # A tuple, not a list: `InterviewPlan` is strict, so the only thing wrong
    # with this payload must be the field added below - otherwise the test
    # would pass for the wrong reason.
    InterviewPlan: {
        "questions": tuple({"text": f"Question {n}?"} for n in range(5)),
        "suggested_animal": "sea otter",
    },
    Narration: {"text": "The otter takes to the water."},
}


@pytest.mark.parametrize(
    "model",
    list(_VALID_PAYLOADS),
    ids=[model.__name__ for model in _VALID_PAYLOADS],
)
def test_every_real_response_schema_still_rejects_a_reply_with_an_unexpected_field(
    model: type[BaseModel],
) -> None:
    """Stripping the outgoing key must not loosen what we accept (R7/R8).

    Off-schema payloads are rejected, not coerced: `extra="forbid"` stays on
    every reply schema, so `model_validate` - the exact call the reply parser
    makes - refuses a field we did not ask for. This is the guard against the
    tempting wrong fix for the 400 above: deleting `extra="forbid"` from the
    models instead of stripping the key from the request.
    """
    payload = {**_VALID_PAYLOADS[model], "confidence": 0.9}

    with pytest.raises(ValidationError) as excinfo:
        model.model_validate(payload)

    assert [error["type"] for error in excinfo.value.errors()] == ["extra_forbidden"]


# ==========================================================================
# R1.4 - a malformed or truncated reply is rejected explicitly, never coerced.
# ==========================================================================


async def test_generate_raises_when_there_are_no_candidates() -> None:
    transport = FakeTransport(_reply(candidates=[]))

    with pytest.raises(GeminiResponseError) as excinfo:
        await _generate(_client(transport))

    assert "candidate" in str(excinfo.value)


async def test_generate_raises_when_the_candidate_has_no_content() -> None:
    """An absent attribute is rejected, not surfaced as an `AttributeError`."""
    transport = FakeTransport(_reply(candidates=[types.Candidate()]))

    with pytest.raises(GeminiResponseError):
        await _generate(_client(transport))


async def test_generate_raises_when_finish_reason_is_max_tokens() -> None:
    """The truncated-reply guard.

    A JSON string cut off mid-object is a *broken* reply, not a short answer, and
    completing it would be coercion. The partial text is valid-looking here so
    that the test fails if anything ever attempts to parse it.
    """
    partial = '{"verdict": "HUM", "subj'
    transport = FakeTransport(_reply(partial, finish_reason=types.FinishReason.MAX_TOKENS))

    with pytest.raises(GeminiResponseError) as excinfo:
        await _generate(_client(transport))

    message = str(excinfo.value)
    assert "MAX_TOKENS" in message
    assert "HUM" not in message, "the truncated reply body must never reach the message"


async def test_generate_raises_when_a_part_is_not_text() -> None:
    """Inline data in a text-only reply is a defect, not a variation (R1.4.3)."""
    transport = FakeTransport(
        _reply(parts=[types.Part(inline_data=types.Blob(data=b"\x89PNG", mime_type="image/png"))])
    )

    with pytest.raises(GeminiResponseError) as excinfo:
        await _generate(_client(transport))

    assert "text" in str(excinfo.value)


async def test_generate_raises_when_a_part_carries_a_function_call() -> None:
    transport = FakeTransport(
        _reply(
            parts=[
                types.Part(
                    function_call=types.FunctionCall(name="lookup", args={"q": "who"})
                )
            ]
        )
    )

    with pytest.raises(GeminiResponseError):
        await _generate(_client(transport))


async def test_generate_raises_when_a_part_is_empty_text() -> None:
    """An empty part is not usable text and must not be joined into the payload."""
    transport = FakeTransport(_reply(parts=[types.Part(text="")]))

    with pytest.raises(GeminiResponseError):
        await _generate(_client(transport))


async def test_generate_raises_when_the_text_is_not_json() -> None:
    """R1.4.4: the `JSONDecodeError` is chained, never rendered."""
    transport = FakeTransport(_reply("I am afraid I cannot do that."))

    with pytest.raises(GeminiResponseError) as excinfo:
        await _generate(_client(transport))

    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)
    assert "I am afraid I cannot do that." not in str(excinfo.value)


async def test_generate_raises_field_names_when_the_json_fails_the_schema() -> None:
    """R1.4.5: field *names*, never `str(exc)` and never the reply body.

    The sentinel rides in the offending value, which is exactly what
    `str(ValidationError)` would render - so if the implementation ever reaches
    for it, this test fails.
    """
    reply_body = json.dumps({"verdict": {"nested": RESPONSE_BODY_SENTINEL}, "subject": "a man"})
    transport = FakeTransport(_reply(reply_body))

    with pytest.raises(GeminiResponseError) as excinfo:
        await _generate(_client(transport))

    message = str(excinfo.value)
    assert "verdict" in message
    assert RESPONSE_BODY_SENTINEL not in message
    assert "a man" not in message


async def test_generate_raises_when_the_schema_error_has_no_locatable_field() -> None:
    """R1.4: say so, rather than staying silent about an unhelpful error.

    Reached with a schema whose validation failure carries no field location at
    all - the case where `validation_error_fields` returns nothing. The message
    must still explain itself rather than read as an empty list.
    """

    class _NoLocatableSchema(BaseModel):
        """A schema whose validation error has no `loc` to extract.

        A real `BaseModel` subclass because the boundary now *serialises* every
        schema it is handed (`model_json_schema()`), while `model_validate` is
        overridden so the validation failure carries no field location.
        """

        @staticmethod
        def model_validate(payload: object) -> Any:
            raise ValidationError.from_exception_data(
                "_NoLocatableSchema",
                [{"type": "greater_than", "loc": (), "input": payload, "ctx": {"gt": 0}}],
            )

    reply_body = json.dumps({"verdict": RESPONSE_BODY_SENTINEL})
    transport = FakeTransport(_reply(reply_body))

    with pytest.raises(GeminiResponseError) as excinfo:
        await _client(transport).generate(_request(), _NoLocatableSchema, -1, 2)

    message = str(excinfo.value)
    assert "field" in message.lower()
    assert RESPONSE_BODY_SENTINEL not in message


async def test_generate_rejects_a_reply_missing_every_required_field() -> None:
    transport = FakeTransport(_reply(json.dumps({})))

    with pytest.raises(GeminiResponseError) as excinfo:
        await _generate(_client(transport))

    message = str(excinfo.value)
    assert "verdict" in message
    assert "subject" in message


async def test_generate_rejects_an_extra_field_a_strict_schema_does_not_declare() -> None:
    """Off-schema in both directions, driven by the caller's schema.

    The boundary imposes no policy of its own: a stage that wants unexpected keys
    rejected declares `extra="forbid"`, and the boundary then rejects the reply
    instead of quietly dropping the key.
    """

    class StrictVerdict(BaseModel):
        model_config = ConfigDict(extra="forbid")

        verdict: str
        subject: str

    transport = FakeTransport(
        _reply(json.dumps({"verdict": "HUMAN", "subject": "a man", "confidence": 0.9}))
    )

    with pytest.raises(GeminiResponseError) as excinfo:
        await _client(transport).generate(_request(), StrictVerdict, -1, 2)

    assert "confidence" in str(excinfo.value)


async def test_generate_rejects_a_reply_whose_verdict_is_the_wrong_type() -> None:
    """No coercion: an int verdict does not become the string ``"1"``."""
    transport = FakeTransport(_reply(json.dumps({"verdict": 1, "subject": "a man"})))

    with pytest.raises(GeminiResponseError) as excinfo:
        await _generate(_client(transport))

    assert "verdict" in str(excinfo.value)


async def test_generate_never_retries_a_rejected_reply() -> None:
    """R1.4: rejection is terminal. A retry would hide a systematic defect."""
    transport = FakeTransport(_reply("not json"))

    with pytest.raises(GeminiResponseError):
        await _generate(_client(transport))

    assert len(transport.calls) == 1


# ==========================================================================
# R1.6 - two error classes, deliberately separate.
# ==========================================================================


async def test_generate_raises_unavailable_on_timeout() -> None:
    transport = FakeTransport(error=httpx.ReadTimeout("timed out"))

    with pytest.raises(GeminiUnavailableError):
        await _generate(_client(transport))


async def test_generate_raises_unavailable_on_transport_error() -> None:
    transport = FakeTransport(error=httpx.ConnectError("connection refused"))

    with pytest.raises(GeminiUnavailableError):
        await _generate(_client(transport))


async def test_generate_raises_unavailable_on_server_error() -> None:
    transport = FakeTransport(error=errors.ServerError(503, {"error": {"message": "overloaded"}}))

    with pytest.raises(GeminiUnavailableError):
        await _generate(_client(transport))


async def test_generate_raises_unavailable_on_rate_limit() -> None:
    """429 - throttled, so the session is worth preserving and retrying later."""
    transport = FakeTransport(error=errors.ClientError(429, {"error": {"message": "quota"}}))

    with pytest.raises(GeminiUnavailableError):
        await _generate(_client(transport))


@pytest.mark.parametrize("code", [500, 502, 503, 504, 599])
async def test_generate_raises_unavailable_for_any_server_status(code: int) -> None:
    transport = FakeTransport(error=errors.APIError(code, {"error": {"message": "boom"}}))

    with pytest.raises(GeminiUnavailableError):
        await _generate(_client(transport))


async def test_generate_raises_a_response_error_for_a_client_status() -> None:
    """A 400 is our defect, not an outage, so it is the loud class."""
    transport = FakeTransport(error=errors.ClientError(400, {"error": {"message": "bad request"}}))

    with pytest.raises(GeminiResponseError):
        await _generate(_client(transport))


async def test_generate_raises_a_response_error_on_an_unreadable_api_reply() -> None:
    """`UnknownApiResponseError` is the SDK saying "I could not read that"."""
    transport = FakeTransport(error=errors.UnknownApiResponseError("shape mismatch"))

    with pytest.raises(GeminiResponseError):
        await _generate(_client(transport))


def test_both_error_classes_subclass_gemini_error() -> None:
    """R1.6: a stage can catch the base class when it does not care which."""
    assert issubclass(GeminiUnavailableError, gemini.GeminiError)
    assert issubclass(GeminiResponseError, gemini.GeminiError)
    assert issubclass(gemini.GeminiError, Exception)


def test_the_two_error_classes_are_distinct_from_each_other() -> None:
    """A stage must be able to tell "the network is down" from "that was junk"."""
    assert not issubclass(GeminiUnavailableError, GeminiResponseError)
    assert not issubclass(GeminiResponseError, GeminiUnavailableError)


async def test_an_unavailable_error_is_not_rejected_as_a_bad_reply() -> None:
    transport = FakeTransport(error=errors.ServerError(500, {}))

    with pytest.raises(gemini.GeminiError) as excinfo:
        await _generate(_client(transport))

    assert not isinstance(excinfo.value, GeminiResponseError)


# ==========================================================================
# R1.7 - no secret, and no response body, ever reaches a log or a message.
# ==========================================================================


def _api_error_with_body(code: int) -> errors.APIError:
    """An APIError whose rendered text embeds a recognisable sentinel."""
    return errors.ServerError(
        code,
        {
            "error": {
                "code": code,
                "status": "UNAVAILABLE",
                "message": f"backend overloaded: {RESPONSE_BODY_SENTINEL}",
            }
        },
    )


async def _drive_failure(error: BaseException) -> tuple[Any, str, str]:
    """Run one failure through `generate`, returning the client and both texts."""
    client = _client(FakeTransport(error=error))
    raised: BaseException | None = None
    try:
        await _generate(client)
    except gemini.GeminiError as exc:
        raised = exc
    assert raised is not None, "a scripted transport failure must raise GeminiError"
    return client, str(raised), repr(raised)


async def test_gemini_failures_never_log_the_api_key(app_records: LogRecords) -> None:
    """R1.7, mechanism 1: the API key cannot reach a record.

    The key is passed into the client, so it is genuinely in play on every path
    swept here; the assertion is that it never leaves in the other direction.
    Even a `repr`/`str` of the client itself must not carry it, so a future
    `logger.exception(..., extra={"client": client})` cannot leak it either.
    """
    failures = [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
        _api_error_with_body(503),
        errors.ClientError(429, {"error": {"message": "quota"}}),
        errors.ClientError(400, {"error": {"message": "bad request"}}),
    ]

    for failure in failures:
        app_records.records.clear()
        client, exception_text, exception_repr = await _drive_failure(failure)

        assert app_records.records, "a failure must be logged, never silent"
        rendered = _rendered(app_records.records)
        assert API_KEY not in rendered
        assert "SUPERSECRET" not in rendered
        assert "GEMINI-KEY" not in rendered
        assert API_KEY not in f"{client!r}{client!s}{exception_text}{exception_repr}"


async def test_gemini_failures_never_log_the_response_body(app_records: LogRecords) -> None:
    """R1.7, mechanism 2: `APIError.__str__` embeds `self.details`.

    This is the same leak class Phase 1 fixed for pydantic's `ValidationError`, in
    a different library: the raw HTTP body would otherwise be rendered into every
    traceback the failing call produces.
    """
    error = _api_error_with_body(503)

    # Premise: the SDK really does render the body. If it stops, this test fails
    # loudly and the guard can be re-evaluated rather than left as folklore.
    assert RESPONSE_BODY_SENTINEL in str(error)

    client, exception_text, exception_repr = await _drive_failure(error)

    assert RESPONSE_BODY_SENTINEL not in exception_text
    assert RESPONSE_BODY_SENTINEL not in exception_repr
    assert RESPONSE_BODY_SENTINEL not in _rendered(app_records.records)
    # Nor in the client itself, so a future `extra={"client": client}` is safe.
    assert RESPONSE_BODY_SENTINEL not in f"{client!r}{client!s}"
    assert "503" in exception_text, "the status code is safe and worth keeping"


async def test_a_rejected_reply_body_never_reaches_a_record(app_records: LogRecords) -> None:
    """The mirror case: the model's own words must not be logged either."""
    transport = FakeTransport(
        _reply(json.dumps({"verdict": RESPONSE_BODY_SENTINEL}))
    )

    with pytest.raises(GeminiResponseError):
        await _generate(_client(transport))

    assert app_records.records
    assert RESPONSE_BODY_SENTINEL not in _rendered(app_records.records)


async def test_a_gemini_failure_logs_the_taxonomy_fields_at_error_level(
    app_records: LogRecords,
) -> None:
    """The record carries `error_type` and `error_code` - and nothing else leaky."""
    await _drive_failure(errors.ServerError(503, {"error": {"message": "overloaded"}}))

    records = app_records.at_level(logging.ERROR)
    assert records, "a Gemini failure must be logged at ERROR"
    contexts = [app_records.extra_of(record) for record in records]
    failed = [context for context in contexts if context.get("event") == "gemini_call_failed"]
    assert failed, "the taxonomy record must name what failed"
    context = failed[0]
    assert context["error_type"] == "ServerError"
    assert context["error_code"] == 503
    assert context["stage"] == "bouncer"
    assert context["model"] == gemini.MODEL_ID
    assert context["chat_id"] == -1001234567890
    assert context["update_id"] == 4242


async def test_a_rejected_reply_logs_the_reason_and_the_offending_fields(
    app_records: LogRecords,
) -> None:
    with pytest.raises(GeminiResponseError):
        await _generate(_client(FakeTransport(_reply(json.dumps({"verdict": "HUMAN"})))))

    rejected = [
        app_records.extra_of(record)
        for record in app_records.at_level(logging.ERROR)
        if app_records.extra_of(record).get("event") == "gemini_reply_rejected"
    ]
    assert rejected, "a rejected reply must be logged at ERROR"
    assert rejected[0]["stage"] == "bouncer"
    assert rejected[0]["fields"] == ("subject",)


async def test_every_gemini_record_carries_the_correlation_context(
    app_records: LogRecords,
) -> None:
    """D8: `event`, `stage`, `model`, `chat_id`, `update_id` and `duration_ms`."""
    transport = FakeTransport(_reply(json.dumps({"verdict": "HUMAN", "subject": "a man"})))

    await _generate(_client(transport), _request(stage=Stage.INTERVIEWER), chat_id=-42, update_id=7)

    calls = [
        app_records.extra_of(record)
        for record in app_records.records
        if app_records.extra_of(record).get("event") == "gemini_call"
    ]
    assert calls, "every Gemini call must be decorated"
    context = calls[0]
    assert context["stage"] == "interviewer"
    assert context["model"] == gemini.MODEL_ID
    assert context["chat_id"] == -42
    assert context["update_id"] == 7
    assert isinstance(context["duration_ms"], float)


async def test_a_failed_call_is_still_decorated_with_stage_and_model(
    app_records: LogRecords,
) -> None:
    """R1.7/E.12: the decoration must survive the failure path."""
    transport = FakeTransport(error=httpx.ReadTimeout("timed out"))

    with pytest.raises(GeminiUnavailableError):
        await _generate(_client(transport), _request(stage=Stage.SCRIPTER))

    calls = [
        app_records.extra_of(record)
        for record in app_records.records
        if app_records.extra_of(record).get("event") == "gemini_call"
    ]
    assert calls
    assert calls[0]["stage"] == "scripter"
    assert calls[0]["model"] == gemini.MODEL_ID


# ==========================================================================
# R1.5 / R1.8 - bounded, and one model id.
# ==========================================================================


def test_generate_applies_the_timeout_to_the_client() -> None:
    """R1.5: the SDK default is not relied on."""
    client = GenAiGeminiClient(api_key=API_KEY)

    assert client.http_options.timeout == gemini.GEMINI_TIMEOUT_MS == 20_000


async def test_the_production_transport_reaches_the_async_sdk_method() -> None:
    """The real SDK puts the async call at `.aio.models.generate_content`.

    Regression test for a bug the fake could not see. `GeminiTransport` declares
    a flat `generate_content`, and `genai.Client` satisfies it on paper, but the
    client has no such attribute - the async call is nested under `.aio.models`.
    Wiring the client in directly therefore type-checked, passed every test, and
    would have raised `AttributeError` on the first live Gemini call.

    Asserted against the concrete client from the installed SDK, so the next SDK
    upgrade that moves the method fails here rather than in production.
    """
    captured: dict[str, str] = {}

    class _SdkStub:
        """Stands in for `genai.Client`, mirroring its real nesting only."""

        @property
        def aio(self) -> _AioStub:
            return _AioStub(captured)

    class _AioStub:
        def __init__(self, sink: dict[str, str]) -> None:
            self.models = _ModelsStub(sink)

    class _ModelsStub:
        def __init__(self, sink: dict[str, str]) -> None:
            self._sink = sink

        async def generate_content(
            self,
            *,
            model: str,
            contents: types.Content,
            config: types.GenerateContentConfig,
        ) -> types.GenerateContentResponse:
            self._sink["model"] = model
            return _reply('{"ok": true}')

    transport = gemini._GenAiTransport(_SdkStub())

    await transport.generate_content(
        model="gemini-3.1-flash-lite",
        contents=types.Content(role="user", parts=[types.Part.from_text(text="hi")]),
        config=types.GenerateContentConfig(),
    )

    assert captured["model"] == "gemini-3.1-flash-lite"


def test_the_sdk_client_really_has_no_flat_generate_content() -> None:
    """Pins the assumption the adapter above exists to correct.

    If a future SDK version moves the method onto the client, this fails and the
    adapter can be deleted instead of silently doing nothing.
    """
    client = genai.Client(api_key=API_KEY)

    assert not hasattr(client, "generate_content")
    assert inspect.iscoroutinefunction(client.aio.models.generate_content)


def test_the_timeout_constant_is_twenty_seconds_in_milliseconds() -> None:
    assert gemini.GEMINI_TIMEOUT_MS == 20_000


def test_a_shorter_timeout_can_be_requested_explicitly() -> None:
    client = GenAiGeminiClient(api_key=API_KEY, timeout_ms=1_500)

    assert client.http_options.timeout == 1_500


def test_the_model_constant_is_the_verified_model_id() -> None:
    """R1.8 / D1. Guards the one id every stage shares."""
    assert gemini.MODEL_ID == "gemini-3.1-flash-lite"


def test_gemini_client_is_a_protocol() -> None:
    """R1.2: a signature change to the interface without the implementation fails.

    `runtime_checkable` checks member *presence*, which is exactly the guarantee
    that matters here: the production class keeps satisfying the port that the
    stages and the fakes are written against.
    """
    assert isinstance(_client(FakeTransport(_reply())), gemini.GeminiClient)
    assert isinstance(GenAiGeminiClient(api_key=API_KEY), gemini.GeminiClient)


def test_a_bare_object_is_not_a_gemini_client() -> None:
    """The other half of the guard: the protocol does not accept anything."""
    assert not isinstance(object(), gemini.GeminiClient)


def test_the_gemini_boundary_is_the_only_place_genai_is_imported() -> None:
    """R1: no stage may reach past the boundary into the SDK."""
    import ast
    from pathlib import Path

    package = Path(gemini.__file__).parent
    offenders = [
        module.name
        for module in package.glob("*.py")
        if module.name != "gemini.py"
        and "google.genai" in ast.dump(ast.parse(module.read_text()))
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# Rendering helper: the log output a human would actually see.
# --------------------------------------------------------------------------


def _rendered(records: list[logging.LogRecord]) -> str:
    """Format records the way the application's own handler would.

    `LogRecord.getMessage()` is not enough: the decorator attaches `exc_info`, and
    the stdlib formatter renders the traceback, which is precisely where a
    chained `APIError` would print its response body.
    """
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    return "\n".join(formatter.format(record) for record in records)
