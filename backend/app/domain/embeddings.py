"""Embedding rules (framework-free)."""

import hashlib
import json
from datetime import timedelta
from enum import StrEnum
from typing import Any, Final

# Column width of fact_embeddings.embedding. text-embedding-3-small produces
# 1536 dimensions natively; changing this is a schema migration plus re-embedding.
EMBEDDING_DIMENSIONS: Final = 1536

# Bump when render_fact_document changes, so old embeddings can be recognised.
FACT_TEXT_TEMPLATE: Final = "fact-text-v1"


class EmbeddingJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def render_fact_document(entity_type: str, external_id: str, prop: str, value: Any) -> str:
    """The text that represents one fact version for semantic search.

    Deterministic: the same fact and value always render identically (sorted
    JSON keys), which is what makes content-hash caching of embeddings safe.
    Validity times are deliberately excluded; time is filtered exactly in SQL,
    never approximated through embeddings.
    """
    readable_property = prop.replace("_", " ").replace(".", " / ")
    rendered_value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
    return f"{entity_type} {external_id}\n{readable_property}: {rendered_value}"


def document_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def retry_delay(attempts: int, *, base_seconds: float, cap_seconds: float) -> timedelta:
    """Exponential backoff for failed embedding jobs: base * 2^(attempts-1), capped."""
    if attempts < 1:
        return timedelta(0)
    return timedelta(seconds=min(cap_seconds, base_seconds * 2 ** (attempts - 1)))
