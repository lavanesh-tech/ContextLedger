import json
import logging
import sys

import pytest

from app.core.config import Settings
from app.core.correlation import _correlation_id
from app.core.logging import (
    HANDLER_NAME,
    CorrelationIdFilter,
    JsonFormatter,
    configure_logging,
)


def _record(message: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("contextledger.test", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_json_object_with_core_fields() -> None:
    line = JsonFormatter().format(_record(correlation_id="abc-123"))

    payload = json.loads(line)
    assert "\n" not in line
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "contextledger.test"
    assert payload["correlation_id"] == "abc-123"
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_includes_extra_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record(http_status=204, duration_ms=1.5)))

    assert payload["http_status"] == 204
    assert payload["duration_ms"] == 1.5


def test_extra_fields_cannot_overwrite_core_fields() -> None:
    record = _record(level="FAKE", logger="spoofed")

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "contextledger.test"


def test_json_formatter_serialises_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]


def test_filter_attaches_the_active_correlation_id() -> None:
    token = _correlation_id.set("req-42")
    try:
        record = _record()
        CorrelationIdFilter().filter(record)
    finally:
        _correlation_id.reset(token)

    assert record.__dict__["correlation_id"] == "req-42"


def test_filter_uses_none_outside_a_request() -> None:
    record = _record()
    CorrelationIdFilter().filter(record)

    assert record.__dict__["correlation_id"] is None


def test_configure_logging_is_idempotent_and_keeps_foreign_handlers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(_env_file=None, log_level="DEBUG")

    configure_logging(settings)
    configure_logging(settings)

    root = logging.getLogger()
    owned = [h for h in root.handlers if h.get_name() == HANDLER_NAME]
    assert len(owned) == 1
    assert caplog.handler in root.handlers
    assert root.level == logging.DEBUG
    assert logging.getLogger("uvicorn.error").propagate is True
