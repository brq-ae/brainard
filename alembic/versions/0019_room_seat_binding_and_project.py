"""Room seat assignment/binding and an optional room project field (ADR-0018,
revised before implementation): two new, independent, additive columns.

- `rooms.project` (text, nullable, indexed): an optional, free-form project
  label (NOT a `projects`-registry FK -- see ADR-0018 decision 12 for why),
  so the join prompt's Layer 1 target check can state a precise, comparable
  fact instead of only a name/topic an agent has to eyeball. NULL means "no
  stated project" -- every pre-existing room already has no such field to
  read, so NULL is exactly what every existing room already "had."

- `room_members.bound_machine_id` (varchar(26), nullable, indexed, FK to
  `machines.id`): the seat's bound machine, if any -- set either by the
  owner at room-creation time (optional assignment, ADR-0018 decision 3
  first path) or by the first machine token that successfully posts/attaches
  as that member (claim-on-first-write, decision 3 second path;
  `app/room_seats.py`'s `check_and_bind_seat`). NULL means "open, unclaimed
  and unassigned" -- the literal state every seat has always been in, since
  this column didn't exist before. No `ondelete`: `machines` rows are never
  deleted (only revoked via `Machine.status`), so the FK's default RESTRICT
  should never fire.

NO BACKFILL, NO DATA MIGRATION for either column (ADR-0018 decisions 10/11):
`bound_machine_id = NULL` blocks nothing that would have gone through before
this ADR (every seat has always behaved as "any machine token may post as
any member" until now), and backfilling it by guessing which machine most
recently/most often posted as a given `sender` was considered and rejected
(no record exists to make that guess safely -- see the ADR's Alternatives
Considered). `project = NULL` is likewise just "this room predates the
field," not a state requiring any repair. Re-checked directly against the
live database before writing this migration (2026-09-10): 47 rooms, 2 open,
45 closed -- both new columns are safe no-ops against every one of them.

SAFE, BOTH DIRECTIONS: both columns are purely additive and nullable, so
upgrading can never fail against any existing row (nothing is required to
already have a value), and downgrading simply drops both columns -- there is
no data to preserve on the way down, mirroring 0016/0018's own "nothing to
un-backfill since the column itself ceases to exist" posture for a column
with no real data dependency.

Revises 0018 (consensus floor and objection kind) -- next free migration
number.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("project", sa.Text(), nullable=True))
    op.create_index("ix_rooms_project", "rooms", ["project"])

    op.add_column("room_members", sa.Column("bound_machine_id", sa.String(length=26), nullable=True))
    op.create_index("ix_room_members_bound_machine_id", "room_members", ["bound_machine_id"])
    op.create_foreign_key(
        "fk_room_members_bound_machine_id_machines",
        "room_members",
        "machines",
        ["bound_machine_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_room_members_bound_machine_id_machines", "room_members", type_="foreignkey")
    op.drop_index("ix_room_members_bound_machine_id", table_name="room_members")
    op.drop_column("room_members", "bound_machine_id")

    op.drop_index("ix_rooms_project", table_name="rooms")
    op.drop_column("rooms", "project")
