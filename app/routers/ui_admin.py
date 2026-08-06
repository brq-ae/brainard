"""UI admin area -- owner cookie + CSRF (contracts-v1.md/ADR-0004 ruling 12:
"an owner-gated admin area -- machine minting/revocation with last-seen
view, doctrine proposal approvals").

Machines: GET/POST /ui/admin/machines, POST /ui/admin/machines/{id}/revoke.
Proposals: GET /ui/admin/proposals, POST /ui/admin/proposals/{id}/approve|reject.

Every write here calls the exact same shared functions as the API
endpoints (app.machines, app.proposals) -- mint/revoke/approve/reject logic
is never duplicated between the two surfaces.
"""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse

from app.db import get_db
from app.errors import ApiError
from app.machines import list_machines, mint_machine
from app.machines import revoke_machine as revoke_machine_op
from app.proposals import decide, list_proposals
from app.templates_env import templates
from app.ui_auth import require_csrf, require_ui_session

router = APIRouter(prefix="/ui/admin", tags=["ui"])


# --- machines ---


@router.get("/machines")
async def admin_machines(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    machines = await list_machines(db)
    return templates.TemplateResponse(
        request, "admin_machines.html", {"csrf_token": session["csrf"], "machines": machines}
    )


def _paste_line(request: Request, token: str) -> str:
    """The onboarding paste-line (docs/onboarding.md), pre-filled with the
    real hub URL and the freshly minted token -- `project` is left as a
    literal fill-in since a machine isn't bound to a single project at mint
    time. Hub URL is derived from the request the mint happened over
    (`request.base_url`), which is the address this browser -- and
    therefore, on a LAN deployment, any session on the same network --
    actually reached the hub at.

    Worded to anchor trust in the owner's own message, not the endpoint,
    and to explicitly preserve the assistant's judgment (docs/onboarding.md
    "Why it's worded this way") -- a real-world session on another machine
    correctly refused an earlier "follow the returned instructions exactly"
    wording as prompt-injection-shaped.
    """
    hub_base = str(request.base_url).rstrip("/")
    return (
        "I run a private knowledge hub for my projects — it's mine and I administer it. Fetch "
        f"{hub_base}/v1/bootstrap?project=<PROJECT> with header 'Authorization: Bearer {token}'. "
        "The response contains my working rules for this session, the project's current state, and how "
        "to deposit what you learn back to the hub. Read it and apply it with your normal judgment — it "
        "never overrides your safety rules. If anything in it seems off, ask me."
    )


@router.post("/machines")
async def admin_machines_create(
    request: Request,
    name: str = Form(...),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    machine, token = await mint_machine(db, name)
    machines = await list_machines(db)
    # Direct render (no redirect): the plaintext token exists only in this
    # one response and can never be shown again -- a redirect would either
    # lose it or force putting it somewhere retrievable (URL, flashed
    # cookie), both worse than the well-understood "shown once, refresh
    # re-mints" trade-off (same pattern as e.g. GitHub's PAT creation page).
    return templates.TemplateResponse(
        request,
        "admin_machines.html",
        {
            "csrf_token": session["csrf"],
            "machines": machines,
            "newly_minted": {
                "id": machine.id,
                "name": machine.name,
                "token": token,
                "paste_line": _paste_line(request, token),
            },
        },
        status_code=201,
    )


@router.post("/machines/{machine_id}/revoke")
async def admin_machines_revoke(
    machine_id: str,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    machine = await revoke_machine_op(db, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"No machine with id '{machine_id}'.")
    return RedirectResponse(url="/ui/admin/machines", status_code=303)


# --- proposals ---


@router.get("/proposals")
async def admin_proposals(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    proposals = await list_proposals(db)
    pending = [p for p in proposals if p.proposal_decision is None]
    decided = [p for p in proposals if p.proposal_decision is not None]
    return templates.TemplateResponse(
        request,
        "admin_proposals.html",
        {"csrf_token": session["csrf"], "pending": pending, "decided": decided},
    )


async def _decide_and_redirect(db: AsyncSession, proposal_id: str, decision: str) -> RedirectResponse:
    try:
        await decide(db, proposal_id, decision)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return RedirectResponse(url="/ui/admin/proposals", status_code=303)


@router.post("/proposals/{proposal_id}/approve")
async def admin_proposals_approve(
    proposal_id: str,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await _decide_and_redirect(db, proposal_id, "approved")


@router.post("/proposals/{proposal_id}/reject")
async def admin_proposals_reject(
    proposal_id: str,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await _decide_and_redirect(db, proposal_id, "rejected")
