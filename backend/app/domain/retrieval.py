"""Pure rules for hybrid retrieval: query validation, privacy visibility, ranking.

Nothing here touches the database or a model provider, so every rule is unit
tested directly. The SQL side (app/repositories/retrieval.py) only produces
candidate lists; *how* they are combined is decided here.

Ranking, in order:

1. Each branch (vector similarity, full-text) ranks its own candidates.
2. **Reciprocal Rank Fusion**: ``rrf = Σ 1 / (rrf_k + rank)`` over the branches
   that found the version. RRF uses ranks, not raw scores, so a cosine distance
   and a ``ts_rank_cd`` value never have to be put on the same scale.
3. **Trust**: ``trust = authority / 100 * confidence`` (both recorded on the version).
4. ``final = rrf * ((1 - trust_weight) + trust_weight * trust)``.
   ``trust_weight = 0`` is pure relevance; ``1`` makes an untrusted fact score 0.

Ties are broken by fact version id, so identical inputs always give identical output.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from app.domain.errors import ValidationFailedError
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole

MAX_QUERY_CHARS: Final = 1000
MAX_LIMIT: Final = 50
DEFAULT_RRF_K: Final = 60
DEFAULT_TRUST_WEIGHT: Final = 0.3

# Least to most sensitive.
PRIVACY_ORDER: Final = (
    PrivacyScope.PUBLIC,
    PrivacyScope.INTERNAL,
    PrivacyScope.CONFIDENTIAL,
    PrivacyScope.RESTRICTED,
)

# The most sensitive scope each role may retrieve. The caller can narrow it
# further (e.g. an agent that must only ever see PUBLIC facts), never widen it.
ROLE_MAX_PRIVACY: Final = {
    MembershipRole.VIEWER: PrivacyScope.INTERNAL,
    MembershipRole.ENGINEER: PrivacyScope.CONFIDENTIAL,
    MembershipRole.ADMIN: PrivacyScope.RESTRICTED,
}

_WHITESPACE = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    cleaned = _WHITESPACE.sub(" ", query).strip()
    if not cleaned:
        raise ValidationFailedError("query must not be empty")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ValidationFailedError(f"query must be at most {MAX_QUERY_CHARS} characters")
    return cleaned


def visible_privacy_scopes(
    role: MembershipRole, *ceilings: PrivacyScope | None
) -> frozenset[PrivacyScope]:
    """Scopes the actor may see: capped by role, and by every given ceiling
    (a request's ``max_privacy_scope``, an agent token's ceiling...). Ceilings can
    only narrow what the role allows, never widen it."""
    ceiling = PRIVACY_ORDER.index(ROLE_MAX_PRIVACY[role])
    for requested in ceilings:
        if requested is not None:
            ceiling = min(ceiling, PRIVACY_ORDER.index(PrivacyScope(requested)))
    return frozenset(PRIVACY_ORDER[: ceiling + 1])


def validate_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailedError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def validate_trust_weight(weight: float) -> float:
    if not 0.0 <= weight <= 1.0:
        raise ValidationFailedError("trust_weight must be between 0 and 1")
    return weight


def candidate_pool_size(limit: int) -> int:
    """How many candidates each branch fetches before fusion."""
    return min(200, max(40, limit * 4))


def trust_score(authority: int, confidence: Decimal | float) -> float:
    return (authority / 100.0) * float(confidence)


@dataclass(frozen=True, slots=True)
class VectorHit:
    fact_version_id: UUID
    distance: float  # cosine distance: 0 = same direction, 2 = opposite


@dataclass(frozen=True, slots=True)
class TextHit:
    fact_version_id: UUID
    score: float  # ts_rank_cd: higher is better


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    fact_version_id: UUID
    vector_rank: int | None
    vector_distance: float | None
    text_rank: int | None
    text_score: float | None
    rrf_score: float
    trust: float
    score: float


def fuse(
    vector_hits: Iterable[VectorHit],
    text_hits: Iterable[TextHit],
    trust: Mapping[UUID, float],
    *,
    trust_weight: float = DEFAULT_TRUST_WEIGHT,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[RankedCandidate]:
    """Combine both branches into one deterministic ranking (best first)."""
    validate_trust_weight(weight=trust_weight)
    if rrf_k < 1:
        raise ValidationFailedError("rrf_k must be positive")
    vectors = _dedupe(sorted(vector_hits, key=lambda h: (h.distance, h.fact_version_id)))
    texts = _dedupe(sorted(text_hits, key=lambda h: (-h.score, h.fact_version_id)))
    vector_rank = {hit.fact_version_id: (i, hit) for i, hit in enumerate(vectors, start=1)}
    text_rank = {hit.fact_version_id: (i, hit) for i, hit in enumerate(texts, start=1)}

    ranked: list[RankedCandidate] = []
    for version_id in vector_rank.keys() | text_rank.keys():
        v = vector_rank.get(version_id)
        t = text_rank.get(version_id)
        rrf = (1.0 / (rrf_k + v[0]) if v else 0.0) + (1.0 / (rrf_k + t[0]) if t else 0.0)
        version_trust = trust.get(version_id, 0.0)
        ranked.append(
            RankedCandidate(
                fact_version_id=version_id,
                vector_rank=v[0] if v else None,
                vector_distance=v[1].distance if v else None,
                text_rank=t[0] if t else None,
                text_score=t[1].score if t else None,
                rrf_score=rrf,
                trust=version_trust,
                score=rrf * ((1.0 - trust_weight) + trust_weight * version_trust),
            )
        )
    return sorted(ranked, key=lambda c: (-c.score, c.fact_version_id))


def _dedupe[HitT: (VectorHit, TextHit)](hits: Sequence[HitT]) -> list[HitT]:
    """Keep each version's best hit (the input is already sorted best first)."""
    seen: set[UUID] = set()
    unique: list[HitT] = []
    for hit in hits:
        if hit.fact_version_id not in seen:
            seen.add(hit.fact_version_id)
            unique.append(hit)
    return unique
