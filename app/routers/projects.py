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
"""

import base64
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import and_, func, nullslast, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_machine_or_owner, require_owner
from app.db import get_db
from app.doctrine import current_overlay
from app.documents import latest_mirrored_documents
from app.errors import ApiError
from app.models import Deposit, Handoff, KnowledgeEntry, Machine, MirroredDocument, Project
from app.projects import apply_project_update, validate_project_update
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


def _unknown_project_error(name: str) -> ApiError:
    return ApiError(
        404,
        "unknown_project",
        f"No project named '{name}' is registered. Recovery: check the name (e.g. via GET /v1/projects), "
        "or mention it in a machine deposit first (auto-stub is deliberate there), resend.",
    )


# --- GET /v1/projects/{name} ---


async def _project_machines(db: AsyncSession, name: str) -> list[ProjectMachineInfo]:
    rows = (
        await db.execute(
            select(Machine.id, Machine.name, func.max(Deposit.received_at).label("last_deposit_at"))
            .join(Deposit, Deposit.machine_id == Machine.id)
            .where(Deposit.project == name)
            .group_by(Machine.id, Machine.name)
            .order_by(func.max(Deposit.received_at).desc())
        )
    ).all()
    return [ProjectMachineInfo(id=r.id, name=r.name, last_deposit_at=r.last_deposit_at) for r in rows]


async def _project_document_counts(db: AsyncSession, name: str) -> ProjectDocumentCounts:
    subq = latest_mirrored_documents().where(MirroredDocument.project == name).subquery()
    rows = (await db.execute(select(subq.c.kind, func.count()).group_by(subq.c.kind))).all()
    counts = {"adr": 0, "doc": 0}
    for kind, n in rows:
        counts[kind] = n
    return ProjectDocumentCounts(**counts)


@router.get("/{name}", response_model=ProjectDetailResponse)
async def get_project(
    name: str,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetailResponse:
    project = await db.get(Project, name)
    if project is None:
        raise _unknown_project_error(name)

    machines = await _project_machines(db, name)

    overlay_row = await current_overlay(db, name)
    overlay_version = overlay_row.version if overlay_row is not None else None

    latest_handoff = await db.scalar(
        select(Handoff).where(Handoff.project == name).order_by(Handoff.received_at.desc()).limit(1)
    )
    handoff_out = (
        None
        if latest_handoff is None
        else ProjectHandoffOut(
            id=latest_handoff.id,
            stands=latest_handoff.stands,
            in_flight=latest_handoff.in_flight,
            blocked=latest_handoff.blocked,
            next_steps=latest_handoff.next_steps,
            notes=latest_handoff.notes,
            received_at=latest_handoff.received_at,
            deposit_id=latest_handoff.deposit_id,
        )
    )

    active_library_entries = await db.scalar(
        select(func.count()).select_from(KnowledgeEntry).where(
            KnowledgeEntry.project == name,
            KnowledgeEntry.status == "active",
            KnowledgeEntry.is_doctrine_proposal.is_(False),
        )
    )
    document_counts = await _project_document_counts(db, name)
    total_deposits = await db.scalar(select(func.count()).select_from(Deposit).where(Deposit.project == name))

    return ProjectDetailResponse(
        name=project.name,
        description=project.description,
        status=project.status,
        created_at=project.created_at,
        machines=machines,
        overlay_version=overlay_version,
        latest_handoff=handoff_out,
        counts=ProjectCounts(
            active_library_entries=active_library_entries or 0,
            mirrored_documents=document_counts,
            total_deposits=total_deposits or 0,
        ),
    )


# --- GET /v1/projects (list, cursor-paginated, newest activity first) ---


def _encode_project_cursor(latest_deposit_at: datetime | None, name: str) -> str:
    ts_part = latest_deposit_at.isoformat() if latest_deposit_at is not None else ""
    raw = f"{ts_part}|{name}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_project_cursor(cursor: str) -> tuple[datetime | None, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_s, name = raw.split("|", 1)
        return (datetime.fromisoformat(ts_s) if ts_s else None), name
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            422,
            "invalid_cursor",
            "The `cursor` parameter is not a valid cursor for this endpoint. Recovery: omit `cursor` to start "
            "from the first page, or reuse a `next_cursor` value returned by a previous GET /v1/projects call.",
        ) from exc


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectListResponse:
    latest_deposit_subq = (
        select(Deposit.project, func.max(Deposit.received_at).label("latest_deposit_at"))
        .group_by(Deposit.project)
        .subquery()
    )
    ts_col = latest_deposit_subq.c.latest_deposit_at

    stmt = select(Project, ts_col).outerjoin(latest_deposit_subq, Project.name == latest_deposit_subq.c.project)

    if cursor is not None:
        cursor_ts, cursor_name = _decode_project_cursor(cursor)
        # Keyset pagination in (latest_deposit_at DESC NULLS LAST, name DESC)
        # order. A project with no deposits yet (ts IS NULL) always sorts
        # after every project that has one, regardless of cursor position.
        if cursor_ts is not None:
            stmt = stmt.where(
                or_(
                    and_(ts_col.is_not(None), tuple_(ts_col, Project.name) < tuple_(cursor_ts, cursor_name)),
                    ts_col.is_(None),
                )
            )
        else:
            stmt = stmt.where(and_(ts_col.is_(None), Project.name < cursor_name))

    stmt = stmt.order_by(nullslast(ts_col.desc()), Project.name.desc()).limit(limit + 1)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    results = [
        ProjectListItem(
            name=p.name, status=p.status, description=p.description, created_at=p.created_at, latest_deposit_at=ts
        )
        for p, ts in page_rows
    ]

    next_cursor = None
    if has_more and page_rows:
        last_p, last_ts = page_rows[-1]
        next_cursor = _encode_project_cursor(last_ts, last_p.name)

    return ProjectListResponse(results=results, next_cursor=next_cursor)


# --- GET /v1/projects/{name}/handoffs (chain, newest first, cursor-paginated) ---


def _encode_handoff_cursor(received_at: datetime, id_: str) -> str:
    raw = f"{received_at.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_handoff_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_s, id_ = raw.split("|", 1)
        return datetime.fromisoformat(ts_s), id_
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            422,
            "invalid_cursor",
            "The `cursor` parameter is not a valid cursor for this endpoint. Recovery: omit `cursor` to start "
            "from the first page, or reuse a `next_cursor` value returned by a previous call to this endpoint.",
        ) from exc


@router.get("/{name}/handoffs", response_model=HandoffListResponse)
async def list_project_handoffs(
    name: str,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> HandoffListResponse:
    if await db.get(Project, name) is None:
        raise _unknown_project_error(name)

    stmt = select(Handoff).where(Handoff.project == name)

    if cursor is not None:
        cursor_ts, cursor_id = _decode_handoff_cursor(cursor)
        stmt = stmt.where(tuple_(Handoff.received_at, Handoff.id) < tuple_(cursor_ts, cursor_id))

    stmt = stmt.order_by(Handoff.received_at.desc(), Handoff.id.desc()).limit(limit + 1)

    rows = (await db.scalars(stmt)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    results = [
        ProjectHandoffOut(
            id=h.id,
            stands=h.stands,
            in_flight=h.in_flight,
            blocked=h.blocked,
            next_steps=h.next_steps,
            notes=h.notes,
            received_at=h.received_at,
            deposit_id=h.deposit_id,
        )
        for h in page_rows
    ]

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_handoff_cursor(last.received_at, last.id)

    return HandoffListResponse(results=results, next_cursor=next_cursor)


# --- PATCH /v1/projects/{name} (owner-only, deliberate write) ---


@router.patch("/{name}", response_model=ProjectPatchResponse)
async def update_project(
    name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectPatchResponse:
    project = await db.get(Project, name)
    if project is None:
        raise _unknown_project_error(name)

    validate_project_update(body)
    apply_project_update(project, body)
    await db.commit()

    return ProjectPatchResponse(name=project.name, description=project.description, status=project.status)
