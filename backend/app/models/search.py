"""Full-text search documents for fact versions.

Rows are written by the database itself: an AFTER INSERT trigger on
``fact_versions`` (migration 0006) renders the version's searchable text with
``contextledger_fact_search_text`` and stores its ``tsvector``. The document
therefore exists in the same transaction as the version, for every write path
(ORM, bulk SQL, backfill), and the table is append-only like the versions it indexes.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, Index, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantOwnedMixin

# Text search configuration: English stemming and stop words, so a question like
# "what is the credit limit" matches "credit_limit" without matching "is"/"the".
SEARCH_CONFIG = "english"


class FactSearchDocument(TenantOwnedMixin, Base):
    __tablename__ = "fact_search_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "fact_version_id"],
            ["fact_versions.organization_id", "fact_versions.id"],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_fact_search_documents_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    fact_version_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    search_vector: Mapped[str] = mapped_column(TSVECTOR)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
