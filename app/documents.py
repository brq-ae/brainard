"""Shared mirrored-document helpers (contracts-v1.md §5).

Used by GET /v1/search (scope=decisions/all -- latest ADR/doc mirror per
path only) and GET /v1/projects/{name} (mirrored-doc counts by kind). Kept
here once so "latest version per (project, path)" is computed identically
everywhere -- supersede-never-erase means every prior version of a mirrored
document stays in the table, so "latest" always needs the same
max-version-per-path join.

Note on the write side (app/routers/deposits.py's `_apply_documents`): the
"next version" for a (project, path) is computed as max(version) + 1 at
insert time, which is inherently racy under two truly concurrent deposits
targeting the same path -- the DB-level unique index on (project, path,
version) (see MirroredDocument in app/models.py) is what actually catches
the collision. `create_deposit` handles that IntegrityError with a bounded
in-server retry (recomputing the version fresh each attempt) and, if every
attempt still collides, a proper enveloped 503 (`deposit_conflict_retry`)
-- never a raw, unexplained 500.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models import MirroredDocument


def latest_mirrored_documents() -> Select:
    """A SELECT over `mirrored_documents` restricted to exactly the latest
    version per (project, path). Callers add their own further `.where(...)`
    (e.g. `.where(MirroredDocument.kind == "adr")`,
    `.where(MirroredDocument.project == name)`).
    """
    latest = (
        select(
            MirroredDocument.project,
            MirroredDocument.path,
            func.max(MirroredDocument.version).label("max_version"),
        )
        .group_by(MirroredDocument.project, MirroredDocument.path)
        .subquery()
    )
    return select(MirroredDocument).join(
        latest,
        (MirroredDocument.project == latest.c.project)
        & (MirroredDocument.path == latest.c.path)
        & (MirroredDocument.version == latest.c.max_version),
    )


def latest_mirrored_document_ids() -> Select:
    """Same restriction as `latest_mirrored_documents`, but selecting only
    `id` -- for use inside an `IN (...)` filter alongside other computed
    columns (rank, literal type label, ...) that don't fit cleanly through
    the full-row SELECT above (see app/routers/search.py).
    """
    latest = (
        select(
            MirroredDocument.project,
            MirroredDocument.path,
            func.max(MirroredDocument.version).label("max_version"),
        )
        .group_by(MirroredDocument.project, MirroredDocument.path)
        .subquery()
    )
    return select(MirroredDocument.id).join(
        latest,
        (MirroredDocument.project == latest.c.project)
        & (MirroredDocument.path == latest.c.path)
        & (MirroredDocument.version == latest.c.max_version),
    )


# --- UI-only reads: per-project mirrored document list + per-path version
# history (app/routers/ui_projects.py). No such listing exists in the
# session-facing API surface (contracts-v1.md §7 names only search and the
# project detail's counts), so there's nothing to refactor out of here --
# same rationale as app/journal.py.


async def list_documents_for_project(db: AsyncSession, project: str) -> list[MirroredDocument]:
    """The latest version of every mirrored path under `project`, grouped by
    kind then path.
    """
    stmt = latest_mirrored_documents().where(MirroredDocument.project == project).order_by(
        MirroredDocument.kind, MirroredDocument.path
    )
    return list((await db.scalars(stmt)).all())


async def document_versions(db: AsyncSession, project: str, path: str) -> list[MirroredDocument]:
    """Full version history for one (project, path), newest first --
    supersede-never-erase means every prior version is still there."""
    stmt = (
        select(MirroredDocument)
        .where(MirroredDocument.project == project, MirroredDocument.path == path)
        .order_by(MirroredDocument.version.desc())
    )
    return list((await db.scalars(stmt)).all())


async def get_document_version(
    db: AsyncSession, project: str, path: str, version: int | None = None
) -> MirroredDocument | None:
    """A specific version of a mirrored document, or the latest if
    `version` is omitted."""
    stmt = select(MirroredDocument).where(MirroredDocument.project == project, MirroredDocument.path == path)
    if version is None:
        stmt = stmt.order_by(MirroredDocument.version.desc())
    else:
        stmt = stmt.where(MirroredDocument.version == version)
    return await db.scalar(stmt.limit(1))
