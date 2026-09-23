"""Structural guard: every table is either explicitly global or tenant-owned.

When a later phase adds a table (facts, decisions, ...) without an
organization_id foreign key, this test fails until the table is either made
tenant-owned or deliberately added to GLOBAL_TABLES.
"""

import app.models  # noqa: F401  (registers every model)
from app.db.base import Base
from app.models.mixins import GLOBAL_TABLES


def test_every_table_is_global_or_tenant_owned() -> None:
    for table in Base.metadata.sorted_tables:
        if table.name in GLOBAL_TABLES:
            continue
        column = table.columns.get("organization_id")
        assert column is not None, f"{table.name} has no organization_id"
        assert column.nullable is False, f"{table.name}.organization_id must be NOT NULL"
        targets = {fk.target_fullname for fk in column.foreign_keys}
        assert targets == {"organizations.id"}, (
            f"{table.name}.organization_id must reference organizations"
        )


def test_global_tables_exist() -> None:
    assert set(Base.metadata.tables) >= GLOBAL_TABLES


def test_tenant_foreign_keys_restrict_deletion() -> None:
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            assert fk.ondelete == "RESTRICT", (
                f"{table.name}.{fk.parent.name} must be ON DELETE RESTRICT"
            )
