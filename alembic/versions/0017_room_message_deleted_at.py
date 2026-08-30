"""Deleting individual room messages (ADR-0015): one new column,
`room_messages.deleted_at`.

Nullable timestamp -- same shape `rooms.closed_at` already has (0009). NULL
means "never deleted"; set once, the first time the owner tombstones this
message (app/rooms.py's `delete_message`), at the same moment the row's
`text` is overwritten in place with a fixed marker. No other schema change
is needed: the counter this feature decrements (`rooms.message_count`)
already exists, and `RoomMessage.seq`/`id`/`sender`/`kind`/`created_at` are
all preserved unchanged by a delete, so nothing about them needs migrating.

Revises 0016 (the owner-open gate) -- next free migration number.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("room_messages", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("room_messages", "deleted_at")
