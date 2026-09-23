from decimal import Decimal
from uuid import UUID

import pytest

from app.domain.errors import ValidationFailedError
from app.domain.facts import PrivacyScope
from app.domain.retrieval import (
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    TextHit,
    VectorHit,
    candidate_pool_size,
    fuse,
    normalize_query,
    trust_score,
    validate_limit,
    visible_privacy_scopes,
)
from app.domain.roles import MembershipRole

A = UUID(int=1)
B = UUID(int=2)
C = UUID(int=3)


# --- query and limits --------------------------------------------------------------


def test_query_whitespace_is_collapsed() -> None:
    assert normalize_query("  credit \n\t limit  ") == "credit limit"


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_empty_query_is_rejected(query: str) -> None:
    with pytest.raises(ValidationFailedError, match="empty"):
        normalize_query(query)


def test_overlong_query_is_rejected() -> None:
    normalize_query("x" * MAX_QUERY_CHARS)
    with pytest.raises(ValidationFailedError, match="at most"):
        normalize_query("x" * (MAX_QUERY_CHARS + 1))


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
def test_limit_bounds(limit: int) -> None:
    with pytest.raises(ValidationFailedError, match="limit"):
        validate_limit(limit)


@pytest.mark.parametrize(("limit", "pool"), [(1, 40), (10, 40), (20, 80), (50, 200)])
def test_candidate_pool_grows_with_limit_and_is_capped(limit: int, pool: int) -> None:
    assert candidate_pool_size(limit) == pool


# --- privacy -----------------------------------------------------------------------


def test_roles_have_increasing_privacy_ceilings() -> None:
    viewer = visible_privacy_scopes(MembershipRole.VIEWER)
    engineer = visible_privacy_scopes(MembershipRole.ENGINEER)
    admin = visible_privacy_scopes(MembershipRole.ADMIN)

    assert viewer == {PrivacyScope.PUBLIC, PrivacyScope.INTERNAL}
    assert engineer == viewer | {PrivacyScope.CONFIDENTIAL}
    assert admin == set(PrivacyScope)


def test_caller_can_narrow_but_never_widen() -> None:
    narrowed = visible_privacy_scopes(MembershipRole.ADMIN, PrivacyScope.PUBLIC)
    not_widened = visible_privacy_scopes(MembershipRole.VIEWER, PrivacyScope.RESTRICTED)

    assert narrowed == {PrivacyScope.PUBLIC}
    assert not_widened == {PrivacyScope.PUBLIC, PrivacyScope.INTERNAL}


# --- ranking -----------------------------------------------------------------------


def test_trust_combines_authority_and_confidence() -> None:
    assert trust_score(100, Decimal("1.000")) == 1.0
    assert trust_score(50, Decimal("0.500")) == pytest.approx(0.25)
    assert trust_score(0, 1) == 0.0


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    ranked = fuse(
        [VectorHit(A, 0.10), VectorHit(B, 0.20)],
        [TextHit(B, 0.9), TextHit(C, 0.8)],
        {A: 1.0, B: 1.0, C: 1.0},
        trust_weight=0.0,
    )

    assert [c.fact_version_id for c in ranked] == [B, A, C]
    both = ranked[0]
    assert (both.vector_rank, both.text_rank) == (2, 1)
    assert both.rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert ranked[1].text_rank is None
    assert ranked[2].vector_rank is None
    assert ranked[2].vector_distance is None


def test_branch_ranks_come_from_scores_not_input_order() -> None:
    ranked = fuse([VectorHit(A, 0.9), VectorHit(B, 0.1)], [], {}, trust_weight=0.0)

    assert [(c.fact_version_id, c.vector_rank) for c in ranked] == [(B, 1), (A, 2)]


def test_trust_weight_can_reorder_close_candidates() -> None:
    hits = [TextHit(A, 0.5), TextHit(B, 0.4)]
    trust = {A: 0.1, B: 1.0}

    relevance_only = fuse([], hits, trust, trust_weight=0.0)
    trust_heavy = fuse([], hits, trust, trust_weight=1.0)

    assert [c.fact_version_id for c in relevance_only] == [A, B]
    assert [c.fact_version_id for c in trust_heavy] == [B, A]
    assert trust_heavy[1].score == pytest.approx(0.1 / 61)


def test_ties_are_broken_by_id_deterministically() -> None:
    first = fuse([], [TextHit(C, 0.5), TextHit(A, 0.5), TextHit(B, 0.5)], {})
    second = fuse([], [TextHit(B, 0.5), TextHit(C, 0.5), TextHit(A, 0.5)], {})

    assert first == second
    assert [c.fact_version_id for c in first] == [A, B, C]


def test_duplicate_hits_keep_the_best() -> None:
    ranked = fuse([VectorHit(A, 0.5), VectorHit(A, 0.1)], [], {A: 1.0})

    assert len(ranked) == 1
    assert ranked[0].vector_distance == 0.1


def test_empty_input_gives_empty_ranking() -> None:
    assert fuse([], [], {}) == []


@pytest.mark.parametrize("weight", [-0.1, 1.1])
def test_invalid_trust_weight(weight: float) -> None:
    with pytest.raises(ValidationFailedError, match="trust_weight"):
        fuse([], [], {}, trust_weight=weight)


def test_invalid_rrf_k() -> None:
    with pytest.raises(ValidationFailedError, match="rrf_k"):
        fuse([], [], {}, rrf_k=0)
