"""Application configuration, loaded from `.env` by `pydantic-settings`.

Two rules govern this module, both non-negotiable:

1. **A missing or blank secret is fatal.** No default, no fallback, no partial
   start. A blank value is the most likely real-world mistake in `.env`, and it
   must be caught at load rather than exploding inside a third-party library.
2. **A secret is never rendered.** Both fields are `SecretStr`, so an accidental
   `repr`, f-string or `model_dump` masks the value. On top of that, the
   `ValidationError` raised for a *missing* key embeds the *sibling* secret in
   its own text (see `settings_error_fields`), so the error message is built
   from field names only.
"""

from __future__ import annotations

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "settings_error_fields", "settings_error_message"]


class Settings(BaseSettings):
    """The complete configuration contract. Every field is required."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Unrelated variables in the developer's shell or `.env` must not be
        # able to break startup.
        extra="ignore",
    )

    telegram_bot_token: SecretStr
    gemini_api_key: SecretStr

    @field_validator("telegram_bot_token", "gemini_api_key")
    @classmethod
    def _reject_blank(cls, value: SecretStr) -> SecretStr:
        """Treat empty and whitespace-only secrets as missing.

        This cannot be delegated to ``Field(min_length=1)``: that constraint is
        not enforced on ``SecretStr``, so ``TELEGRAM_BOT_TOKEN="   "`` would
        otherwise load successfully and only fail much later as
        ``telegram.error.InvalidToken``.
        """
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value


def settings_error_fields(exc: ValidationError) -> tuple[str, ...]:
    """Return only the offending field *names*, de-duplicated, in order.

    Never return ``str(exc)`` to a caller, a log or stdout. For a missing key
    pydantic renders the whole input mapping, which includes the sibling
    secret's value:

        1 validation error for Settings
        gemini_api_key
          Field required [type=missing,
          input_value={'telegram_bot_token': 'SUPERSECRET'}, input_type=dict]

    ``ValidationError.errors()`` is the only safe source: it carries the same
    field locations, and this function reads nothing else from it.
    """
    names: list[str] = []
    for error in exc.errors():
        location = error.get("loc", ())
        if location:
            names.append(str(location[0]))
    return tuple(dict.fromkeys(names))


def settings_error_message(exc: ValidationError) -> str:
    """Build the user-facing fatal message. Field names only, no values."""
    fields = settings_error_fields(exc)
    named = ", ".join(fields) if fields else "<not reported by the validator>"
    return (
        f"Invalid configuration: missing or blank required setting(s): {named}. "
        "Fill them in your .env file - see .env.example for the exact names."
    )
