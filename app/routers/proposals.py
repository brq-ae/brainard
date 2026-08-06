"""Doctrine proposals -- GET /v1/proposals, POST /v1/proposals/{id}/approve,
POST /v1/proposals/{id}/reject (contracts-v1.md §4). Owner-token only.

The actual list/decide logic lives in app/proposals.py, shared with the
UI admin area (app/routers/ui_admin.py) so the two surfaces never drift.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.proposals import decide, list_proposals
from app.schemas import LibrarySource, ProposalDecisionResponse, ProposalListItem

router = APIRouter(prefix="/v1/proposals", tags=["proposals"])


@router.get("", response_model=list[ProposalListItem])
async def get_proposals(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> list[ProposalListItem]:
    rows = await list_proposals(db)
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


@router.post("/{proposal_id}/approve", response_model=ProposalDecisionResponse)
async def approve_proposal(
    proposal_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> ProposalDecisionResponse:
    entry = await decide(db, proposal_id, "approved")
    return ProposalDecisionResponse(id=entry.id, decision=entry.proposal_decision, decided_at=entry.proposal_decided_at)


@router.post("/{proposal_id}/reject", response_model=ProposalDecisionResponse)
async def reject_proposal(
    proposal_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> ProposalDecisionResponse:
    entry = await decide(db, proposal_id, "rejected")
    return ProposalDecisionResponse(id=entry.id, decision=entry.proposal_decision, decided_at=entry.proposal_decided_at)
