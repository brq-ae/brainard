"""Library entry reads -- GET /v1/library/{id} (contracts-v1.md §3, §7).

Machine OR owner token: the first session-facing read route, so it's also
the first real exercise of `require_machine_or_owner`.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_machine_or_owner
from app.db import get_db
from app.errors import ApiError
from app.models import Flag, KnowledgeEntry
from app.schemas import LibraryDuplicateHint, LibraryEntryRef, LibraryEntryResponse, LibrarySource

router = APIRouter(prefix="/v1/library", tags=["library"])


async def _parents(db: AsyncSession, entry: KnowledgeEntry) -> list[LibraryEntryRef]:
    if not entry.supersedes:
        return []
    rows = (await db.scalars(select(KnowledgeEntry).where(KnowledgeEntry.id.in_(entry.supersedes)))).all()
    by_id = {r.id: r for r in rows}
    # Preserve the entry's own supersedes[] order; a missing row would mean a
    # parent was deleted out-of-band, which never happens under
    # supersede-never-erase -- skipped defensively rather than 500ing.
    return [LibraryEntryRef(id=r.id, title=r.title, status=r.status) for pid in entry.supersedes if (r := by_id.get(pid))]


async def _children(db: AsyncSession, entry: KnowledgeEntry) -> list[LibraryEntryRef]:
    rows = (
        await db.scalars(
            select(KnowledgeEntry).where(KnowledgeEntry.id != entry.id, KnowledgeEntry.supersedes.any(entry.id))
        )
    ).all()
    return [LibraryEntryRef(id=r.id, title=r.title, status=r.status) for r in rows]


async def _duplicate_hints(db: AsyncSession, entry: KnowledgeEntry) -> list[LibraryDuplicateHint]:
    """Hints attached when this entry was created (§3: "visible to
    readers"). `detail` carries the title as it was at hint time; not
    re-joined against the current entry row so a hint still reads even if
    the related entry is later retired/superseded.
    """
    flags = (await db.scalars(select(Flag).where(Flag.type == "duplicate", Flag.entry_id == entry.id))).all()
    return [
        LibraryDuplicateHint(
            entry_id=f.related_entry_id,
            title=(f.detail or {}).get("title", ""),
            rank=float((f.detail or {}).get("rank", 0.0)),
        )
        for f in flags
        if f.related_entry_id is not None
    ]


@router.get("/{entry_id}", response_model=LibraryEntryResponse)
async def get_library_entry(
    entry_id: str,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> LibraryEntryResponse:
    entry = await db.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise ApiError(
            404,
            "entry_not_found",
            f"No library entry with id '{entry_id}'. Recovery: check the id (e.g. via GET /v1/search), resend.",
        )

    return LibraryEntryResponse(
        id=entry.id,
        title=entry.title,
        namespace=entry.namespace,
        project=entry.project,
        tags=entry.tags,
        status=entry.status,
        retire_reason=entry.retire_reason,
        supersedes=entry.supersedes,
        body=entry.body,
        source=LibrarySource(machine_id=entry.machine_id, tool=entry.tool, session=entry.session),
        created_at=entry.created_at,
        deposit_id=entry.deposit_id,
        parents=await _parents(db, entry),
        children=await _children(db, entry),
        duplicate_hints=await _duplicate_hints(db, entry),
    )
