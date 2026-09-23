"""Turn service results (dataclasses, UUIDs, datetimes, Decimals, enums) into JSON."""

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetimes are never returned by ContextLedger")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return str(value)  # exact: 0.900 stays "0.900", not 0.9000000001
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [to_jsonable(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, set | frozenset) else items
    raise TypeError(f"cannot serialise {type(value).__name__}")
