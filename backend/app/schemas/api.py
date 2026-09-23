"""Request and response bodies of the v1 REST API.

Requests are strict (``extra="forbid"``): an unknown field is a 422, never
silently ignored. Read models that already exist as frozen dataclasses in the
service layer (resolved facts, receipts, impact reports...) are returned as-is
and documented from their type hints. ORM rows are mapped through the
``*Out`` models below, so internal columns never leak by accident.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.evidence import EvidenceRelation, EvidenceType
from app.domain.facts import PrivacyScope, SourceType
from app.domain.retrieval import DEFAULT_TRUST_WEIGHT, MAX_LIMIT
from app.domain.roles import MembershipRole
from app.services.retrieval import RetrievalQuery
from app.temporal.model import ResolvedFact, TimelineEntry


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Response(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- users and organizations ---------------------------------------------------------


class UserCreate(Request):
    email: str = Field(examples=["ada@example.com"])
    display_name: str = Field(examples=["Ada Lovelace"])


class UserOut(Response):
    id: UUID
    email: str
    display_name: str
    is_active: bool
    created_at: datetime


class OrganizationCreate(Request):
    name: str = Field(examples=["Acme Credit"])
    slug: str = Field(examples=["acme-credit"])


class OrganizationOut(Response):
    id: UUID
    name: str
    slug: str
    created_at: datetime


class MemberAdd(Request):
    user_id: UUID
    role: MembershipRole


class MemberRoleChange(Request):
    role: MembershipRole


class MemberOut(Response):
    user_id: UUID
    role: MembershipRole
    created_at: datetime


# --- sources, facts, evidence ------------------------------------------------------------


class SourceCreate(Request):
    name: str = Field(examples=["billing-db"])
    source_type: SourceType
    uri: str | None = Field(default=None, examples=["https://billing.example.com"])
    default_authority: int = Field(default=50, ge=0, le=100)


class SourceOut(Response):
    id: UUID
    name: str
    source_type: SourceType
    uri: str | None
    default_authority: int
    created_at: datetime


class FactVersionCreate(Request):
    entity_type: str = Field(examples=["customer"])
    external_id: str = Field(examples=["customer-991"])
    property: str = Field(examples=["credit_limit"])
    value: Any = Field(examples=[5000])
    source_id: UUID
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None
    observed_at: AwareDatetime | None = None
    authority: int | None = Field(default=None, ge=0, le=100)
    confidence: Decimal = Field(default=Decimal("1.000"), ge=0, le=1)
    privacy_scope: PrivacyScope = PrivacyScope.INTERNAL
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)


class FactVersionOut(Response):
    id: UUID
    fact_id: UUID
    version: int
    value: Any
    source_id: UUID
    valid_from: datetime
    valid_until: datetime | None
    observed_at: datetime
    recorded_at: datetime
    valid_until_recorded_at: datetime | None
    supersedes_id: UUID | None
    authority: int
    confidence: Decimal
    privacy_scope: PrivacyScope


class EntityFacts(BaseModel):
    facts: list[ResolvedFact]
    withheld_by_privacy_scope: int = Field(
        description="Facts that exist but are above your role's privacy ceiling"
    )


class EntityTimeline(BaseModel):
    entries: list[TimelineEntry]
    withheld_by_privacy_scope: int


class EvidenceCreate(Request):
    source_id: UUID
    evidence_type: EvidenceType
    excerpt: str = Field(examples=["Credit limit for customer-991 is 5000 USD."])
    captured_at: AwareDatetime
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    privacy_scope: PrivacyScope = PrivacyScope.INTERNAL


class EvidenceOut(Response):
    id: UUID
    source_id: UUID
    evidence_type: EvidenceType
    excerpt: str
    content_sha256: str
    uri: str | None
    privacy_scope: PrivacyScope
    captured_at: datetime
    recorded_at: datetime


class CapturedEvidenceOut(BaseModel):
    evidence: EvidenceOut
    created: bool = Field(description="False when identical content already existed")


class EvidenceAttach(Request):
    evidence_id: UUID
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS


class AttachResult(BaseModel):
    linked: bool = Field(description="False when the link already existed")


class LinkedEvidenceOut(BaseModel):
    evidence: EvidenceOut
    source: SourceOut
    relation: EvidenceRelation
    linked_at: datetime
    linked_by_user_id: UUID | None


class ProvenanceOut(BaseModel):
    version: FactVersionOut
    source: SourceOut
    evidence: list[LinkedEvidenceOut]
    withheld_evidence: int


# --- search and decisions ----------------------------------------------------------------


class SearchRequest(Request):
    query: str = Field(examples=["What is the credit limit of customer-991?"])
    limit: int = Field(default=10, ge=1, le=MAX_LIMIT)
    valid_at: AwareDatetime | None = None
    known_at: AwareDatetime | None = None
    entity_type: str | None = None
    external_ids: list[str] = Field(default_factory=list, max_length=100)
    properties: list[str] = Field(default_factory=list, max_length=100)
    source_ids: list[UUID] = Field(default_factory=list, max_length=100)
    min_authority: int | None = Field(default=None, ge=0, le=100)
    min_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    max_privacy_scope: PrivacyScope | None = None
    trust_weight: float = Field(default=DEFAULT_TRUST_WEIGHT, ge=0, le=1)

    def to_query(self) -> RetrievalQuery:
        return RetrievalQuery(**self.model_dump())


class DecisionCreate(Request):
    snapshot_id: UUID
    action: str = Field(examples=["credit.approve_increase"])
    outcome: Any = Field(examples=[{"approved": True, "new_limit": 7500}])
    relied_on: list[UUID] = Field(max_length=50)
    rationale: str | None = Field(default=None, max_length=4000)
    agent: str | None = Field(default=None, examples=["credit-review-agent v3"])


class DecisionRefs(BaseModel):
    decision_ids: list[UUID]
