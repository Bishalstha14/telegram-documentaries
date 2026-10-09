"""The typed contract at the Telegram boundary.

Nothing that arrives from Telegram reaches application logic directly. Every
update is parsed into an :class:`InboundUpdate` first, and that model is frozen,
ignores unknown fields, and - this is the load-bearing part - **refuses to
coerce**.

Why coercion is treated as a defect rather than a convenience
-------------------------------------------------------------
python-telegram-bot performs no validation on ``chat.id``; it hands back
whatever JSON contained. Pydantic's default (lax) validation would turn
``"-1001234567890"`` into the integer ``-1001234567890`` and look like it
succeeded. A chat id that is sometimes an ``int`` and sometimes a ``str`` is
precisely the defect that silently breaks session lookups once persistent state
lands. So a payload arriving at an illegal state is rejected explicitly, here,
at the edge - never silently repaired.

Two failure modes, deliberately distinct:

* **No message** (``edited_message``, a poll, a reaction) - a normal Phase 1
  no-op. Logged at debug and reported as ``None``; the caller returns early.
* **A malformed message** (no chat, or ``chat.id`` not an integer) - an
  ``InvalidInboundUpdateError`` is raised. The caller decides how to tell the
  user; it must never invent a default chat id to keep going.

Why ``validation_error_fields`` lives here
------------------------------------------
Any pydantic ``ValidationError`` renders the offending ``input_value``, so
printing ``str(exc)`` leaks whatever was in the input - including a sibling
secret. That is true of a missing ``.env`` key and equally true of an
off-schema Gemini reply, which is why this module owns the one safe reader and
both callers reuse it (D7).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
)
from telegram import Update

from telegram_documentaries import observability

__all__ = [
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
]

logger = observability.get_logger("contracts")


def _non_blank(value: str) -> str:
    """Reject whitespace-only text; `min_length=1` alone accepts `"   "`."""
    if not value.strip():
        raise ValueError("must contain non-whitespace characters")
    return value


def validation_error_fields(exc: ValidationError) -> tuple[str, ...]:
    """Return only the offending field *names*, de-duplicated, in order.

    Never return ``str(exc)`` to a caller, a log or stdout. For a missing key
    pydantic renders the whole input mapping, which includes the sibling
    secret's value::

        1 validation error for Settings
        gemini_api_key
          Field required [type=missing,
          input_value={'telegram_bot_token': 'SUPERSECRET'}, input_type=dict]

    ``ValidationError.errors()`` is the only safe source: it carries the same
    field locations, and this function reads nothing else from it.

    Args:
        exc: The validation failure to summarise. Only field *locations* are read.

    Returns:
        The offending field names, in report order and without duplicates. An
        error with no location contributes nothing - a payload fragment must never
        stand in for a field name.
    """
    names: list[str] = []
    for error in exc.errors():
        location = error.get("loc", ())
        if location:
            names.append(str(location[0]))
    return tuple(dict.fromkeys(names))


class InvalidInboundUpdateError(ValueError):
    """An inbound update cannot be trusted and has been rejected.

    A :class:`ValueError` so that broad ``except ValueError`` handling elsewhere
    can never mistake it for a routine, ignorable failure. ``update_id`` and a
    value-free ``reason`` are exposed for logging; the identifier doubles as the
    correlation key for the offending request.
    """

    def __init__(self, reason: str, *, update_id: int) -> None:
        super().__init__(f"update {update_id}: {reason}")
        self.reason = reason
        self.update_id = update_id


class MediaKind(StrEnum):
    """The kinds of media the bot will name back to the user (R2).

    A closed set rather than a free string, so the reply can say "a sticker"
    without parsing anything - and so a typo in the parser cannot reach the
    user's screen as an unhandled label.
    """

    STICKER = "sticker"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    ANIMATION = "animation"
    DOCUMENT = "document"
    UNKNOWN = "unknown"

    @classmethod
    def of(cls, message: object) -> MediaKind:
        """Name the media carried by `message`, or report `UNKNOWN`.

        Reads the Telegram media fields by name rather than by enumeration of
        the payload, so a media type Telegram adds next year lands in `UNKNOWN`
        instead of being mistaken for something the bot understands.
        """
        for field, member in (
            ("sticker", cls.STICKER),
            ("video", cls.VIDEO),
            ("audio", cls.AUDIO),
            ("voice", cls.VOICE),
            ("animation", cls.ANIMATION),
            ("document", cls.DOCUMENT),
        ):
            if getattr(message, field, None) is not None:
                return member
        return cls.UNKNOWN


class PhotoAttachment(BaseModel):
    """A portrait photo, with the largest size already selected (R2.1).

    `StrictInt` for the numeric fields: a width of `"90"` is not a width, and
    accepting it would let a malformed payload reach the pixel-area tiebreak as
    something other than a number.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str = "photo"
    file_id: Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]
    file_unique_id: Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    file_size: StrictInt | None = Field(default=None, gt=0)


class UnsupportedAttachment(BaseModel):
    """Media the bot does not handle, named for the reply (R2).

    Note:
        Not an error. Sending a sticker is a normal thing for a person to do,
        and the reply's job is to say what it saw, not to complain.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str = "unsupported"
    media_kind: MediaKind


#: The hub narrows on `kind`, so both arms must be reachable from one annotation.
InboundAttachment = PhotoAttachment | UnsupportedAttachment


class InboundUpdate(BaseModel):
    """A Telegram update: validated, typed and immutable.

    ``StrictInt`` rather than ``int``: in lax mode pydantic would accept the
    string ``"42"``. ``StrictInt`` keeps the promise that a value annotated
    ``int`` really *is* an ``int`` however the model was constructed - the second
    line of defence behind the explicit check in :meth:`from_telegram`.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: Telegram update identifier, and the log correlation key.
    update_id: StrictInt
    #: Telegram chat identifier. Typed ``int``, never ``str`` - see module docs.
    chat_id: StrictInt
    #: Message text, when the message carries any and no attachment took
    #: precedence (R2.3 - a photo's caption lives in `caption`, never here).
    text: str | None = None
    #: The message's media, already typed, or `None` for a plain text message.
    attachment: InboundAttachment | None = None

    @classmethod
    def from_telegram(cls, update: Update) -> InboundUpdate | None:
        """Parse one Telegram update, or report that it carries no message.

        Args:
            update: A real ``telegram.Update``, as parsed by python-telegram-bot.

        Returns:
            The typed update, or ``None`` when there is no message to act on.

        Raises:
            InvalidInboundUpdateError: The message exists but cannot be trusted - no
                chat, or a ``chat.id`` that is absent or is not an integer. A
                missing id is rejected through this same error rather than
                surfacing as an ``AttributeError``.
        """
        message = update.message
        if message is None:
            logger.debug(
                "inbound_update_ignored",
                extra={
                    "event": "inbound_update_ignored",
                    "update_id": update.update_id,
                    "reason": "no message in update",
                },
            )
            return None

        chat = message.chat
        if chat is None:
            raise InvalidInboundUpdateError(
                "message has no chat to reply to", update_id=update.update_id
            )

        # Widened to `object` deliberately: python-telegram-bot annotates
        # `Chat.id` as `int` but does not enforce it, so the type checker must not
        # be allowed to conclude that the checks below are dead code.
        #
        # `getattr` with a `None` default, not a plain attribute access: a chat
        # object missing `id` outright would otherwise raise `AttributeError`,
        # which escapes this module's contract and lands in the generic error
        # handler. `None` falls through to the check below, so an absent id is
        # rejected through the same typed error as a wrongly-typed one.
        raw_chat_id: object = getattr(chat, "id", None)
        if isinstance(raw_chat_id, bool) or not isinstance(raw_chat_id, int):
            raise InvalidInboundUpdateError(
                f"chat.id must be an int, got {type(raw_chat_id).__name__}",
                update_id=update.update_id,
            )

        attachment = _attachment_of(message, update_id=update.update_id)

        # R2.3: when an attachment is present the hub ignores `text` entirely.
        # Stated in code, not only in prose, so a later change to this line
        # cannot quietly turn a caption or a stray text field into an answer.
        text = None if attachment is not None else _text_of(message)

        return cls(
            update_id=update.update_id,
            chat_id=raw_chat_id,
            text=text,
            attachment=attachment,
        )


def _attachment_of(message: object, *, update_id: int) -> InboundAttachment | None:
    """Type the message's media, or report that it carries none (R2).

    Args:
        message: The parsed `telegram.Message`.
        update_id: Correlation key for the rejection, if one is needed.

    Returns:
        A `PhotoAttachment` when a photo is present - largest size selected by
        `(file_size or 0, width * height)` so a reordered payload cannot change
        which photograph is judged (R2.1) - an `UnsupportedAttachment` naming
        anything else, or `None` for a plain text message.

    Raises:
        InvalidInboundUpdateError: A `PhotoSize` is malformed - a blank
            `file_id` or a non-positive dimension. Raised rather than skipped,
            so a broken photo is never judged as if it were a smaller valid one
            (R2.2).
    """
    photo = getattr(message, "photo", None)
    if photo:
        chosen = _largest_photo(photo, update_id=update_id)
        return PhotoAttachment(
            file_id=_required_str(chosen, "file_id", update_id=update_id),
            file_unique_id=_required_str(chosen, "file_unique_id", update_id=update_id),
            width=_positive_int(chosen, "width", update_id=update_id),
            height=_positive_int(chosen, "height", update_id=update_id),
            file_size=_optional_size(chosen),
        )

    if any(getattr(message, field, None) is not None for field in _MEDIA_FIELDS):
        return UnsupportedAttachment(media_kind=MediaKind.of(message))

    return None


#: Every Telegram media field the parser looks for. Deliberately wider than
#: `MediaKind`'s named set: a field listed here but not named by `MediaKind.of`
#: becomes `UNKNOWN`, which is an honest answer. A field *missing* from this
#: list would be answered with "that was not a photo", which is a wrong one.
_MEDIA_FIELDS = (
    "sticker",
    "video",
    "audio",
    "voice",
    "animation",
    "document",
    "video_note",
    "contact",
    "dice",
    "game",
    "poll",
    "location",
    "venue",
)


def _largest_photo(sizes: object, *, update_id: int) -> object:
    """Pick the largest size by bytes, then pixels (R2.1).

    Not by list position: Telegram currently sends up to four sizes ascending,
    but "currently" is not a contract, and a reordered payload must not change
    which photograph is judged. `file_size` is optional, hence `or 0`, and the
    pixel area only ever breaks a tie.
    """
    if not isinstance(sizes, (list, tuple)) or not sizes:
        raise InvalidInboundUpdateError(
            "photo has no sizes to choose from", update_id=update_id
        )

    ranked: list[tuple[int, int, object]] = []
    for candidate in sizes:
        width = _positive_int(candidate, "width", update_id=update_id)
        height = _positive_int(candidate, "height", update_id=update_id)
        size = getattr(candidate, "file_size", None)
        byte_size = size if isinstance(size, int) and not isinstance(size, bool) else 0
        ranked.append((byte_size, width * height, candidate))

    return max(ranked, key=lambda entry: (entry[0], entry[1]))[2]


def _required_str(source: object, field: str, *, update_id: int) -> str:
    value = getattr(source, field, None)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInboundUpdateError(
            f"photo.{field} must be a non-blank string", update_id=update_id
        )
    return value


def _positive_int(source: object, field: str, *, update_id: int) -> int:
    """Read a strictly positive int, rejecting bools and strings alike.

    `isinstance(True, int)` is `True` in Python, so the bool check comes first
    - a `true` where a width belongs is a malformed payload, not a one.
    """
    value = getattr(source, field, None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidInboundUpdateError(
            f"photo.{field} must be a positive int, got {value!r}",
            update_id=update_id,
        )
    return value


def _optional_size(source: object) -> int | None:
    """`file_size` is optional; a malformed one is treated as absent.

    Deliberately lenient where the dimensions are not: the size only ranks an
    otherwise valid photograph, so an unusable figure narrows the tiebreak
    rather than invalidating the photo.
    """
    value = getattr(source, "file_size", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _text_of(message: object) -> str | None:
    """The message text, normalised to ``str | None``.

    A missing text and an explicit ``None`` both mean "no text". Anything that
    is not a string is reported as no text rather than coerced, and the model
    validates whatever is left.
    """
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else None


# --------------------------------------------------------------------------
# The outbound reply contract: `VoiceNote` and `Reply` (R2.1, R2.2)
#
# The models above guard what comes *in* from Telegram. `VoiceNote` guards what
# goes *out*: the adapter uploads these bytes, so they are proven to be a
# sendable MP3 at the edge, exactly as an inbound photo is proven before it is
# written to disk.
# --------------------------------------------------------------------------

#: Telegram's ``sendVoice`` ceiling, in bytes (50 MB). A note at or above this
#: cannot be uploaded at all, so it is refused before it is ever offered.
_TELEGRAM_VOICE_NOTE_MAX_BYTES = 50 * 1024 * 1024


def has_mp3_header(data: bytes) -> bool:
    """Whether `data` opens as an MP3 (R2.1).

    Two openings are accepted: an ID3v2 tag (``b"ID3"``), or an MPEG frame sync,
    where the first byte is ``0xFF`` and the top three bits of the second are
    set. Together they cover what Telegram's ``sendVoice`` will accept.

    A named helper rather than an inline expression because the Narrator reuses
    it to check the bytes its own encoder produced (R2.6) before they leave that
    module - one definition, so the inbound and outbound checks cannot drift.
    """
    if data.startswith(b"ID3"):
        return True
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _require_mp3_header(data: bytes) -> bytes:
    """Pydantic adapter for :func:`has_mp3_header`; returns the value unchanged."""
    if not has_mp3_header(data):
        raise ValueError("must begin with an ID3 tag or an MPEG frame sync")
    return data


class VoiceNote(BaseModel):
    """A sendable voice note: validated at the *outbound* Telegram boundary.

    ``data`` is checked before the send is attempted - non-empty, strictly below
    Telegram's ``sendVoice`` ceiling, and opening as an MP3 - so the adapter is
    handed something already proven uploadable. ``fallback_text`` carries the
    narration as plain text, so if the voice send fails the adapter can still
    deliver it (D9); the reply itself guarantees the narration survives, whatever
    happens to the audio.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Encoded MP3 bytes. Telegram's ceiling is inclusive at the top, so the
    #: bound is strictly below it: ``max_length`` of ``limit - 1``.
    data: Annotated[
        bytes,
        Field(min_length=1, max_length=_TELEGRAM_VOICE_NOTE_MAX_BYTES - 1),
        AfterValidator(_require_mp3_header),
    ]
    #: Telegram accepts MP3 for a voice note; any other type is a caller bug.
    mime_type: Literal["audio/mpeg"]
    #: Playback length, derived upstream from the PCM sample count.
    duration_seconds: float = Field(gt=0)
    #: The narration as text, delivered if the voice send fails (D9).
    fallback_text: Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]


#: What a ``handle_*`` method returns for one update: the narration as a
#: `VoiceNote` on success, and - because `VoiceNote` carries its own
#: `fallback_text` - still exactly one object when delivery degrades to text
#: (D4, R2.2).
Reply = str | VoiceNote
