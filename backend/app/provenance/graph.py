"""Neo4j access: connection, schema, projection writes and provenance traversals.

Every read query is anchored on a node matched by ``id`` *and* ``org`` and only
follows edges to nodes of the same ``org``. Tenant isolation does not depend on
ids being unguessable.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction
from neo4j.time import DateTime as Neo4jDateTime

from app.core.config import Settings
from app.provenance.projection import CYPHER, SCHEMA_STATEMENTS, TABLE_ORDER


def build_driver(settings: Settings) -> AsyncDriver:
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )


async def ensure_schema(driver: AsyncDriver, database: str) -> None:
    """Uniqueness constraints and tenant indexes (idempotent)."""
    async with driver.session(database=database) as session:
        for statement in SCHEMA_STATEMENTS:
            await session.run(statement)


class GraphWriter:
    def __init__(self, driver: AsyncDriver, database: str) -> None:
        self._driver = driver
        self._database = database

    async def apply(self, rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]]) -> int:
        """Write every table's rows in ONE Neo4j transaction. Returns rows written."""

        async def work(tx: AsyncManagedTransaction) -> int:
            written = 0
            for table in TABLE_ORDER:
                rows = rows_by_table.get(table)
                if rows:
                    result = await tx.run(CYPHER[table], rows=list(rows))
                    await result.consume()
                    written += len(rows)
            return written

        async with self._driver.session(database=self._database) as session:
            return await session.execute_write(work)


# --- reads ----------------------------------------------------------------------------


def _as_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, Neo4jDateTime):
        return value.to_native()
    if isinstance(value, datetime):
        return value
    raise TypeError(f"unexpected temporal value {value!r}")


@dataclass(frozen=True, slots=True)
class DecisionRef:
    decision_id: UUID
    action: str
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class ImpactRow:
    """One (affected fact version, how it is affected, dependent decision) triple."""

    fact_version_id: UUID
    via: str
    decision: DecisionRef | None


IMPACT_OF_VERSION = """
MATCH (v:FactVersion {id: $id, org: $org})
CALL (v) {
    MATCH (d:Decision {org: $org})-[:RELIED_ON]->(v)
    RETURN d, 'relied_on' AS via
    UNION
    MATCH (d:Decision {org: $org})-[:BASED_ON]->(:Snapshot {org: $org})-[:INCLUDED]->(v)
    WHERE NOT (d)-[:RELIED_ON]->(v)
    RETURN d, 'in_context' AS via
}
RETURN v.id AS version_id, via, d.id AS decision_id, d.action AS action,
       d.decided_at AS decided_at
"""

SUPERSEDED_BY = """
MATCH (v:FactVersion {id: $id, org: $org})
OPTIONAL MATCH (later:FactVersion {org: $org})-[:SUPERSEDES*1..]->(v)
RETURN v.id AS version_id, collect(later.id) AS later_ids
"""

IMPACT_OF_SOURCE = """
MATCH (s:Source {id: $id, org: $org})
CALL (s) {
    MATCH (v:FactVersion {org: $org})-[:ASSERTED_BY]->(s)
    RETURN v, 'asserted_by_source' AS via
    UNION
    MATCH (s)<-[:FROM_SOURCE]-(:Evidence {org: $org})-[l:EVIDENCE_FOR]->(v:FactVersion {org: $org})
    WHERE l.relation = 'SUPPORTS'
    RETURN v, 'supported_by_source_evidence' AS via
}
OPTIONAL MATCH (d:Decision {org: $org})-[:RELIED_ON]->(v)
RETURN v.id AS version_id, via, d.id AS decision_id, d.action AS action,
       d.decided_at AS decided_at
"""

IMPACT_OF_EVIDENCE = """
MATCH (x:Evidence {id: $id, org: $org})-[l:EVIDENCE_FOR]->(v:FactVersion {org: $org})
OPTIONAL MATCH (d:Decision {org: $org})-[:RELIED_ON]->(v)
RETURN v.id AS version_id, toLower(l.relation) AS via, d.id AS decision_id,
       d.action AS action, d.decided_at AS decided_at
"""

DECISION_LINEAGE = """
MATCH (d:Decision {id: $id, org: $org})-[:RELIED_ON]->(v:FactVersion {org: $org})
MATCH (v)-[:ASSERTED_BY]->(s:Source {org: $org})
MATCH (v)-[:VERSION_OF]->(f:Fact {org: $org})-[:OF_ENTITY]->(e:Entity {org: $org})
OPTIONAL MATCH (x:Evidence {org: $org})-[l:EVIDENCE_FOR]->(v)
RETURN v.id AS version_id, v.privacy_scope AS privacy_scope, f.property AS property,
       e.entity_type AS entity_type, e.external_id AS external_id, s.id AS source_id,
       s.name AS source_name,
       collect(CASE WHEN x IS NULL THEN NULL ELSE
           {id: x.id, relation: l.relation, source_id: [(x)-[:FROM_SOURCE]->(xs) | xs.id][0]}
       END) AS evidence
ORDER BY version_id
"""

EXISTS = {
    "FactVersion": "MATCH (n:FactVersion {id: $id, org: $org}) RETURN count(n) AS n",
    "Source": "MATCH (n:Source {id: $id, org: $org}) RETURN count(n) AS n",
    "Evidence": "MATCH (n:Evidence {id: $id, org: $org}) RETURN count(n) AS n",
    "Decision": "MATCH (n:Decision {id: $id, org: $org}) RETURN count(n) AS n",
}


class GraphReader:
    def __init__(self, driver: AsyncDriver, database: str) -> None:
        self._driver = driver
        self._database = database

    async def _records(self, query: str, **params: Any) -> list[dict[str, Any]]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, **params)
            return [record.data() for record in await result.fetch(100_000)]

    async def exists(self, label: str, *, org: UUID, node_id: UUID) -> bool:
        records = await self._records(EXISTS[label], id=str(node_id), org=str(org))
        return bool(records and records[0]["n"])

    async def _impact(self, query: str, *, org: UUID, node_id: UUID) -> list[ImpactRow]:
        records = await self._records(query, id=str(node_id), org=str(org))
        return [
            ImpactRow(
                fact_version_id=UUID(r["version_id"]),
                via=r["via"],
                decision=None
                if r["decision_id"] is None
                else DecisionRef(
                    decision_id=UUID(r["decision_id"]),
                    action=r["action"],
                    decided_at=_as_datetime(r["decided_at"]),
                ),
            )
            for r in records
        ]

    async def impact_of_version(self, *, org: UUID, version_id: UUID) -> list[ImpactRow]:
        return await self._impact(IMPACT_OF_VERSION, org=org, node_id=version_id)

    async def superseded_by(self, *, org: UUID, version_id: UUID) -> list[UUID]:
        records = await self._records(SUPERSEDED_BY, id=str(version_id), org=str(org))
        return sorted(UUID(x) for x in records[0]["later_ids"]) if records else []

    async def impact_of_source(self, *, org: UUID, source_id: UUID) -> list[ImpactRow]:
        return await self._impact(IMPACT_OF_SOURCE, org=org, node_id=source_id)

    async def impact_of_evidence(self, *, org: UUID, evidence_id: UUID) -> list[ImpactRow]:
        return await self._impact(IMPACT_OF_EVIDENCE, org=org, node_id=evidence_id)

    async def decision_lineage(self, *, org: UUID, decision_id: UUID) -> list[dict[str, Any]]:
        return await self._records(DECISION_LINEAGE, id=str(decision_id), org=str(org))
