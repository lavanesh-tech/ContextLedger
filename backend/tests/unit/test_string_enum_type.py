import pytest
from sqlalchemy.engine.default import DefaultDialect

from app.db.types import StringEnum
from app.domain.roles import MembershipRole

DIALECT = DefaultDialect()


def test_binds_enum_members_and_valid_strings_as_their_value() -> None:
    column_type = StringEnum(MembershipRole, length=16)

    assert column_type.process_bind_param(MembershipRole.ADMIN, DIALECT) == "ADMIN"
    assert column_type.process_bind_param("VIEWER", DIALECT) == "VIEWER"
    assert column_type.process_bind_param(None, DIALECT) is None


def test_rejects_unknown_values_before_they_reach_the_database() -> None:
    with pytest.raises(ValueError, match="OWNER"):
        StringEnum(MembershipRole, length=16).process_bind_param("OWNER", DIALECT)


def test_reads_values_back_as_enum_members() -> None:
    value = StringEnum(MembershipRole, length=16).process_result_value("ENGINEER", DIALECT)

    assert value is MembershipRole.ENGINEER


def test_is_stored_as_varchar() -> None:
    assert str(StringEnum(MembershipRole, length=16).compile(dialect=DIALECT)) == "VARCHAR(16)"
