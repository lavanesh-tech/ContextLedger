"""Structured logging built on the standard library.

Every record is emitted as one JSON object per line (or a readable text line
when ``log_json`` is disabled for local debugging). The active correlation ID
is attached to every record by :class:`CorrelationIdFilter`, so log lines from
anywhere in a request can be joined together without passing IDs around.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final

from app.core.config import Settings
from app.core.correlation import get_correlation_id

HANDLER_NAME: Final = "contextledger"

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_RESERVED_ATTRS: Final = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "correlation_id",
    "taskName",
}

_UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")


class CorrelationIdFilter(logging.Filter):
    """Copy the current request's correlation ID onto each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = get_correlation_id()
        return True


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_ATTRS and not key.startswith("_")
        }
        base: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        # Core fields win over ``extra`` keys so callers cannot spoof them.
        payload: dict[str, object] = {**extras, **base}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_TEXT_FORMAT: Final = "%(asctime)s %(levelname)-8s [%(correlation_id)s] %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Install the ContextLedger handler on the root logger.

    Safe to call more than once: only the handler this function owns is
    replaced, so handlers added by test tooling (e.g. pytest's caplog) survive.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.addFilter(CorrelationIdFilter())
    handler.setFormatter(JsonFormatter() if settings.log_json else logging.Formatter(_TEXT_FORMAT))

    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == HANDLER_NAME]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Route uvicorn's own loggers through the same JSON handler. Its access log
    # is redundant with our request log and is disabled via --no-access-log.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
