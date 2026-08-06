"""Phase 3: library -- knowledge_entries, flags, FTS search_vector columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept textually identical to the `_*_SEARCH_VECTOR_SQL` constants in
# app/models.py -- that module drives `Base.metadata.create_all` (used by the
# test suite), this migration drives `alembic upgrade head` (used by the real
# stack). See docs/dev.md for why the two paths exist.
_EVENT_SEARCH_VECTOR_SQL = "to_tsvector('english', coalesce(summary, ''))"

_HANDOFF_SEARCH_VECTOR_SQL = (
    "to_tsvector('english', "
    "coalesce(stands, '') || ' ' || coalesce(in_flight, '') || ' ' || "
    "coalesce(blocked, '') || ' ' || coalesce(next_steps, '') || ' ' || coalesce(notes, ''))"
)

_KNOWLEDGE_ENTRY_SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(body, '')), 'B')"
)


def upgrade() -> None:
    # Deposit acknowledgment detail for knowledge[] items, stored verbatim
    # for idempotent replay (see Deposit.knowledge_ack docstring).
    op.add_column("deposits", sa.Column("knowledge_ack", JSONB, nullable=True))

    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("project", sa.String(length=255), sa.ForeignKey("projects.name"), nullable=True),
        sa.Column("tags", ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("retire_reason", sa.Text, nullable=True),
        sa.Column("supersedes", ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("machine_id", sa.String(length=26), sa.ForeignKey("machines.id"), nullable=False),
        sa.Column("tool", sa.String(length=255), nullable=False),
        sa.Column("session", sa.String(length=255), nullable=False),
        sa.Column("deposit_id", sa.String(length=26), sa.ForeignKey("deposits.deposit_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_entries_namespace", "knowledge_entries", ["namespace"])
    op.create_index("ix_knowledge_entries_project", "knowledge_entries", ["project"])
    op.create_index("ix_knowledge_entries_status", "knowledge_entries", ["status"])
    op.create_index("ix_knowledge_entries_machine_id", "knowledge_entries", ["machine_id"])
    op.create_index("ix_knowledge_entries_deposit_id", "knowledge_entries", ["deposit_id"])

    op.create_table(
        "flags",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("entry_id", sa.String(length=26), sa.ForeignKey("knowledge_entries.id"), nullable=False),
        sa.Column("related_entry_id", sa.String(length=26), sa.ForeignKey("knowledge_entries.id"), nullable=True),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flags_type", "flags", ["type"])
    op.create_index("ix_flags_entry_id", "flags", ["entry_id"])
    op.create_index("ix_flags_related_entry_id", "flags", ["related_entry_id"])

    # --- FTS: generated tsvector columns + GIN indexes ---
    # `op.execute` (rather than `op.add_column` with `sa.Computed`) because a
    # generated column must be added as a single ALTER TABLE ... ADD COLUMN
    # ... GENERATED ALWAYS AS (...) STORED statement; alembic's `add_column`
    # helper does not compile the GENERATED clause on all versions, so the
    # DDL is spelled out explicitly for reliability.

    op.execute(
        f"""
        ALTER TABLE knowledge_entries
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS ({_KNOWLEDGE_ENTRY_SEARCH_VECTOR_SQL}) STORED
        """
    )
    op.create_index(
        "ix_knowledge_entries_search_vector", "knowledge_entries", ["search_vector"], postgresql_using="gin"
    )

    op.execute(
        f"""
        ALTER TABLE handoffs
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS ({_HANDOFF_SEARCH_VECTOR_SQL}) STORED
        """
    )
    op.create_index("ix_handoffs_search_vector", "handoffs", ["search_vector"], postgresql_using="gin")

    op.execute(
        f"""
        ALTER TABLE events
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS ({_EVENT_SEARCH_VECTOR_SQL}) STORED
        """
    )
    op.create_index("ix_events_search_vector", "events", ["search_vector"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_events_search_vector", table_name="events")
    op.execute("ALTER TABLE events DROP COLUMN search_vector")

    op.drop_index("ix_handoffs_search_vector", table_name="handoffs")
    op.execute("ALTER TABLE handoffs DROP COLUMN search_vector")

    op.drop_index("ix_knowledge_entries_search_vector", table_name="knowledge_entries")
    op.execute("ALTER TABLE knowledge_entries DROP COLUMN search_vector")

    op.drop_index("ix_flags_related_entry_id", table_name="flags")
    op.drop_index("ix_flags_entry_id", table_name="flags")
    op.drop_index("ix_flags_type", table_name="flags")
    op.drop_table("flags")

    op.drop_index("ix_knowledge_entries_deposit_id", table_name="knowledge_entries")
    op.drop_index("ix_knowledge_entries_machine_id", table_name="knowledge_entries")
    op.drop_index("ix_knowledge_entries_status", table_name="knowledge_entries")
    op.drop_index("ix_knowledge_entries_project", table_name="knowledge_entries")
    op.drop_index("ix_knowledge_entries_namespace", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")

    op.drop_column("deposits", "knowledge_ack")
