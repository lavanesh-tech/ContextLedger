"""Helpers for reading the Alembic migration history at runtime."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def expected_schema_revision(alembic_ini: Path) -> str | None:
    """Return the head revision this build of the code expects, or None.

    None means the migration files are not available (e.g. a stripped image).
    Raises if the history has more than one head: that is a merge mistake
    that must be fixed before deploying.
    """
    if not alembic_ini.is_file():
        return None
    return ScriptDirectory.from_config(Config(str(alembic_ini))).get_current_head()


# Objects Alembic's autogenerate cannot represent in SQLAlchemy metadata, so
# they are defined only in migrations (raw SQL) and excluded from drift checks.
# "ex_" = PostgreSQL EXCLUDE constraints (and the GiST index backing them).
UNMANAGED_NAME_PREFIXES: tuple[str, ...] = ("ex_",)


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """Alembic ``include_name`` hook used by env.py and the drift test."""
    return not (name and name.startswith(UNMANAGED_NAME_PREFIXES))
