"""Built-in librarian run history -- librarian_runs (ADR-0010 phase 2: the
built-in engine). One row per completed run, written once at completion --
see app/models.py's LibrarianRun docstring for why there is no "in
progress" row.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "librarian_runs",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("counts", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text, nullable=True),
        sa.CheckConstraint("status IN ('ok', 'error', 'skipped')", name="ck_librarian_runs_status"),
    )
    op.create_index("ix_librarian_runs_started_at", "librarian_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_librarian_runs_started_at", table_name="librarian_runs")
    op.drop_table("librarian_runs")
