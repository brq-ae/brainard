"""Shared flag read/resolve logic -- GET /v1/flags, POST /v1/flags/{id}/resolve
(contracts-v1.md §3; ADR-0004: "the librarian's inbox is fully specified by
the contracts themselves: duplicate hints, fork flags...").

Flags are raised by app/routers/deposits.py's `_apply_knowledge` while
applying a deposit's knowledge[] compartment -- never blocking acceptance.
Kept here, one place, for the same reason app/library.py and app/journal.py
keep their query logic out of the router: a future UI surface (if one is
ever added for flags) reuses this instead of drifting.
"""

import base64
from datetime import UTC, datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Flag

VALID_FLAG_TYPES = frozenset({"fork", "duplicate"})


def _encode_cursor(created_at: datetime, id_: str) -> str:
    raw = f"{created_at.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_s, id_ = raw.split("|", 1)
        return datetime.fromisoformat(ts_s), id_
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid_cursor", "The `cursor` parameter is not valid for this listing.") from exc


async def list_flags(
    db: AsyncSession,
    *,
    unresolved: bool = True,
    type: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[Flag], str | None]:
    """Newest-first, ULID-keyset cursor pagination -- same (created_at, id)
    tuple-cursor shape as app/library.py's `list_entries` and
    app/journal.py's `list_events`, so the three list surfaces never drift on
    pagination semantics. `unresolved=True` (the default) is the librarian's
    actual working queue: `resolved_at IS NULL`.
    """
    stmt = select(Flag)
    if unresolved:
        stmt = stmt.where(Flag.resolved_at.is_(None))
    if type:
        stmt = stmt.where(Flag.type == type)

    if cursor is not None:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(Flag.created_at, Flag.id) < tuple_(cursor_ts, cursor_id))

    stmt = stmt.order_by(Flag.created_at.desc(), Flag.id.desc()).limit(limit + 1)

    rows = (await db.scalars(stmt)).all()
    has_more = len(rows) > limit
    page_rows = list(rows[:limit])

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return page_rows, next_cursor


async def get_flag_or_404(db: AsyncSession, flag_id: str) -> Flag:
    flag = await db.get(Flag, flag_id)
    if flag is None:
        raise ApiError(
            404,
            "flag_not_found",
            f"No flag with id '{flag_id}'. Recovery: check the id (e.g. via GET /v1/flags), resend.",
        )
    return flag


async def resolve_flag(db: AsyncSession, flag_id: str, machine_id: str) -> tuple[Flag, bool]:
    """Idempotent resolve (contracts-v1.md Principles: fuzzy-judgment queues
    never block, and closing one out is never a one-shot-only action): a flag
    already resolved -- by this machine or any other -- returns its existing
    resolution unchanged, 200, `already_resolved=True`. Re-running a batch,
    or two librarian runs racing on the same flag, both land safely.
    """
    flag = await get_flag_or_404(db, flag_id)
    if flag.resolved_at is not None:
        return flag, True
    flag.resolved_at = datetime.now(UTC)
    flag.resolved_by = machine_id
    await db.commit()
    return flag, False
