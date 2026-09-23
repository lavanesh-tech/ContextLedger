"""Static checks on the migration history (no database needed)."""

import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.base import Base
from app.db.migrations import expected_schema_revision

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture(scope="module")
def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def test_history_has_exactly_one_head(script: ScriptDirectory) -> None:
    assert len(script.get_heads()) == 1


def test_revision_ids_are_sequential_and_linear(script: ScriptDirectory) -> None:
    revisions = list(reversed(list(script.walk_revisions())))  # oldest first

    for index, revision in enumerate(revisions, start=1):
        assert re.fullmatch(r"\d{4}", revision.revision)
        assert revision.revision == f"{index:04d}"
        expected_parent = None if index == 1 else f"{index - 1:04d}"
        assert revision.down_revision == expected_parent


def test_every_migration_can_be_downgraded(script: ScriptDirectory) -> None:
    for revision in script.walk_revisions():
        assert callable(getattr(revision.module, "downgrade", None)), revision.revision


def test_expected_revision_is_the_head(script: ScriptDirectory) -> None:
    assert expected_schema_revision(ALEMBIC_INI) == script.get_current_head()


def test_expected_revision_is_none_without_migration_files(tmp_path: Path) -> None:
    assert expected_schema_revision(tmp_path / "missing.ini") is None


def test_constraint_naming_convention_is_configured() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert "fk" in Base.metadata.naming_convention
