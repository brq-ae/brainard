"""Consensus floor and objection statements before a debate/critique room
closes as agreed (ADR-0017): one new column on `rooms`, and an alteration
(not merely an addition) to `room_messages`' `kind` CHECK constraint.

- `rooms.consensus_floor` (integer, not-null, default 20): the minimum
  message count a debate/critique room must reach before an agent's
  `kind="done"` post can close it as agreed (`app/rooms.py`'s gate in
  `post_message`). Stored on every room regardless of mode -- same
  "additive column, low-risk" shape as `agent_uploads_allowed` (0015) --
  only ever read by the gate for debate/critique rooms.
- `room_messages.ck_room_messages_kind`: widened from
  `kind IN ('message', 'done', 'system')` to additionally admit
  `'objection'`, the new agent-postable kind ADR-0017 decision 4
  introduces. This IS the riskier half of this migration: unlike every
  prior change to this constraint's neighborhood (which have all been
  additive columns), this is an alteration of an existing CHECK
  constraint on a table that may already hold rows.

BACKFILL (independent-review finding, fixed here rather than shipped as a
follow-up -- same posture as 0016_room_owner_open_gate.py's backfill):
`consensus_floor` lands with a flat `server_default="20"` applied to every
pre-existing row, with no awareness of that row's own `max_messages`. For
a pre-existing debate/critique room whose `max_messages` is already <= 20,
that flat default lands it directly in the state ADR-0017 decision 2 says
must be REJECTED at create time (`consensus_floor >= max_messages`,
`create_room`'s "consensus_floor_unreachable") -- the room would come out
of this migration permanently unable to ever close as agreed via the new
gate, the exact class of "retroactively re-lock a room that was running
fine under the old rules" bug 0016's own backfill exists to prevent.

Measured against the live database THIS migration will actually run
against (checked directly, 2026-09, before writing this backfill): every
existing debate room's cap is 24 or 50 (mode `debate`), every existing
critique room's cap is 100 (mode `critique`) -- both floors far above the
20 default, so this backfill is a NO-OP on this specific database; the
only open room found (`collaborate`, cap 120) is outside `_CONSENSUS_GATED_MODES`
entirely and was never touched by the flat default in the first place. The
backfill below exists anyway, because this migration file is not scoped to
today's data -- it must be correct against any database it is ever run
against, including a fresh install, a fork, or a future room this table
doesn't have yet.

The predicate, applied once, to every pre-existing `rooms` row where the
just-defaulted `consensus_floor` (20) would be unreachable against that
row's own `max_messages`: for `mode IN ('debate', 'critique')` AND
`consensus_floor >= max_messages`, set
`consensus_floor = LEAST(20, GREATEST(1, max_messages - 1))` -- one less
than the cap (always < max_messages, so always reachable by construction),
floored at 1 (`CONSENSUS_FLOOR_MIN`, `app/rooms.py`) and capped at the
original default of 20 so a very large `max_messages` still gets the
intended default rather than an inflated floor. `freeform`/`collaborate`/
`brainstorm` rows are deliberately left at the flat default 20 regardless
of `max_messages` -- decision 3 scopes the gate itself to debate/critique
only, so an unreachable floor on any other mode is inert (the gate never
reads it) and backfilling it would just be non-truthful busywork.

One known residual: a pre-existing debate/critique room with
`max_messages = 1` has no integer floor `>= CONSENSUS_FLOOR_MIN (1)` that
is also `< max_messages (1)` -- the formula above still applies
(`LEAST(20, GREATEST(1, 0)) = 1`) and leaves that one room's floor equal
to its cap, i.e. still technically "unreachable" by the same `>=` test
`create_room` rejects at create time. This is not a gap in the backfill;
it is a pre-existing, inherent impossibility for a cap that small (the
ordinary message cap already closes such a room at its very first message,
`close_reason="cap"`, before an agreed "done" could ever matter) that no
backfill formula can resolve, since `CONSENSUS_FLOOR_MIN` is 1 and no
floor below that is valid.

SAFE AGAINST EXISTING ROWS, BOTH DIRECTIONS:
Upgrading only ever WIDENS the allowed set (superset of the original), so
it can never fail against any row that already satisfied the original
constraint -- every existing 'message'/'done'/'system' row trivially still
satisfies `kind IN ('message', 'done', 'system', 'objection')`. This part
is a schema-only change; the `consensus_floor` backfill above is the only
data migration this file performs. Downgrading narrows the set back to the
original three values -- this will correctly fail (as any CHECK constraint
the data no longer satisfies should) if any row has `kind = 'objection'` by
the time downgrade runs; that is the expected, correct behavior of undoing
a widened vocabulary that has since been used, not a migration bug. A
downgrade run before any `'objection'` row has ever been inserted (the
normal "undo this deploy" case) always succeeds. Downgrade drops
`consensus_floor` outright (same "no data to restore on the way down"
posture 0016's downgrade documents) -- there is nothing to un-backfill
since the column itself ceases to exist.

Implementation: PostgreSQL has no `ALTER CONSTRAINT ... ADD VALUE` for a
plain CHECK (that's an enum-type operation, and `kind` is a plain
`VARCHAR`, deliberately not a DB enum -- see `app/models.py`'s `RoomMessage`
docstring history); the standard, safe way to change a CHECK constraint's
condition is drop-then-recreate under the same name, which is what both
`upgrade`/`downgrade` below do. Each drop+create pair runs as a single DDL
transaction per Alembic's default Postgres behavior, so there is no window
where the table has no `kind` constraint at all.

Revises 0017 (message deletion's `deleted_at` column) -- next free
migration number.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORIGINAL_KIND_CHECK = "kind IN ('message', 'done', 'system')"
_WIDENED_KIND_CHECK = "kind IN ('message', 'done', 'system', 'objection')"


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("consensus_floor", sa.Integer(), nullable=False, server_default="20"),
    )
    # Backfill: on any pre-existing debate/critique row where the flat
    # default just applied above (20) would already be unreachable against
    # that row's own `max_messages`, replace it with a reachable floor --
    # see the module docstring's BACKFILL section for the full predicate,
    # the formula's reasoning, and why non-gated modes are deliberately
    # left untouched. A no-op on this database today (measured caps are all
    # well above 20); not a no-op in general.
    op.execute(
        """
        UPDATE rooms
        SET consensus_floor = LEAST(20, GREATEST(1, max_messages - 1))
        WHERE mode IN ('debate', 'critique')
          AND consensus_floor >= max_messages
        """
    )
    # Widen the kind CHECK constraint to also admit 'objection'. Safe
    # against any pre-existing row: the new condition is a strict superset
    # of the old one, so nothing that already satisfied it can newly fail.
    op.drop_constraint("ck_room_messages_kind", "room_messages", type_="check")
    op.create_check_constraint("ck_room_messages_kind", "room_messages", _WIDENED_KIND_CHECK)


def downgrade() -> None:
    # Narrow the kind CHECK constraint back to its original three values.
    # This intentionally fails if any row has kind='objection' at this
    # point -- that is the constraint doing its job, not a migration bug
    # (see module docstring).
    op.drop_constraint("ck_room_messages_kind", "room_messages", type_="check")
    op.create_check_constraint("ck_room_messages_kind", "room_messages", _ORIGINAL_KIND_CHECK)
    op.drop_column("rooms", "consensus_floor")
