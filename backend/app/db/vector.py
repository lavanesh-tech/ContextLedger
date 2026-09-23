"""PostgreSQL ``vector`` column type (pgvector) without extra client libraries.

Vectors travel as pgvector's text format (``[0.1,0.2,...]``): asyncpg passes
types it has no binary codec for as text, so no numpy or driver plug-in is
needed. The comparator adds pgvector distance operators to column expressions.
"""

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import Float
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.engine import Dialect
from sqlalchemy.types import UserDefinedType


def to_pgvector_text(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def from_pgvector_text(text: str) -> list[float]:
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise ValueError("not a pgvector text value")
    inner = body[1:-1].strip()
    return [float(part) for part in inner.split(",")] if inner else []


class Vector(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dimensions: int | None = None) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw: Any) -> str:
        return "vector" if self.dimensions is None else f"vector({self.dimensions})"

    def bind_processor(self, dialect: Dialect) -> Callable[[Any], str | None]:
        dimensions = self.dimensions

        def process(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            values = list(value)
            if dimensions is not None and len(values) != dimensions:
                raise ValueError(f"expected {dimensions} dimensions, got {len(values)}")
            return to_pgvector_text(values)

        return process

    def result_processor(self, dialect: Dialect, coltype: object) -> Callable[[Any], Any]:
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            if isinstance(value, str):
                return from_pgvector_text(value)
            return [float(v) for v in value]

        return process

    class comparator_factory(UserDefinedType.Comparator[list[float]]):  # noqa: N801
        def _distance(self, operator: str, other: Any) -> Any:
            return self.op(operator, return_type=Float)(other)

        def cosine_distance(self, other: Any) -> Any:
            return self._distance("<=>", other)

        def l2_distance(self, other: Any) -> Any:
            return self._distance("<->", other)


# Let SQLAlchemy (and therefore Alembic's drift check) reflect "vector(N)" columns.
ischema_names["vector"] = Vector
