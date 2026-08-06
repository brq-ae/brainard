"""Project registry reads/writes -- GET /v1/projects, GET /v1/projects/{name},
GET /v1/projects/{name}/handoffs, PATCH /v1/projects/{name}
(contracts-v1.md §5, §7).

Reads (`GET`) accept machine OR owner token, matching the precedent already
set by GET /v1/library/{id} and GET /v1/search (both listed "session-facing
(machine token)" in §7 yet already implemented with `require_machine_or_owner`
-- the owner needs the same read access for the future admin UI). The
deliberate write, `PATCH /v1/projects/{name}`, is owner-only per this
phase's brief -- the checkpoint-bound write path is the deposit envelope's
`project_update` field instead (app/routers/deposits.py).

`GET /v1/projects` (list) is a surface addition beyond the literal §7 API
list, which only names `GET /v1/projects/{name}` and `.../handoffs` --
added because phase 6's UI needs a way to enumerate projects at all. Flagged
as a deviation in the phase 5 report.

The actual query logic (machines/document-counts/handoff/list/handoff-chain)
lives in app/projects.py, shared with the UI project pages
(app/routers/ui_projects.py, app/routers/ui_dashboard.py) so no surface
drifts on what these facts are.
"""

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from app.auth import Principal, require_machine_or_owner, require_owner
from app.db import get_db
from app.doctrine import current_overlay
from app.models import Project
from app.projects import (
    apply_project_update,
    handoffs_page,
    list_projects_page,
    project_counts,
    project_latest_handoff,
    project_machines,
    unknown_project_error,
    validate_project_update,
)
from app.schemas import (
    ProjectCounts,
    ProjectDetailResponse,
    ProjectDocumentCounts,
    ProjectHandoffOut,
    ProjectListItem,
    ProjectListResponse,
    ProjectMachineInfo,
    ProjectPatchResponse,
    HandoffListResponse,
)

router = APIRouter(prefix="/v1/projects", tags=["projects"])


def _handoff_out(h) -> ProjectHandoffOut:
    return ProjectHandoffOut(
        id=h.id,
        stands=h.stands,
        in_flight=h.in_flight,
        blocked=h.blocked,
        next_steps=h.next_steps,
        notes=h.notes,
        received_at=h.received_at,
        deposit_id=h.deposit_id,
    )


@router.get("/{name}", response_model=ProjectDetailResponse)
async def get_project(
    name: str,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetailResponse:
    project = await db.get(Project, name)
    if project is None:
        raise unknown_project_error(name)

    machines = await project_machines(db, name)
    overlay_row = await current_overlay(db, name)
    latest_handoff = await project_latest_handoff(db, name)
    counts = await project_counts(db, name)

    return ProjectDetailResponse(
        name=project.name,
        description=project.description,
        status=project.status,
        created_at=project.created_at,
        machines=[ProjectMachineInfo(id=m.id, name=m.name, last_deposit_at=m.last_deposit_at) for m in machines],
        overlay_version=overlay_row.version if overlay_row is not None else None,
        latest_handoff=None if latest_handoff is None else _handoff_out(latest_handoff),
        counts=ProjectCounts(
            active_library_entries=counts["active_library_entries"],
            mirrored_documents=ProjectDocumentCounts(**counts["mirrored_documents"]),
            total_deposits=counts["total_deposits"],
        ),
    )


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectListResponse:
    page_rows, next_cursor = await list_projects_page(db, cursor=cursor, limit=limit)
    results = [
        ProjectListItem(name=p.name, status=p.status, description=p.description, created_at=p.created_at, latest_deposit_at=ts)
        for p, ts in page_rows
    ]
    return ProjectListResponse(results=results, next_cursor=next_cursor)


@router.get("/{name}/handoffs", response_model=HandoffListResponse)
async def list_project_handoffs(
    name: str,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> HandoffListResponse:
    if await db.get(Project, name) is None:
        raise unknown_project_error(name)

    page_rows, next_cursor = await handoffs_page(db, name, cursor=cursor, limit=limit)
    return HandoffListResponse(results=[_handoff_out(h) for h in page_rows], next_cursor=next_cursor)


@router.patch("/{name}", response_model=ProjectPatchResponse)
async def update_project(
    name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectPatchResponse:
    project = await db.get(Project, name)
    if project is None:
        raise unknown_project_error(name)

    validate_project_update(body)
    apply_project_update(project, body)
    await db.commit()

    return ProjectPatchResponse(name=project.name, description=project.description, status=project.status)
