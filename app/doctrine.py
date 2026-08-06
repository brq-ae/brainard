"""Shared doctrine version lookups (contracts-v1.md §4).

Used by both the doctrine admin endpoints (app/routers/doctrine.py -- writes,
and "current version" reads for GET /v1/doctrine and overlay validation),
the bootstrap endpoint (app/routers/bootstrap.py -- compiles the current
global + overlay for a project), and the UI doctrine page
(app/routers/ui_doctrine.py). Kept here once so no surface drifts on what
"current" means.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DoctrineVersion


async def current_global(db: AsyncSession) -> DoctrineVersion | None:
    """The highest-numbered 'global' doctrine version, if any has ever been
    written. Doctrine is immutable/append-only (supersede-never-erase), so
    "current" is simply "latest by version".
    """
    return await db.scalar(
        select(DoctrineVersion).where(DoctrineVersion.kind == "global").order_by(DoctrineVersion.version.desc()).limit(1)
    )


async def current_overlay(db: AsyncSession, project: str) -> DoctrineVersion | None:
    """The highest-numbered 'overlay' doctrine version for `project`, if any."""
    return await db.scalar(
        select(DoctrineVersion)
        .where(DoctrineVersion.kind == "overlay", DoctrineVersion.project == project)
        .order_by(DoctrineVersion.version.desc())
        .limit(1)
    )


async def next_version(db: AsyncSession, kind: str, project: str | None) -> int:
    """The version number the next write for this (kind, project) should
    use -- 1 if none exists yet, else one past the current highest.
    """
    current = await db.scalar(
        select(DoctrineVersion.version)
        .where(DoctrineVersion.kind == kind, DoctrineVersion.project == project)
        .order_by(DoctrineVersion.version.desc())
        .limit(1)
    )
    return (current or 0) + 1


async def current_overlays_all(db: AsyncSession) -> list[DoctrineVersion]:
    """The current (latest-by-version) overlay for every project that has
    ever had one written. Shared by GET /v1/doctrine
    (app/routers/doctrine.py) and the UI doctrine page
    (app/routers/ui_doctrine.py) so the "current overlay per project" query
    lives in exactly one place.
    """
    latest = (
        select(DoctrineVersion.project, func.max(DoctrineVersion.version).label("max_version"))
        .where(DoctrineVersion.kind == "overlay")
        .group_by(DoctrineVersion.project)
        .subquery()
    )
    rows = (
        await db.scalars(
            select(DoctrineVersion)
            .join(
                latest,
                (DoctrineVersion.project == latest.c.project) & (DoctrineVersion.version == latest.c.max_version),
            )
            .where(DoctrineVersion.kind == "overlay")
            .order_by(DoctrineVersion.project)
        )
    ).all()
    return list(rows)


async def version_history(db: AsyncSession) -> list[DoctrineVersion]:
    """Every doctrine version ever written (global bumps and every project's
    overlay bumps), newest first -- for the UI's version history view
    (phase 6 brief). Doctrine is supersede-never-erase, so this is a simple
    full read, not filtered to "current".
    """
    rows = (await db.scalars(select(DoctrineVersion).order_by(DoctrineVersion.created_at.desc()))).all()
    return list(rows)


def stale_override_reason(global_rules_by_id: dict[str, dict], override_id: str) -> str | None:
    """Closes the phase 4 advisory at the UI layer (phase 6 brief): an
    overlay override whose target rule is now `non_negotiable` (a tier
    change made *after* the override was written -- allowed, since overlay
    writes only validate against the tier at write time) or no longer
    exists in the current global doctrine is "inactive" -- the override
    still exists in overlay history, but the server-compiled bootstrap
    doctrine (app/routers/bootstrap.py's `_compile_doctrine`) will never
    apply it, because non-negotiables are always served as-is and a
    deleted-by-implication id has nothing left to override.

    Returns None when the override is live/active, else a human-readable
    reason string.
    """
    rule = global_rules_by_id.get(override_id)
    if rule is None:
        return "inactive — target rule no longer exists in current global doctrine"
    if rule["tier"] == "non_negotiable":
        return "inactive — target rule is now non-negotiable"
    return None
