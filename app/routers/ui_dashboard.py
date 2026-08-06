"""UI dashboard -- GET /ui (phase 6 brief: "fleet at a glance"). Owner
session required. Reuses the same shared query modules as every other UI
page and the API itself -- app.machines, app.projects, app.proposals,
app.deposits_read.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deposits_read import recent_deposits
from app.machines import list_machines
from app.projects import list_projects_page
from app.proposals import list_proposals
from app.templates_env import templates
from app.ui_auth import require_ui_session

router = APIRouter(prefix="/ui", tags=["ui"])

MACHINE_HIGHLIGHT_COUNT = 6
PROJECT_CARD_COUNT = 6
RECENT_DEPOSIT_COUNT = 10


@router.get("")
async def dashboard(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    machines = await list_machines(db)
    active_machines = [m for m in machines if m.status == "active"]
    machine_highlights = sorted(machines, key=lambda m: m.last_seen or m.created_at, reverse=True)[:MACHINE_HIGHLIGHT_COUNT]

    project_rows, _ = await list_projects_page(db, limit=PROJECT_CARD_COUNT)

    deposits = await recent_deposits(db, limit=RECENT_DEPOSIT_COUNT)

    proposals = await list_proposals(db)
    pending_proposal_count = sum(1 for p in proposals if p.proposal_decision is None)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "csrf_token": session["csrf"],
            "machine_count": len(machines),
            "active_machine_count": len(active_machines),
            "machine_highlights": machine_highlights,
            "project_rows": project_rows,
            "deposits": deposits,
            "pending_proposal_count": pending_proposal_count,
        },
    )
