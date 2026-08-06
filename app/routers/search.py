"""Full-text search -- GET /v1/search (contracts-v1.md §6 note, §7).

Machine OR owner token. The actual query construction lives in
app/search.py, shared with the UI search page (app/routers/ui_search.py)
so the two surfaces never drift.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_machine_or_owner
from app.db import get_db
from app.schemas import SearchResponse, SearchResultItem
from app.search import SearchScope, run_search

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1),
    scope: SearchScope = "default",
    include_history: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    results, next_cursor = await run_search(
        db, q=q, scope=scope, include_history=include_history, cursor=cursor, limit=limit
    )
    return SearchResponse(
        results=[
            SearchResultItem(type=r.type, id=r.id, snippet=r.snippet, project=r.project, rank=r.rank, path=r.path, version=r.version)
            for r in results
        ],
        next_cursor=next_cursor,
    )
