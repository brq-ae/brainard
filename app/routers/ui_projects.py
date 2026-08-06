"""UI projects -- GET /ui/projects (list), GET /ui/projects/{name} (detail:
registry facts, machines, counts, latest handoff, paginated handoff chain,
mirrored documents list), GET /ui/projects/{name}/documents/{path} (per-path
version history + document view, rendered like library bodies). Owner
session required. Reuses the same app/projects.py + app/documents.py query
functions as the API's project endpoints.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from app.db import get_db
from app.doctrine import current_overlay
from app.documents import document_versions, get_document_version, list_documents_for_project
from app.models import Project
from app.projects import handoffs_page, list_projects_page, project_counts, project_latest_handoff, project_machines
from app.templates_env import templates
from app.ui_auth import require_ui_session

router = APIRouter(prefix="/ui/projects", tags=["ui"])

HANDOFF_PAGE_SIZE = 10


@router.get("")
async def project_list(
    request: Request,
    cursor: str | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    page_rows, next_cursor = await list_projects_page(db, cursor=cursor, limit=20)
    return templates.TemplateResponse(
        request,
        "projects_list.html",
        {"csrf_token": session["csrf"], "project_rows": page_rows, "next_cursor": next_cursor},
    )


@router.get("/{name}")
async def project_detail(
    name: str,
    request: Request,
    handoff_cursor: str | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    project = await db.get(Project, name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"No project named '{name}' is registered.")

    machines = await project_machines(db, name)
    overlay_row = await current_overlay(db, name)
    latest_handoff = await project_latest_handoff(db, name)
    counts = await project_counts(db, name)
    documents = await list_documents_for_project(db, name)
    handoff_rows, next_handoff_cursor = await handoffs_page(db, name, cursor=handoff_cursor, limit=HANDOFF_PAGE_SIZE)

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "csrf_token": session["csrf"],
            "project": project,
            "machines": machines,
            "overlay_version": overlay_row.version if overlay_row is not None else None,
            "latest_handoff": latest_handoff,
            "counts": counts,
            "documents": documents,
            "handoffs": handoff_rows,
            "next_handoff_cursor": next_handoff_cursor,
        },
    )


@router.get("/{name}/documents/{path:path}")
async def document_view(
    name: str,
    path: str,
    request: Request,
    version: int | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Project, name) is None:
        raise HTTPException(status_code=404, detail=f"No project named '{name}' is registered.")

    doc = await get_document_version(db, name, path, version)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document at path '{path}' for project '{name}'.")

    versions = await document_versions(db, name, path)

    return templates.TemplateResponse(
        request,
        "document_view.html",
        {
            "csrf_token": session["csrf"],
            "project_name": name,
            "path": path,
            "doc": doc,
            "versions": versions,
        },
    )
