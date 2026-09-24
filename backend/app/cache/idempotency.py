"""``Idempotency-Key`` support for POST requests (safe client retries).

A client that sends ``Idempotency-Key: <unique value>`` with a POST may retry the
same request after a timeout or dropped connection without doing the work twice:

1. The first request **reserves** the key (atomic ``SET NX`` with a short lock
   TTL) together with a fingerprint of the request (method, path, query, body).
2. When it completes with a 2xx, the response (status, a few headers, body) is
   stored for ``idempotency_ttl_seconds``. Any other outcome releases the key,
   so the client can fix the problem and retry with the same key.
3. A retry with the same key and the same request gets the stored response back,
   marked ``Idempotent-Replayed: true``. The endpoint does not run again.
4. The same key while the first request is still running: 409 (retry later).
   The same key with a *different* request: 422 (a client bug).

Keys are scoped to the authenticated caller (user or agent client), so one
caller cannot replay or block another caller's keys. The raw key is hashed
before it is used as a Redis key.

Failure policy: fail **closed**. A client that sends a key is asking for a
guarantee; if the store is down the request is refused with 503 rather than
executed without protection.

Scope and limits: POST only; requests with an invalid token are passed through
unchanged (the endpoint answers 401); responses over 1 MiB are not stored.
This is at-most-once *execution per key*, not exactly-once delivery: a request
whose reservation lock expires (``idempotency_lock_seconds``) while it is still
running can be executed a second time.
"""

import base64
import hashlib
import json
import logging
import re
from typing import Any
from uuid import UUID

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import problem_response
from app.auth.tokens import InvalidTokenError, TokenService
from app.cache.store import KeyValueStore, StoreUnavailableError

logger = logging.getLogger("contextledger.idempotency")

HEADER = "idempotency-key"
REPLAYED_HEADER = "Idempotent-Replayed"
USER_ID_HEADER = "x-contextledger-user-id"
KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,255}$")  # printable ASCII, no spaces
MAX_STORED_BODY = 1024 * 1024
REPLAYED_RESPONSE_HEADERS = frozenset({"content-type", "location", "etag"})
EXCLUDED_PATHS = ("/oauth/token", "/auth/dev-token")  # credentials must never be stored


class IdempotencyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        store: KeyValueStore,
        tokens: TokenService,
        allow_header_auth: bool,
        ttl_seconds: int,
        lock_seconds: int,
    ) -> None:
        self.app = app
        self._store = store
        self._tokens = tokens
        self._allow_header_auth = allow_header_auth
        self._ttl = ttl_seconds
        self._lock = lock_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        raw_key = request.headers.get(HEADER)
        if raw_key is None or request.url.path.endswith(EXCLUDED_PATHS):
            await self.app(scope, receive, send)
            return
        if not KEY_PATTERN.fullmatch(raw_key):
            await problem_response(
                request,
                400,
                "idempotency_key_invalid",
                "Idempotency-Key must be 1-255 printable ASCII characters without spaces",
            )(scope, receive, send)
            return
        subject = self._subject(request)
        if subject is None:  # unauthenticated: let the endpoint answer 401
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        fingerprint = hashlib.sha256(
            b"\0".join(
                [scope["method"].encode(), scope["path"].encode(), scope["query_string"], body]
            )
        ).hexdigest()
        store_key = "cl:idempotency:" + hashlib.sha256(f"{subject}\0{raw_key}".encode()).hexdigest()
        pending = json.dumps({"state": "pending", "fingerprint": fingerprint}).encode()

        try:
            reserved = await self._store.set(
                store_key, pending, ttl_seconds=self._lock, only_if_absent=True
            )
            existing = None if reserved else await self._store.get(store_key)
        except StoreUnavailableError as exc:
            logger.warning("idempotency.store_unavailable", extra={"error": str(exc)})
            await problem_response(
                request,
                503,
                "service_unavailable",
                "idempotency keys cannot be honoured right now; retry later",
            )(scope, receive, send)
            return

        if not reserved:
            await self._answer_existing(request, existing, fingerprint, scope, receive, send)
            return
        await self._execute(store_key, fingerprint, body, scope, send)

    def _subject(self, request: Request) -> str | None:
        authorization = request.headers.get("authorization")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return None
            try:
                claims = self._tokens.verify(token.strip())
            except InvalidTokenError:
                return None
            if claims.agent_client_id is not None:
                return f"agent:{claims.agent_client_id}"
            return f"user:{claims.user_id}"
        raw = request.headers.get(USER_ID_HEADER)
        if self._allow_header_auth and raw:
            try:
                return f"user:{UUID(raw)}"
            except ValueError:
                return None
        return None

    async def _answer_existing(
        self,
        request: Request,
        existing: bytes | None,
        fingerprint: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        record: dict[str, Any] = {} if existing is None else json.loads(existing)
        if record and record["fingerprint"] != fingerprint:
            await problem_response(
                request,
                422,
                "idempotency_key_reused",
                "this Idempotency-Key was already used for a different request",
            )(scope, receive, send)
            return
        if record.get("state") != "done":
            # Still running (or released a moment ago): the client should retry.
            await problem_response(
                request,
                409,
                "idempotency_key_in_use",
                "a request with this Idempotency-Key is still being processed",
                headers={"Retry-After": "1"},
            )(scope, receive, send)
            return
        headers = [(k.encode(), v.encode()) for k, v in record["headers"]]
        headers.append((REPLAYED_HEADER.lower().encode(), b"true"))
        await send({"type": "http.response.start", "status": record["status"], "headers": headers})
        await send({"type": "http.response.body", "body": base64.b64decode(record["body"])})

    async def _execute(
        self, store_key: str, fingerprint: str, body: bytes, scope: Scope, send: Send
    ) -> None:
        status = 500
        headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []
        size = 0

        async def replay_receive() -> Message:
            nonlocal body
            chunk, body = body, b""
            return {"type": "http.request", "body": chunk, "more_body": False}

        async def capture(message: Message) -> None:
            nonlocal status, size
            if message["type"] == "http.response.start":
                status = message["status"]
                for name, value in message.get("headers", []):
                    if name.decode().lower() in REPLAYED_RESPONSE_HEADERS:
                        headers.append((name.decode().lower(), value.decode()))
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                size += len(chunk)
                if size <= MAX_STORED_BODY:
                    chunks.append(chunk)
            await send(message)

        try:
            await self.app(scope, replay_receive, capture)
        except BaseException:
            await self._release(store_key)
            raise
        if not 200 <= status < 300 or size > MAX_STORED_BODY:
            await self._release(store_key)
            return
        record = {
            "state": "done",
            "fingerprint": fingerprint,
            "status": status,
            "headers": headers,
            "body": base64.b64encode(b"".join(chunks)).decode(),
        }
        try:
            await self._store.set(store_key, json.dumps(record).encode(), ttl_seconds=self._ttl)
        except StoreUnavailableError as exc:
            # The work is done and answered; only the replay record is lost.
            logger.warning("idempotency.record_lost", extra={"error": str(exc)})

    async def _release(self, store_key: str) -> None:
        try:
            await self._store.delete(store_key)
        except StoreUnavailableError as exc:
            # The reservation expires after idempotency_lock_seconds anyway.
            logger.warning("idempotency.release_failed", extra={"error": str(exc)})


async def _read_body(receive: Receive) -> bytes:
    parts: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        parts.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(parts)
