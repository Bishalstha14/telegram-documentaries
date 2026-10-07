"""Temporary media: the portrait on disk between the Bouncer and the Converter.

A downloaded photo is a privacy liability the user cannot see, so this module is
written to be boring and loud:

* **Validate before writing.** An oversized photo is refused on Telegram's
  *declared* `file_size`, before a byte is fetched. A lying `file_size` cannot
  get a huge payload past the cap either, because the received length is
  checked as well.
* **Fail loud.** A failed `purge` is logged at `exception` and re-raised
  (R5.3). TECH.md puts temp cleanup in the "fails loud" column precisely because
  a leaked file is user-invisible; swallowing the error would leave the file on
  disk with nothing recorded.
* **One directory per integer `chat_id`.** The id is a `StrictInt` converted
  with `str()`, so a path segment can never be attacker-shaped (R5.1).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from telegram_documentaries import observability
from telegram_documentaries.state import StoredPhoto

logger = observability.get_logger(__name__)

__all__ = [
    "MAX_PHOTO_BYTES",
    "SUPPORTED_MIME_TYPES",
    "InvalidInboundPhotoError",
    "MediaError",
    "MediaStore",
    "StoredPhoto",
]

#: The download ceiling. Telegram caps a bot-API photo well below this; 10 MiB
#: leaves room for a large modern phone photo while still bounding what one
#: untrusted update can put on disk (R5.2).
MAX_PHOTO_BYTES = 10 * 1024 * 1024

#: Only formats Gemini and the Converter are expected to handle. A PDF or a
#: video masquerading as a photo is refused rather than written (R5.2).
SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png"})

_ROOT = "telegram_documentaries"
_FILENAME = "photo.jpg"


class MediaError(Exception):
    """Base for media failures, so one `except` catches the pair."""


class InvalidInboundPhotoError(MediaError):
    """A photo that cannot be stored (R5.2).

    Carries the declared size when there was one, so the caller can log why a
    photo was refused without logging the photo or its bytes.
    """

    def __init__(self, reason: str, *, declared_byte_size: int | None = None) -> None:
        self.reason = reason
        self.declared_byte_size = declared_byte_size
        detail = f" ({declared_byte_size} bytes declared)" if declared_byte_size else ""
        super().__init__(f"the photo cannot be stored: {reason}{detail}")


class MediaStore:
    """Per-`chat_id` temporary directories under the system temp dir.

    Args:
        base_dir: The root to write under. Defaults to
            `<tempfile.gettempdir()>/telegram_documentaries`. Injectable so tests
            stay inside their own `tmp_path`; production uses the default, which
            is deliberately **not** configurable by environment variable in this
            phase (R5.4) so a misconfigured deployment cannot redirect writes.

    R5.4 records no cleanup scheduler: a stale directory survives until its
    `chat_id` restarts, because the bot is a single process whose own lifetime
    bounds the useful life of a temporary file.
    """

    def __init__(self, *, base_dir: Path | None = None) -> None:
        self.base_dir = (
            base_dir if base_dir is not None else Path(tempfile.gettempdir()) / _ROOT
        )

    def session_dir(self, chat_id: int) -> Path:
        """The directory holding one chat's photo.

        Args:
            chat_id: A `StrictInt` Telegram chat id. Validated here rather than
                trusted, so a non-integer can never become a path segment (R5.1).
        """
        _require_int(chat_id)
        return self.base_dir / str(chat_id)

    def save_photo(
        self,
        *,
        chat_id: int,
        data: bytes,
        mime_type: str,
        declared_byte_size: int | None = None,
    ) -> StoredPhoto:
        """Validate, then write the portrait for one chat.

        Args:
            chat_id: The owning chat.
            data: The downloaded bytes.
            mime_type: What the bytes claim to be.
            declared_byte_size: Telegram's declared `file_size`, if known.
                Checked *before* the data, so an oversized photo is refused
                without its bytes ever being fetched (R5.2).

        Returns:
            The `StoredPhoto` that goes into session state, so the media store's
            opinion and the session's opinion of a photo are one typed value.

        Raises:
            InvalidInboundPhotoError: The photo is empty, over the cap, or of an
                unsupported type. Nothing is written in any of those cases.
        """
        _require_int(chat_id)

        # Cap first: the whole point is that an oversized photo costs nothing.
        if declared_byte_size is not None and declared_byte_size > MAX_PHOTO_BYTES:
            raise InvalidInboundPhotoError(
                "declared size is over the cap",
                declared_byte_size=declared_byte_size,
            )
        if not data:
            raise InvalidInboundPhotoError("the photo is empty")
        if not data.strip():
            raise InvalidInboundPhotoError("the photo is only whitespace")
        if len(data) > MAX_PHOTO_BYTES:
            raise InvalidInboundPhotoError("the photo is over the cap")
        if mime_type not in SUPPORTED_MIME_TYPES:
            raise InvalidInboundPhotoError(f"unsupported type {mime_type}")

        directory = self.session_dir(chat_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / _FILENAME
        target.write_bytes(data)

        logger.info(
            "media_photo_saved",
            extra={
                "event": "media_photo_saved",
                "chat_id": chat_id,
                "byte_size": len(data),
                "mime_type": mime_type,
            },
        )
        return StoredPhoto(path=str(target), byte_size=len(data), mime_type=mime_type)

    def purge(self, *, chat_id: int, update_id: int) -> None:
        """Delete one chat's directory, photo and all.

        Called by `/start`, by `/restart` and by the Bouncer's rejection path, so
        a photo never outlives the conversation that uploaded it.

        Args:
            chat_id: The chat whose media goes.
            update_id: Telegram update id, for log correlation only.

        Raises:
            MediaError: The directory existed but could not be deleted. Logged
                at `exception` and re-raised, because a silently failed cleanup
                leaves the file on disk and nothing recorded (R5.3).
        """
        directory = self.session_dir(chat_id)

        if not directory.is_dir():
            logger.debug(
                "media_purge_skipped",
                extra={
                    "event": "media_purge_skipped",
                    "chat_id": chat_id,
                    "reason": "no media directory",
                    "update_id": update_id,
                },
            )
            return

        try:
            shutil.rmtree(directory)
        except OSError as exc:
            logger.exception(
                "media_purge_failed",
                extra={
                    "event": "media_purge_failed",
                    "chat_id": chat_id,
                    "update_id": update_id,
                },
            )
            # `exc.__class__.__name__` only: a filesystem error's message can
            # carry the path, and the path carries the chat id. Nothing here is
            # a secret, but the habit of not rendering `str(exc)` is the point.
            raise MediaError(
                f"could not delete media for chat_id={chat_id}"
                f" ({type(exc).__name__})"
            ) from None

        logger.info(
            "media_purged",
            extra={
                "event": "media_purged",
                "chat_id": chat_id,
                "update_id": update_id,
            },
        )


def _require_int(chat_id: object) -> None:
    """Refuse a non-integer chat id before it can reach the filesystem.

    `bool` is excluded explicitly: it is an `int` subclass, and `True` becoming
    the directory name `"True"` is exactly the sort of surprise R5.1 rules out.
    """
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise MediaError(f"chat_id must be an int, got {type(chat_id).__name__}")
