"""RED: the typed inbound contract at the Telegram boundary (R3).

Every value that crosses into the application passes through
`InboundUpdate.from_telegram`. The contract it enforces is that a malformed
payload is **rejected explicitly and never coerced** into something that looks
well-formed - a string `chat_id` that pydantic would happily turn into an int is
the exact defect this module exists to prevent.

Updates are built from raw payload dicts through `Update.de_json(...)` (R7), so
these tests exercise python-telegram-bot's real parsing path.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from telegram import Update

from telegram_documentaries import contracts
from telegram_documentaries.contracts import InboundUpdate, InvalidInboundUpdateError

#: `LogRecorder` from conftest; unannotated here because `tests/` is deliberately
#: outside mypy's scope (a loose test double would need casts under strict mode).
LogRecords = Any


def _message_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal, well-formed private-chat message payload."""
    chat: dict[str, Any] = {"id": -1001234567890, "type": "supergroup"}
    payload: dict[str, Any] = {
        "message_id": 17,
        "date": 1_700_000_000,
        "chat": chat,
        "from": {"id": 555, "is_bot": False, "first_name": "Ada"},
        "text": "/start",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_from_telegram_parses_a_well_formed_start_command(make_update: Any) -> None:
    update = make_update(update_id=9001, message=_message_payload())

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.update_id == 9001
    assert inbound.chat_id == -1001234567890
    assert inbound.text == "/start"


def test_from_telegram_keeps_a_negative_supergroup_chat_id(make_update: Any) -> None:
    """Telegram chat ids are negative for groups; a `str` there breaks Phase 3."""
    update = make_update(
        message=_message_payload(
            chat={"id": -1009876543210, "type": "supergroup"}
        )
    )

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.chat_id == -1009876543210
    assert isinstance(inbound.chat_id, int)


def test_from_telegram_returns_none_when_the_message_has_no_text(make_update: Any) -> None:
    """A photo with no caption is still a message; `text` is simply absent."""
    payload = _message_payload()
    del payload["text"]
    update = make_update(message=payload)

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.text is None


# --------------------------------------------------------------------------
# No message -> None, never an exception.
# --------------------------------------------------------------------------


def test_from_telegram_returns_none_when_the_update_has_no_message(make_update: Any) -> None:
    update = make_update(update_id=9002)

    assert InboundUpdate.from_telegram(update) is None


def test_from_telegram_returns_none_for_an_edited_message(make_update: Any) -> None:
    """Out-of-order input is Phase 2's problem; here it is simply ignored."""
    update = make_update(update_id=9003, edited_message=_message_payload())

    assert InboundUpdate.from_telegram(update) is None


def test_from_telegram_returns_none_for_a_poll_update(make_update: Any) -> None:
    poll = {
        "id": "pol-1",
        "question": "Cat or dog?",
        "options": [],
        "total_voter_count": 0,
        "is_closed": True,
        "is_anonymous": True,
        "type": "regular",
        "allows_multiple_answers": False,
        "allows_revoting": False,
        "members_only": False,
    }
    update = make_update(update_id=9004, poll=poll)

    assert InboundUpdate.from_telegram(update) is None


def test_a_message_less_update_is_ignored_at_debug_level(
    make_update: Any,
    app_records: LogRecords,
) -> None:
    update = make_update(update_id=9005)

    assert InboundUpdate.from_telegram(update) is None

    debug_records = app_records.at_level(logging.DEBUG)
    assert debug_records, "a message-less update must be logged, not silently dropped"
    context = app_records.extra_of(debug_records[-1])
    assert context["event"] == "inbound_update_ignored"
    assert context["update_id"] == 9005


# --------------------------------------------------------------------------
# Malformed input -> rejected explicitly, NEVER coerced.
# --------------------------------------------------------------------------


def test_from_telegram_raises_when_the_message_has_no_chat(make_update: Any) -> None:
    payload = _message_payload()
    del payload["chat"]
    update = make_update(update_id=9006, message=payload)

    with pytest.raises(InvalidInboundUpdateError) as excinfo:
        InboundUpdate.from_telegram(update)

    assert "9006" in str(excinfo.value)


def test_from_telegram_raises_when_the_chat_has_no_id() -> None:
    """A chat object missing `id` must not surface as `AttributeError`.

    R3 requires an *absent* `chat.id` to be rejected through the typed error,
    same as a wrongly-typed one. A plain `chat.id` access would raise
    `AttributeError`, which `bot.on_start` does not catch, so the payload would
    escape this contract into the generic error handler.

    Driven through a stub rather than `Update.de_json`, because
    python-telegram-bot's `Chat.__init__` requires `id` and would reject the
    payload before this contract ever saw it. That makes the case unreachable via
    the library's own parser - which is exactly why it needs a direct test:
    it guards the invariant rather than a path the library happens to prevent.
    """
    chat_without_id = SimpleNamespace(type="private")
    message = SimpleNamespace(chat=chat_without_id, text="/start")
    update = SimpleNamespace(update_id=9007, message=message)

    with pytest.raises(InvalidInboundUpdateError) as excinfo:
        InboundUpdate.from_telegram(update)  # type: ignore[arg-type]

    assert "9007" in str(excinfo.value)
    assert "chat.id" in str(excinfo.value)


def test_from_telegram_raises_when_chat_id_is_not_an_integer(make_update: Any) -> None:
    """The load-bearing case: python-telegram-bot does **not** validate `chat.id`.

    It hands back whatever JSON contained, so a string id reaches us verbatim.
    Lax pydantic validation would coerce `"-1001234567890"` into an int and hide
    the defect; this contract must reject it instead.
    """
    update = make_update(
        update_id=9007,
        message=_message_payload(chat={"id": "-1001234567890", "type": "supergroup"}),
    )
    assert isinstance(update.message.chat.id, str)  # premise: PTB passed it through

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_from_telegram_raises_when_chat_id_is_a_float(make_update: Any) -> None:
    update = make_update(
        update_id=9008,
        message=_message_payload(chat={"id": 1234.5, "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_from_telegram_raises_when_chat_id_is_a_boolean(make_update: Any) -> None:
    """`bool` subclasses `int`, so an `isinstance` check alone would let it through."""
    update = make_update(
        update_id=9009,
        message=_message_payload(chat={"id": True, "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_from_telegram_raises_when_chat_id_is_null(make_update: Any) -> None:
    update = make_update(
        update_id=9010,
        message=_message_payload(chat={"id": None, "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(update)


def test_a_rejected_chat_id_is_never_silently_replaced_by_a_default(make_update: Any) -> None:
    """There is no fallback: a malformed chat id yields no `InboundUpdate` at all."""
    update = make_update(
        update_id=9011,
        message=_message_payload(chat={"id": "nope", "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError) as excinfo:
        InboundUpdate.from_telegram(update)

    # The reason names the offending field and says nothing about a chosen value.
    assert "chat.id" in str(excinfo.value)
    assert "9011" in str(excinfo.value)


def test_from_telegram_raises_a_domain_error_not_a_pydantic_error(make_update: Any) -> None:
    """Callers catch `InvalidInboundUpdateError`; a `ValidationError` would escape them."""
    update = make_update(
        update_id=9012,
        message=_message_payload(chat={"id": "x", "type": "private"}),
    )

    with pytest.raises(InvalidInboundUpdateError) as excinfo:
        InboundUpdate.from_telegram(update)

    assert not isinstance(excinfo.value, ValidationError)


# --------------------------------------------------------------------------
# The model itself refuses to coerce, even when constructed directly.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chat_id", ["-1001234567890", 1234.5, True, None])
def test_inbound_update_refuses_to_coerce_a_non_integer_chat_id(chat_id: Any) -> None:
    """Second line of defence: `from_telegram` is not the only way in."""
    with pytest.raises(ValidationError):
        InboundUpdate(update_id=1, chat_id=chat_id, text=None)


def test_inbound_update_accepts_a_plain_integer_chat_id() -> None:
    inbound = InboundUpdate(update_id=1, chat_id=-100, text="/start")

    assert inbound.chat_id == -100
    assert isinstance(inbound.chat_id, int)


# --------------------------------------------------------------------------
# Model shape
# --------------------------------------------------------------------------


def test_inbound_update_is_frozen() -> None:
    """A contract value that mutated mid-flight would poison every later stage."""
    inbound = InboundUpdate(update_id=1, chat_id=42, text="/start")

    with pytest.raises(ValidationError):
        inbound.chat_id = 99  # type: ignore[misc]


def test_inbound_update_ignores_unknown_extra_fields() -> None:
    """Telegram adds fields without warning; they must not break parsing."""
    payload = _message_payload()
    payload["some_future_telegram_field"] = {"nested": True}
    update = Update.de_json({"update_id": 9013, "message": payload}, None)

    inbound = InboundUpdate.from_telegram(update)

    assert inbound is not None
    assert inbound.chat_id == -1001234567890
    assert not hasattr(inbound, "some_future_telegram_field")


def test_inbound_update_requires_a_chat_id() -> None:
    with pytest.raises(ValidationError):
        InboundUpdate(update_id=1, text="/start")


def test_invalid_inbound_update_is_a_value_error() -> None:
    """So generic `except ValueError` handling can never swallow it silently."""
    assert issubclass(InvalidInboundUpdateError, ValueError)


def test_contracts_module_exports_only_the_public_surface() -> None:
    """An exact set, so a new name has to be a deliberate public decision."""
    assert set(contracts.__all__) == {
        "InboundAttachment",
        "InboundUpdate",
        "InvalidInboundUpdateError",
        "MediaKind",
        "PhotoAttachment",
        "Reply",
        "UnsupportedAttachment",
        "VoiceNote",
        "has_mp3_header",
        "validation_error_fields",
    }


def test_contracts_no_longer_exposes_settings_error_fields() -> None:
    """D7 / R2.4: the `.env`-specific name is gone, with no legacy shim.

    A shim would keep the old vocabulary alive in `config`, invite its reuse for
    Gemini replies, and make the next move harder to see. Asserting its absence
    is what stops it creeping back.
    """
    assert not hasattr(contracts, "settings_error_fields")
    assert "settings_error_fields" not in contracts.__all__


def test_contracts_exposes_validation_error_fields() -> None:
    assert callable(contracts.validation_error_fields)
    assert contracts.validation_error_fields.__module__ == "telegram_documentaries.contracts"


# --------------------------------------------------------------------------
# R2.4 / D7 - the leak guard, generalised from `.env` keys to any model.
#
# This helper is the project's single mechanism for reading a pydantic error
# safely. Gemini's off-schema reply fails validation exactly the way a missing
# `.env` key does - the offending `input_value` carries a sibling secret - so it
# is the same guard, re-used, rather than a copy.
# --------------------------------------------------------------------------


class _Probe(BaseModel):
    """Three required fields, shaped like a Gemini stage reply."""

    verdict: str
    subject: str
    line: str


#: A distinctive marker standing in for a secret that must never be extracted.
SIBLING_SECRET = "AIzaSUPERSECRET-SIBLING-SECRET-VALUE"


def test_validation_error_fields_returns_names_only() -> None:
    """The mechanism-level guard: field *names*, never values.

    The premise is asserted first - that the raw error really does carry the
    sibling value inside its own ``input_value`` - so this test cannot quietly
    become vacuous if pydantic ever stops leaking.
    """
    with pytest.raises(ValidationError) as excinfo:
        # `line` is missing, so pydantic reports the whole input mapping - which
        # still holds the sibling secret in `subject`.
        _Probe.model_validate({"verdict": "HUMAN", "subject": SIBLING_SECRET})
    exc = excinfo.value

    # Premise: the error object carries the sibling value inside `input_value`.
    assert SIBLING_SECRET in str(exc.errors()), (
        "the validation error no longer carries the sibling value; R2.4's handling "
        "must be re-evaluated rather than quietly deleted"
    )

    fields = contracts.validation_error_fields(exc)

    assert fields == ("line",)
    for field in fields:
        assert not field.strip().startswith("{")
    assert SIBLING_SECRET not in ", ".join(fields)


def test_validation_error_fields_keeps_order_and_deduplicates() -> None:
    """Field names arrive in report order, once each - the message reads cleanly."""
    exc = ValidationError.from_exception_data(
        "_Probe",
        [
            {"type": "missing", "loc": ("verdict",), "input": None},
            {"type": "missing", "loc": ("subject",), "input": None},
            {"type": "missing", "loc": ("subject",), "input": None},
        ],
    )

    assert contracts.validation_error_fields(exc) == ("verdict", "subject")


def test_validation_error_fields_ignores_errors_with_no_field_location() -> None:
    """A whole-model error carries no `loc`; a payload fragment must not stand in."""
    exc = ValidationError.from_exception_data(
        "_Probe",
        [
            {
                "type": "greater_than",
                "loc": (),
                "input": {"secret": SIBLING_SECRET},
                "ctx": {"gt": 0},
            }
        ],
    )

    assert contracts.validation_error_fields(exc) == ()
    assert SIBLING_SECRET not in ", ".join(contracts.validation_error_fields(exc))


def test_validation_error_fields_reports_the_outer_field_of_a_nested_error() -> None:
    """A nested failure is reported by the field the caller actually owns."""
    exc = ValidationError.from_exception_data(
        "_Probe",
        [{"type": "missing", "loc": ("verdict", 0, "text"), "input": None}],
    )

    assert contracts.validation_error_fields(exc) == ("verdict",)


# --------------------------------------------------------------------------
# R2 - the typed attachment union
#
# The decision table narrows on `attachment.kind` rather than inspecting a
# payload, so the union is the whole reason the hub can live in the domain layer
# instead of inside a Telegram handler.
# --------------------------------------------------------------------------


def _photo(size: int, **overrides: Any) -> dict[str, Any]:
    """One Telegram photo size. `size` orders them ascending, as Telegram does."""
    payload: dict[str, Any] = {
        "file_id": f"photo-{size}",
        "file_unique_id": f"unique-{size}",
        "width": 90 * size,
        "height": 90 * size,
        "file_size": 1_000 * size,
    }
    payload.update(overrides)
    return payload


def _attachment_message(attachment_key: str, attachment: Any, **overrides: Any) -> Any:
    """A message payload carrying `attachment_key`, wrapped for `Update.de_json`."""
    payload = _message_payload(**overrides)
    payload.pop("text", None)
    payload[attachment_key] = attachment
    return {"message": payload}


def test_a_photo_message_carries_a_photo_attachment(make_update: Any) -> None:
    update = make_update(**_attachment_message("photo", [_photo(1), _photo(2)]))

    parsed = InboundUpdate.from_telegram(update)

    assert parsed is not None
    attachment = parsed.attachment
    assert attachment is not None
    assert attachment.kind == "photo"
    assert attachment.file_id == "photo-2"
    assert attachment.file_unique_id == "unique-2"
    assert attachment.width == 180
    assert attachment.height == 180
    assert attachment.file_size == 2_000


def test_the_largest_photo_is_selected_by_size_not_by_position(make_update: Any) -> None:
    """R2.1: a reordered payload must not change which photo is judged."""
    ascending = [_photo(1), _photo(2), _photo(3), _photo(4)]

    parsed = InboundUpdate.from_telegram(
        make_update(**_attachment_message("photo", ascending))
    )

    assert parsed is not None and parsed.attachment is not None
    assert parsed.attachment.file_id == "photo-4"


def test_a_reordered_photo_list_selects_the_same_photograph(make_update: Any) -> None:
    ascending = [_photo(1), _photo(2), _photo(3), _photo(4)]
    shuffled = [ascending[2], ascending[0], ascending[3], ascending[1]]

    parsed = InboundUpdate.from_telegram(
        make_update(**_attachment_message("photo", shuffled))
    )

    assert parsed is not None and parsed.attachment is not None
    assert parsed.attachment.file_id == "photo-4"


def test_a_missing_file_size_falls_back_to_pixel_area(make_update: Any) -> None:
    """R2.1: `file_size` is optional, hence the `(file_size or 0, w*h)` order."""
    small = _photo(1, file_size=None)
    large = _photo(2, file_size=None)

    parsed = InboundUpdate.from_telegram(
        make_update(**_attachment_message("photo", [small, large]))
    )

    assert parsed is not None and parsed.attachment is not None
    assert parsed.attachment.file_id == "photo-2"
    assert parsed.attachment.file_size is None


def test_a_larger_file_size_beats_a_larger_pixel_area(make_update: Any) -> None:
    """The primary key is bytes; pixels only break a tie."""
    few_big_pixels = {
        "file_id": "big-pixels",
        "file_unique_id": "u1",
        "width": 4000,
        "height": 4000,
        "file_size": 10,
    }
    many_small_pixels = {
        "file_id": "many-pixels",
        "file_unique_id": "u2",
        "width": 100,
        "height": 100,
        "file_size": 9_999,
    }

    parsed = InboundUpdate.from_telegram(
        make_update(**_attachment_message("photo", [few_big_pixels, many_small_pixels]))
    )

    assert parsed is not None and parsed.attachment is not None
    assert parsed.attachment.file_id == "many-pixels"


def test_an_equal_file_size_is_broken_by_pixel_area(make_update: Any) -> None:
    first = _photo(1, file_size=500)
    second = _photo(3, file_size=500)

    parsed = InboundUpdate.from_telegram(
        make_update(**_attachment_message("photo", [first, second]))
    )

    assert parsed is not None and parsed.attachment is not None
    assert parsed.attachment.file_id == "photo-3"


def test_a_photo_with_a_blank_file_id_is_rejected(make_update: Any) -> None:
    """R2.2: never a partially-populated attachment."""
    broken = _photo(1, file_id="   ")

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(
            make_update(**_attachment_message("photo", [broken]))
        )


def test_a_photo_with_a_non_positive_width_is_rejected(make_update: Any) -> None:
    broken = _photo(1, width=0)

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(
            make_update(**_attachment_message("photo", [broken]))
        )


def test_a_photo_with_a_negative_height_is_rejected(make_update: Any) -> None:
    broken = _photo(1, height=-5)

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(
            make_update(**_attachment_message("photo", [broken]))
        )


def test_a_photo_with_a_blank_unique_id_is_rejected(make_update: Any) -> None:
    broken = _photo(1, file_unique_id="")

    with pytest.raises(InvalidInboundUpdateError):
        InboundUpdate.from_telegram(
            make_update(**_attachment_message("photo", [broken]))
        )


def test_an_attachment_rejection_names_the_problem(make_update: Any) -> None:
    broken = _photo(1, width=0)

    with pytest.raises(InvalidInboundUpdateError) as caught:
        InboundUpdate.from_telegram(
            make_update(**_attachment_message("photo", [broken]))
        )

    assert caught.value.reason
    assert caught.value.update_id == 4242


# -- unsupported media ------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected_kind"),
    [
        ("sticker", "sticker"),
        ("video", "video"),
        ("audio", "audio"),
        ("voice", "voice"),
        ("animation", "animation"),
        ("document", "document"),
    ],
)
def test_a_recognised_media_kind_is_named(make_update: Any, key: str, expected_kind: str) -> None:
    """R2: a closed set, so the reply can name what was sent without parsing."""
    attachment_payload: dict[str, Any] = {
        "file_id": "file-x",
        "file_unique_id": "unique-x",
        "width": 1,
        "height": 1,
        "duration": 1,
    }
    if key in {"audio", "voice"}:
        attachment_payload.pop("width")
        attachment_payload.pop("height")
    if key == "document":
        attachment_payload = {"file_name": "x.pdf", **attachment_payload}
    if key == "sticker":
        attachment_payload = {
            "type": "regular",
            "is_animated": False,
            "is_video": False,
            **attachment_payload,
        }

    update = make_update(**_attachment_message(key, attachment_payload))
    parsed = InboundUpdate.from_telegram(update)

    assert parsed is not None and parsed.attachment is not None
    assert parsed.attachment.kind == "unsupported"
    assert parsed.attachment.media_kind.value == expected_kind


def test_a_media_kind_outside_the_closed_set_becomes_unknown(make_update: Any) -> None:
    """R2: `unknown` is the honest label, not a crash and not a guess."""
    update = make_update(
        **_attachment_message(
            "video_note",
            {"file_id": "f", "file_unique_id": "u", "length": 5, "duration": 1},
        )
    )
    parsed = InboundUpdate.from_telegram(update)

    assert parsed is not None and parsed.attachment is not None
    assert parsed.attachment.kind == "unsupported"
    assert parsed.attachment.media_kind.value == "unknown"


def test_a_message_with_no_media_and_no_text_has_no_attachment(make_update: Any) -> None:
    payload = _message_payload()
    payload.pop("text")

    parsed = InboundUpdate.from_telegram(make_update(message=payload))

    assert parsed is not None
    assert parsed.attachment is None
    assert parsed.text is None


def test_a_plain_text_message_has_no_attachment(make_update: Any) -> None:
    parsed = InboundUpdate.from_telegram(
        make_update(message=_message_payload(text="hello"))
    )

    assert parsed is not None
    assert parsed.attachment is None
    assert parsed.text == "hello"


# -- R2.3: a caption is never an answer -------------------------------------


def test_a_photo_caption_is_not_mistaken_for_text(make_update: Any) -> None:
    """R2.3: a caption arrives in `caption`, so it must not become an answer."""
    payload = _attachment_message(
        "photo", [_photo(1)], caption="this is my answer to question two"
    )

    parsed = InboundUpdate.from_telegram(make_update(**payload))

    assert parsed is not None
    assert parsed.attachment is not None
    assert parsed.text is None


def test_an_attachment_takes_precedence_over_text(make_update: Any) -> None:
    """R2.3: when an attachment is present the hub ignores `text` entirely."""
    payload = _attachment_message("photo", [_photo(1)])
    payload["text"] = "should be ignored"

    parsed = InboundUpdate.from_telegram(make_update(**payload))

    assert parsed is not None
    assert parsed.attachment is not None
    assert parsed.text is None


# -- the union itself -------------------------------------------------------


def test_the_union_is_discriminated_on_kind() -> None:
    from telegram_documentaries.contracts import PhotoAttachment, UnsupportedAttachment

    photo = PhotoAttachment(
        kind="photo",
        file_id="a",
        file_unique_id="b",
        width=1,
        height=1,
        file_size=None,
    )
    other = UnsupportedAttachment(kind="unsupported", media_kind="sticker")

    assert photo.kind == "photo"
    assert other.kind == "unsupported"


def test_photo_attachment_refuses_a_blank_file_id() -> None:
    from pydantic import ValidationError as PydanticValidationError

    from telegram_documentaries.contracts import PhotoAttachment

    with pytest.raises(PydanticValidationError):
        PhotoAttachment(
            kind="photo",
            file_id="  ",
            file_unique_id="b",
            width=1,
            height=1,
            file_size=None,
        )


def test_photo_attachment_refuses_a_non_integer_width() -> None:
    from pydantic import ValidationError as PydanticValidationError

    from telegram_documentaries.contracts import PhotoAttachment

    with pytest.raises(PydanticValidationError):
        PhotoAttachment(
            kind="photo",
            file_id="a",
            file_unique_id="b",
            width="10",  # type: ignore[arg-type]
            height=1,
            file_size=None,
        )


def test_the_media_kind_is_a_closed_str_enum() -> None:
    from telegram_documentaries.contracts import MediaKind

    assert [member.value for member in MediaKind] == [
        "sticker",
        "video",
        "audio",
        "voice",
        "animation",
        "document",
        "unknown",
    ]


def test_an_unsupported_attachment_refuses_a_kind_outside_the_set() -> None:
    from pydantic import ValidationError as PydanticValidationError

    from telegram_documentaries.contracts import UnsupportedAttachment

    with pytest.raises(PydanticValidationError):
        UnsupportedAttachment(kind="unsupported", media_kind="banana")


def test_the_attachment_models_are_frozen() -> None:
    from telegram_documentaries.contracts import PhotoAttachment

    photo = PhotoAttachment(
        kind="photo",
        file_id="a",
        file_unique_id="b",
        width=1,
        height=1,
        file_size=None,
    )

    with pytest.raises(ValidationError):
        photo.file_id = "changed"  # type: ignore[misc]


def test_the_union_is_exported_from_the_contracts_surface() -> None:
    for name in (
        "PhotoAttachment",
        "UnsupportedAttachment",
        "InboundAttachment",
        "MediaKind",
    ):
        assert name in contracts.__all__, name


# --------------------------------------------------------------------------
# R2.1 / R2.2 - the outbound reply contract: `VoiceNote` and `Reply`
#
# `VoiceNote` is the *outbound* Telegram boundary: the adapter uploads it. It is
# validated here so the adapter is handed bytes already proven to be a sendable
# MP3, and it carries its own `fallback_text` so a failed voice send can still
# deliver the narration (D9).
# --------------------------------------------------------------------------

#: A minimal ID3v2 head: three bytes of magic, then version, flags and size.
_ID3_HEAD = b"ID3\x03\x00\x00\x00\x00\x00\x00"

#: A minimal MPEG frame sync: `0xFF` with the next byte's top three bits set.
_MPEG_SYNC = b"\xff\xfb"

#: Telegram's `sendVoice` ceiling, by the spec's own definition (R2.1).
_TELEGRAM_VOICE_NOTE_LIMIT = 50 * 1024 * 1024


def _voice_note(**overrides: Any) -> Any:
    """A well-formed `VoiceNote`, with any field overridable."""
    fields: dict[str, Any] = {
        "data": _ID3_HEAD + b"\x00" * 64,
        "mime_type": "audio/mpeg",
        "duration_seconds": 1.5,
        "fallback_text": "The narration, as text.",
    }
    fields.update(overrides)
    return contracts.VoiceNote(**fields)


@pytest.mark.parametrize("head", [_ID3_HEAD, _MPEG_SYNC])
def test_voice_note_accepts_a_note_that_opens_as_an_mp3(head: bytes) -> None:
    """R2.1: an ID3 tag or an MPEG frame sync are both valid MP3 openings."""
    note = _voice_note(data=head + b"\x00" * 32)

    assert note.mime_type == "audio/mpeg"
    assert note.data.startswith(head)
    assert note.duration_seconds == 1.5
    assert note.fallback_text == "The narration, as text."


def test_voice_note_rejects_empty_data() -> None:
    with pytest.raises(ValidationError):
        _voice_note(data=b"")


def test_voice_note_rejects_a_head_that_is_neither_id3_nor_a_frame_sync() -> None:
    """R2.1: `GIF89a` is not an MP3, whatever the extension claimed."""
    with pytest.raises(ValidationError):
        _voice_note(data=b"GIF89a" + b"\x00" * 16)


def test_voice_note_rejects_a_0xff_byte_without_the_frame_sync_bits() -> None:
    """`0xFF` alone is not a sync: the second byte's top three bits must be set."""
    with pytest.raises(ValidationError):
        _voice_note(data=b"\xff\x1f" + b"\x00" * 16)


def test_voice_note_rejects_a_mime_type_other_than_audio_mpeg() -> None:
    """The `Literal` is the contract; Telegram would reject anything else."""
    with pytest.raises(ValidationError):
        _voice_note(mime_type="audio/ogg")


@pytest.mark.parametrize("duration", [0, -1.0])
def test_voice_note_rejects_a_non_positive_duration(duration: float) -> None:
    with pytest.raises(ValidationError):
        _voice_note(duration_seconds=duration)


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_voice_note_rejects_blank_fallback_text(text: str) -> None:
    """D9: a blank fallback could not deliver the narration if the send failed."""
    with pytest.raises(ValidationError):
        _voice_note(fallback_text=text)


def test_voice_note_rejects_an_extra_field() -> None:
    """`extra="forbid"`: the contract is exact, not a superset."""
    with pytest.raises(ValidationError):
        _voice_note(caption="not a field")


def test_voice_note_is_frozen() -> None:
    note = _voice_note()

    with pytest.raises(ValidationError):
        note.duration_seconds = 9.0  # type: ignore[misc]


def test_voice_note_rejects_data_at_the_telegram_limit() -> None:
    """R2.1: the note must be *below* Telegram's 50 MB `sendVoice` limit."""
    data = _ID3_HEAD + b"\x00" * (_TELEGRAM_VOICE_NOTE_LIMIT - len(_ID3_HEAD))

    assert len(data) == _TELEGRAM_VOICE_NOTE_LIMIT
    with pytest.raises(ValidationError):
        _voice_note(data=data)


def test_voice_note_accepts_data_just_below_the_telegram_limit() -> None:
    """The boundary is inclusive-below: one byte under the ceiling is valid."""
    data = _ID3_HEAD + b"\x00" * (_TELEGRAM_VOICE_NOTE_LIMIT - 1 - len(_ID3_HEAD))

    note = _voice_note(data=data)

    assert len(note.data) == _TELEGRAM_VOICE_NOTE_LIMIT - 1


def test_has_mp3_header_recognises_id3_and_sync_and_rejects_others() -> None:
    """The one shared header check, reused by `VoiceNote` and the Narrator."""
    assert contracts.has_mp3_header(_ID3_HEAD)
    assert contracts.has_mp3_header(_MPEG_SYNC)
    assert not contracts.has_mp3_header(b"")
    assert not contracts.has_mp3_header(b"\xff")
    assert not contracts.has_mp3_header(b"\xff\x1f")
    assert not contracts.has_mp3_header(b"GIF89a")


def test_reply_accepts_a_plain_string() -> None:
    """R2.2: every non-narration row still returns a `str`."""
    assert isinstance("a narration", contracts.Reply)


def test_reply_accepts_a_voice_note() -> None:
    assert isinstance(_voice_note(), contracts.Reply)


def test_reply_is_a_union_not_any_object() -> None:
    """R2.2: `str | VoiceNote`, so an unrelated type is not a reply."""
    assert not isinstance(b"not a reply", contracts.Reply)
    assert not isinstance(True, contracts.Reply)
