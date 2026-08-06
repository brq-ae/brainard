"""Shared journal (events) read logic -- UI-only (contracts-v1.md §7 notes
the journal is "opt-in per query" via GET /v1/search?scope=journal; no
standalone events-listing endpoint exists in the session-facing API
surface). Kept here, in one place, for the UI journal page
(app/routers/ui_journal.py) -- same rationale as app/library.py's
`list_entries`.
"""

import base64
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Event


def _encode_cursor(ts: datetime, id_: str) -> str:
    raw = f"{ts.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_s, id_ = raw.split("|", 1)
        return datetime.fromisoformat(ts_s), id_
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid_cursor", "The `cursor` parameter is not valid for this listing.") from exc


async def list_events(
    db: AsyncSession,
    *,
    project: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[Event], str | None]:
    stmt = select(Event)
    if project:
        stmt = stmt.where(Event.project == project)
    if kind:
        stmt = stmt.where(Event.kind == kind)

    if cursor is not None:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(Event.ts, Event.id) < tuple_(cursor_ts, cursor_id))

    stmt = stmt.order_by(Event.ts.desc(), Event.id.desc()).limit(limit + 1)

    rows = (await db.scalars(stmt)).all()
    has_more = len(rows) > limit
    page_rows = list(rows[:limit])

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last.ts, last.id)

    return page_rows, next_cursor
