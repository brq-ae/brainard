"""Room management: delete and free-form groups (ADR-0008, extends
ADR-0006/0007). Adds a nullable `group_name` label on rooms -- exposed as
`group` in the API/UI ('group' is a SQL reserved word, hence the column
name). Delete itself (DELETE /v1/rooms/{id}) needs no schema change: it
hard-deletes room_messages/room_members/rooms rows via explicit application-
level cascade in app/rooms.py's delete_room (the existing FKs have no
ondelete=CASCADE).

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("group_name", sa.Text(), nullable=True))
    # Backs GET /v1/rooms?group=X's exact-match filter and the UI's group
    # filter/dropdown -- a plain btree index (not partial/unique; many rooms
    # may share, or lack, a group).
    op.create_index("ix_rooms_group_name", "rooms", ["group_name"])


def downgrade() -> None:
    op.drop_index("ix_rooms_group_name", table_name="rooms")
    op.drop_column("rooms", "group_name")
