"""How PostgreSQL rows become nodes and relationships in the provenance graph.

Graph model (every node carries ``org``, its tenant; ids are PostgreSQL UUIDs)::

    (Fact)-[:OF_ENTITY]->(Entity)
    (FactVersion)-[:VERSION_OF]->(Fact)
    (FactVersion)-[:ASSERTED_BY]->(Source)
    (FactVersion)-[:SUPERSEDES]->(FactVersion)
    (Evidence)-[:FROM_SOURCE]->(Source)
    (Evidence)-[:EVIDENCE_FOR {relation, linked_at}]->(FactVersion)
    (Snapshot)-[:INCLUDED {position}]->(FactVersion)
    (Decision)-[:BASED_ON]->(Snapshot)
    (Decision)-[:RELIED_ON]->(FactVersion)

The graph holds identities, times, scopes and hashes, never fact values or
evidence excerpts: those stay in PostgreSQL, behind its tenant and privacy rules.

Every statement is an idempotent ``MERGE`` keyed by id. A relationship whose far
end has not been projected yet creates a placeholder node with only ``id`` and
``org``, which the far end's own projection later fills in. So events can be
applied in any order, and applying one twice changes nothing.
"""

from collections.abc import Mapping
from typing import Any, Final

from app.domain.decisions import value_sha256
from app.models.decision import ContextSnapshot, ContextSnapshotFact, Decision, DecisionFact
from app.models.entity import Entity
from app.models.evidence import Evidence, FactVersionEvidence
from app.models.fact import Fact, FactVersion
from app.models.source import FactSource

LABELS: Final = ("Entity", "Fact", "FactVersion", "Source", "Evidence", "Snapshot", "Decision")

SCHEMA_STATEMENTS: Final = tuple(
    f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
    for label in LABELS
) + tuple(
    f"CREATE INDEX {label.lower()}_org IF NOT EXISTS FOR (n:{label}) ON (n.org)" for label in LABELS
)

CYPHER: Final[Mapping[str, str]] = {
    "entities": """
        UNWIND $rows AS r
        MERGE (e:Entity {id: r.id})
        SET e.org = r.org, e.entity_type = r.entity_type, e.external_id = r.external_id
    """,
    "facts": """
        UNWIND $rows AS r
        MERGE (f:Fact {id: r.id})
        SET f.org = r.org, f.property = r.property
        MERGE (e:Entity {id: r.entity_id}) ON CREATE SET e.org = r.org
        MERGE (f)-[:OF_ENTITY]->(e)
    """,
    "fact_sources": """
        UNWIND $rows AS r
        MERGE (s:Source {id: r.id})
        SET s.org = r.org, s.name = r.name, s.source_type = r.source_type,
            s.default_authority = r.default_authority
    """,
    "fact_versions": """
        UNWIND $rows AS r
        MERGE (v:FactVersion {id: r.id})
        SET v.org = r.org, v.version = r.version, v.value_sha256 = r.value_sha256,
            v.valid_from = r.valid_from, v.valid_until = r.valid_until,
            v.recorded_at = r.recorded_at, v.valid_until_recorded_at = r.valid_until_recorded_at,
            v.authority = r.authority, v.confidence = r.confidence,
            v.privacy_scope = r.privacy_scope
        MERGE (f:Fact {id: r.fact_id}) ON CREATE SET f.org = r.org
        MERGE (v)-[:VERSION_OF]->(f)
        MERGE (s:Source {id: r.source_id}) ON CREATE SET s.org = r.org
        MERGE (v)-[:ASSERTED_BY]->(s)
        FOREACH (previous_id IN
                 CASE WHEN r.supersedes_id IS NULL THEN [] ELSE [r.supersedes_id] END |
            MERGE (p:FactVersion {id: previous_id}) ON CREATE SET p.org = r.org
            MERGE (v)-[:SUPERSEDES]->(p)
        )
    """,
    "evidence": """
        UNWIND $rows AS r
        MERGE (x:Evidence {id: r.id})
        SET x.org = r.org, x.evidence_type = r.evidence_type, x.content_sha256 = r.content_sha256,
            x.uri = r.uri, x.privacy_scope = r.privacy_scope, x.captured_at = r.captured_at
        MERGE (s:Source {id: r.source_id}) ON CREATE SET s.org = r.org
        MERGE (x)-[:FROM_SOURCE]->(s)
    """,
    "fact_version_evidence": """
        UNWIND $rows AS r
        MERGE (x:Evidence {id: r.evidence_id}) ON CREATE SET x.org = r.org
        MERGE (v:FactVersion {id: r.fact_version_id}) ON CREATE SET v.org = r.org
        MERGE (x)-[l:EVIDENCE_FOR]->(v)
        SET l.relation = r.relation, l.linked_at = r.linked_at
    """,
    "context_snapshots": """
        UNWIND $rows AS r
        MERGE (n:Snapshot {id: r.id})
        SET n.org = r.org, n.valid_at = r.valid_at, n.known_at = r.known_at,
            n.captured_by_user_id = r.captured_by_user_id
    """,
    "context_snapshot_facts": """
        UNWIND $rows AS r
        MERGE (n:Snapshot {id: r.snapshot_id}) ON CREATE SET n.org = r.org
        MERGE (v:FactVersion {id: r.fact_version_id}) ON CREATE SET v.org = r.org
        MERGE (n)-[i:INCLUDED]->(v)
        SET i.position = r.position
    """,
    "decisions": """
        UNWIND $rows AS r
        MERGE (d:Decision {id: r.id})
        SET d.org = r.org, d.action = r.action, d.agent = r.agent, d.decided_at = r.decided_at,
            d.decided_by_user_id = r.decided_by_user_id, d.receipt_sha256 = r.receipt_sha256
        MERGE (n:Snapshot {id: r.snapshot_id}) ON CREATE SET n.org = r.org
        MERGE (d)-[:BASED_ON]->(n)
    """,
    "decision_facts": """
        UNWIND $rows AS r
        MERGE (d:Decision {id: r.decision_id}) ON CREATE SET d.org = r.org
        MERGE (v:FactVersion {id: r.fact_version_id}) ON CREATE SET v.org = r.org
        MERGE (d)-[:RELIED_ON]->(v)
    """,
}

# Nodes before relationships keeps placeholder creation rare (not required).
TABLE_ORDER: Final = (
    "entities",
    "fact_sources",
    "facts",
    "evidence",
    "fact_versions",
    "fact_version_evidence",
    "context_snapshots",
    "context_snapshot_facts",
    "decisions",
    "decision_facts",
)


def to_graph_row(row: object) -> dict[str, Any]:
    """Convert one ORM row to the parameters its Cypher statement expects."""
    match row:
        case Entity():
            return {
                "id": str(row.id),
                "org": str(row.organization_id),
                "entity_type": row.entity_type,
                "external_id": row.external_id,
            }
        case Fact():
            return {
                "id": str(row.id),
                "org": str(row.organization_id),
                "entity_id": str(row.entity_id),
                "property": row.property,
            }
        case FactSource():
            return {
                "id": str(row.id),
                "org": str(row.organization_id),
                "name": row.name,
                "source_type": str(row.source_type),
                "default_authority": row.default_authority,
            }
        case FactVersion():
            return {
                "id": str(row.id),
                "org": str(row.organization_id),
                "fact_id": str(row.fact_id),
                "source_id": str(row.source_id),
                "supersedes_id": None if row.supersedes_id is None else str(row.supersedes_id),
                "version": row.version,
                "value_sha256": value_sha256(row.value),
                "valid_from": row.valid_from,
                "valid_until": row.valid_until,
                "recorded_at": row.recorded_at,
                "valid_until_recorded_at": row.valid_until_recorded_at,
                "authority": row.authority,
                "confidence": float(row.confidence),
                "privacy_scope": str(row.privacy_scope),
            }
        case Evidence():
            return {
                "id": str(row.id),
                "org": str(row.organization_id),
                "source_id": str(row.source_id),
                "evidence_type": str(row.evidence_type),
                "content_sha256": row.content_sha256,
                "uri": row.uri,
                "privacy_scope": str(row.privacy_scope),
                "captured_at": row.captured_at,
            }
        case FactVersionEvidence():
            return {
                "org": str(row.organization_id),
                "fact_version_id": str(row.fact_version_id),
                "evidence_id": str(row.evidence_id),
                "relation": str(row.relation),
                "linked_at": row.linked_at,
            }
        case ContextSnapshot():
            return {
                "id": str(row.id),
                "org": str(row.organization_id),
                "valid_at": row.valid_at,
                "known_at": row.known_at,
                "captured_by_user_id": str(row.captured_by_user_id),
            }
        case ContextSnapshotFact():
            return {
                "org": str(row.organization_id),
                "snapshot_id": str(row.snapshot_id),
                "fact_version_id": str(row.fact_version_id),
                "position": row.position,
            }
        case Decision():
            return {
                "id": str(row.id),
                "org": str(row.organization_id),
                "snapshot_id": str(row.snapshot_id),
                "action": row.action,
                "agent": row.agent,
                "decided_at": row.decided_at,
                "decided_by_user_id": str(row.decided_by_user_id),
                "receipt_sha256": row.receipt_sha256,
            }
        case DecisionFact():
            return {
                "org": str(row.organization_id),
                "decision_id": str(row.decision_id),
                "fact_version_id": str(row.fact_version_id),
            }
    raise TypeError(f"no graph projection for {type(row).__name__}")
