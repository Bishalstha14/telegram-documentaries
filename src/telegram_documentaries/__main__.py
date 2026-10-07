"""Entry point: `python -m telegram_documentaries`.

Three rules govern this module, in order of importance.

**A secret is never printed. Not once, not anywhere.**
The single riskiest thing in this phase lives right here. When one key is
missing, pydantic's `ValidationError` renders the *whole* input mapping in its
own text, which includes the sibling key's real value:

    1 validation error for Settings
    gemini_api_key
      Field required [type=missing,
      input_value={'telegram_bot_token': 'SUPERSECRET'}, input_type=dict]

So `str(exc)` is never logged, never printed, and never attached to a log
record as `exc_info` - a formatted traceback renders the same leaky text. Field
names are read from `ValidationError.errors()` and nothing else (R2.3).

**Bad configuration fails loud, once, with a way to fix it.**
One CRITICAL line naming the offending field(s), pointing at `.env.example`,
and exit code `1`. No default, no fallback, and no partial start: the gateway is
built only after settings validate, so an invalid config never reaches the
network (R2.4, D5). The original error is chained onto
:class:`ConfigurationError` for a developer with a debugger attached, which is
safe precisely because it is never rendered (MISSION.md #2).

**A return code, not an exception.**
`main() -> int` keeps the process exit code a testable value instead of
something inferred from a traceback, and keeps `python -m` free of a
`sys.exit` buried in business logic.
"""

from __future__ import annotations

from pydantic import ValidationError
from telegram import Bot

from telegram_documentaries import bot, observability
from telegram_documentaries.config import Settings, settings_error_message
from telegram_documentaries.contracts import validation_error_fields
from telegram_documentaries.gemini import GenAiGeminiClient
from telegram_documentaries.media import MediaStore
from telegram_documentaries.pipeline import ConversationPipeline
from telegram_documentaries.state import SessionStore

__all__ = ["ConfigurationError", "load_settings", "main"]

logger = observability.get_logger("main")


class ConfigurationError(Exception):
    """A `Settings` failure, with the offending field names attached.

    The originating :class:`~pydantic.ValidationError` is chained as
    ``__cause__`` so a developer can inspect it, and is deliberately never
    rendered: its text carries the sibling secret (see the module docstring).
    """

    def __init__(self, message: str, fields: tuple[str, ...]) -> None:
        super().__init__(message)
        self.fields = fields


def load_settings() -> Settings:
    """Load and validate `Settings`, or raise `ConfigurationError`.

    Wrapping exists to make the leak impossible to reach by accident: `main()`
    handles only `ConfigurationError`, so no code path from here can format the
    raw `ValidationError`. The chain is preserved for debugging; the message
    names fields and nothing else.
    """
    try:
        return Settings()
    except ValidationError as exc:
        # Field names only. `str(exc)` is never touched - see the module
        # docstring for exactly what it would leak.
        raise ConfigurationError(
            settings_error_message(exc), validation_error_fields(exc)
        ) from exc


async def _log_gateway_started(application: bot.GatewayApplication) -> None:
    """Log `gateway_started` once Telegram has confirmed who the bot is.

    Registered as `post_init` because that is the first moment
    `application.bot.username` is populated - `Bot.bot` raises before
    initialisation, so the username genuinely cannot be logged earlier.

    The username is safe to log (R4.4); the token never is, and never reaches
    this function.
    """
    logger.info(
        "gateway_started",
        extra={
            "event": "gateway_started",
            "bot_username": application.bot.username,
        },
    )


def _build_pipeline(settings: Settings) -> ConversationPipeline:
    """Assemble the hub with every real dependency (D10).

    The four collaborators are built here rather than inside the adapter, so
    `bot.py` depends on the domain and never constructs it.

    Note:
        The photo port needs a `Bot` before `build_application` has built the
        `Application` that owns one, so the fetcher gets a second `Bot` over the
        same token. `Bot` is a thin stateless HTTP client, so this costs one
        connection pool rather than any correctness - and it keeps the port
        constructible in a test with nothing but a fake. The token is read from
        `settings` and, like everywhere else, never logged (R4.4).
    """
    return ConversationPipeline(
        client=GenAiGeminiClient(
            api_key=settings.gemini_api_key.get_secret_value()
        ),
        sessions=SessionStore(),
        media=MediaStore(),
        fetcher=bot.TelegramPhotoFetcher(Bot(settings.telegram_bot_token.get_secret_value())),
    )


def _report_invalid_settings(error: ConfigurationError) -> None:
    """Emit the single fatal line naming the offending field(s)."""
    logger.critical(
        str(error),
        extra={
            "event": "settings_invalid",
            "fields": ", ".join(error.fields),
        },
    )


def main() -> int:
    """Start the gateway. Returns the process exit code.

    Returns:
        `0` when polling ran and stopped cleanly, `1` when configuration is
        invalid. A `KeyboardInterrupt` from Ctrl-C is absorbed by
        `Application.run_polling`'s own shutdown, so stopping the bot is also a
        clean `0`.
    """
    observability.configure_logging()

    try:
        settings = load_settings()
    except ConfigurationError as error:
        # Exactly one line, then out. No `exc_info` - see the module docstring.
        _report_invalid_settings(error)
        return 1

    logger.info("settings_loaded", extra={"event": "settings_loaded"})

    # `get_secret_value` is the only way to read a `SecretStr`, and the value
    # goes straight from here into the Telegram client. It is never logged,
    # never formatted into a message, and never stored (R4.4).
    token = settings.telegram_bot_token.get_secret_value()
    application = bot.build_application(token, _build_pipeline(settings))
    application.post_init = _log_gateway_started
    application.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
