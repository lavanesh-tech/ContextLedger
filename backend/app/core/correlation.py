"""Request correlation IDs.

A correlation ID ties together every log line, and later every trace span,
Kafka event and decision receipt, produced while handling one request.

The middleware is written as plain ASGI rather than Starlette's
``BaseHTTPMiddleware``: it does not buffer responses, works with streaming,
and keeps the ``ContextVar`` visible to the downstream handler.
"""

import json
import logging
import re
import time
from contextvars import ContextVar
from typing import Final
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("contextledger.http")

# Accept only short, log-safe IDs from clients. Anything else (newlines, very
# long values, unusual characters) is discarded to prevent log injection.
_VALID_CORRELATION_ID: Final = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Return the correlation ID of the request being handled, if any."""
    return _correlation_id.get()


def new_correlation_id() -> str:
    return str(uuid4())


def resolve_correlation_id(candidate: str | None) -> str:
    """Reuse a caller-supplied ID when it is safe, otherwise mint a new one."""
    if candidate and _VALID_CORRELATION_ID.fullmatch(candidate):
        return candidate
    return new_correlation_id()


class CorrelationIdMiddleware:
    """Assign a correlation ID to each HTTP request and log its completion."""

    def __init__(self, app: ASGIApp, header_name: str = "X-Correlation-ID") -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = resolve_correlation_id(Headers(scope=scope).get(self.header_name))
        token = _correlation_id.set(correlation_id)
        started = time.perf_counter()
        status_code = 500  # assumed until the app actually starts a response
        failed = False

        response_started = False

        async def send_with_header(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[self.header_name] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        except Exception:
            failed = True
            if response_started:  # too late to replace the response: let the server abort it
                raise
            # Answer here, inside the middleware, so even an unexpected error carries
            # the correlation ID the client can quote. No internals are exposed.
            status_code = 500
            body = json.dumps(
                {
                    "type": "about:blank",
                    "title": "Internal Server Error",
                    "status": 500,
                    "code": "internal_error",
                    "detail": "An unexpected error occurred. Quote the correlation ID.",
                    "correlation_id": correlation_id,
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/problem+json"),
                        (b"content-length", str(len(body)).encode()),
                        (self.header_name.lower().encode(), correlation_id.encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        finally:
            # Query strings are deliberately not logged: they can carry
            # identifiers or tokens that do not belong in log storage.
            logger.log(
                logging.ERROR if failed else logging.INFO,
                "request.completed",
                extra={
                    "http_method": scope["method"],
                    "http_path": scope["path"],
                    "http_status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
                exc_info=failed,
            )
            _correlation_id.reset(token)
