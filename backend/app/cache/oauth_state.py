"""Single-use OAuth ``state`` values (CSRF protection for redirect-based flows).

``issue`` creates an unguessable state string bound to a payload (the client,
redirect URI, PKCE challenge, ...). ``consume`` returns the payload exactly once:
the read and the delete are one atomic ``GETDEL``, so two concurrent callbacks
with the same state cannot both succeed. Only a SHA-256 of the state is used as
the key, so the store never holds a usable state value.

It fails **closed**: if the store is unavailable, ``consume`` raises and the
flow is refused. Accepting an unverifiable state would defeat its purpose.

The client-credentials grant (``/oauth/token``) needs no state; this store is
used by redirect-based flows (the OAuth authorization-code flow for remote MCP
clients).
"""

import hashlib
import json
import secrets
from typing import Any

from app.cache.store import KeyValueStore

STATE_BYTES = 32


def _key(state: str) -> str:
    return "cl:oauth:state:" + hashlib.sha256(state.encode()).hexdigest()


class OAuthStateStore:
    def __init__(self, store: KeyValueStore, *, ttl_seconds: int = 600) -> None:
        self._store = store
        self._ttl = ttl_seconds

    async def issue(self, payload: dict[str, Any]) -> str:
        state = secrets.token_urlsafe(STATE_BYTES)
        await self._store.set(
            _key(state), json.dumps(payload, sort_keys=True).encode(), ttl_seconds=self._ttl
        )
        return state

    async def consume(self, state: str) -> dict[str, Any] | None:
        """The payload for ``state``, or None if unknown, expired or already used."""
        if not state or len(state) > 512:
            return None
        raw = await self._store.get_and_delete(_key(state))
        if raw is None:
            return None
        payload: dict[str, Any] = json.loads(raw)
        return payload
