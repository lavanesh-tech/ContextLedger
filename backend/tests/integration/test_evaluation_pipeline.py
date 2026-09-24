"""The whole evaluation dataset through the real pipeline (PostgreSQL), with scripted models.

This is the deterministic half of the evaluation, run as a regression test: for
every case, the model must receive the fact versions it needs and never a value
it must not see, and an invented citation must always be withheld.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.evaluation.dataset import load_dataset
from app.evaluation.metrics import pipeline_metrics
from app.evaluation.runner import DATASET, citation_guard, echo_model, evaluate_dataset

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.mark.parametrize("prompt_version", ["grounded-answer-v1", "grounded-answer-v2"])
async def test_every_case_supplies_exactly_what_it_should(
    sessions: Sessions, prompt_version: str
) -> None:
    dataset = load_dataset(DATASET)

    outcomes = await evaluate_dataset(sessions, dataset, echo_model, prompt_version=prompt_version)

    failures = [
        (o.case_id, o.context_leaks, o.context_has_expected)
        for o in outcomes
        if o.context_leaks or not o.context_has_expected
    ]
    assert failures == []
    metrics = pipeline_metrics(outcomes)
    assert metrics["context_correctness"] == 1.0
    assert metrics["forbidden_values_supplied"] == 0
    # Nothing authorized at that time: no model call at all.
    skipped = {o.case_id for o in outcomes if not o.model_called}
    assert {"insufficient-02", "isolation-01", "privacy-01"} <= skipped


async def test_invented_citations_are_always_withheld(sessions: Sessions) -> None:
    rate = await citation_guard(
        sessions, load_dataset(DATASET), prompt_version="grounded-answer-v2"
    )
    assert rate == 1.0
