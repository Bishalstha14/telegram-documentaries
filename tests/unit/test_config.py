"""RED: configuration fails loudly, and never leaks a secret.

Covers R2.1 (SecretStr), R2.2 (blank is missing) and R2.3 (the validation error
is never printed verbatim).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from telegram_documentaries import config

# Distinctive marker: if this substring ever reaches a log, a rendered model or
# an error message, a secret has leaked. It is not a plausible real token.
TOKEN = "123456:SUPERSECRET-TOKEN-VALUE"
GEMINI_KEY = "AIzaSUPERSECRET-GEMINI-KEY-VALUE"


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may read the developer's real `.env` or shell environment."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_settings_loads_both_secrets_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _valid_environment(monkeypatch)

    settings = config.Settings(_env_file=None)

    assert settings.telegram_bot_token.get_secret_value() == TOKEN
    assert settings.gemini_api_key.get_secret_value() == GEMINI_KEY


def test_settings_loads_both_secrets_from_a_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nGEMINI_API_KEY={GEMINI_KEY}\n")

    settings = config.Settings(_env_file=env_file)

    assert settings.telegram_bot_token.get_secret_value() == TOKEN
    assert settings.gemini_api_key.get_secret_value() == GEMINI_KEY


def test_settings_ignores_unknown_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    _valid_environment(monkeypatch)
    monkeypatch.setenv("SOME_UNRELATED_DEVELOPER_VARIABLE", "noise")

    settings = config.Settings(_env_file=None)

    assert settings.telegram_bot_token.get_secret_value() == TOKEN


# --------------------------------------------------------------------------
# Missing keys
# --------------------------------------------------------------------------


def test_settings_missing_key_raises_a_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _valid_environment(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY")

    with pytest.raises(ValidationError):
        config.Settings(_env_file=None)


def test_settings_missing_both_keys_names_both_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as excinfo:
        config.Settings(_env_file=None)

    assert config.settings_error_fields(excinfo.value) == (
        "telegram_bot_token",
        "gemini_api_key",
    )


# --------------------------------------------------------------------------
# R2.2 — blank is treated as missing.
#
# `Field(min_length=1)` does not enforce on SecretStr: a whitespace-only token
# loads fine and then dies deep inside python-telegram-bot as InvalidToken.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"])
@pytest.mark.parametrize("blank", ["", "   ", " \t\n "])
def test_settings_blank_key_is_rejected_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    blank: str,
) -> None:
    _valid_environment(monkeypatch)
    monkeypatch.setenv(field, blank)

    with pytest.raises(ValidationError) as excinfo:
        config.Settings(_env_file=None)

    assert config.settings_error_fields(excinfo.value) == (field.lower(),)


def test_settings_blank_token_is_rejected_rather_than_silently_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the exact trap: a blank token must never reach the Telegram client."""
    _valid_environment(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")

    with pytest.raises(ValidationError):
        config.Settings(_env_file=None)


# --------------------------------------------------------------------------
# R2.1 — rendering a Settings never exposes a secret.
# --------------------------------------------------------------------------


def test_settings_repr_and_dump_never_expose_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _valid_environment(monkeypatch)
    settings = config.Settings(_env_file=None)

    rendered = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        settings.model_dump_json(indent=2),
    ]

    for text in rendered:
        assert TOKEN not in text
        assert GEMINI_KEY not in text
        assert "SUPERSECRET" not in text


def test_settings_secret_values_are_readable_only_via_get_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Masking must not be so aggressive that the real value is unreachable."""
    _valid_environment(monkeypatch)
    settings = config.Settings(_env_file=None)

    dumped = settings.model_dump()

    assert TOKEN not in str(dumped["telegram_bot_token"])
    assert dumped["telegram_bot_token"].get_secret_value() == TOKEN


# --------------------------------------------------------------------------
# R2.3 — the validation error is NEVER printed verbatim.
# --------------------------------------------------------------------------


def test_settings_validation_error_text_never_contains_the_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanism-level prevention for the R2 leak.

    Pydantic's error object carries the *sibling* secret inside its ``input``
    payload. The premise is asserted first, so that this test cannot silently
    become vacuous.
    """
    _valid_environment(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY")

    with pytest.raises(ValidationError) as excinfo:
        config.Settings(_env_file=None)
    exc = excinfo.value

    # Premise: the error really does carry the sibling secret. This is why only
    # field locations may ever be read out of it.
    assert TOKEN in str(exc.errors()), "the validation error no longer carries the sibling secret"

    # Field names only, extracted from ValidationError.errors().
    assert config.settings_error_fields(exc) == ("gemini_api_key",)

    message = config.settings_error_message(exc)
    assert "gemini_api_key" in message
    assert ".env.example" in message
    assert TOKEN not in message
    assert "SUPERSECRET" not in message


def test_pydantic_error_text_leaks_a_slice_of_the_sibling_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the trap R2.3 exists to neutralise.

    ``str(exc)`` truncates ``input_value``, so a naive substring check for the
    whole token can pass while a slice of the secret is still on screen. This
    test records the observed behaviour. If a future pydantic stops leaking
    entirely, this test fails loudly and the R2.3 handling must be re-evaluated
    rather than quietly deleted.
    """
    _valid_environment(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY")

    with pytest.raises(ValidationError) as excinfo:
        config.Settings(_env_file=None)

    assert TOKEN not in str(excinfo.value)  # truncated, so not verbatim
    assert TOKEN[-10:] in str(excinfo.value)  # but a real slice is exposed


def test_settings_error_message_names_every_missing_field(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as excinfo:
        config.Settings(_env_file=None)

    message = config.settings_error_message(excinfo.value)

    assert "telegram_bot_token" in message
    assert "gemini_api_key" in message


def test_settings_error_fields_ignores_non_field_validation_errors() -> None:
    """Only real field names reach the message, never a payload fragment."""
    exc = ValidationError.from_exception_data(
        "Settings",
        [{"type": "missing", "loc": ("gemini_api_key",), "input": None}],
    )

    assert config.settings_error_fields(exc) == ("gemini_api_key",)
    assert TOKEN not in config.settings_error_message(exc)
