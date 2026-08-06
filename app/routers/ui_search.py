"""UI search -- GET /ui/search (query box + scope selector), same query
logic as GET /v1/search (app/search.py). Owner session required.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.search import run_search
from app.templates_env import templates
from app.ui_auth import require_ui_session

router = APIRouter(prefix="/ui/search", tags=["ui"])

SCOPES = ("default", "journal", "all", "proposals", "decisions")


@router.get("")
async def search_page(
    request: Request,
    q: str | None = Query(default=None),
    scope: str = Query(default="default"),
    include_history: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    if scope not in SCOPES:
        scope = "default"

    results: list = []
    next_cursor = None
    if q:
        results, next_cursor = await run_search(
            db, q=q, scope=scope, include_history=include_history, cursor=cursor, limit=25
        )

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "csrf_token": session["csrf"],
            "q": q or "",
            "scope": scope,
            "scopes": SCOPES,
            "include_history": include_history,
            "results": results,
            "next_cursor": next_cursor,
            "searched": q is not None and q != "",
        },
    )
