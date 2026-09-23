"""Custom column types."""

from enum import StrEnum
from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class StringEnum[EnumT: StrEnum](TypeDecorator[EnumT]):
    """Store a StrEnum as VARCHAR (plus a CHECK constraint on the table).

    Preferred over a native PostgreSQL ENUM type because adding a value to a
    native enum needs special migration handling, while VARCHAR + CHECK is an
    ordinary constraint change.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[EnumT], length: int) -> None:
        super().__init__(length=length)
        self.enum_class = enum_class

    def process_bind_param(self, value: EnumT | str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return self.enum_class(value).value  # raises ValueError for unknown values

    def process_result_value(self, value: Any | None, dialect: Dialect) -> EnumT | None:
        return None if value is None else self.enum_class(value)
