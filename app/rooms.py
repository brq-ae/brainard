"""Agent Chat Rooms -- shared domain logic (ADR-0006, phase A: core
rooms/messages/long-poll/guardrails/notify). Used by app/routers/rooms.py;
kept here, in one place, so validation/versioning logic never drifts between
routes -- same "shared module" pattern as app/notifications.py and
app/projects.py.

Two-agent rooms for v1 (ADR-0006 decision 2): `room_members` models the
general concept, but exactly 2 distinct members are enforced here at create
time. Liveness is a long-poll (`poll_messages`) that deliberately never holds
a single DB session across its wait -- see that function's docstring.
"""

import asyncio
import base64
import time
from datetime import UTC, datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.models import Room, RoomMember, RoomMessage
from app.notify import notify_room_closed

# --- room creation ---

MAX_MESSAGES_MIN = 1
MAX_MESSAGES_MAX = 10000
DEFAULT_MAX_MESSAGES = 100
REQUIRED_MEMBER_COUNT = 2

_RECOVERY_ROOM_MEMBERS = "resend `members` as exactly two distinct non-empty agent-name strings"


def _validate_members(members: list[str]) -> list[str]:
    cleaned = [m.strip() if isinstance(m, str) else m for m in members]
    if len(cleaned) != REQUIRED_MEMBER_COUNT or any(not isinstance(m, str) or not m for m in cleaned):
        raise ApiError(
            422,
            "invalid_room_members",
            f"`members` must contain exactly {REQUIRED_MEMBER_COUNT} non-empty agent-name strings, got "
            f"{members!r}. Recovery: {_RECOVERY_ROOM_MEMBERS}.",
        )
    if cleaned[0] == cleaned[1]:
        raise ApiError(
            422,
            "duplicate_room_members",
            f"`members` named the same agent twice ('{cleaned[0]}') -- a room needs two distinct "
            f"participants. Recovery: {_RECOVERY_ROOM_MEMBERS}.",
        )
    return cleaned


def _validate_max_messages(max_messages: int | None) -> int:
    if max_messages is None:
        return DEFAULT_MAX_MESSAGES
    if not isinstance(max_messages, int) or isinstance(max_messages, bool) or not (
        MAX_MESSAGES_MIN <= max_messages <= MAX_MESSAGES_MAX
    ):
        raise ApiError(
            422,
            "invalid_max_messages",
            f"`max_messages` must be an integer between {MAX_MESSAGES_MIN} and {MAX_MESSAGES_MAX}, got "
            f"{max_messages!r}. Recovery: resend within range, or omit it to use the default "
            f"({DEFAULT_MAX_MESSAGES}).",
        )
    return max_messages


async def create_room(db: AsyncSession, name: str, members: list[str], max_messages: int | None) -> Room:
    if not name or not name.strip():
        raise ApiError(422, "invalid_room_name", "`name` must be non-empty. Recovery: resend with a non-empty name.")
    cleaned_members = _validate_members(members)
    resolved_max = _validate_max_messages(max_messages)

    now = datetime.now(UTC)
    room = Room(
        id=str(ULID()),
        name=name.strip(),
        status="open",
        max_messages=resolved_max,
        message_count=0,
        notify_on_close=True,
        created_at=now,
    )
    db.add(room)
    await db.flush()  # room.id must exist before the member rows FK to it

    for agent_name in cleaned_members:
        db.add(RoomMember(id=str(ULID()), room_id=room.id, agent_name=agent_name, created_at=now))

    await db.commit()
    return room


# --- reads ---


async def get_room(db: AsyncSession, room_id: str) -> Room | None:
    return await db.get(Room, room_id)


async def get_members(db: AsyncSession, room_id: str) -> list[str]:
    rows = (
        await db.scalars(
            select(RoomMember.agent_name).where(RoomMember.room_id == room_id).order_by(RoomMember.created_at)
        )
    ).all()
    return list(rows)


async def get_members_for_rooms(db: AsyncSession, room_ids: list[str]) -> dict[str, list[str]]:
    """Batched member lookup for GET /v1/rooms (list) -- one query for the
    whole page instead of one per room.
    """
    if not room_ids:
        return {}
    rows = (
        await db.execute(
            select(RoomMember.room_id, RoomMember.agent_name)
            .where(RoomMember.room_id.in_(room_ids))
            .order_by(RoomMember.created_at)
        )
    ).all()
    result: dict[str, list[str]] = {}
    for room_id, agent_name in rows:
        result.setdefault(room_id, []).append(agent_name)
    return result


async def get_recent_messages(db: AsyncSession, room_id: str, limit: int = 50) -> list[RoomMessage]:
    rows = (
        await db.scalars(
            select(RoomMessage).where(RoomMessage.room_id == room_id).order_by(RoomMessage.seq.desc()).limit(limit)
        )
    ).all()
    return list(reversed(rows))  # oldest-first, chat reading order


# --- GET /v1/rooms (list, cursor-paginated, newest first) ---


def _encode_room_cursor(created_at: datetime, id_: str) -> str:
    raw = f"{created_at.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_room_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_s, id_ = raw.split("|", 1)
        return datetime.fromisoformat(ts_s), id_
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid_cursor", "The `cursor` parameter is not valid for this listing.") from exc


async def list_rooms(db: AsyncSession, *, cursor: str | None = None, limit: int = 20) -> tuple[list[Room], str | None]:
    stmt = select(Room).order_by(Room.created_at.desc(), Room.id.desc())
    if cursor is not None:
        cursor_ts, cursor_id = _decode_room_cursor(cursor)
        stmt = stmt.where(tuple_(Room.created_at, Room.id) < tuple_(cursor_ts, cursor_id))
    stmt = stmt.limit(limit + 1)

    rows = (await db.scalars(stmt)).all()
    has_more = len(rows) > limit
    page_rows = list(rows[:limit])

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_room_cursor(last.created_at, last.id)

    return page_rows, next_cursor


# --- POST /v1/rooms/{id}/messages ---

MAX_TEXT_BYTES = 32 * 1024
VALID_POST_KINDS = frozenset({"message", "done"})
MAX_INSERT_ATTEMPTS = 3

_RECOVERY_MESSAGE_CONFLICT = "resend the same message; the sequence number will be recomputed automatically"


async def _next_seq(db: AsyncSession, room_id: str) -> int:
    max_seq = await db.scalar(select(func.max(RoomMessage.seq)).where(RoomMessage.room_id == room_id))
    return (max_seq or 0) + 1


async def _insert_message_and_maybe_close(
    db: AsyncSession, room: Room, sender: str, text: str, kind: str, now: datetime
) -> tuple[RoomMessage, str | None]:
    """The single insert-and-guardrail attempt, factored out so
    `post_message`'s retry loop below can catch exactly this call's
    IntegrityError on the unique (room_id, seq) index -- mirrors
    app/routers/deposits.py's `_insert_deposit` / retry-loop split. Called
    only while `room`'s row lock (see `post_message`) is held, so
    `_next_seq` and the `message_count` increment are race-free.

    Deliberately applies a guardrail close (done-signal or cap) in the
    *same* transaction/commit as the insert, not a separate one afterward:
    the room's row lock is released at COMMIT, so if the close happened in
    a later, second commit, a concurrent writer already blocked on the lock
    could acquire it the instant this commit lands -- seeing the
    just-incremented message_count but a still-'open' status -- and slip
    one more message past the cap before this function's own close-commit
    runs. Closing here, before the one commit, means any lock-waiter that
    acquires the lock next always observes the fully-updated row (message
    inserted, count incremented, and closed if the guardrail fired) as one
    atomic unit.
    """
    seq = await _next_seq(db, room.id)
    message = RoomMessage(id=str(ULID()), room_id=room.id, seq=seq, sender=sender, text=text, kind=kind, created_at=now)
    db.add(message)
    room.message_count += 1

    close_reason: str | None = None
    if kind == "done":
        close_reason = "done"
    elif room.message_count >= room.max_messages:
        close_reason = "cap"

    if close_reason is not None:
        room.status = "closed"
        room.closed_at = now
        room.close_reason = close_reason

    await db.commit()
    return message, close_reason


async def _apply_close(db: AsyncSession, room: Room, reason: str) -> None:
    room.status = "closed"
    room.closed_at = datetime.now(UTC)
    room.close_reason = reason
    await db.commit()
    # Best-effort, never raises -- see app/notify.py's module docstring.
    await notify_room_closed(db, room)


async def post_message(db: AsyncSession, room_id: str, sender: str, text: str, kind: str = "message") -> tuple[RoomMessage, Room]:
    """Posts one message, assigning the next per-room `seq` and incrementing
    `message_count` under a `SELECT ... FOR UPDATE` row lock on the room
    (acquired below, right before those two operations) -- concurrent posts
    to the *same* room serialize on that lock, so two racing transactions
    can never both read the same "current" message_count/max(seq) and lose
    one's update, which would otherwise let the hard cap (guardrail 3 of 3)
    be overrun under concurrency. Posts to *different* rooms are unaffected
    -- the lock is per-room-row, not global.

    Applies the guardrails in order (ADR-0006 decision 5) once the message
    is committed: a 'done' kind closes the room (`close_reason` 'done');
    otherwise hitting the `max_messages` cap closes it (`close_reason`
    'cap'). Either close fires the best-effort owner notification.

    The bounded IntegrityError retry on the unique (room_id, seq) index is
    kept as a defensive backstop (mirrors app/routers/deposits.py's
    insert-conflict loop) but should no longer fire in the normal
    concurrent-post case now that the lock serializes same-room writers --
    `_next_seq` and the insert always run for one room-lock holder at a
    time.
    """
    if kind not in VALID_POST_KINDS:
        raise ApiError(
            422,
            "invalid_message_kind",
            f"`kind` must be one of {sorted(VALID_POST_KINDS)}, got {kind!r}.",
        )
    if not isinstance(sender, str) or not sender.strip():
        raise ApiError(422, "invalid_sender", "`sender` must be a non-empty string.")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(422, "empty_message_text", "`text` must be non-empty.")
    text_bytes = len(text.encode("utf-8"))
    if text_bytes > MAX_TEXT_BYTES:
        raise ApiError(
            422,
            "message_text_too_large",
            f"`text` is {text_bytes} bytes, exceeding the {MAX_TEXT_BYTES}-byte cap. Recovery: shorten it, resend.",
        )

    # Unlocked pre-checks: existence, open/closed, and sender membership
    # never need the row lock to be correct (membership is immutable after
    # create; a stale "open" read here is fine because status is re-checked
    # below, *after* the lock is acquired, which is the check that actually
    # has to be race-free).
    room = await db.get(Room, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
    if room.status != "open":
        raise ApiError(
            409,
            "room_closed",
            f"Room '{room.name}' is closed (reason: {room.close_reason}); no further messages are accepted. "
            "Recovery: start a new room.",
        )

    sender = sender.strip()
    if sender != "owner":
        members = await get_members(db, room_id)
        if sender not in members:
            raise ApiError(
                403,
                "sender_not_room_member",
                f"'{sender}' is not a member of room '{room.name}' (members: {members}) and is not the "
                "literal 'owner'. Recovery: post as one of the room's members, or as 'owner'.",
            )

    # Serialization point: acquire the room's row lock. A concurrent post to
    # this same room that got here first holds this lock until its commit
    # (in `_insert_message_and_maybe_close` below); this call blocks until
    # then, so every racing writer sees the *previous* writer's committed
    # message_count/seq/status, never a stale snapshot. `populate_existing`
    # is required in addition to `with_for_update`: `room` may already be
    # present in this Session's identity map from the unlocked `db.get`
    # above, and without `populate_existing`, SQLAlchemy would leave that
    # already-loaded object's attributes untouched even though the FOR
    # UPDATE SQL below legitimately re-fetches newer committed data -- i.e.
    # the lock would be acquired correctly at the DB level, but the Python
    # object could still read back stale message_count/status. This makes
    # the fetch force-refresh the object's attributes from the locked row.
    room = await db.scalar(
        select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
    )
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
    # Re-check status under the lock: another concurrent post may have
    # closed the room (done/cap) in the window between the unlocked check
    # above and acquiring this lock.
    if room.status != "open":
        raise ApiError(
            409,
            "room_closed",
            f"Room '{room.name}' is closed (reason: {room.close_reason}); no further messages are accepted. "
            "Recovery: start a new room.",
        )

    now = datetime.now(UTC)
    message: RoomMessage | None = None
    close_reason: str | None = None
    for attempt in range(1, MAX_INSERT_ATTEMPTS + 1):
        try:
            message, close_reason = await _insert_message_and_maybe_close(db, room, sender, text.strip(), kind, now)
        except IntegrityError:
            await db.rollback()
            await db.refresh(room)
            if attempt < MAX_INSERT_ATTEMPTS:
                continue
            raise ApiError(
                503,
                "room_message_conflict_retry",
                "A concurrent post collided with this one repeatedly and in-server retries did not resolve "
                f"it; nothing was stored. Recovery: {_RECOVERY_MESSAGE_CONFLICT}.",
            ) from None
        else:
            break

    assert message is not None  # loop above always assigns or raises

    if close_reason is not None:
        # Best-effort, never raises -- see app/notify.py's module docstring.
        await notify_room_closed(db, room)

    return message, room


# --- POST /v1/rooms/{id}/close ---

VALID_CLOSE_REASONS = frozenset({"done", "owner", "cap", "stall"})


async def close_room(db: AsyncSession, room_id: str, reason: str | None) -> Room:
    if reason is not None and reason not in VALID_CLOSE_REASONS:
        raise ApiError(
            422,
            "invalid_close_reason",
            f"`reason` must be one of {sorted(VALID_CLOSE_REASONS)}, got {reason!r}.",
        )
    room = await db.get(Room, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
    if room.status == "closed":
        return room  # idempotent: closing an already-closed room just returns its state

    await _apply_close(db, room, reason or "owner")
    return room


# --- GET /v1/rooms/{id}/messages -- the long-poll ---

POLL_INTERVAL_SECS = 1
MAX_WAIT_SECS = 30


async def _room_and_messages_since(room_id: str, since: int) -> tuple[Room | None, list[RoomMessage]]:
    """One poll-iteration check, on its own short-lived session that the
    `async with` below closes (returning its pooled connection) before this
    call even returns -- well before the caller's next `asyncio.sleep`. See
    `poll_messages` docstring for why this must never be the request-scoped
    `get_db` session.
    """
    async with AsyncSessionLocal() as session:
        room = await session.get(Room, room_id)
        if room is None:
            return None, []
        rows = (
            await session.scalars(
                select(RoomMessage)
                .where(RoomMessage.room_id == room_id, RoomMessage.seq > since)
                .order_by(RoomMessage.seq)
            )
        ).all()
        return room, list(rows)


async def poll_messages(room_id: str, since: int, wait: int) -> tuple[Room, list[RoomMessage]]:
    """Long-poll for messages with seq > `since`. Returns as soon as any
    exist, OR the room is no longer open, OR `wait` seconds have elapsed
    (capped at MAX_WAIT_SECS) -- whichever comes first.

    CRITICAL: this function takes no `db: AsyncSession` -- there is no
    request-scoped session held across the `asyncio.sleep` calls below. Each
    iteration opens a fresh `AsyncSessionLocal()`, does its one query, and
    closes it (`_room_and_messages_since`) before sleeping -- so a slow
    long-poll never pins a connection out of the pool for the whole wait.
    Only the caller's route handler contributes a (short-lived, per-request)
    auth-check session via `require_machine_or_owner`, not this loop.
    """
    wait = max(0, min(wait, MAX_WAIT_SECS))
    deadline = time.monotonic() + wait

    while True:
        room, messages = await _room_and_messages_since(room_id, since)
        if room is None:
            raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
        if messages or room.status != "open" or time.monotonic() >= deadline:
            return room, messages
        await asyncio.sleep(POLL_INTERVAL_SECS)
