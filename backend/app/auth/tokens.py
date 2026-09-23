"""Access tokens: short-lived ES256 JWTs issued and verified by ContextLedger.

Claims::

    iss, aud          fixed per deployment, always checked
    sub               "user:<uuid>" or "agent:<agent client uuid>"
    iat, nbf, exp, jti
    uid               the user the request acts as (agents: their service user)
    org               agents only: the one organization the token is valid for
    scope             agents only: space-separated permissions (narrow the role)
    privacy           agents only: the most sensitive privacy scope readable

Verification pins the algorithm (no "none", no algorithm confusion), selects
the key by ``kid`` (so keys can be rotated: old public keys stay trusted until
their tokens expire), and requires every time claim.
"""

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Environment, Settings
from app.domain.facts import PrivacyScope
from app.domain.roles import Permission

ALGORITHM: Final = "ES256"
LEEWAY: Final = timedelta(seconds=30)
logger = logging.getLogger("contextledger.auth")


class InvalidTokenError(Exception):
    """The token is malformed, expired, not for us, or signed by an unknown key."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    scopes: frozenset[Permission] | None
    max_privacy_scope: PrivacyScope | None
    agent_client_id: uuid.UUID | None
    expires_at: datetime
    token_id: str


def generate_private_key_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def public_key_pem(private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


class TokenService:
    def __init__(
        self,
        *,
        private_key_pem: str,
        key_id: str,
        issuer: str,
        audience: str,
        ttl: timedelta,
        extra_public_keys: Mapping[str, str] | None = None,
    ) -> None:
        self._private_key = private_key_pem
        self._key_id = key_id
        self._issuer = issuer
        self._audience = audience
        self.ttl = ttl
        self._public_keys = {key_id: public_key_pem(private_key_pem), **(extra_public_keys or {})}

    @classmethod
    def from_settings(cls, settings: Settings) -> "TokenService":
        private = settings.jwt_signing_key.get_secret_value()
        if not private:
            if settings.environment not in {Environment.LOCAL, Environment.TEST}:
                raise RuntimeError("CONTEXTLEDGER_JWT_SIGNING_KEY is required outside local/test")
            logger.warning("auth.ephemeral_signing_key", extra={"reason": "no key configured"})
            private = generate_private_key_pem()
        return cls(
            private_key_pem=private,
            key_id=settings.jwt_key_id,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            ttl=timedelta(seconds=settings.jwt_access_token_ttl_seconds),
            extra_public_keys=json.loads(settings.jwt_previous_public_keys.get_secret_value()),
        )

    def issue(
        self,
        *,
        subject: str,
        user_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
        scopes: frozenset[Permission] | None = None,
        max_privacy_scope: PrivacyScope | None = None,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        issued = now or datetime.now(UTC)
        expires = issued + self.ttl
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": subject,
            "uid": str(user_id),
            "iat": issued,
            "nbf": issued,
            "exp": expires,
            "jti": uuid.uuid4().hex,
        }
        if organization_id is not None:
            claims["org"] = str(organization_id)
        if scopes is not None:
            claims["scope"] = " ".join(sorted(scopes))
        if max_privacy_scope is not None:
            claims["privacy"] = str(max_privacy_scope)
        token = jwt.encode(
            claims, self._private_key, algorithm=ALGORITHM, headers={"kid": self._key_id}
        )
        return token, expires

    def verify(self, token: str) -> TokenClaims:
        try:
            header = jwt.get_unverified_header(token)
            key = self._public_keys.get(str(header.get("kid")))
            if key is None:
                raise InvalidTokenError("unknown signing key")
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY,
                options={"require": ["iss", "aud", "sub", "uid", "iat", "nbf", "exp", "jti"]},
            )
            subject = str(claims["sub"])
            agent_id = (
                uuid.UUID(subject.removeprefix("agent:")) if subject.startswith("agent:") else None
            )
            if agent_id is None and not subject.startswith("user:"):
                raise InvalidTokenError("unknown subject type")
            return TokenClaims(
                subject=subject,
                user_id=uuid.UUID(claims["uid"]),
                organization_id=uuid.UUID(claims["org"]) if "org" in claims else None,
                scopes=frozenset(Permission(s) for s in claims["scope"].split())
                if "scope" in claims
                else None,
                max_privacy_scope=PrivacyScope(claims["privacy"]) if "privacy" in claims else None,
                agent_client_id=agent_id,
                expires_at=datetime.fromtimestamp(claims["exp"], UTC),
                token_id=str(claims["jti"]),
            )
        except InvalidTokenError:
            raise
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise InvalidTokenError(type(exc).__name__) from exc


def main() -> None:
    """``make jwt-key``: print a new ES256 private key (PEM) for CONTEXTLEDGER_JWT_SIGNING_KEY."""
    print(generate_private_key_pem(), end="")  # noqa: T201


if __name__ == "__main__":
    main()
