"""Background sweeper (ADR-0007 decision 3): the Brain's first always-on
background task. Roughly every SWEEP_INTERVAL seconds it:

  (a) closes every open room whose `expires_at` deadline has passed, via
      app.rooms.close_room -- the SAME atomic close path the message cap
      uses (close_reason 'time') -- which also fires the existing
      best-effort owner notification (app/notify.py); never hand-rolls a
      close here.
  (b) posts a one-time "closing soon" system-message nudge (app.rooms.
      post_closing_nudge) into every open room entering its warning
      window, guarded by `closing_warned_at` so it can never double-post.
  (c) (ADR-0012 stage 3) reclaims scratch attachment blobs whose
      reference-counted lifetime has expired -- app.attachments.
      `sweep_expired_blobs`, the SAME reference-counted lock-check-delete
      domain logic already covered directly in tests/test_attachments.py
      (grace period, shared-blob protection, Brain-document protection);
      this task only wires that logic into the existing cycle so it
      actually runs.

`sweep_once` is the testable unit: callers (tests, `run_sweeper` below)
pass a `session_factory` and get back exactly what one cycle did, without
racing the 60s loop. Every DB touch opens its own fresh, short-lived
session via `session_factory` (default AsyncSessionLocal) -- never one
session held across the whole cycle or across a sleep -- same per-
iteration session discipline as app.rooms.poll_messages's long-poll.

Single-worker deployment only (ADR-0007 consequences): one sweeper
instance is assumed; a future multi-worker deployment would need a lock
(e.g. a Postgres advisory lock) around sweep_once so two workers don't
race the same rooms. Not needed today.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.attachments import sweep_expired_blobs
from app.db import AsyncSessionLocal
from app.models import Room
from app.rooms import close_room, post_closing_nudge

logger = logging.getLogger(__name__)

# How long before `expires_at` the one-time closing nudge fires.
WARN_SECONDS = 120
# How often run_sweeper() runs a cycle.
SWEEP_INTERVAL = 60

_CLOSING_NUDGE_TEXT = (
    f"About {WARN_SECONDS // 60} minutes left — post your closing statements now."
)


async def _close_expired_rooms(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Scans for open, expired rooms in one short-lived session, then
    closes each via `close_room` in its OWN short-lived session/
    transaction -- so a slow notify on one room's close can't hold a
    connection open while the rest of the sweep waits, and so the atomic
    close (with its own re-check-under-lock-equivalent idempotency: closing
    an already-closed room is a no-op, see app.rooms.close_room) is always
    the unit of work, never batched with anything else.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        room_ids = list(
            (
                await session.scalars(
                    select(Room.id).where(
                        Room.status == "open",
                        Room.expires_at.is_not(None),
                        Room.expires_at <= now,
                    )
                )
            ).all()
        )

    closed: list[str] = []
    for room_id in room_ids:
        try:
            async with session_factory() as session:
                room = await close_room(session, room_id, "time")
                # close_room is idempotent -- if a concurrent request (owner
                # close, or the cap) closed it first for a different reason
                # between the scan above and this call, close_reason won't be
                # 'time'; only report rooms THIS call actually time-closed.
                if room.close_reason == "time":
                    closed.append(room_id)
        except Exception:
            # Isolate this room's failure from the rest of the batch -- one
            # bad room (e.g. a transient DB error on its close) must not
            # abort the loop and skip every other expired room until the
            # next cycle. `run_sweeper`'s outer try/except remains the
            # backstop for anything outside this per-room loop; this is the
            # finer-grained isolation *within* one cycle.
            logger.exception("room sweeper: closing expired room %s failed -- skipping it this cycle", room_id)
    return closed


async def _warn_closing_rooms(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Scans for open, not-yet-warned rooms entering their warning window
    in one short-lived session (a hint only -- see post_closing_nudge's
    docstring for why the real "not already warned" guard is the row lock
    taken inside it, not this query), then posts the nudge for each in its
    own short-lived session/transaction.

    Any room with `expires_at <= now` would already have been closed by
    `_close_expired_rooms` above (called first in `sweep_once`) and so
    would no longer be 'open' -- this scan only ever sees rooms still
    genuinely inside their warning window.
    """
    now = datetime.now(UTC)
    warn_cutoff = now + timedelta(seconds=WARN_SECONDS)
    async with session_factory() as session:
        room_ids = list(
            (
                await session.scalars(
                    select(Room.id).where(
                        Room.status == "open",
                        Room.expires_at.is_not(None),
                        Room.closing_warned_at.is_(None),
                        Room.expires_at <= warn_cutoff,
                    )
                )
            ).all()
        )

    warned: list[str] = []
    for room_id in room_ids:
        try:
            async with session_factory() as session:
                message = await post_closing_nudge(session, room_id, _CLOSING_NUDGE_TEXT)
                if message is not None:
                    warned.append(room_id)
        except Exception:
            # Same per-room isolation as _close_expired_rooms above -- one
            # room's failed nudge must not abort the loop and skip the
            # remaining rooms' nudges until the next cycle.
            logger.exception("room sweeper: closing-soon nudge for room %s failed -- skipping it this cycle", room_id)
    return warned


async def _sweep_attachment_blobs(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """ADR-0012 stage 3: one pass of `app.attachments.sweep_expired_blobs`,
    on its own short-lived session -- same per-iteration session discipline
    as the two scans above, and the same isolation: wrapped in its own
    try/except so a failure here (a transient DB error, anything else
    unexpected) is logged and skipped, never raised -- it must not abort
    this cycle's room-closing/nudging (which already ran, above, by the
    time this is called) or crash the always-on loop `run_sweeper` starts.
    A failed pass is simply retried next cycle, same recovery posture as
    every other step in this file.

    `sweep_expired_blobs` already does the real work -- reference-counted
    check under a row lock, one commit per blob, so no partial state from a
    mid-sweep failure -- and is exercised directly (grace period, shared-
    blob protection, Brain-document protection) in
    tests/test_attachments.py; this function only adds the try/except and
    the log line so a background caller sees what was reclaimed.
    """
    try:
        async with session_factory() as session:
            deleted = await sweep_expired_blobs(session)
    except Exception:
        logger.exception("room sweeper: attachment blob sweep failed -- will retry next cycle")
        return []
    if deleted:
        logger.info("room sweeper: reclaimed %d expired attachment blob(s): %s", len(deleted), deleted)
    return deleted


async def sweep_once(session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal) -> dict[str, list[str]]:
    """One sweep cycle (ADR-0007 decision 3; ADR-0012 stage 3 adds the
    attachment-blob reclaim). Returns `{"closed": [room_id, ...], "warned":
    [room_id, ...], "reclaimed_blobs": [sha256, ...]}` -- call this directly
    in tests rather than waiting on the 60s `run_sweeper` loop.
    """
    closed = await _close_expired_rooms(session_factory)
    warned = await _warn_closing_rooms(session_factory)
    reclaimed_blobs = await _sweep_attachment_blobs(session_factory)
    return {"closed": closed, "warned": warned, "reclaimed_blobs": reclaimed_blobs}


async def run_sweeper(session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal) -> None:
    """The always-on loop, started from app/main.py's lifespan. Each cycle
    is wrapped in its own try/except: a bad cycle (a transient DB error, or
    anything else unexpected -- app/notify.py's own calls inside
    close_room already swallow their own failures, so this is a backstop,
    not the primary safety net) is logged and the loop simply retries next
    cycle. It must NEVER crash the app -- this is the Brain's first always-
    on background task and nothing else depends on it being up for the
    rest of the app to keep serving requests.

    `except Exception` deliberately does not catch `asyncio.CancelledError`
    (which is not an `Exception` subclass in Python 3.8+) -- so
    app/main.py's lifespan shutdown, which cancels this task, actually
    stops the loop instead of the cancellation being swallowed and retried
    forever.
    """
    while True:
        try:
            await sweep_once(session_factory)
        except Exception:
            logger.exception("room sweeper cycle failed -- will retry next cycle")
        await asyncio.sleep(SWEEP_INTERVAL)
