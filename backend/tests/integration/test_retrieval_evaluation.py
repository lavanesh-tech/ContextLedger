"""The retrieval evaluation against real PostgreSQL, on a few committed cases."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.evaluation.retrieval_eval import DATASET, evaluate_case, load_retrieval_dataset

pytestmark = pytest.mark.integration


async def test_selected_cases_behave_as_labelled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cases = {c.id: c for c in load_retrieval_dataset(DATASET).cases}

    lexical = await evaluate_case(sessions, cases["lexical-01"])
    revoked = await evaluate_case(sessions, cases["revoked-02"])
    tenant = await evaluate_case(sessions, cases["tenant-01"])
    privacy = await evaluate_case(sessions, cases["privacy-01"])

    assert lexical["hybrid"].ranked_keys[0] == "customer-991_credit_limit"
    assert lexical["hybrid"].vector_search == "used"
    assert lexical["text_only"].vector_search == "unavailable"
    for result in (revoked, tenant, privacy):
        for config in result.values():
            assert config.violations == []  # deterministic filters, every configuration
    assert revoked["hybrid"].ranked_keys[0] == "v2"
