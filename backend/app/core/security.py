"""HTTP hardening middleware (Phase 28 security review).

* ``SecurityHeadersMiddleware`` adds response headers that cost nothing for a JSON
  API: no MIME sniffing, no framing, no referrer, no caching of tenant data, and a
  deny-all Content-Security-Policy (the API serves no HTML, except the interactive
  docs, which are disabled outside local development and keep their own CSP-free
  page). HSTS is sent only outside local/test, where TLS is terminated in front of
  the app.
* ``BodySizeLimitMiddleware`` rejects request bodies above a limit with 413 before
  they are parsed: by ``Content-Length`` when present, otherwise while streaming.

Both are pure ASGI (no response buffering) and apply to every route.
"""

import json
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DOCS_PATHS: Final = ("/docs", "/redoc", "/openapi.json")

BASE_HEADERS: Final = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
API_CSP: Final = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
HSTS: Final = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        docs = str(scope.get("path", "")).startswith(DOCS_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in BASE_HEADERS.items():
                    headers.setdefault(name, value)
                if not docs:
                    headers.setdefault("Content-Security-Policy", API_CSP)
                    headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", HSTS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await self._reject(send, 400, "invalid Content-Length header")
                    return
                if declared > self.max_bytes:
                    await self._reject(send, 413, self._detail())
                    return

        # No Content-Length (chunked): count while the app reads. Exceptions raised
        # from receive() would be swallowed by body parsing, so on overflow the app
        # sees a disconnect and whatever response it starts is replaced by a 413.
        received = 0
        too_large = False
        replaced = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal replaced
            if too_large:
                if not replaced:
                    replaced = True
                    await self._reject(send, 413, self._detail())
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large and not replaced:
            await self._reject(send, 413, self._detail())

    def _detail(self) -> str:
        return f"request body exceeds {self.max_bytes} bytes"

    @staticmethod
    async def _reject(send: Send, status: int, detail: str) -> None:
        title = "Content Too Large" if status == 413 else "Bad Request"
        body = json.dumps(
            {
                "type": "about:blank",
                "title": title,
                "status": status,
                "code": "payload_too_large" if status == 413 else "bad_request",
                "detail": detail,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
