"""UI journal -- GET /ui/journal: recent events, filterable by
project/kind, paginated. Owner session required.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.journal import list_events
from app.projects import list_project_names
from app.routers.deposits import VALID_EVENT_KINDS
from app.templates_env import templates
from app.ui_auth import require_ui_session

router = APIRouter(prefix="/ui/journal", tags=["ui"])

KINDS = sorted(VALID_EVENT_KINDS)


@router.get("")
async def journal(
    request: Request,
    project: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    events, next_cursor = await list_events(db, project=project or None, kind=kind or None, cursor=cursor)
    project_names = await list_project_names(db)

    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "csrf_token": session["csrf"],
            "events": events,
            "next_cursor": next_cursor,
            "project_names": project_names,
            "kinds": KINDS,
            "f_project": project or "",
            "f_kind": kind or "",
        },
    )
