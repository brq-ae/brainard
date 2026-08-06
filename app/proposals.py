"""Shared doctrine proposal operations (contracts-v1.md §4).

Used by both the API router (app/routers/proposals.py) and the owner-gated
UI admin area (app/routers/ui_admin.py) -- kept here once so approve/reject
logic never drifts between the two surfaces (phase 6 brief: "do NOT
duplicate mint/revoke/approve logic").

NOTE: `decide` only *records the owner's decision* on the proposal entry
itself. It never mutates doctrine -- promotion into doctrine is a separate,
deliberate act (the owner's own subsequent POST to /v1/doctrine/global or
/v1/doctrine/overlays/{project}), same as documented in the original
app/routers/proposals.py.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import KnowledgeEntry


async def list_proposals(db: AsyncSession) -> list[KnowledgeEntry]:
    rows = (
        await db.scalars(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_doctrine_proposal.is_(True), KnowledgeEntry.status == "active")
            .order_by(KnowledgeEntry.created_at.desc())
        )
    ).all()
    return list(rows)


async def get_undecided_proposal(db: AsyncSession, proposal_id: str) -> KnowledgeEntry:
    entry = await db.get(KnowledgeEntry, proposal_id)
    if entry is None or not entry.is_doctrine_proposal:
        raise ApiError(
            404,
            "proposal_not_found",
            f"No doctrine proposal with id '{proposal_id}'. Recovery: check the id (e.g. via GET /v1/proposals), resend.",
        )
    if entry.proposal_decision is not None:
        raise ApiError(
            422,
            "proposal_already_decided",
            f"Proposal '{proposal_id}' was already {entry.proposal_decision} at "
            f"{entry.proposal_decided_at.isoformat() if entry.proposal_decided_at else 'an earlier time'}. "
            "Recovery: none -- decisions are final; file a new proposal to revisit.",
        )
    return entry


async def decide(db: AsyncSession, proposal_id: str, decision: str) -> KnowledgeEntry:
    entry = await get_undecided_proposal(db, proposal_id)
    entry.proposal_decision = decision
    entry.proposal_decided_at = datetime.now(UTC)
    await db.commit()
    return entry
