"""Client secret generation and hashing (scrypt, stdlib only).

Secrets are 256-bit random URL-safe strings shown to the creator exactly once.
Only a salted scrypt hash is stored, and verification compares in constant time.
"""

import base64
import hashlib
import hmac
import secrets
from typing import Final

_N: Final = 2**14
_R: Final = 8
_P: Final = 1
_PREFIX: Final = "scrypt"


def new_client_id() -> str:
    return f"cl_{secrets.token_hex(12)}"


def new_client_secret() -> str:
    return f"cs_{secrets.token_urlsafe(32)}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"{_PREFIX}${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        prefix, n, r, p, salt, digest = stored.split("$")
        if prefix != _PREFIX:
            return False
        candidate = hashlib.scrypt(
            secret.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, _unb64(digest))
