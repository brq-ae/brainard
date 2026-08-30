"""Rooms wait for the owner's first message (ADR-0014): four new columns on
`rooms`, plus a data backfill so the gate does not retroactively re-lock
rooms that were already running under the old (no-gate) rules.

- `opened_at` (nullable timestamp): NULL means "waiting for the owner";
  set once, the first time `post_message` commits a message with
  `sender == 'owner'` -- mirrors the existing `closed_at`/`closing_warned_at`
  nullable-timestamp shape already on this table (0009/0010).
- `requires_owner_open` (boolean, default true): the per-room opt-out --
  same "always-on boolean the owner can flip off" shape as
  `agent_uploads_allowed` (0015) / `notify_on_close` (0009).
- `pending_duration_seconds` (nullable integer): holds a relative
  `duration_seconds` deadline request until the room actually opens, at
  which point it's resolved into `expires_at`. An explicit `expires_at` is
  never deferred (stored directly at create time, unchanged).
- `owner_open_reminder_sent_at` (nullable timestamp): one-shot guard for the
  owner ntfy ping fired when an agent parks on an unopened room -- same
  one-shot shape `closing_warned_at` already uses for the sweeper's nudge.

BACKFILL (independent-review finding, fixed here rather than shipped as a
follow-up): unlike a plain additive nullable-with-default column,
`requires_owner_open=true` + `opened_at=NULL` is a NEW restriction, not a
no-op, for any row that already existed before this migration ran --
`app/rooms.py`'s gate (decision 1) reads exactly those two columns to decide
whether the NEXT agent post is rejected, and every pre-existing room was
created and has been running under the old rules, where agents were free to
post from the start. Without a backfill, a live, already-`open` room with
real message history would come out of this migration gated as if it had
never been touched, and the very next agent message would be rejected with
"the owner has not posted in this room yet" -- false, and disruptive to an
in-flight conversation.

The predicate, applied once, in two passes, to every row that exists in
`rooms` at the moment this migration runs (i.e. everything "pre-existing" by
definition -- nothing created after this point can be touched by an
`UPDATE` that already committed):

1. Any pre-existing room with at least one message from `sender = 'owner'`
   (`room_messages.sender = 'owner'`, regardless of the room's current
   `status` or whether that message was later tombstoned by
   ADR-0015's delete -- the `sender` column survives a tombstone, and the
   event of the owner posting genuinely happened at that timestamp either
   way): `opened_at` is backfilled to that room's EARLIEST owner message's
   `created_at`. That is the truthful moment this room actually opened, so
   `requires_owner_open` is left at its new default `true` -- the room is
   correctly recorded as opened, not exempted from the rule.
2. Any pre-existing room that is NOT covered by (1) but already has at
   least one message of any kind (`EXISTS (SELECT 1 FROM room_messages
   WHERE room_id = rooms.id)`): it ran its entire history so far under the
   old no-gate rules -- agents legitimately started on their own, since
   that was the only behavior that ever existed. Retroactively gating it
   would change behavior mid-conversation, exactly the regression this
   backfill exists to prevent. `requires_owner_open` is set to `false` for
   these rooms; `opened_at` is left NULL (no owner message ever happened,
   so there is no truthful timestamp to invent).
3. Everything else -- a pre-existing room with NO messages of any kind at
   all (`message_count = 0` and no `room_messages` rows) -- is left
   untouched: `opened_at` stays NULL, `requires_owner_open` stays at its new
   default `true`. This is the one case the predicate must NOT un-gate: a
   room with no history yet is indistinguishable, by anything this
   migration can observe, from a room a human just created seconds before
   `alembic upgrade head` ran -- exactly like a genuinely brand-new room
   created after this migration, both should get the gate, and both do.

Query order matters: pass (1) runs first and is scoped to rooms with an
owner message, so pass (2)'s `opened_at IS NULL` scope only ever reaches
rooms pass (1) did not already resolve -- an owner-message room can never
be un-gated by (2), and a no-message room can never match either pass's
`EXISTS`/join, satisfying (3) by simply doing nothing to it.

Revises 0015 (the agent-uploads switch) -- next free migration number.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "rooms",
        sa.Column("requires_owner_open", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("rooms", sa.Column("pending_duration_seconds", sa.Integer(), nullable=True))
    op.add_column("rooms", sa.Column("owner_open_reminder_sent_at", sa.DateTime(timezone=True), nullable=True))

    # Pass 1: rooms with a real owner message -- backfill the truthful
    # `opened_at` from the earliest one. `requires_owner_open` is left at
    # its just-applied default `true`.
    op.execute(
        """
        UPDATE rooms
        SET opened_at = first_owner.first_owner_at
        FROM (
            SELECT room_id, MIN(created_at) AS first_owner_at
            FROM room_messages
            WHERE sender = 'owner'
            GROUP BY room_id
        ) AS first_owner
        WHERE rooms.id = first_owner.room_id
        """
    )

    # Pass 2: rooms with history but no owner message ever -- they ran the
    # old no-gate rules for real; un-gate them rather than freezing them.
    # Deliberately does NOT touch rooms with zero messages (predicate case
    # 3 in the docstring above) -- the `EXISTS` below is false for those,
    # so they keep the fresh default (`requires_owner_open=true`,
    # `opened_at=NULL`), same as any genuinely new room.
    op.execute(
        """
        UPDATE rooms
        SET requires_owner_open = false
        WHERE opened_at IS NULL
          AND EXISTS (SELECT 1 FROM room_messages WHERE room_messages.room_id = rooms.id)
        """
    )


def downgrade() -> None:
    # No data to restore on the way down: these four columns simply cease
    # to exist, and `app/rooms.py`'s gate logic (which is what interprets
    # them) is gone from behavior the moment the code that reads them is
    # rolled back too -- dropping the columns outright remains correct.
    op.drop_column("rooms", "owner_open_reminder_sent_at")
    op.drop_column("rooms", "pending_duration_seconds")
    op.drop_column("rooms", "requires_owner_open")
    op.drop_column("rooms", "opened_at")
