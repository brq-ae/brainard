"""Agent chat rooms -- rooms, room_members, room_messages (ADR-0006, phase A:
core rooms/messages/long-poll/guardrails/notify). v1 is two-agent rooms,
enforced at the API layer (app/rooms.py); room_members models the general
many-member concept per the ADR.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rooms",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        # Hard backstop cap (guardrail 3 of 3, ADR-0006 decision 5).
        sa.Column("max_messages", sa.Integer, nullable=False, server_default="100"),
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("notify_on_close", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text, nullable=True),
    )
    op.create_check_constraint("ck_rooms_status", "rooms", "status IN ('open', 'closed')")
    op.create_check_constraint(
        "ck_rooms_close_reason", "rooms", "close_reason IN ('done', 'owner', 'cap', 'stall') OR close_reason IS NULL"
    )

    op.create_table(
        "room_members",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("room_id", sa.String(length=26), sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("agent_name", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_room_members_room_id", "room_members", ["room_id"])
    op.create_index("ix_room_members_room_agent", "room_members", ["room_id", "agent_name"], unique=True)

    op.create_table(
        "room_messages",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("room_id", sa.String(length=26), sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("sender", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="message"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_check_constraint("ck_room_messages_kind", "room_messages", "kind IN ('message', 'done', 'system')")
    op.create_index("ix_room_messages_room_id", "room_messages", ["room_id"])
    # Serves both "index on (room_id, seq)" and "unique on (room_id, seq)".
    op.create_index("ix_room_messages_room_seq", "room_messages", ["room_id", "seq"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_room_messages_room_seq", table_name="room_messages")
    op.drop_index("ix_room_messages_room_id", table_name="room_messages")
    op.drop_constraint("ck_room_messages_kind", "room_messages", type_="check")
    op.drop_table("room_messages")

    op.drop_index("ix_room_members_room_agent", table_name="room_members")
    op.drop_index("ix_room_members_room_id", table_name="room_members")
    op.drop_table("room_members")

    op.drop_constraint("ck_rooms_close_reason", "rooms", type_="check")
    op.drop_constraint("ck_rooms_status", "rooms", type_="check")
    op.drop_table("rooms")
