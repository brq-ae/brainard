"""Room agent-upload switch (ADR-0012 stage 2, decision 7): a per-room
boolean disabling AGENT (never owner) file uploads, toggleable mid-room by
the owner. Default true (agent uploads allowed -- the checkbox is an
opt-out), same "always-on boolean the owner can flip off" shape as
`rooms.notify_on_close` (0009_agent_chat_rooms.py).

Revises 0014 (ADR-0012 stage 1's storage core) -- same feature, second
migration, following this repo's existing precedent of a feature landing
across more than one migration (e.g. 0009 agent_chat_rooms then 0010 room
modes/time limits, both ADR-0006/0007 extending the same tables).

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("agent_uploads_allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("rooms", "agent_uploads_allowed")
