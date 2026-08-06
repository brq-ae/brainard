"""UI library -- GET /ui/library (filterable list), GET /ui/library/{id}
(entry page with rendered body, frontmatter, supersession chain,
duplicate hints, flags). Owner session required. Reuses the same
app/library.py query functions as GET /v1/library/{id}.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from app.db import get_db
from app.errors import ApiError
from app.library import VALID_LIBRARY_STATUSES, children, duplicate_hints, get_entry_or_404, list_entries, parents
from app.projects import list_project_names
from app.templates_env import templates
from app.ui_auth import require_ui_session

router = APIRouter(prefix="/ui/library", tags=["ui"])

NAMESPACES = ("lessons", "howto", "reference")
STATUS_FILTERS = ("active", "all", *sorted(VALID_LIBRARY_STATUSES - {"active"}))


@router.get("")
async def library_list(
    request: Request,
    namespace: str | None = Query(default=None),
    status: str = Query(default="active"),
    project: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    if status not in STATUS_FILTERS:
        status = "active"
    entries, next_cursor = await list_entries(
        db, namespace=namespace or None, status=status, project=project or None, cursor=cursor
    )
    project_names = await list_project_names(db)

    return templates.TemplateResponse(
        request,
        "library_list.html",
        {
            "csrf_token": session["csrf"],
            "entries": entries,
            "next_cursor": next_cursor,
            "namespaces": NAMESPACES,
            "statuses": STATUS_FILTERS,
            "project_names": project_names,
            "f_namespace": namespace or "",
            "f_status": status,
            "f_project": project or "",
        },
    )


@router.get("/{entry_id}")
async def library_entry(
    entry_id: str,
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    try:
        entry = await get_entry_or_404(db, entry_id)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    parent_rows = await parents(db, entry)
    child_rows = await children(db, entry)
    hint_rows = await duplicate_hints(db, entry)

    return templates.TemplateResponse(
        request,
        "library_entry.html",
        {
            "csrf_token": session["csrf"],
            "entry": entry,
            "parents": parent_rows,
            "children": child_rows,
            "duplicate_hints": hint_rows,
        },
    )
