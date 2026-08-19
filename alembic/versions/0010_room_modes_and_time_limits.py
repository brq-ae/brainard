"""Room modes and time limits (ADR-0007, extends ADR-0006): a room gains an
optional mode (freeform default | debate | collaborate | brainstorm |
critique), a topic, an expiry deadline, and a one-time closing-nudge guard;
room_members gains a per-member side for asymmetric modes.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("mode", sa.Text(), nullable=False, server_default="freeform"))
    op.create_check_constraint(
        "ck_rooms_mode",
        "rooms",
        "mode IN ('freeform', 'debate', 'collaborate', 'brainstorm', 'critique')",
    )
    op.add_column("rooms", sa.Column("topic", sa.Text(), nullable=True))
    op.add_column("rooms", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    # Guards the one-time closing nudge the sweeper posts as the deadline
    # nears (app/room_sweeper.py) -- NULL until posted, set once.
    op.add_column("rooms", sa.Column("closing_warned_at", sa.DateTime(timezone=True), nullable=True))

    # Extend the existing close_reason check (0009) to also allow 'time'
    # (the sweeper's expiry close) -- drop and recreate since Postgres has
    # no ALTER CHECK CONSTRAINT.
    op.drop_constraint("ck_rooms_close_reason", "rooms", type_="check")
    op.create_check_constraint(
        "ck_rooms_close_reason",
        "rooms",
        "close_reason IN ('done', 'owner', 'cap', 'stall', 'time') OR close_reason IS NULL",
    )

    # 'for'/'against' (debate), 'proposer'/'critic' (critique); NULL for
    # symmetric/freeform members.
    op.add_column("room_members", sa.Column("side", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("room_members", "side")

    op.drop_constraint("ck_rooms_close_reason", "rooms", type_="check")
    op.create_check_constraint(
        "ck_rooms_close_reason", "rooms", "close_reason IN ('done', 'owner', 'cap', 'stall') OR close_reason IS NULL"
    )

    op.drop_column("rooms", "closing_warned_at")
    op.drop_column("rooms", "expires_at")
    op.drop_constraint("ck_rooms_mode", "rooms", type_="check")
    op.drop_column("rooms", "mode")
