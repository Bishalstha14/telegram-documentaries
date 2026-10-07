"""Structured logging over the stdlib `logging` module.

The constitution prefers a decorator over scattering `log.info(...)` through
business logic, so `@logged` is the single entry point for instrumentation: it
stamps the correlation context and the elapsed time onto every call, and it
never swallows an exception - it logs and re-raises.

Deliberately *not* called `logging.py`: shadowing a stdlib module name inside
the package is a permanent footgun for every future reader.
"""

from __future__ import annotations

import functools
import inspect
import logging
import sys
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

__all__ = ["LOGGER_NAME", "configure_logging", "get_logger", "logged"]

LOGGER_NAME = "telegram_documentaries"

#: Attributes that must never be overwritten by caller-supplied ``extra``.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_P = TypeVar("_P")
_R = TypeVar("_R")

logger = logging.getLogger(LOGGER_NAME)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach the application's stdout handler exactly once.

    Idempotent by design: `__main__` and any test may call it without the log
    output being duplicated.
    """
    if logger.handlers:
        logger.setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_KeyValueFormatter())

    logger.addHandler(handler)
    logger.setLevel(level)
    # The application logger owns its own output; do not also spill onto the
    # root logger, which pytest configures separately.
    logger.propagate = False


def get_logger(suffix: str | None = None) -> logging.Logger:
    """Return a child of the application logger, or the logger itself."""
    if suffix is None:
        return logger
    return logger.getChild(suffix)


class _KeyValueFormatter(logging.Formatter):
    """Render `extra` context as stable, greppable `key=value` pairs."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context = {
            key: value
            for key, value in vars(record).items()
            if key not in _RESERVED_RECORD_ATTRS
        }
        if not context:
            return base
        rendered = " ".join(f"{key}={value}" for key, value in sorted(context.items()))
        return f"{base} | {rendered}"


def logged(
    event: str,
    *,
    chat_id: str = "chat_id",
    update_id: str = "update_id",
) -> Callable[[Callable[..., _R]], Callable[..., _R]]:
    """Instrument a sync or async callable with a structured log line.

    The emitted record always carries ``event``, ``chat_id``, ``update_id`` and
    ``duration_ms``. ``chat_id`` and ``update_id`` default to the wrapped
    callable's own parameter names, so the context lines up with the domain
    vocabulary; pass the keyword explicitly for any other source.

    An exception is logged at ``ERROR`` with traceback and re-raised unchanged.
    This decorator never converts a failure into a success.
    """

    def decorate(func: Callable[..., _R]) -> Callable[..., _R]:
        def _context(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, Any]:
            """Pull the correlation ids out of the call, defaulting to None."""
            bound = inspect.signature(func).bind_partial(*args, **kwargs)
            return (
                bound.arguments.get(chat_id),
                bound.arguments.get(update_id),
            )

        def _record(
            level: int,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
            started: float,
            exc_info: Any = None,
        ) -> None:
            call_chat_id, call_update_id = _context(args, kwargs)
            logger.log(
                level,
                "%s",
                event,
                extra={
                    "event": event,
                    "chat_id": call_chat_id,
                    "update_id": call_update_id,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
                exc_info=exc_info,
            )

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                except Exception:
                    _record(logging.ERROR, args, kwargs, started, exc_info=True)
                    raise
                _record(logging.INFO, args, kwargs, started)
                return result

            return cast("Callable[..., _R]", async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception:
                _record(logging.ERROR, args, kwargs, started, exc_info=True)
                raise
            _record(logging.INFO, args, kwargs, started)
            return result

        return cast("Callable[..., _R]", sync_wrapper)

    return decorate
