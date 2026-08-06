"""Phase 5: projects & handoffs -- mirrored_documents (ADR/doc mirror),
deposits.documents_ack

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept textually identical to `_MIRRORED_DOCUMENT_SEARCH_VECTOR_SQL` in
# app/models.py -- see docs/dev.md for why the two paths (ORM metadata for
# tests, this migration for the real stack) exist.
_MIRRORED_DOCUMENT_SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(content, '')), 'B')"
)


def upgrade() -> None:
    # Deposit acknowledgment detail for documents[] items, stored verbatim
    # for idempotent replay (see Deposit.documents_ack docstring).
    op.add_column("deposits", sa.Column("documents_ack", JSONB, nullable=True))

    op.create_table(
        "mirrored_documents",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("project", sa.String(length=255), sa.ForeignKey("projects.name"), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("deposit_id", sa.String(length=26), sa.ForeignKey("deposits.deposit_id"), nullable=False),
        sa.Column("machine_id", sa.String(length=26), sa.ForeignKey("machines.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mirrored_documents_project", "mirrored_documents", ["project"])
    op.create_index("ix_mirrored_documents_kind", "mirrored_documents", ["kind"])
    op.create_index("ix_mirrored_documents_deposit_id", "mirrored_documents", ["deposit_id"])
    op.create_index("ix_mirrored_documents_machine_id", "mirrored_documents", ["machine_id"])
    op.create_index("ix_mirrored_documents_project_path", "mirrored_documents", ["project", "path"])
    op.create_index(
        "ix_mirrored_documents_project_path_version",
        "mirrored_documents",
        ["project", "path", "version"],
        unique=True,
    )

    op.execute(
        f"""
        ALTER TABLE mirrored_documents
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS ({_MIRRORED_DOCUMENT_SEARCH_VECTOR_SQL}) STORED
        """
    )
    op.create_index(
        "ix_mirrored_documents_search_vector", "mirrored_documents", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_mirrored_documents_search_vector", table_name="mirrored_documents")
    op.execute("ALTER TABLE mirrored_documents DROP COLUMN search_vector")

    op.drop_index("ix_mirrored_documents_project_path_version", table_name="mirrored_documents")
    op.drop_index("ix_mirrored_documents_project_path", table_name="mirrored_documents")
    op.drop_index("ix_mirrored_documents_machine_id", table_name="mirrored_documents")
    op.drop_index("ix_mirrored_documents_deposit_id", table_name="mirrored_documents")
    op.drop_index("ix_mirrored_documents_kind", table_name="mirrored_documents")
    op.drop_index("ix_mirrored_documents_project", table_name="mirrored_documents")
    op.drop_table("mirrored_documents")

    op.drop_column("deposits", "documents_ack")
