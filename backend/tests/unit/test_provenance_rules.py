import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.provenance.graph import DecisionRef, ImpactRow
from app.provenance.projection import CYPHER, LABELS, SCHEMA_STATEMENTS, TABLE_ORDER, to_graph_row
from app.repositories.graph_outbox import PROJECTED
from app.services.provenance import group_impact

V1, V2 = UUID(int=1), UUID(int=2)
D1, D2 = UUID(int=10), UUID(int=20)
EARLY = datetime(2026, 1, 1, tzinfo=UTC)
LATE = datetime(2026, 2, 1, tzinfo=UTC)
MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0008_graph_outbox.py"


def test_every_projected_table_has_a_statement_and_an_order() -> None:
    assert set(CYPHER) == set(TABLE_ORDER) == set(PROJECTED)
    assert len(TABLE_ORDER) == len(set(TABLE_ORDER))


def test_outbox_triggers_cover_exactly_the_projected_tables() -> None:
    spec = importlib.util.spec_from_file_location("migration_0008", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert {table: columns for table, (_, columns) in PROJECTED.items()} == dict(
        module.PROJECTED_TABLES
    )


def test_schema_has_a_unique_id_and_tenant_index_per_label() -> None:
    assert len(SCHEMA_STATEMENTS) == 2 * len(LABELS)
    assert all("IF NOT EXISTS" in statement for statement in SCHEMA_STATEMENTS)


def test_every_statement_is_an_idempotent_merge() -> None:
    for table, cypher in CYPHER.items():
        assert "CREATE (" not in cypher, table
        assert cypher.strip().startswith("UNWIND $rows AS r"), table


def test_unknown_rows_are_rejected() -> None:
    with pytest.raises(TypeError, match="no graph projection"):
        to_graph_row(object())


def test_impact_rows_group_by_decision_deterministically() -> None:
    first = DecisionRef(D1, "credit.approve_increase", EARLY)
    second = DecisionRef(D2, "credit.defer_review", LATE)
    rows = [
        ImpactRow(V2, "asserted_by_source", second),
        ImpactRow(V1, "supported_by_source_evidence", first),
        ImpactRow(V1, "asserted_by_source", first),
        ImpactRow(V2, "asserted_by_source", None),
    ]

    versions, decisions = group_impact(rows)
    shuffled = group_impact(list(reversed(rows)))

    assert versions == (V1, V2)
    assert [d.decision_id for d in decisions] == [D1, D2]  # oldest first
    assert decisions[0].because_of == (
        (V1, "asserted_by_source"),
        (V1, "supported_by_source_evidence"),
    )
    assert (versions, decisions) == shuffled


def test_no_rows_means_no_impact() -> None:
    assert group_impact([]) == ((), ())
