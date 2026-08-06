"""Library entry reads -- GET /v1/library/{id} (contracts-v1.md §3, §7).

Machine OR owner token: the first session-facing read route, so it's also
the first real exercise of `require_machine_or_owner`.

The actual entry/parents/children/duplicate-hints queries live in
app/library.py, shared with the UI library pages
(app/routers/ui_library.py) so the two surfaces never drift.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_machine_or_owner
from app.db import get_db
from app.library import children, duplicate_hints, get_entry_or_404, parents
from app.schemas import LibraryDuplicateHint, LibraryEntryRef, LibraryEntryResponse, LibrarySource

router = APIRouter(prefix="/v1/library", tags=["library"])


@router.get("/{entry_id}", response_model=LibraryEntryResponse)
async def get_library_entry(
    # Intentionally unfiltered by `is_doctrine_proposal`: by-id readability of
    # a proposal entry is deliberate (the filer needs to re-read their own
    # proposal), unlike its exclusion from search/digest/discovery paths --
    # ids reach machines only via their own deposit acks or GET /v1/proposals.
    entry_id: str,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> LibraryEntryResponse:
    entry = await get_entry_or_404(db, entry_id)

    parent_rows = await parents(db, entry)
    child_rows = await children(db, entry)
    hint_rows = await duplicate_hints(db, entry)

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
        parents=[LibraryEntryRef(id=r.id, title=r.title, status=r.status) for r in parent_rows],
        children=[LibraryEntryRef(id=r.id, title=r.title, status=r.status) for r in child_rows],
        duplicate_hints=[
            LibraryDuplicateHint(entry_id=f.related_entry_id, title=(f.detail or {}).get("title", ""), rank=float((f.detail or {}).get("rank", 0.0)))
            for f in hint_rows
        ],
    )
