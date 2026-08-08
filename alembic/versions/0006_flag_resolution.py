"""Phase 8 (librarian support): resolved_at/resolved_by on flags

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The librarian's inbox gains a close-out marker: both null means
    # unresolved (the default GET /v1/flags filter). Set together, once,
    # server-side by POST /v1/flags/{id}/resolve -- never re-cleared.
    op.add_column("flags", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "flags",
        sa.Column("resolved_by", sa.String(length=26), sa.ForeignKey("machines.id"), nullable=True),
    )
    op.create_index("ix_flags_resolved_at", "flags", ["resolved_at"])
    op.create_index("ix_flags_resolved_by", "flags", ["resolved_by"])


def downgrade() -> None:
    op.drop_index("ix_flags_resolved_by", table_name="flags")
    op.drop_index("ix_flags_resolved_at", table_name="flags")
    op.drop_column("flags", "resolved_by")
    op.drop_column("flags", "resolved_at")
