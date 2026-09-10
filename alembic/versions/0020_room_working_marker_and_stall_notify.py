"""Room engagement: a "working" marker and a sustained-silence owner
notification (ADR-0020): three new, additive columns.

- `room_members.working_until` (timestamptz, nullable, no default): a
  forward-looking EXPIRY timestamp, not a boolean "is working" flag -- a
  flag would need an explicit clear a crashed/killed agent never gets to
  perform, leaving a false-positive "still working" forever. Refreshed to
  `now() + WORKING_LEASE_SECS` (app/rooms.py) as a side effect of that
  member polling `GET /v1/rooms/{id}/messages` with a matching
  `agent_name` query parameter (also new, this ADR). NULL means "never
  polled with `agent_name` since this column existed" -- the literal,
  correct state of every pre-existing seat.

- `rooms.stall_notify_secs` (integer, not-null, default 1200 -- 20
  minutes): how long a room may sit with no new message AND no member
  showing a live `working_until` lease before the server fires a
  best-effort, one-shot owner ntfy ping. Owner-settable per room at create
  time, same "judgment call that reasonably differs per room" posture
  `rooms.consensus_floor` (migration 0018) already has.

- `rooms.stall_notify_sent_at` (timestamptz, nullable): the one-shot guard
  for that ping -- same nullable-timestamp shape as
  `rooms.owner_open_reminder_sent_at` (migration 0016). DELIBERATELY its
  own column, not a reuse of that one: the two guard different,
  mutually-exclusive-in-practice owner-facing events (ADR-0014's "room
  never opened" vs. this ADR's "room went quiet after opening") -- see the
  ADR's decision 3 for the full reasoning. Reusing one column for both
  would let either event silently suppress the other.

NO BACKFILL NEEDED, for any of the three columns (verified directly
against the live database before writing this migration, 2026-09-10: 48
rooms, 1 open, 96 room_members, migration head still 0019 -- every one of
them is a safe no-op against all three columns below):

- `working_until = NULL` is the literal "never polled with `agent_name`"
  state for every existing seat, since the `agent_name` poll parameter
  that would ever set it did not exist before this ADR. This is NOT the
  same shape as ADR-0014's `opened_at` backfill (migration 0016), which
  needed real data because turning `requires_owner_open` on retroactively
  imposed a NEW RESTRICTION on rooms that had already satisfied the old,
  gate-free rules. Nothing here imposes a restriction: a NULL
  `working_until` simply means `partner_working` reads `false` for that
  seat until its first `agent_name`-bearing poll -- an honest reflection
  of reality (nothing has refreshed it yet), not a wrongful rejection of
  anything that used to work.
- `stall_notify_secs` defaulting to 1200 on every existing row imposes no
  new restriction on anything already in flight either: a room mid-
  conversation simply becomes eligible for a stall check from this point
  forward -- the same "additive capability, not a retroactive
  restriction" shape `agent_uploads_allowed` (migration 0015) already
  established, explicitly contrasted against `requires_owner_open` in
  ADR-0014's own Consequences.
- `stall_notify_sent_at = NULL` is the correct "never sent" state for
  every existing room, open or closed -- a closed room's poll loop has
  already exited via `room.status != "open"`, so the stall path is
  unreachable for it regardless of this column's value.

SAFE, BOTH DIRECTIONS: all three columns are purely additive; upgrading
can never fail against any existing row (nothing is required to already
have a value beyond the flat `stall_notify_secs` default), and downgrading
simply drops all three -- there is no data to preserve on the way down,
same "nothing to un-backfill since the column itself ceases to exist"
posture 0016/0018/0019 already established for their own additions.

Revises 0019 (room seat binding and project) -- next free migration
number. The live migration head at the time this was written was still
0019 (this ADR's build had not yet been deployed).

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("room_members", sa.Column("working_until", sa.DateTime(timezone=True), nullable=True))

    op.add_column(
        "rooms",
        sa.Column("stall_notify_secs", sa.Integer(), nullable=False, server_default="1200"),
    )
    op.add_column("rooms", sa.Column("stall_notify_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("rooms", "stall_notify_sent_at")
    op.drop_column("rooms", "stall_notify_secs")

    op.drop_column("room_members", "working_until")
