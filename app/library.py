"""Shared library entry read logic (contracts-v1.md §3, §7).

`parents`/`children`/`duplicate_hints`/`get_entry_or_404` back both
GET /v1/library/{id} (app/routers/library.py) and the UI's library pages
(app/routers/ui_library.py). `list_entries` is a UI-only addition -- no
list endpoint exists in the session-facing API surface (§7 names only
GET /v1/library/{id}), so there is nothing to refactor out of; it lives
here anyway to keep every library query in one place.
"""

import base64
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Flag, KnowledgeEntry

VALID_LIBRARY_STATUSES = frozenset({"active", "superseded", "retired"})


async def get_entry_or_404(db: AsyncSession, entry_id: str) -> KnowledgeEntry:
    entry = await db.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise ApiError(
            404,
            "entry_not_found",
            f"No library entry with id '{entry_id}'. Recovery: check the id (e.g. via GET /v1/search), resend.",
        )
    return entry


async def parents(db: AsyncSession, entry: KnowledgeEntry) -> list[KnowledgeEntry]:
    if not entry.supersedes:
        return []
    rows = (await db.scalars(select(KnowledgeEntry).where(KnowledgeEntry.id.in_(entry.supersedes)))).all()
    by_id = {r.id: r for r in rows}
    # Preserve the entry's own supersedes[] order; a missing row would mean a
    # parent was deleted out-of-band, which never happens under
    # supersede-never-erase -- skipped defensively rather than 500ing.
    return [r for pid in entry.supersedes if (r := by_id.get(pid))]


async def children(db: AsyncSession, entry: KnowledgeEntry) -> list[KnowledgeEntry]:
    rows = (
        await db.scalars(
            select(KnowledgeEntry).where(KnowledgeEntry.id != entry.id, KnowledgeEntry.supersedes.any(entry.id))
        )
    ).all()
    return list(rows)


async def duplicate_hints(db: AsyncSession, entry: KnowledgeEntry) -> list[Flag]:
    """Hints attached when this entry was created (§3: "visible to
    readers"). `detail` carries the title as it was at hint time; not
    re-joined against the current entry row so a hint still reads even if
    the related entry is later retired/superseded.
    """
    flags = (await db.scalars(select(Flag).where(Flag.type == "duplicate", Flag.entry_id == entry.id))).all()
    return [f for f in flags if f.related_entry_id is not None]


# --- UI-only list query (namespace/status/project filters, cursor paginated) ---


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


async def list_entries(
    db: AsyncSession,
    *,
    namespace: str | None = None,
    status: str = "active",
    project: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[KnowledgeEntry], str | None]:
    """Filterable browse listing for the UI. `status` is one of
    'active' (default), 'superseded', 'retired', or 'all' (history toggle).
    Doctrine proposals are always excluded -- they have their own admin
    surface (app/proposals.py, app/routers/ui_admin.py).
    """
    stmt = select(KnowledgeEntry).where(KnowledgeEntry.is_doctrine_proposal.is_(False))
    if namespace:
        stmt = stmt.where(KnowledgeEntry.namespace == namespace)
    if project:
        stmt = stmt.where(KnowledgeEntry.project == project)
    if status != "all":
        stmt = stmt.where(KnowledgeEntry.status == status)

    if cursor is not None:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(KnowledgeEntry.created_at, KnowledgeEntry.id) < tuple_(cursor_ts, cursor_id))

    stmt = stmt.order_by(KnowledgeEntry.created_at.desc(), KnowledgeEntry.id.desc()).limit(limit + 1)

    rows = (await db.scalars(stmt)).all()
    has_more = len(rows) > limit
    page_rows = list(rows[:limit])

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return page_rows, next_cursor
