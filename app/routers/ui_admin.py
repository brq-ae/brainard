"""UI admin area -- owner cookie + CSRF (contracts-v1.md/ADR-0004 ruling 12:
"an owner-gated admin area -- machine minting/revocation with last-seen
view, doctrine proposal approvals").

Machines: GET/POST /ui/admin/machines, POST /ui/admin/machines/{id}/revoke,
POST /ui/admin/machines/{id}/reactivate.
Proposals: GET /ui/admin/proposals, POST /ui/admin/proposals/{id}/approve|reject.

Every write here calls the exact same shared functions as the API
endpoints (app.machines, app.proposals) -- mint/revoke/reactivate/approve/
reject logic is never duplicated between the two surfaces.
"""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse

from app.db import get_db
from app.errors import ApiError
from app.librarian_engine import LIBRARIAN_MACHINE_ID
from app.machines import list_machines, mint_machine
from app.machines import reactivate_machine as reactivate_machine_op
from app.machines import revoke_machine as revoke_machine_op
from app.machines import update_machine as update_machine_op
from app.models import Machine
from app.onboarding import PROJECT_PLACEHOLDER, TOKEN_PLACEHOLDER, generate_onboarding_prompt, resolve_base_url
from app.proposals import decide, list_proposals
from app.roles import DEFAULT_ROLE
from app.room_ai import ROOM_AI_MACHINE_ID
from app.templates_env import templates
from app.ui_auth import require_csrf, require_ui_session

router = APIRouter(prefix="/ui/admin", tags=["ui"])


# --- machines ---

# Reserved, tokenless machine identities (app.reserved_machines) whose rows
# exist only to give an in-app, non-session write path (never over HTTP,
# never require_machine) something legible to attribute to -- the built-in
# librarian (ADR-0010) and room AI action deposits (ADR-0011). Neither has a
# usable bearer token, a rename/role/onboarding-prompt affordance, or (for
# the room-AI one) anything to actually authenticate with. Both get a
# "system" badge and a short explanation instead (admin_machines.html);
# Revoke/Reactivate stays a real, independent kill switch for each.
_SYSTEM_MACHINE_DESCRIPTIONS: dict[str, str] = {
    LIBRARIAN_MACHINE_ID: (
        "Built-in librarian identity (ADR-0010) -- every deposit/flag-resolution the built-in librarian "
        "makes is attributed to this row. It has no usable bearer token and never authenticates over the "
        "API. Revoking it disables the built-in librarian: every scheduled or \"Run now\" run will skip "
        "cleanly (no LLM call, no deposit) until it's reactivated."
    ),
    ROOM_AI_MACHINE_ID: (
        "Room AI actions identity (ADR-0011) -- every deposit made from a room's AI-action result "
        "(summarize/verdict/decisions/lessons) is attributed to this row. It has no usable bearer token and "
        "never authenticates over the API. Revoking it disables deposits from room AI actions until it's "
        "reactivated (running an action is unaffected -- only depositing its result)."
    ),
}


def _prompts_by_machine_id(request: Request, machines: list[Machine]) -> dict[str, str]:
    """The "regenerate onboarding prompt" text for every listed machine,
    keyed by id -- always with `TOKEN_PLACEHOLDER` standing in for the
    token, since a machine's real token can never be retrieved again after
    mint (contracts-v1.md §1). `default_project`, if the owner set one,
    fills the project slot; otherwise the literal `<PROJECT>` placeholder,
    same as the mint-time prompt when no project was given.

    Skips every reserved system row (see `_SYSTEM_MACHINE_DESCRIPTIONS`
    above) entirely -- none of them has a usable bearer token or
    authenticates over the API, so an onboarding prompt would be
    nonsensical; the template never offers this affordance for those rows
    (see admin_machines.html).
    """
    base_url = resolve_base_url(request)
    return {
        m.id: generate_onboarding_prompt(
            base_url=base_url,
            token=TOKEN_PLACEHOLDER,
            project=m.default_project or PROJECT_PLACEHOLDER,
            agent_name=m.name,
            role=m.role,
        )
        for m in machines
        if m.id not in _SYSTEM_MACHINE_DESCRIPTIONS
    }


@router.get("/machines")
async def admin_machines(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    machines = await list_machines(db)
    return templates.TemplateResponse(
        request,
        "admin_machines.html",
        {
            "csrf_token": session["csrf"],
            "machines": machines,
            "prompts": _prompts_by_machine_id(request, machines),
            "librarian_machine_id": LIBRARIAN_MACHINE_ID,
            "system_machine_descriptions": _SYSTEM_MACHINE_DESCRIPTIONS,
        },
    )


@router.post("/machines")
async def admin_machines_create(
    request: Request,
    name: str = Form(...),
    role: str = Form(DEFAULT_ROLE),
    default_project: str = Form(""),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    project_hint = default_project.strip() or None
    try:
        machine, token = await mint_machine(db, name, role=role, default_project=project_hint)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    machines = await list_machines(db)
    prompt = generate_onboarding_prompt(
        base_url=resolve_base_url(request),
        token=token,
        project=machine.default_project or PROJECT_PLACEHOLDER,
        agent_name=machine.name,
        role=machine.role,
    )
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
            "prompts": _prompts_by_machine_id(request, machines),
            "librarian_machine_id": LIBRARIAN_MACHINE_ID,
            "system_machine_descriptions": _SYSTEM_MACHINE_DESCRIPTIONS,
            "newly_minted": {
                "id": machine.id,
                "name": machine.name,
                "token": token,
                "prompt": prompt,
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


@router.post("/machines/{machine_id}/reactivate")
async def admin_machines_reactivate(
    machine_id: str,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    machine = await reactivate_machine_op(db, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"No machine with id '{machine_id}'.")
    return RedirectResponse(url="/ui/admin/machines", status_code=303)


@router.post("/machines/{machine_id}/update")
async def admin_machines_update(
    machine_id: str,
    name: str = Form(...),
    role: str = Form(DEFAULT_ROLE),
    default_project: str = Form(""),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    project_hint = default_project.strip() or None
    try:
        machine = await update_machine_op(
            db, machine_id, {"role": role, "default_project": project_hint, "name": name}
        )
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
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
