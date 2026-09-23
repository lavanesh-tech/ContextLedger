import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest

from app.auth.secrets import hash_secret, new_client_id, new_client_secret, verify_secret
from app.auth.tokens import (
    InvalidTokenError,
    TokenService,
    generate_private_key_pem,
    public_key_pem,
)
from app.core.config import Environment, Settings
from app.domain.facts import PrivacyScope
from app.domain.roles import Permission

KEY = generate_private_key_pem()
OTHER_KEY = generate_private_key_pem()
USER = UUID(int=7)
ORG = UUID(int=9)
AGENT = UUID(int=11)


def service(key: str = KEY, **overrides: object) -> TokenService:
    options: dict[str, object] = {
        "private_key_pem": key,
        "key_id": "k1",
        "issuer": "contextledger",
        "audience": "contextledger-api",
        "ttl": timedelta(minutes=15),
    }
    options.update(overrides)
    return TokenService(**options)  # type: ignore[arg-type]


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


# --- happy paths ------------------------------------------------------------------------


def test_user_token_round_trip() -> None:
    token, expires = service().issue(subject=f"user:{USER}", user_id=USER)

    claims = service().verify(token)

    assert claims.user_id == USER
    assert claims.organization_id is None
    assert claims.scopes is None
    assert claims.agent_client_id is None
    assert abs((claims.expires_at - expires).total_seconds()) < 1


def test_agent_token_carries_tenant_scopes_and_privacy() -> None:
    token, _ = service().issue(
        subject=f"agent:{AGENT}",
        user_id=USER,
        organization_id=ORG,
        scopes=frozenset({Permission.READ_FACTS, Permission.RECORD_DECISIONS}),
        max_privacy_scope=PrivacyScope.PUBLIC,
    )

    claims = service().verify(token)

    assert claims.agent_client_id == AGENT
    assert claims.organization_id == ORG
    assert claims.scopes == frozenset({Permission.READ_FACTS, Permission.RECORD_DECISIONS})
    assert claims.max_privacy_scope is PrivacyScope.PUBLIC


def test_rotated_keys_keep_verifying_until_removed() -> None:
    old_token, _ = service(OTHER_KEY, key_id="k0").issue(subject=f"user:{USER}", user_id=USER)
    rotated = service(extra_public_keys={"k0": public_key_pem(OTHER_KEY)})

    assert rotated.verify(old_token).user_id == USER
    with pytest.raises(InvalidTokenError):
        service().verify(old_token)


# --- rejections ---------------------------------------------------------------------------


def test_expired_tokens_are_rejected() -> None:
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    token, _ = service().issue(subject=f"user:{USER}", user_id=USER, now=long_ago)

    with pytest.raises(InvalidTokenError, match="Expired"):
        service().verify(token)


@pytest.mark.parametrize(
    ("field", "value"), [("issuer", "someone-else"), ("audience", "other-api")]
)
def test_tokens_for_another_issuer_or_audience_are_rejected(field: str, value: str) -> None:
    token, _ = service(**{field: value}).issue(subject=f"user:{USER}", user_id=USER)

    with pytest.raises(InvalidTokenError):
        service().verify(token)


def test_a_forged_signature_is_rejected() -> None:
    token, _ = service(OTHER_KEY).issue(subject=f"user:{USER}", user_id=USER)

    with pytest.raises(InvalidTokenError):
        service().verify(token)


def test_tampered_claims_are_rejected() -> None:
    token, _ = service().issue(subject=f"user:{USER}", user_id=USER)
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["uid"] = str(UUID(int=666))

    with pytest.raises(InvalidTokenError):
        service().verify(f"{header}.{_b64(claims)}.{signature}")


def test_alg_none_is_rejected() -> None:
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": "contextledger",
        "aud": "contextledger-api",
        "sub": f"user:{USER}",
        "uid": str(USER),
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "jti": "x",
    }
    unsigned = f"{_b64({'alg': 'none', 'kid': 'k1'})}.{_b64(claims)}."

    with pytest.raises(InvalidTokenError):
        service().verify(unsigned)


def test_algorithm_confusion_with_the_public_key_is_rejected() -> None:
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "iss": "contextledger",
            "aud": "contextledger-api",
            "sub": f"user:{USER}",
            "uid": str(USER),
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "jti": "x",
        },
        "attacker-controlled-hmac-secret-of-32-bytes-or-more",
        algorithm="HS256",
        headers={"kid": "k1"},
    )

    with pytest.raises(InvalidTokenError):
        service().verify(forged)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer x"])
def test_garbage_is_rejected(garbage: str) -> None:
    with pytest.raises(InvalidTokenError):
        service().verify(garbage)


def test_unknown_subject_types_are_rejected() -> None:
    token, _ = service().issue(subject=f"robot:{USER}", user_id=USER)

    with pytest.raises(InvalidTokenError, match="subject"):
        service().verify(token)


# --- settings and secrets -------------------------------------------------------------------


def test_local_environments_get_an_ephemeral_key() -> None:
    tokens = TokenService.from_settings(Settings(_env_file=None, environment=Environment.TEST))
    token, _ = tokens.issue(subject=f"user:{USER}", user_id=USER)

    assert tokens.verify(token).user_id == USER


def test_client_secrets_are_hashed_and_verified_in_constant_time() -> None:
    secret = new_client_secret()
    stored = hash_secret(secret)

    assert secret not in stored
    assert stored != hash_secret(secret)  # salted
    assert verify_secret(secret, stored)
    assert not verify_secret(secret + "x", stored)
    assert not verify_secret(secret, "not-a-hash")
    assert new_client_id().startswith("cl_")
    assert len(secret) > 40
