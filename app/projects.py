"""Shared project registry logic (contracts-v1.md §5, §7).

`validate_project_update`/`apply_project_update` back the two write paths
-- a deposit's optional `project_update` envelope field (app/routers/
deposits.py) and the owner's deliberate `PATCH /v1/projects/{name}`
(app/routers/projects.py). The read-side query functions below back both
the API's project endpoints (app/routers/projects.py) and the UI's project
pages (app/routers/ui_projects.py, app/routers/ui_dashboard.py) -- kept
here once so no surface drifts on what a project's registry facts, machine
list, document counts, or handoff chain are.
"""

import base64
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, nullslast, or_, select, tuple_
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import latest_mirrored_documents
from app.errors import ApiError
from app.models import Deposit, Handoff, KnowledgeEntry, Machine, MirroredDocument, Project

VALID_PROJECT_STATUSES = frozenset({"active", "paused", "done"})
_ALLOWED_KEYS = frozenset({"description", "status"})

_RECOVERY_FIX_AND_RESEND = "fix the listed field(s), resend"


def validate_project_update(data: Any) -> None:
    """Whole-object, self-explaining validation. Raises ApiError naming every
    problem at once -- mirrors the deposits[]/knowledge[] validation style.
    An empty object is valid (no-op update).
    """
    if not isinstance(data, dict):
        raise ApiError(
            422,
            "invalid_project_update",
            "`project_update` must be an object with optional `description`/`status` fields. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
        )

    unknown_keys = sorted(set(data) - _ALLOWED_KEYS)
    if unknown_keys:
        raise ApiError(
            422,
            "invalid_project_update",
            f"Unknown field(s) {unknown_keys} in project update; only {sorted(_ALLOWED_KEYS)} are recognized. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
            extra={"unknown_keys": unknown_keys},
        )

    if "description" in data and data["description"] is not None and not isinstance(data["description"], str):
        raise ApiError(
            422,
            "invalid_project_update",
            f"`description` must be a string or null. Recovery: {_RECOVERY_FIX_AND_RESEND}.",
        )

    if "status" in data and data["status"] not in VALID_PROJECT_STATUSES:
        raise ApiError(
            422,
            "invalid_project_update",
            f"`status` must be one of {sorted(VALID_PROJECT_STATUSES)}, got {data['status']!r}. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
        )


def apply_project_update(project: Project, data: dict[str, Any]) -> None:
    """Applies an already-validated `{description?, status?}` object.
    Absent keys leave the current value untouched (partial update).
    """
    if "description" in data:
        project.description = data["description"]
    if "status" in data:
        project.status = data["status"]


async def list_project_names(db: AsyncSession) -> list[str]:
    """All registered project names, alphabetical -- used to populate filter
    dropdowns on the UI's library/journal pages (app/routers/ui_library.py,
    app/routers/ui_journal.py).
    """
    return list((await db.scalars(select(Project.name).order_by(Project.name))).all())


def unknown_project_error(name: str) -> ApiError:
    return ApiError(
        404,
        "unknown_project",
        f"No project named '{name}' is registered. Recovery: check the name (e.g. via GET /v1/projects), "
        "or mention it in a machine deposit first (auto-stub is deliberate there), resend.",
    )


# --- read helpers backing GET /v1/projects/{name} and the UI project detail page ---


async def project_machines(db: AsyncSession, name: str) -> list[Row]:
    """Rows of (id, name, last_deposit_at) -- machines that have deposited
    on this project, newest activity first.
    """
    rows = (
        await db.execute(
            select(Machine.id, Machine.name, func.max(Deposit.received_at).label("last_deposit_at"))
            .join(Deposit, Deposit.machine_id == Machine.id)
            .where(Deposit.project == name)
            .group_by(Machine.id, Machine.name)
            .order_by(func.max(Deposit.received_at).desc())
        )
    ).all()
    return list(rows)


async def project_document_counts(db: AsyncSession, name: str) -> dict[str, int]:
    subq = latest_mirrored_documents().where(MirroredDocument.project == name).subquery()
    rows = (await db.execute(select(subq.c.kind, func.count()).group_by(subq.c.kind))).all()
    counts = {"adr": 0, "doc": 0}
    for kind, n in rows:
        counts[kind] = n
    return counts


async def project_latest_handoff(db: AsyncSession, name: str) -> Handoff | None:
    return await db.scalar(select(Handoff).where(Handoff.project == name).order_by(Handoff.received_at.desc()).limit(1))


async def project_counts(db: AsyncSession, name: str) -> dict[str, Any]:
    active_library_entries = await db.scalar(
        select(func.count()).select_from(KnowledgeEntry).where(
            KnowledgeEntry.project == name,
            KnowledgeEntry.status == "active",
            KnowledgeEntry.is_doctrine_proposal.is_(False),
        )
    )
    document_counts = await project_document_counts(db, name)
    total_deposits = await db.scalar(select(func.count()).select_from(Deposit).where(Deposit.project == name))
    return {
        "active_library_entries": active_library_entries or 0,
        "mirrored_documents": document_counts,
        "total_deposits": total_deposits or 0,
    }


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


async def list_projects_page(
    db: AsyncSession, *, cursor: str | None = None, limit: int = 20
) -> tuple[list[tuple[Project, datetime | None]], str | None]:
    """Rows of (Project, latest_deposit_at), newest activity first, projects
    with no deposits yet sorted last regardless of name.
    """
    latest_deposit_subq = (
        select(Deposit.project, func.max(Deposit.received_at).label("latest_deposit_at"))
        .group_by(Deposit.project)
        .subquery()
    )
    ts_col = latest_deposit_subq.c.latest_deposit_at

    stmt = select(Project, ts_col).outerjoin(latest_deposit_subq, Project.name == latest_deposit_subq.c.project)

    if cursor is not None:
        cursor_ts, cursor_name = _decode_project_cursor(cursor)
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
    page_rows = [(p, ts) for p, ts in rows[:limit]]

    next_cursor = None
    if has_more and page_rows:
        last_p, last_ts = page_rows[-1]
        next_cursor = _encode_project_cursor(last_ts, last_p.name)

    return page_rows, next_cursor


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


async def handoffs_page(
    db: AsyncSession, name: str, *, cursor: str | None = None, limit: int = 20
) -> tuple[list[Handoff], str | None]:
    stmt = select(Handoff).where(Handoff.project == name)

    if cursor is not None:
        cursor_ts, cursor_id = _decode_handoff_cursor(cursor)
        stmt = stmt.where(tuple_(Handoff.received_at, Handoff.id) < tuple_(cursor_ts, cursor_id))

    stmt = stmt.order_by(Handoff.received_at.desc(), Handoff.id.desc()).limit(limit + 1)

    rows = (await db.scalars(stmt)).all()
    has_more = len(rows) > limit
    page_rows = list(rows[:limit])

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_handoff_cursor(last.received_at, last.id)

    return page_rows, next_cursor
