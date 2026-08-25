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
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.attachments import release_blobs_for_deleted_room
from app.auth import Principal
from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.models import Room, RoomAttachment, RoomMember, RoomMessage
from app.notify import notify_room_closed
from app.room_modes import DEFAULT_MODE, ROOM_MODES, role_text_for, validate_mode

logger = logging.getLogger(__name__)

# --- room creation ---

MAX_MESSAGES_MIN = 1
MAX_MESSAGES_MAX = 10000
DEFAULT_MAX_MESSAGES = 100
REQUIRED_MEMBER_COUNT = 2

# ADR-0007: optional wall-clock deadline. Either `duration_seconds` or an
# explicit `expires_at` may be given (not both); 30 days is a sane upper
# bound on how long an unattended room may run for.
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 30 * 24 * 3600

# ADR-0008: free-form room group label. Owner-supplied free text, sane upper
# bound just to keep it a short label, not a paragraph.
MAX_GROUP_LENGTH = 100

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


def _validate_topic(mode: str, topic: str | None) -> str | None:
    """Non-freeform modes require a non-empty topic -- it's interpolated
    into every mode's role text (app/room_modes.py). freeform ignores it:
    accepted if given (stored, harmless) but never required, since
    freeform's role text is None and never reads it.
    """
    cleaned = topic.strip() if isinstance(topic, str) else None
    if mode != DEFAULT_MODE and not cleaned:
        raise ApiError(
            422,
            "missing_room_topic",
            f"`topic` is required and must be non-empty for mode {mode!r}. Recovery: resend with a "
            "non-empty `topic`.",
        )
    return cleaned or None


def _validate_group(group: str | None) -> str | None:
    """ADR-0008: a room's optional free-form group label. Trims whitespace;
    blank/empty (after trim) means "no group", same as `_validate_topic`'s
    empty-becomes-None convention. Enforces a sane max length -- this is a
    short label, not a paragraph. Used both at room creation and by the
    bulk `assign_group_to_rooms` below, so the two never drift on what
    counts as a valid group value.
    """
    if group is None:
        return None
    if not isinstance(group, str):
        raise ApiError(422, "invalid_room_group", f"`group` must be a string or null, got {group!r}.")
    cleaned = group.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_GROUP_LENGTH:
        raise ApiError(
            422,
            "invalid_room_group",
            f"`group` is {len(cleaned)} characters, exceeding the {MAX_GROUP_LENGTH}-character cap. "
            "Recovery: shorten it, resend.",
        )
    return cleaned


def _validate_sides(mode: str, members: list[str], sides: dict[str, str] | None) -> dict[str, str | None]:
    """Symmetric modes (freeform, collaborate, brainstorm) ignore `sides`
    entirely -- every member's side is None. Asymmetric modes (debate,
    critique) require `sides` to assign each of the mode's two distinct
    side keys (app/room_modes.py's `ROOM_MODES[mode].sides`) to exactly one
    of the room's two members.
    """
    mode_def = ROOM_MODES[mode]
    if mode_def.sides is None:
        return dict.fromkeys(members)

    expected_sides = set(mode_def.sides)
    given = sides or {}
    if set(given.keys()) != set(members) or set(given.values()) != expected_sides:
        raise ApiError(
            422,
            "invalid_room_sides",
            f"`sides` must assign exactly one of {sorted(expected_sides)} to each of {members} for mode "
            f"{mode!r}, got {given!r}. Recovery: resend `sides` as {{member_name: side}} covering both "
            "members with both distinct side values.",
        )
    return dict(given)


def _validate_deadline(now: datetime, duration_seconds: int | None, expires_at: datetime | None) -> datetime | None:
    """Either `duration_seconds` (computes `now + duration_seconds`) or an
    explicit `expires_at` may be given, not both. Returns None (no
    deadline) if neither is given. Enforces the deadline is in the future
    and within MAX_DURATION_SECONDS (30 days) of now either way, so the two
    input forms end up validated identically.
    """
    if duration_seconds is not None and expires_at is not None:
        raise ApiError(
            422,
            "invalid_room_deadline",
            "Provide at most one of `duration_seconds` or `expires_at`, not both. Recovery: resend with "
            "only one of the two set.",
        )

    if duration_seconds is not None:
        if (
            not isinstance(duration_seconds, int)
            or isinstance(duration_seconds, bool)
            or not (MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS)
        ):
            raise ApiError(
                422,
                "invalid_room_deadline",
                f"`duration_seconds` must be an integer between {MIN_DURATION_SECONDS} and "
                f"{MAX_DURATION_SECONDS} (30 days), got {duration_seconds!r}.",
            )
        return now + timedelta(seconds=duration_seconds)

    if expires_at is not None:
        resolved = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
        if resolved <= now:
            raise ApiError(
                422,
                "invalid_room_deadline",
                f"`expires_at` must be in the future, got {resolved.isoformat()!r}.",
            )
        if resolved > now + timedelta(seconds=MAX_DURATION_SECONDS):
            raise ApiError(
                422,
                "invalid_room_deadline",
                f"`expires_at` is more than {MAX_DURATION_SECONDS // 86400} days out, got "
                f"{resolved.isoformat()!r}. Recovery: pick a closer deadline.",
            )
        return resolved

    return None


async def create_room(
    db: AsyncSession,
    name: str,
    members: list[str],
    max_messages: int | None,
    *,
    mode: str = DEFAULT_MODE,
    topic: str | None = None,
    sides: dict[str, str] | None = None,
    duration_seconds: int | None = None,
    expires_at: datetime | None = None,
    group: str | None = None,
) -> Room:
    """ADR-0007 extends room creation with an optional mode+topic (shapes
    the join prompt's injected role text, app/onboarding.py) and an
    optional deadline (`duration_seconds` or `expires_at`, enforced by the
    background sweeper, app/room_sweeper.py). All new validation is
    self-explaining ApiErrors, same posture as the phase A validators
    above; mode/topic/sides are cross-validated together since which
    `sides` shape is valid depends on the mode.

    ADR-0008 further extends it with an optional free-form `group` label
    (stored as `Room.group_name` -- 'group' is a SQL reserved word),
    validated by the same `_validate_group` the bulk `assign_group_to_rooms`
    below uses.
    """
    if not name or not name.strip():
        raise ApiError(422, "invalid_room_name", "`name` must be non-empty. Recovery: resend with a non-empty name.")
    cleaned_members = _validate_members(members)
    resolved_max = _validate_max_messages(max_messages)
    validate_mode(mode)
    cleaned_topic = _validate_topic(mode, topic)
    member_sides = _validate_sides(mode, cleaned_members, sides)
    cleaned_group = _validate_group(group)

    now = datetime.now(UTC)
    resolved_expires_at = _validate_deadline(now, duration_seconds, expires_at)

    room = Room(
        id=str(ULID()),
        name=name.strip(),
        status="open",
        max_messages=resolved_max,
        message_count=0,
        notify_on_close=True,
        # ADR-0012 decision 7: always created allowed (the checkbox is an
        # opt-out) -- no create-time parameter for it, same posture
        # `notify_on_close` already has here; toggled mid-room instead via
        # `set_agent_uploads_allowed` below.
        agent_uploads_allowed=True,
        created_at=now,
        mode=mode,
        topic=cleaned_topic,
        expires_at=resolved_expires_at,
        group_name=cleaned_group,
    )
    db.add(room)
    await db.flush()  # room.id must exist before the member rows FK to it

    for agent_name in cleaned_members:
        db.add(
            RoomMember(
                id=str(ULID()),
                room_id=room.id,
                agent_name=agent_name,
                created_at=now,
                side=member_sides.get(agent_name),
            )
        )

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


async def get_member_sides(db: AsyncSession, room_id: str) -> dict[str, str | None]:
    """ADR-0007: `{agent_name: side}` for a room's members -- `side` is None
    for symmetric/freeform members. A separate function from `get_members`
    (list[str], unchanged) rather than changing that function's return
    shape, so the phase A callers that already depend on `get_members`
    returning plain names (post_message's membership check, ui_rooms.py's
    templates -- UI is out of scope for this part) are untouched.
    """
    rows = (
        await db.execute(
            select(RoomMember.agent_name, RoomMember.side)
            .where(RoomMember.room_id == room_id)
            .order_by(RoomMember.created_at)
        )
    ).all()
    return {agent_name: side for agent_name, side in rows}


async def get_member_sides_for_rooms(db: AsyncSession, room_ids: list[str]) -> dict[str, dict[str, str | None]]:
    """Batched `get_member_sides` for GET /v1/rooms (list) -- one query for
    the whole page, mirroring `get_members_for_rooms`.
    """
    if not room_ids:
        return {}
    rows = (
        await db.execute(
            select(RoomMember.room_id, RoomMember.agent_name, RoomMember.side)
            .where(RoomMember.room_id.in_(room_ids))
            .order_by(RoomMember.created_at)
        )
    ).all()
    result: dict[str, dict[str, str | None]] = {}
    for room_id, agent_name, side in rows:
        result.setdefault(room_id, {})[agent_name] = side
    return result


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


async def get_all_messages(db: AsyncSession, room_id: str) -> list[RoomMessage]:
    """Every message in the room, oldest-first (chat reading order) -- used
    by the transcript export (app/room_export.py) and the AI actions
    (app/room_ai.py, ADR-0011), unlike `get_recent_messages` above (bounded
    to the most recent N, used by the live view's initial render). Never
    truly unbounded: a room's message count is capped at MAX_MESSAGES_MAX
    (10000) by `_validate_max_messages` at create time.
    """
    rows = (
        await db.scalars(select(RoomMessage).where(RoomMessage.room_id == room_id).order_by(RoomMessage.seq))
    ).all()
    return list(rows)


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


async def list_rooms(
    db: AsyncSession, *, cursor: str | None = None, limit: int = 20, group: str | None = None
) -> tuple[list[Room], str | None]:
    """ADR-0008: `group`, when given, exact-matches `Room.group_name` --
    e.g. so an observer AI can be pointed at just one group's rooms
    (GET /v1/rooms?group=X). `None` (the default) means "no filter", not
    "rooms with no group"; there is no way to query for ungrouped rooms
    specifically via this parameter, matching the ADR's stated shape.
    """
    stmt = select(Room).order_by(Room.created_at.desc(), Room.id.desc())
    if group is not None:
        stmt = stmt.where(Room.group_name == group)
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


async def list_room_groups(db: AsyncSession) -> list[str]:
    """Distinct, non-null group labels currently in use, sorted -- backs the
    UI's group filter dropdown/datalist (app/routers/ui_rooms.py) so the
    owner can pick from existing groups rather than retyping one by hand.
    """
    rows = (
        await db.scalars(
            select(Room.group_name).where(Room.group_name.is_not(None)).distinct().order_by(Room.group_name)
        )
    ).all()
    return list(rows)


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


async def post_message(
    db: AsyncSession, room_id: str, sender: str, text: str, kind: str = "message", *, principal: Principal
) -> tuple[RoomMessage, Room]:
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

    `principal` (the caller's AUTHENTICATED identity, from app/auth.py) is
    required and is what fixes the same `sender="owner"` impersonation bug
    app/attachments.py's `_clean_sender` fixes: `sender` is a bare,
    client-supplied string with no identity of its own, so before this fix
    any machine token could post a message with `sender="owner"` and have
    it recorded -- and rendered in the transcript -- as coming from the
    owner. `sender == "owner"` is now only accepted when
    `principal.kind == "owner"`; every other `sender` (a room member's
    `agent_name`) is unaffected -- machine tokens are deliberately not
    bound to one agent identity in this system, so the membership check
    just below remains the only, and sufficient, guard for that case.
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
    if sender == "owner" and principal.kind != "owner":
        raise ApiError(
            403,
            "owner_sender_requires_owner_token",
            "`sender=owner` can only be claimed by a request authenticated with the owner token. Recovery: "
            "authenticate as the owner, or post as this room's own member `agent_name` instead.",
        )
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

VALID_CLOSE_REASONS = frozenset({"done", "owner", "cap", "stall", "time"})


async def close_room(db: AsyncSession, room_id: str, reason: str | None) -> Room:
    """Closes a room (owner Stop, or the sweeper's time-close --
    app/room_sweeper.py -- reusing this same function, never hand-rolling a
    close).

    Takes the room's row lock -- same `SELECT ... FOR UPDATE` +
    `populate_existing` pattern `post_message` and `post_closing_nudge` use
    in this file -- before checking/applying the close, for the identical
    reason `post_message`'s cap guardrail documents: without the lock, two
    racing closers (an owner Stop racing the sweeper's time-close, or the
    sweeper racing a concurrent 'done'/cap close from `post_message`, or
    two owner Stops) could both read `status == 'open'` before either
    commits, and the second writer's `_apply_close` would then blindly
    overwrite the first winner's `close_reason`/`closed_at` and fire
    `notify_room_closed` a second time with contradictory content. Holding
    the lock across the re-check and the close makes "room closes exactly
    once, with the first winner's reason, and notifies exactly once" true
    under concurrency, not just in the common case.
    """
    if reason is not None and reason not in VALID_CLOSE_REASONS:
        raise ApiError(
            422,
            "invalid_close_reason",
            f"`reason` must be one of {sorted(VALID_CLOSE_REASONS)}, got {reason!r}.",
        )
    # Unlocked pre-check: existence never needs the row lock to be correct
    # (mirrors post_message's own "unlocked pre-checks" reasoning) -- a 404
    # for a genuinely nonexistent room doesn't need serializing on anything.
    room = await db.get(Room, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")

    # Serialization point: acquire the room's row lock. A concurrent closer
    # that got here first holds this lock until its commit (inside
    # `_apply_close` below); this call blocks until then, so this call
    # always sees the *previous* closer's committed status/close_reason,
    # never a stale snapshot. `populate_existing` is required in addition
    # to `with_for_update` for the same reason `post_message` documents:
    # `room` may already be in this Session's identity map from the
    # unlocked `db.get` above, and without it SQLAlchemy would leave that
    # already-loaded object's attributes untouched even though the FOR
    # UPDATE SQL below legitimately re-fetches newer committed data.
    room = await db.scalar(
        select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
    )
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
    # Re-check status under the lock: another concurrent close (owner Stop,
    # the sweeper's time-close, or post_message's own done/cap guardrail
    # close) may have closed the room in the window between the unlocked
    # check above and acquiring this lock -- this is the check that
    # actually has to be race-free, and it now is.
    if room.status == "closed":
        return room  # idempotent: closing an already-closed room just returns its state

    await _apply_close(db, room, reason or "owner")
    return room


# --- server-authored system messages (ADR-0007: the sweeper's closing nudge) ---


async def post_closing_nudge(db: AsyncSession, room_id: str, text: str) -> RoomMessage | None:
    """Posts the sweeper's one-time "closing soon" `kind='system'` message
    (app/room_sweeper.py) under the room's row lock -- same
    `SELECT ... FOR UPDATE` + `populate_existing` pattern `post_message`
    uses -- and sets `closing_warned_at` in that SAME commit. Checking
    "not already warned" and setting `closing_warned_at` under one lock (as
    opposed to the sweeper's earlier unlocked scan-for-candidates query,
    which is only ever a hint) is what actually prevents a double-post if
    two sweep cycles overlap or a future multi-worker deployment races this
    room -- the same reasoning `post_message`'s cap guardrail documents for
    why its close has to happen under the lock, before the one commit.

    Returns None (posts nothing) if, by the time the lock is acquired, the
    room is missing, already closed, or already warned -- there's no
    sender to report an ApiError to here, unlike `post_message`; the caller
    just treats "not eligible anymore" as a no-op for this room this cycle.

    Deliberately bypasses the done/cap guardrails `post_message` applies
    (this must never trip the hard cap right as the room is being kept
    open long enough to receive closing statements) -- it still increments
    `message_count` and takes the next `seq`, so the message appears in the
    transcript like any other, just without guardrail side effects.
    """
    room = await db.scalar(
        select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
    )
    if room is None or room.status != "open" or room.closing_warned_at is not None:
        return None

    now = datetime.now(UTC)
    seq = await _next_seq(db, room.id)
    message = RoomMessage(id=str(ULID()), room_id=room.id, seq=seq, sender="system", text=text, kind="system", created_at=now)
    db.add(message)
    room.message_count += 1
    room.closing_warned_at = now
    await db.commit()
    return message


# --- POST /v1/rooms/{id}/mode (ADR-0009: mid-session, owner-only mode switch) ---


def _mode_switch_announcement(
    mode: str, topic: str | None, members: list[str], member_sides: dict[str, str | None]
) -> str:
    """The kind='system' announcement text `switch_room_mode` posts (ADR-0009
    decision 2): "Mode switched to <label>.[ Topic: <topic>.][ <per-agent or
    shared stance line(s)>]". Every word of stance text is drawn from
    app/room_modes.py's single-sourced `role_text_for` -- never duplicated
    here -- same discipline app/onboarding.py's `_room_session_block` uses
    for the join prompt's own stance paragraph.

    Asymmetric target (debate/critique): one line per member, in `members`
    order, "<agent> (<side label>): <that member's real per-side role text,
    with the *other* member as the real partner name>" -- identical stance
    text to what that agent's own join prompt would show for this mode and
    side.

    Symmetric non-freeform target (collaborate/brainstorm): a single "Both
    agents: <shared role text>" line. `role_text_for` is written from one
    agent's perspective and needs a partner name to fill into its template
    (e.g. "Collaborate with {partner} to ..."); since this one line
    addresses both members at once, "the other agent" stands in for a real
    name here -- the only member-name substitution in this whole
    announcement that isn't a real agent name, and deliberately so: there is
    no single "the partner" for a line describing both members together.

    Freeform target: `role_text_for` returns None (no stance text, same as
    the join prompt) -- the announcement is just the mode (plus topic, if
    one happens to be stored for it).
    """
    mode_def = ROOM_MODES[mode]
    parts = [f"Mode switched to {mode_def.label}."]
    if topic:
        parts.append(f"Topic: {topic}.")

    if mode_def.role_text is None:
        pass  # freeform: no special stance text, same as the join prompt
    elif not mode_def.symmetric:
        agent_a, agent_b = members[0], members[1]
        side_a, side_b = member_sides[agent_a], member_sides[agent_b]
        stance_a = role_text_for(mode, side_a, topic or "", agent_b)
        stance_b = role_text_for(mode, side_b, topic or "", agent_a)
        parts.append(f"{agent_a} ({mode_def.side_labels[side_a]}): {stance_a}")
        parts.append(f"{agent_b} ({mode_def.side_labels[side_b]}): {stance_b}")
    else:
        shared_stance = role_text_for(mode, None, topic or "", "the other agent")
        parts.append(f"Both agents: {shared_stance}")

    return " ".join(parts)


async def switch_room_mode(
    db: AsyncSession, room_id: str, mode: str, topic: str | None, sides: dict[str, str] | None
) -> tuple[Room, str]:
    """ADR-0009: mid-session, owner-only mode switch (app/routers/rooms.py's
    POST /v1/rooms/{id}/mode, and its UI mirror, app/routers/ui_rooms.py's
    POST /ui/rooms/{id}/switch-mode). Reuses `validate_mode`/`_validate_topic`/
    `_validate_sides` -- the exact same validators `create_room` uses -- so a
    switch can never land a room in a mode/topic/sides combination
    `create_room` itself would have rejected.

    Takes the room's row lock -- same `SELECT ... FOR UPDATE` +
    `populate_existing` pattern `post_message`/`close_room`/
    `post_closing_nudge`/`delete_room` all use in this file -- across the
    mode/topic/side update AND the system announcement post, in one commit:
    two concurrent switches (or a switch racing a post/close/delete) always
    serialize on this lock, so no writer ever observes a half-applied switch
    (mode changed but sides not yet updated) or clobbers another writer's
    concurrent change. Room membership (the two `RoomMember` rows) is
    re-read and `_validate_sides`-checked only *after* the lock is acquired
    -- deliberately later than this file's other mutators re-validate their
    own preconditions, because unlike a sender-membership check (immutable
    after create), this function is itself what mutates member rows: reading
    `members` before the lock would risk observing a concurrent
    `delete_room`'s already-committed, mid-flight-emptied membership and
    misreporting a spurious "bad sides" 422 instead of the correct 404. By
    the time this call holds the lock, `delete_room`'s own lock acquisition
    has either already fully committed (room gone -> the 404 just below
    fires) or is still blocked behind this call -- `members` read under the
    lock is therefore always either "the room's real, still-existing two
    members" or unreachable, never a torn in-between read.

    Only an open room may switch (self-explaining 'room_closed', re-checked
    under the lock for the same race reason `close_room`'s own status
    re-check documents). The announcement is posted the same way
    `post_closing_nudge` posts the sweeper's nudge: it takes the next `seq`
    and increments `message_count` (so it counts toward the room's cap, per
    ADR-0009 decision 3) but deliberately bypasses `post_message`'s done/cap
    guardrail check -- a mode switch must never itself trip the cap and
    silently close the room it just changed.

    Returns (room, announcement_text) -- the announcement is returned
    directly (not just left to be read back off the room) so callers (the
    API response, the UI route, tests) don't have to re-fetch the
    just-posted message.
    """
    validate_mode(mode)
    cleaned_topic = _validate_topic(mode, topic)

    # Unlocked pre-check: existence never needs the row lock to be correct
    # (mirrors close_room's/delete_room's own "unlocked pre-checks"
    # reasoning) -- a 404 for a genuinely nonexistent room doesn't need
    # serializing on anything.
    room = await db.get(Room, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")

    # Serialization point: acquire the room's row lock, same pattern as
    # every other mutator in this file. `populate_existing` is required for
    # the identical reason those functions document: `room` may already be
    # in this Session's identity map from the unlocked `db.get` above, and
    # without it SQLAlchemy would leave that already-loaded object's
    # attributes untouched even though the FOR UPDATE SQL below legitimately
    # re-fetches newer committed data.
    room = await db.scalar(
        select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
    )
    if room is None:
        # A concurrent delete_room won the race and already committed
        # between the unlocked pre-check and acquiring this lock.
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
    # Re-check status under the lock: another concurrent close (owner Stop,
    # the sweeper's time-close, or post_message's own done/cap guardrail
    # close) may have closed the room in the window between the unlocked
    # check above and acquiring this lock -- this is the check that
    # actually has to be race-free, and it now is.
    if room.status != "open":
        raise ApiError(
            409,
            "room_closed",
            f"Room '{room.name}' is closed (reason: {room.close_reason}); its mode can no longer be "
            "switched. Recovery: start a new room.",
        )

    # Members are read (and `sides` validated) only now, under the lock --
    # see this function's docstring for why that ordering matters.
    members = await get_members(db, room_id)
    member_sides = _validate_sides(mode, members, sides)

    member_rows = (await db.scalars(select(RoomMember).where(RoomMember.room_id == room_id))).all()
    for member_row in member_rows:
        member_row.side = member_sides.get(member_row.agent_name)

    room.mode = mode
    room.topic = cleaned_topic

    announcement_text = _mode_switch_announcement(mode, cleaned_topic, members, member_sides)
    now = datetime.now(UTC)
    seq = await _next_seq(db, room.id)
    db.add(
        RoomMessage(
            id=str(ULID()),
            room_id=room.id,
            seq=seq,
            sender="system",
            text=announcement_text,
            kind="system",
            created_at=now,
        )
    )
    room.message_count += 1
    # Deliberately no done/cap guardrail check here -- see docstring.

    await db.commit()
    return room, announcement_text


# --- POST /v1/rooms/{id}/agent-uploads (ADR-0012 decisions 7/9: owner-only,
# mid-session toggle of the room's agent-upload switch) ---


def _agent_uploads_announcement(allowed: bool) -> str:
    """The kind='system' announcement text `set_agent_uploads_allowed` posts
    (ADR-0012 decision 9: "agents are briefed, not just refused"). Disabling
    is phrased as an explicit directive (do not generate/upload, put content
    in a message instead) rather than a bare status line, and reminds
    agents that decision 8's Attach-from-Brain path is unaffected either
    way -- the same "state the policy, don't just rely on the API 404/403
    backstop" posture the room-join prompt takes for this same switch.
    """
    if allowed:
        return "Agent file uploads are now allowed in this room."
    return (
        "Agent file uploads are now disabled in this room. Agents: do not generate or upload a file -- put "
        "the content directly in a room message instead. You may still attach a document already saved in "
        "the Brain (Attach from Brain) even while this is off."
    )


async def set_agent_uploads_allowed(db: AsyncSession, room_id: str, allowed: bool) -> tuple[Room, str]:
    """ADR-0012 decisions 7/9: owner-only mid-session toggle of
    `Room.agent_uploads_allowed`. Consistent with how `switch_room_mode`
    (above) toggles mode mid-session -- identical `SELECT ... FOR UPDATE` +
    `populate_existing` row lock, held across the update AND the
    kind='system' announcement post, both in the SAME commit, so a
    concurrent toggle/post/close can never observe a half-applied change or
    clobber this one. Always posts an announcement, even if `allowed`
    already matched the room's current value -- same unconditional-post
    posture `switch_room_mode` takes for mode/topic, rather than special-
    casing a no-op call.

    Only an open room may be toggled (self-explaining 'room_closed', same
    posture as `switch_room_mode`) -- a closed room accepts no further
    attachments at all regardless of this switch
    (`add_room_attachment`'s own 'room_closed' guardrail), so there is
    nothing left to toggle.
    """
    if not isinstance(allowed, bool):
        raise ApiError(422, "invalid_agent_uploads_allowed", f"`allowed` must be a boolean, got {allowed!r}.")

    # Unlocked pre-check: existence never needs the row lock to be correct
    # (mirrors switch_room_mode's/close_room's own "unlocked pre-checks"
    # reasoning) -- a 404 for a genuinely nonexistent room doesn't need
    # serializing on anything.
    room = await db.get(Room, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")

    # Serialization point: acquire the room's row lock, same pattern as
    # every other mutator in this file. `populate_existing` is required for
    # the identical reason those functions document: `room` may already be
    # in this Session's identity map from the unlocked `db.get` above, and
    # without it SQLAlchemy would leave that already-loaded object's
    # attributes untouched even though the FOR UPDATE SQL below legitimately
    # re-fetches newer committed data.
    room = await db.scalar(
        select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
    )
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
    if room.status != "open":
        raise ApiError(
            409,
            "room_closed",
            f"Room '{room.name}' is closed; its agent-upload setting can no longer be changed.",
        )

    room.agent_uploads_allowed = allowed
    announcement_text = _agent_uploads_announcement(allowed)

    now = datetime.now(UTC)
    seq = await _next_seq(db, room.id)
    db.add(
        RoomMessage(
            id=str(ULID()),
            room_id=room.id,
            seq=seq,
            sender="system",
            text=announcement_text,
            kind="system",
            created_at=now,
        )
    )
    room.message_count += 1
    # Deliberately no done/cap guardrail check here -- same reasoning
    # switch_room_mode's own docstring gives: this must never itself trip
    # the cap and silently close the room it just changed.

    await db.commit()
    return room, announcement_text


# --- ADR-0012 stage 3: kind='system' announcements when an attachment is
# added to or removed from a room, so an agent long-polling the room learns
# about the change without a new channel (mirrors the toggle announcement
# above -- same reasoning, a different trigger). ---


def _attachment_added_text(filename: str, byte_size: int, uploaded_by: str) -> str:
    """`filename` is never raw user input by the time it reaches here: every
    caller passes an already-committed `RoomAttachment.filename`, which
    app/attachments.py ran through app/room_export.py's
    `safe_filename_component` before storing it -- a small ASCII allowlist
    that excludes `"` and every control/format/line-separator character, so
    it can never break out of the quotes below or forge a fake extra
    message/line in the transcript.
    """
    return f'Attachment added: "{filename}" ({byte_size} bytes), by {uploaded_by}.'


def _attachment_removed_text(filename: str, removed_by: str) -> str:
    """Same filename-safety guarantee as `_attachment_added_text` above."""
    return f'Attachment removed: "{filename}", by {removed_by}.'


async def _post_attachment_system_message(db: AsyncSession, room_id: str, text: str) -> RoomMessage | None:
    """Shared poster for both announcement texts above. Deliberately its own
    standalone locked transaction rather than sharing the one
    app/attachments.py's add/delete functions already committed: this file
    imports app.attachments (`release_blobs_for_deleted_room`), so the
    reverse import would be circular, and every attachment mutation already
    fully commits on its own before any caller of this function runs -- so
    there is no partial state to protect by trying to merge the two into one
    transaction anyway. Same `SELECT ... FOR UPDATE` + `populate_existing`
    room lock as `switch_room_mode`/`set_agent_uploads_allowed` above.

    Best-effort: the attachment add/delete this announces already succeeded
    and committed by the time this runs, so a failure here (room gone, a
    transient DB error, ...) is logged and swallowed rather than turned into
    a 500 for a request whose real work already succeeded -- same posture
    app/notify.py's `notify_room_closed` takes for the best-effort ntfy ping
    on room close. Returns the posted message, or None if nothing was
    posted (room not found, or the post itself failed).
    """
    try:
        room = await db.scalar(
            select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
        )
        if room is None:
            return None
        now = datetime.now(UTC)
        seq = await _next_seq(db, room.id)
        message = RoomMessage(id=str(ULID()), room_id=room.id, seq=seq, sender="system", text=text, kind="system", created_at=now)
        db.add(message)
        room.message_count += 1
        await db.commit()
        return message
    except Exception:
        await db.rollback()
        logger.exception(
            "best-effort attachment system-message post failed for room %s -- attachment operation still succeeded",
            room_id,
        )
        return None


async def post_attachment_added_message(
    db: AsyncSession, room_id: str, *, filename: str, byte_size: int, uploaded_by: str
) -> RoomMessage | None:
    """Called by app/routers/room_attachments.py and app/routers/ui_rooms.py
    after a successful upload or Attach-from-Brain, so agents long-polling
    this room learn a file was added without a new channel.
    """
    return await _post_attachment_system_message(
        db, room_id, _attachment_added_text(filename, byte_size, uploaded_by)
    )


async def post_attachment_removed_message(
    db: AsyncSession, room_id: str, *, filename: str, removed_by: str
) -> RoomMessage | None:
    """Called by app/routers/room_attachments.py and app/routers/ui_rooms.py
    after a successful owner delete of an attachment.
    """
    return await _post_attachment_system_message(db, room_id, _attachment_removed_text(filename, removed_by))


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


# --- DELETE /v1/rooms/{id} (ADR-0008: owner-only hard delete) ---


async def delete_room(db: AsyncSession, room_id: str) -> tuple[int, int]:
    """Hard-deletes a room and everything under it: its messages, then its
    members, then its attachment references, then the room row itself, all
    in one transaction (one commit). Returns (messages_deleted,
    members_deleted).

    EXPLICIT application-level cascade, not FK ondelete=CASCADE: `RoomMember.
    room_id` and `RoomMessage.room_id` (app/models.py) are plain
    `ForeignKey("rooms.id")` with no `ondelete` set, so Postgres's default FK
    action (NO ACTION/RESTRICT) would otherwise reject deleting a room that
    still has member/message rows. Deleting children first, then the room,
    in this one function/transaction is ADR-0008's own suggested alternative
    to an ondelete=CASCADE migration -- chosen here so the "a room's rows are
    gone" invariant is enforced in the one place (this function) that every
    delete path (API, UI) goes through, same as every other multi-row
    guardrail in this module, rather than relying on schema-level cascade
    semantics a reader would have to go find in the migration.

    ADR-0012 gap closed here: `RoomAttachment.room_id` IS `ondelete="CASCADE"`
    (a deliberate, narrow, documented exception to the convention above --
    see that model's docstring) because this function predates the
    attachments table and didn't know it existed. DB-level cascade alone
    dropped the reference rows fine but never ran the reference-counted
    blob sweep, orphaning any blob that only this room referenced (row and
    bytes both left on disk with nothing pointing at them). Fixed by
    deleting `room_attachments` explicitly, same as `room_messages`/
    `room_members` above (making the CASCADE a harmless no-op backstop
    instead of the only thing standing between this delete and a 500,
    exactly the follow-up that model's docstring calls for), and -- after
    this transaction commits -- eagerly running
    `app.attachments.release_blobs_for_deleted_room` against the blob
    hashes this room used to reference. That reuses the exact same
    reference-counted check `sweep_expired_blobs` runs (a blob survives if
    ANY other room still references it, open or within its own grace
    period, or a Brain `MirroredDocument` references it) -- the only thing
    special about a hard delete is that THIS room's own reference is gone
    immediately, with no grace period of its own to wait out (ADR-0012's
    `RoomAttachment` docstring: "a hard delete is a stronger, more
    deliberate owner action than a room merely closing, so losing the
    grace-period cushion on explicit delete is expected, not a bug").

    Rooms are transient chat, not curated knowledge (ADR-0008 decision 2):
    this is a genuine hard delete, not a status flip -- there is no
    "undelete". Works on an open OR closed room; deleting is always the
    owner's call regardless of room status.

    404s (rather than silently no-op'ing) for an unknown/already-deleted id
    -- "delete this specific room" on a room that isn't there is a rejection,
    not a no-op, same posture as `close_room`'s and `post_message`'s own
    404s for a nonexistent room_id.

    Takes the room's row lock -- same `SELECT ... FOR UPDATE` +
    `populate_existing` pattern `post_message`/`close_room`/
    `post_closing_nudge` use -- before deleting anything. Without it, a
    concurrent `post_message`/`post_closing_nudge` racing this delete could
    insert a new `room_messages` row (or member row) in the window after
    this function's child-deletes ran but before the room row itself was
    deleted, which would either violate the room row's referencing FK on
    delete or, worse, leave an orphaned message pointing at a room that's
    about to vanish. Holding the lock across the unlocked existence
    pre-check's re-fetch and every delete in this function serializes
    delete_room against every other writer of this room's row (or its
    children): a concurrent inserter either lands and commits before this
    lock is acquired (its message/member row is simply deleted here right
    afterward, same as any pre-existing row), or blocks on the room's lock
    until this transaction commits, at which point its own room_id lookup
    (`post_message`'s/`post_closing_nudge`'s own `db.get`/lock re-check)
    finds the room gone and reports its own clean 404/no-op -- never a raw
    IntegrityError, and never a message/member row left behind. The
    post-commit blob reclaim below is deliberately OUTSIDE this lock (it
    takes its own, per-blob locks, same as `sweep_expired_blobs`) -- the
    room row is already gone by then, so nothing about it needs protecting
    any further.
    """
    # Unlocked pre-check: existence never needs the row lock to be correct
    # (mirrors post_message's/close_room's own "unlocked pre-checks"
    # reasoning) -- a 404 for a genuinely nonexistent room doesn't need
    # serializing on anything.
    room = await db.get(Room, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")

    # Serialization point: acquire the room's row lock, same pattern as
    # post_message/close_room/post_closing_nudge. `populate_existing` is
    # required for the identical reason those functions document: `room`
    # may already be in this Session's identity map from the unlocked
    # `db.get` above, and without it SQLAlchemy would leave that
    # already-loaded object's attributes untouched even though the FOR
    # UPDATE SQL below legitimately re-fetches newer committed data.
    room = await db.scalar(
        select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
    )
    if room is None:
        # Another concurrent delete_room won the race and already committed
        # between the unlocked pre-check and acquiring this lock.
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")

    # Captured BEFORE the delete below removes these rows -- this is the
    # only chance to know which blobs this room used to reference; ADR-0012.
    attachment_hashes = list(
        (await db.scalars(select(RoomAttachment.blob_sha256).where(RoomAttachment.room_id == room_id))).all()
    )

    messages_deleted = (await db.execute(delete(RoomMessage).where(RoomMessage.room_id == room_id))).rowcount
    members_deleted = (await db.execute(delete(RoomMember).where(RoomMember.room_id == room_id))).rowcount
    # Explicit, same as messages/members above -- the FK's ondelete=CASCADE
    # (app/models.py's RoomAttachment) is now a harmless backstop, not the
    # only thing preventing an FK violation on the room delete below.
    await db.execute(delete(RoomAttachment).where(RoomAttachment.room_id == room_id))
    await db.delete(room)
    await db.commit()

    if attachment_hashes:
        # Deliberately after the commit above, same ordering rationale
        # app.attachments.sweep_expired_blobs documents for its own file
        # removal: the room (and its attachment references) are already
        # durably gone at this point, so the worst crash outcome here is an
        # orphaned blob a later sweep pass still cleans up -- never a
        # dangling reference to bytes that no longer exist.
        await release_blobs_for_deleted_room(db, attachment_hashes)

    return messages_deleted, members_deleted


# --- POST /v1/rooms/assign-group (ADR-0008: bulk group assignment) ---

MAX_BULK_ASSIGN_ROOMS = 500


async def assign_group_to_rooms(db: AsyncSession, room_ids: list[str], group: str | None) -> tuple[int, str | None]:
    """Bulk-sets (or clears, if `group` is null/blank) `group_name` on every
    room in `room_ids`, in one transaction. Returns (updated_count,
    resolved_group) -- `resolved_group` is the actual value applied (after
    `_validate_group`'s trim/blank-to-None), so callers building a response
    never have to re-derive it themselves.

    Validates every id exists BEFORE writing anything: a partial bulk-assign
    (some rooms updated, others silently skipped because the id was a typo)
    would be a confusing, hard-to-notice failure mode for an owner selecting
    rooms in the UI, so this rejects the whole call with a self-explaining
    404 listing exactly which ids were not found -- same "reject clearly
    rather than partially apply" posture as e.g. deposits.py's per-item
    validation. Uses a single UPDATE ... WHERE id IN (...) for the write
    itself, not a per-room loop, since every included id is already known
    (checked above) to exist.
    """
    if not isinstance(room_ids, list) or not room_ids:
        raise ApiError(
            422,
            "invalid_room_ids",
            f"`room_ids` must be a non-empty list of room id strings, got {room_ids!r}. "
            "Recovery: resend with at least one room id.",
        )
    if len(room_ids) > MAX_BULK_ASSIGN_ROOMS:
        raise ApiError(
            422,
            "invalid_room_ids",
            f"`room_ids` has {len(room_ids)} entries, exceeding the {MAX_BULK_ASSIGN_ROOMS}-room cap per call. "
            "Recovery: split into smaller batches.",
        )
    cleaned_ids = [r.strip() if isinstance(r, str) else r for r in room_ids]
    if any(not isinstance(r, str) or not r for r in cleaned_ids):
        raise ApiError(
            422,
            "invalid_room_ids",
            f"`room_ids` must contain only non-empty room id strings, got {room_ids!r}.",
        )
    unique_ids = sorted(set(cleaned_ids))

    cleaned_group = _validate_group(group)

    existing_ids = set((await db.scalars(select(Room.id).where(Room.id.in_(unique_ids)))).all())
    missing_ids = [id_ for id_ in unique_ids if id_ not in existing_ids]
    if missing_ids:
        raise ApiError(
            404,
            "unknown_room_ids",
            f"{len(missing_ids)} of {len(unique_ids)} `room_ids` do not exist: {missing_ids}. "
            "Recovery: remove the unknown ids, or resend with only existing room ids.",
            extra={"unknown_ids": missing_ids},
        )

    await db.execute(update(Room).where(Room.id.in_(unique_ids)).values(group_name=cleaned_group))
    await db.commit()
    return len(unique_ids), cleaned_group
