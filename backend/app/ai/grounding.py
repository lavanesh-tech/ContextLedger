"""Deterministic grounding: what the model may see, and checking what it cites.

``pack_facts`` turns authorized retrieval results into labelled facts (F1..Fn)
and renders them for the prompt. Only these facts reach the model.

``check_answer`` validates the model's structured output against that exact
set: an answer that cites a label that was never supplied, or cites nothing,
is withheld as UNGROUNDED. The
model's own ``insufficient_evidence`` flag is respected, never its claim of
being grounded.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from app.ai.providers import MalformedGenerationError
from app.services.retrieval import RetrievedFact

MAX_VALUE_CHARS: Final = 500  # a single fact value longer than this is truncated in the prompt


@dataclass(frozen=True, slots=True)
class PackedFact:
    label: str  # F1, F2, ... (stable within one request only)
    fact_version_id: UUID
    fact_id: UUID
    source_id: UUID
    source_name: str
    source_type: str
    entity_type: str
    external_id: str
    property: str
    value: Any
    valid_from: datetime
    valid_until: datetime | None
    recorded_at: datetime
    authority: int
    confidence: Decimal


def pack_facts(results: list[RetrievedFact]) -> list[PackedFact]:
    return [
        PackedFact(
            label=f"F{i}",
            fact_version_id=r.version.id,
            fact_id=r.version.fact_id,
            source_id=r.version.source_id,
            source_name=r.source_name,
            source_type=str(r.source_type),
            entity_type=r.entity_type,
            external_id=r.external_id,
            property=r.property,
            value=r.version.value,
            valid_from=r.version.valid_from,
            valid_until=r.version.valid_until,
            recorded_at=r.version.recorded_at,
            authority=r.version.authority,
            confidence=r.version.confidence,
        )
        for i, r in enumerate(results, start=1)
    ]


def render_facts(facts: list[PackedFact]) -> str:
    """One line per fact. Values are JSON-encoded so text inside a value cannot
    pose as prompt structure (a value is data, never an instruction)."""
    if not facts:
        return "(no facts)"
    lines = []
    for f in facts:
        value = json.dumps(f.value, ensure_ascii=False, sort_keys=True, default=str)
        if len(value) > MAX_VALUE_CHARS:
            value = value[:MAX_VALUE_CHARS] + "...(truncated)"
        until = f.valid_until.isoformat() if f.valid_until else "open-ended"
        lines.append(
            f"[{f.label}] {f.entity_type}/{f.external_id} {f.property} = {value} "
            f"| valid {f.valid_from.isoformat()} to {until} "
            f"| source {json.dumps(f.source_name)} ({f.source_type}) "
            f"| authority {f.authority}, confidence {f.confidence} "
            f"| recorded {f.recorded_at.isoformat()}"
        )
    return "\n".join(lines)


class AnswerStatus(StrEnum):
    ANSWERED = "answered"  # an answer backed by at least one valid citation
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNGROUNDED = "ungrounded"  # the model answered without valid citations: withheld


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    answer: str
    insufficient_evidence: bool
    cited_facts: list[str]
    inferences: list[str]


@dataclass(frozen=True, slots=True)
class GroundingCheck:
    status: AnswerStatus
    cited: list[PackedFact]  # valid citations, in the model's order, no duplicates
    rejected_labels: list[str]  # labels the model cited that were never supplied


def parse_model_answer(payload: Any) -> ModelAnswer:
    """Validate the structured output shape (never trust it blindly)."""
    try:
        answer = payload["answer"]
        insufficient = payload["insufficient_evidence"]
        cited = payload["cited_facts"]
        inferences = payload.get("inferences", [])
    except (KeyError, TypeError) as exc:
        raise MalformedGenerationError("structured answer is missing required fields") from exc
    if not isinstance(answer, str) or not isinstance(insufficient, bool):
        raise MalformedGenerationError("structured answer has fields of the wrong type")
    if not isinstance(cited, list) or not all(isinstance(c, str) for c in cited):
        raise MalformedGenerationError("cited_facts must be a list of labels")
    if not isinstance(inferences, list) or not all(isinstance(i, str) for i in inferences):
        raise MalformedGenerationError("inferences must be a list of strings")
    return ModelAnswer(answer.strip(), insufficient, [c.strip() for c in cited], inferences)


def check_answer(model: ModelAnswer, supplied: list[PackedFact]) -> GroundingCheck:
    by_label = {f.label: f for f in supplied}
    cited: list[PackedFact] = []
    rejected: list[str] = []
    for label in model.cited_facts:
        fact = by_label.get(label.upper())
        if fact is None:
            rejected.append(label)
        elif fact not in cited:
            cited.append(fact)
    if model.insufficient_evidence:
        # Respect the refusal to answer; any citations it still lists are dropped.
        return GroundingCheck(AnswerStatus.INSUFFICIENT_EVIDENCE, [], rejected)
    if rejected or not cited or not model.answer:
        # An invented label means the answer may rest on something that was never
        # supplied: withhold the whole answer rather than keep "the valid part".
        return GroundingCheck(AnswerStatus.UNGROUNDED, [], rejected)
    return GroundingCheck(AnswerStatus.ANSWERED, cited, rejected)
