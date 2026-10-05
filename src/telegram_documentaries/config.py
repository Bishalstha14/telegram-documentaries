"""Application configuration, loaded from `.env` by `pydantic-settings`.

Two rules govern this module, both non-negotiable:

1. **A missing or blank secret is fatal.** No default, no fallback, no partial
   start. A blank value is the most likely real-world mistake in `.env`, and it
   must be caught at load rather than exploding inside a third-party library.
2. **A secret is never rendered.** Both fields are `SecretStr`, so an accidental
   `repr`, f-string or `model_dump` masks the value. On top of that, the
   `ValidationError` raised for a *missing* key embeds the *sibling* secret in
   its own text, so the error message is built from field names only - read via
   `contracts.validation_error_fields`, which this module shares with the Gemini
   boundary rather than reimplementing.
"""

from __future__ import annotations

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from telegram_documentaries.contracts import validation_error_fields

__all__ = ["Settings", "settings_error_message"]


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


def settings_error_message(exc: ValidationError) -> str:
    """Build the user-facing fatal message. Field names only, no values."""
    fields = validation_error_fields(exc)
    named = ", ".join(fields) if fields else "<not reported by the validator>"
    return (
        f"Invalid configuration: missing or blank required setting(s): {named}. "
        "Fill them in your .env file - see .env.example for the exact names."
    )
