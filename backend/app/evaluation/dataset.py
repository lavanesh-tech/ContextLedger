"""Evaluation dataset schema and loader.

A case describes the facts to create (in a fresh, isolated organization), who
asks, when the question is about, and what a correct outcome looks like. The
dataset file carries a version: results always say which version they measured.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.facts import PrivacyScope

T0: Final = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
Category = Literal[
    "temporal",
    "superseded",
    "insufficient_evidence",
    "tenant_isolation",
    "privacy",
    "contradiction",
    "citation",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactSpec(_Strict):
    key: str = Field(min_length=1)
    # "property" in the JSON; renamed here so it does not shadow the builtin @property.
    property_name: str = Field(alias="property")
    value: str | int | float | bool
    hours: float = 0.0  # valid_from = T0 + hours
    entity_type: str = "customer"
    external_id: str = "customer-991"
    source: str = "billing-db"
    authority: int | None = Field(default=None, ge=0, le=100)
    confidence: Decimal = Decimal("1.000")
    privacy_scope: PrivacyScope = PrivacyScope.INTERNAL
    tenant: Literal["self", "other"] = "self"

    @property
    def valid_from(self) -> datetime:
        return T0 + timedelta(hours=self.hours)


class Expectation(_Strict):
    status: Literal["answered", "insufficient_evidence"]
    answer_contains: list[str] = []  # every item must appear (case-insensitive)
    answer_excludes: list[str] = []  # none may appear
    cited_keys: list[str] = []  # facts that must be among the citations
    forbidden_in_context: list[str] = []  # must never be in what the model receives


class EvalCase(_Strict):
    id: str
    category: Category
    question: str
    facts: list[FactSpec]
    expect: Expectation
    valid_at_hours: float | None = None
    caller_role: Literal["ADMIN", "ENGINEER", "VIEWER"] = "ADMIN"

    @property
    def valid_at(self) -> datetime | None:
        return None if self.valid_at_hours is None else T0 + timedelta(hours=self.valid_at_hours)

    @model_validator(mode="after")
    def _keys_are_consistent(self) -> "EvalCase":
        keys = [f.key for f in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.id}: duplicate fact keys")
        unknown = set(self.expect.cited_keys) - set(keys)
        if unknown:
            raise ValueError(f"{self.id}: cited_keys not defined as facts: {sorted(unknown)}")
        return self


class EvalDataset(_Strict):
    name: str
    version: str
    synthetic: bool
    description: str
    cases: list[EvalCase]

    @model_validator(mode="after")
    def _ids_are_unique(self) -> "EvalDataset":
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


def load_dataset(path: Path) -> EvalDataset:
    return EvalDataset.model_validate(json.loads(path.read_text(encoding="utf-8")))
