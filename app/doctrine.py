"""Shared doctrine version lookups (contracts-v1.md §4).

Used by both the doctrine admin endpoints (app/routers/doctrine.py -- writes,
and "current version" reads for GET /v1/doctrine and overlay validation) and
the bootstrap endpoint (app/routers/bootstrap.py -- compiles the current
global + overlay for a project). Kept here once so the two routers never
drift on what "current" means.
"""

from sqlalchemy import select
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
