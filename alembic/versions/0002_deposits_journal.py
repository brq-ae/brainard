"""Phase 2: deposits & journal -- projects, deposits, events, handoffs

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=255), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "deposits",
        sa.Column("deposit_id", sa.String(length=26), primary_key=True),
        sa.Column("machine_id", sa.String(length=26), sa.ForeignKey("machines.id"), nullable=False),
        sa.Column("tool", sa.String(length=255), nullable=False),
        sa.Column("session", sa.String(length=255), nullable=False),
        sa.Column("project", sa.String(length=255), sa.ForeignKey("projects.name"), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("client_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("doctrine_version", sa.String(length=64), nullable=True),
        sa.Column("metrics", JSONB, nullable=True),
        sa.Column("no_handoff", sa.Text, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        # Whether this deposit was the one that auto-created its project stub.
        # Stored directly (rather than re-derived) so idempotent replay can
        # return the original acknowledgment verbatim.
        sa.Column("stub_created", sa.Boolean, nullable=False),
    )
    op.create_index("ix_deposits_machine_id", "deposits", ["machine_id"])
    op.create_index("ix_deposits_project", "deposits", ["project"])

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("deposit_id", sa.String(length=26), sa.ForeignKey("deposits.deposit_id"), nullable=False),
        # Denormalized from the parent deposit so project-scoped journal
        # queries don't need a join through `deposits` -- implementer's call,
        # noted in the phase 2 brief as an open choice.
        sa.Column("project", sa.String(length=255), sa.ForeignKey("projects.name"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("tags", ARRAY(sa.String), nullable=False, server_default="{}"),
    )
    op.create_index("ix_events_deposit_id", "events", ["deposit_id"])
    op.create_index("ix_events_project", "events", ["project"])

    op.create_table(
        "handoffs",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("deposit_id", sa.String(length=26), sa.ForeignKey("deposits.deposit_id"), nullable=False),
        sa.Column("project", sa.String(length=255), sa.ForeignKey("projects.name"), nullable=False),
        sa.Column("stands", sa.Text, nullable=False),
        sa.Column("in_flight", sa.Text, nullable=False),
        sa.Column("blocked", sa.Text, nullable=False),
        sa.Column("next_steps", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_handoffs_deposit_id", "handoffs", ["deposit_id"], unique=True)
    op.create_index("ix_handoffs_project", "handoffs", ["project"])


def downgrade() -> None:
    op.drop_index("ix_handoffs_project", table_name="handoffs")
    op.drop_index("ix_handoffs_deposit_id", table_name="handoffs")
    op.drop_table("handoffs")

    op.drop_index("ix_events_project", table_name="events")
    op.drop_index("ix_events_deposit_id", table_name="events")
    op.drop_table("events")

    op.drop_index("ix_deposits_project", table_name="deposits")
    op.drop_index("ix_deposits_machine_id", table_name="deposits")
    op.drop_table("deposits")

    op.drop_table("projects")
