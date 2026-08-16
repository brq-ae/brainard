"""Notification channel config -- notification_configs (owner-managed ntfy
channel, contracts-v1.md Principles: supersede-never-erase applies -- every
change is a new immutable version).

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_configs",
        sa.Column("id", sa.String(length=26), primary_key=True),
        # Sequential from 1, single global scope -- unlike doctrine_versions
        # there is no (kind, project) dimension to partition by here (one
        # channel, one sequence), so a plain table-wide unique index is the
        # correct analog of doctrine's partial-unique global-version index
        # (see app/models.py NotificationConfig docstring). Computed in the
        # route handler (app/notifications.py's `next_version`), not a DB
        # sequence -- same pattern as DoctrineVersion.version.
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("ntfy_url", sa.Text, nullable=False),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_notification_configs_version",
        "notification_configs",
        ["version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_configs_version", table_name="notification_configs")
    op.drop_table("notification_configs")
