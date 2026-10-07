"""RED: the temporary media store (R5).

A portrait is downloaded to disk because Gemini needs it as bytes and the
Converter needs a path, and a file that outlives its conversation is a privacy
problem the user cannot see. Three properties matter:

* **Validate before writing.** An oversized photo is refused on the *declared*
  size, so a 40 MB upload costs nothing rather than being downloaded and then
  rejected (R5.2).
* **Purge must be loud.** A failed deletion is logged at `exception` and
  re-raised, not swallowed. TECH.md puts temp cleanup in the "fails loud" column
  precisely because the user never sees it happen (R5.3).
* **Isolation.** Two chats never share a directory, so purging one cannot take
  the other's photo with it.

Everything runs under `tmp_path`; the real `tempfile.gettempdir()` is never
written to, and no test creates a file outside its own tmp directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from telegram_documentaries import media
from telegram_documentaries.media import (
    MAX_PHOTO_BYTES,
    InvalidInboundPhotoError,
    MediaError,
    MediaStore,
)

CHAT = 8767055318
OTHER_CHAT = 111111111

JPEG = b"\xff\xd8\xff\xe0" + b"photo-bytes" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"photo-bytes" * 64


@pytest.fixture
def store(tmp_path: Path) -> MediaStore:
    """A store rooted in this test's own directory."""
    return MediaStore(base_dir=tmp_path / "media")


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def test_save_photo_writes_the_bytes_and_returns_a_stored_photo(store: MediaStore) -> None:
    saved = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    assert saved.byte_size == len(JPEG)
    assert saved.mime_type == "image/jpeg"
    assert Path(saved.path).read_bytes() == JPEG


def test_a_saved_photo_reports_a_path_that_exists(store: MediaStore) -> None:
    saved = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    assert Path(saved.path).is_file()


def test_the_saved_file_is_named_photo_jpg(store: MediaStore) -> None:
    saved = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    assert Path(saved.path).name == "photo.jpg"


def test_the_session_directory_path_contains_the_integer_chat_id(
    store: MediaStore,
) -> None:
    """R5.1: the path segment is `str(StrictInt)`, never attacker-shaped."""
    saved = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    assert str(CHAT) in Path(saved.path).parts


@pytest.mark.parametrize(
    "chat_id",
    [1, 42, 999999999999, -1001234567890],
)
def test_any_integer_chat_id_produces_one_directory_segment(
    store: MediaStore, chat_id: int
) -> None:
    saved = store.save_photo(chat_id=chat_id, data=JPEG, mime_type="image/jpeg")

    assert str(chat_id) in Path(saved.path).parts
    assert ".." not in Path(saved.path).parts


def test_a_non_integer_chat_id_is_refused(store: MediaStore) -> None:
    """A chat id is a StrictInt, so a string cannot reach the filesystem."""
    with pytest.raises(MediaError):
        store.save_photo(chat_id="../../etc", data=JPEG, mime_type="image/jpeg")  # type: ignore[arg-type]


def test_two_chats_get_two_different_directories(store: MediaStore) -> None:
    mine = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")
    theirs = store.save_photo(chat_id=OTHER_CHAT, data=JPEG, mime_type="image/jpeg")

    assert mine.path != theirs.path


def test_saving_twice_for_one_chat_replaces_the_photo(store: MediaStore) -> None:
    first = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")
    second = store.save_photo(chat_id=CHAT, data=PNG, mime_type="image/png")

    assert first.path == second.path
    assert Path(second.path).read_bytes() == PNG


# --------------------------------------------------------------------------
# R5.2 - validate before writing
# --------------------------------------------------------------------------


def test_save_photo_rejects_empty_bytes(store: MediaStore) -> None:
    with pytest.raises(InvalidInboundPhotoError):
        store.save_photo(chat_id=CHAT, data=b"", mime_type="image/jpeg")


def test_save_photo_rejects_whitespace_only_bytes(store: MediaStore) -> None:
    with pytest.raises(InvalidInboundPhotoError):
        store.save_photo(chat_id=CHAT, data=b"   \n", mime_type="image/jpeg")


def test_a_rejected_empty_photo_writes_nothing(store: MediaStore) -> None:
    with pytest.raises(InvalidInboundPhotoError):
        store.save_photo(chat_id=CHAT, data=b"", mime_type="image/jpeg")

    assert not store.session_dir(CHAT).exists()


def test_a_declared_size_over_the_cap_is_rejected_before_writing(store: MediaStore) -> None:
    """R5.2: refused on the declared size, so nothing is downloaded first."""
    with pytest.raises(InvalidInboundPhotoError) as caught:
        store.save_photo(
            chat_id=CHAT,
            data=JPEG,
            mime_type="image/jpeg",
            declared_byte_size=MAX_PHOTO_BYTES + 1,
        )

    assert caught.value.declared_byte_size == MAX_PHOTO_BYTES + 1
    assert not store.session_dir(CHAT).exists()


def test_a_declared_size_over_the_cap_is_refused_without_the_data_too(
    store: MediaStore,
) -> None:
    """The cap is checked first, so an oversized photo never needs its bytes."""
    with pytest.raises(InvalidInboundPhotoError):
        store.save_photo(
            chat_id=CHAT,
            data=b"",
            mime_type="image/jpeg",
            declared_byte_size=MAX_PHOTO_BYTES * 2,
        )


def test_a_declared_size_exactly_at_the_cap_is_allowed(store: MediaStore) -> None:
    saved = store.save_photo(
        chat_id=CHAT,
        data=JPEG,
        mime_type="image/jpeg",
        declared_byte_size=MAX_PHOTO_BYTES,
    )

    assert saved.byte_size == len(JPEG)


def test_the_cap_is_a_sane_ceiling() -> None:
    """Documents the chosen limit: 10 MiB, comfortably above a Telegram photo."""
    assert MAX_PHOTO_BYTES == 10 * 1024 * 1024


def test_actual_bytes_over_the_cap_are_also_refused(store: MediaStore) -> None:
    """A lying `file_size` must not get a huge payload past the cap."""
    with pytest.raises(InvalidInboundPhotoError):
        store.save_photo(
            chat_id=CHAT,
            data=b"x" * (MAX_PHOTO_BYTES + 1),
            mime_type="image/jpeg",
        )


def test_an_unsupported_mime_type_is_refused(store: MediaStore) -> None:
    with pytest.raises(InvalidInboundPhotoError):
        store.save_photo(chat_id=CHAT, data=JPEG, mime_type="application/pdf")


def test_the_supported_types_are_jpeg_and_png(store: MediaStore) -> None:
    assert store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg").mime_type
    assert store.save_photo(chat_id=OTHER_CHAT, data=PNG, mime_type="image/png").mime_type


def test_a_webp_photo_is_refused(store: MediaStore) -> None:
    with pytest.raises(InvalidInboundPhotoError):
        store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/webp")


# --------------------------------------------------------------------------
# R5.3 - purge
# --------------------------------------------------------------------------


def test_purge_deletes_the_chat_directory_including_the_photo(store: MediaStore) -> None:
    saved = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    store.purge(chat_id=CHAT, update_id=1)

    assert not Path(saved.path).exists()
    assert not store.session_dir(CHAT).exists()


def test_two_chats_purge_independently(store: MediaStore) -> None:
    """The isolation guard at the media layer."""
    mine = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")
    theirs = store.save_photo(chat_id=OTHER_CHAT, data=PNG, mime_type="image/png")

    store.purge(chat_id=CHAT, update_id=1)

    assert not Path(mine.path).exists()
    assert Path(theirs.path).exists()


def test_purge_on_a_chat_with_no_directory_does_not_raise(store: MediaStore) -> None:
    store.purge(chat_id=424242, update_id=1)


def test_purge_on_a_chat_with_no_directory_logs_at_debug(
    store: MediaStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="telegram_documentaries"):
        store.purge(chat_id=424242, update_id=1)

    skipped = [r for r in caplog.records if r.message == "media_purge_skipped"]
    assert len(skipped) == 1
    assert skipped[0].chat_id == 424242
    assert skipped[0].reason


def test_purge_twice_is_harmless(store: MediaStore) -> None:
    store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    store.purge(chat_id=CHAT, update_id=1)
    store.purge(chat_id=CHAT, update_id=2)


def test_purge_failure_is_logged_at_exception_level_and_re_raised(
    store: MediaStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R5.3: cleanup fails loud, because the user cannot see it happen."""
    store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("device busy")

    monkeypatch.setattr(media.shutil, "rmtree", refuse)

    with (
        caplog.at_level(logging.DEBUG, logger="telegram_documentaries"),
        pytest.raises(MediaError),
    ):
        store.purge(chat_id=CHAT, update_id=1)

    failures = [r for r in caplog.records if r.message == "media_purge_failed"]
    assert len(failures) == 1
    assert failures[0].chat_id == CHAT
    assert failures[0].levelno == logging.ERROR


def test_purge_after_a_failed_purge_can_be_retried(
    store: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")
    monkeypatch.setattr(media.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    with pytest.raises(MediaError):
        store.purge(chat_id=CHAT, update_id=1)
    monkeypatch.undo()

    store.purge(chat_id=CHAT, update_id=2)

    assert not Path(saved.path).exists()


def test_a_purge_failure_does_not_touch_another_chat(
    store: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")
    theirs = store.save_photo(chat_id=OTHER_CHAT, data=PNG, mime_type="image/png")
    real_rmtree = media.shutil.rmtree

    def refuse_one(path: object, *args: object, **kwargs: object) -> None:
        if str(CHAT) in str(path):
            raise OSError("device busy")
        real_rmtree(str(path), *args, **kwargs)

    monkeypatch.setattr(media.shutil, "rmtree", refuse_one)

    with pytest.raises(MediaError):
        store.purge(chat_id=CHAT, update_id=1)

    assert Path(mine.path).exists()
    assert Path(theirs.path).exists()


# --------------------------------------------------------------------------
# R5.4 - the temp base
# --------------------------------------------------------------------------


def test_the_default_base_is_under_the_system_temp_dir() -> None:
    """R5.4: the system temp dir, via the stdlib, not a literal."""
    import tempfile

    default = MediaStore()

    assert default.base_dir.is_relative_to(Path(tempfile.gettempdir()))
    assert default.base_dir.name == "telegram_documentaries"


def test_the_temp_base_is_not_configurable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5.4: no env var, so a misconfigured deployment cannot redirect writes."""
    monkeypatch.setenv("TELEGRAM_DOCUMENTARIES_MEDIA_DIR", "/somewhere/else")

    assert not MediaStore().base_dir.is_relative_to(Path("/somewhere/else"))


def test_the_media_store_and_the_session_agree_on_the_photo_type(store: MediaStore) -> None:
    from telegram_documentaries.state import StoredPhoto

    saved = store.save_photo(chat_id=CHAT, data=JPEG, mime_type="image/jpeg")

    assert isinstance(saved, StoredPhoto)


def test_the_media_error_carries_no_photo_bytes(store: MediaStore) -> None:
    with pytest.raises(InvalidInboundPhotoError) as caught:
        store.save_photo(chat_id=CHAT, data=b"", mime_type="image/jpeg")

    assert JPEG.decode(errors="ignore") not in str(caught.value)


def test_media_exports_what_the_hub_needs() -> None:
    for name in ("MediaStore", "StoredPhoto", "MAX_PHOTO_BYTES", "MediaError",
                 "InvalidInboundPhotoError"):
        assert name in media.__all__, name
