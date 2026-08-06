"""Doctrine proposals -- GET /v1/proposals, POST /v1/proposals/{id}/approve,
POST /v1/proposals/{id}/reject (contracts-v1.md §4). Owner-token only.

NOTE: approving or rejecting a proposal here only *records the owner's
decision* on the proposal entry itself. It never mutates doctrine. Promoting
an approved proposal into doctrine is a separate, deliberate act: the
owner's own subsequent POST to /v1/doctrine/global or
/v1/doctrine/overlays/{project} (contracts-v1.md §4: "the owner approves via
the admin area; approval promotes the change into doctrine" -- promotion is
that follow-up write, not a side effect of approval).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.errors import ApiError
from app.models import KnowledgeEntry
from app.schemas import LibrarySource, ProposalDecisionResponse, ProposalListItem

router = APIRouter(prefix="/v1/proposals", tags=["proposals"])


@router.get("", response_model=list[ProposalListItem])
async def list_proposals(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> list[ProposalListItem]:
    rows = (
        await db.scalars(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_doctrine_proposal.is_(True), KnowledgeEntry.status == "active")
            .order_by(KnowledgeEntry.created_at.desc())
        )
    ).all()
    return [
        ProposalListItem(
            id=r.id,
            title=r.title,
            namespace=r.namespace,
            project=r.project,
            tags=r.tags,
            body=r.body,
            status=r.status,
            proposal_decision=r.proposal_decision,
            proposal_decided_at=r.proposal_decided_at,
            created_at=r.created_at,
            source=LibrarySource(machine_id=r.machine_id, tool=r.tool, session=r.session),
        )
        for r in rows
    ]


async def _get_undecided_proposal(db: AsyncSession, proposal_id: str) -> KnowledgeEntry:
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


async def _decide(db: AsyncSession, proposal_id: str, decision: str) -> ProposalDecisionResponse:
    entry = await _get_undecided_proposal(db, proposal_id)
    entry.proposal_decision = decision
    entry.proposal_decided_at = datetime.now(UTC)
    await db.commit()
    return ProposalDecisionResponse(id=entry.id, decision=entry.proposal_decision, decided_at=entry.proposal_decided_at)


@router.post("/{proposal_id}/approve", response_model=ProposalDecisionResponse)
async def approve_proposal(
    proposal_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> ProposalDecisionResponse:
    return await _decide(db, proposal_id, "approved")


@router.post("/{proposal_id}/reject", response_model=ProposalDecisionResponse)
async def reject_proposal(
    proposal_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> ProposalDecisionResponse:
    return await _decide(db, proposal_id, "rejected")
