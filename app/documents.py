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
