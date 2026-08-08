"""Events -- GET /v1/events (contracts-v1.md §2, §7; phase 8 librarian
support). Machine or owner token.

A filtered, cursor-paginated, exact-match read of the raw journal --
distinct from `GET /v1/search?scope=journal` (full-text ranking over
`summary`). Exists for curation agents (the librarian's `lesson.candidate`
harvest, ADR-0004) that need to walk the journal by kind/project/time, not
rank it by relevance to a query string.

`payload` is never in the response unless `include_payload=true` is
requested -- it can carry up to 256 KB per event (contracts-v1.md §2), so a
listing call defaults to the lightweight columns.

The actual query logic lives in app/journal.py, shared with the UI journal
page (app/routers/ui_journal.py) so the two surfaces never drift.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_machine_or_owner
from app.db import get_db
from app.errors import ApiError
from app.journal import list_events
from app.routers.deposits import VALID_EVENT_KINDS
from app.schemas import EventListItem, EventListResponse

router = APIRouter(prefix="/v1/events", tags=["events"])


@router.get("", response_model=EventListResponse)
async def get_events(
    kind: str | None = Query(default=None),
    project: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    include_payload: bool = Query(default=False),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    if kind is not None and kind not in VALID_EVENT_KINDS:
        raise ApiError(
            422,
            "unknown_event_kind",
            f"`kind` must be one of {sorted(VALID_EVENT_KINDS)}, got {kind!r}.",
        )
    rows, next_cursor = await list_events(db, project=project, kind=kind, since=since, cursor=cursor, limit=limit)
    return EventListResponse(
        results=[
            EventListItem(
                id=e.id,
                deposit_id=e.deposit_id,
                project=e.project,
                seq=e.seq,
                ts=e.ts,
                kind=e.kind,
                summary=e.summary,
                tags=e.tags,
                payload=e.payload if include_payload else None,
            )
            for e in rows
        ],
        next_cursor=next_cursor,
    )
