"""UI notifications -- GET /ui/notifications (current config + full
immutable history), POST /ui/notifications (owner cookie + CSRF, creates the
next version). Owner session required, same as every other /ui/* admin
surface.

Every write here calls the exact same shared function as the API endpoint
(app.notifications.create_version) -- validation/versioning logic is never
duplicated between the two surfaces (same rule as app/routers/ui_admin.py's
machine mint/revoke).
"""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from app.db import get_db
from app.errors import ApiError
from app.notifications import create_version, current_config
from app.notifications import history as config_history
from app.templates_env import templates
from app.ui_auth import require_csrf, require_ui_session

router = APIRouter(prefix="/ui/notifications", tags=["ui"])


@router.get("")
async def notifications_page(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    current = await current_config(db)
    history = await config_history(db)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"csrf_token": session["csrf"], "current": current, "history": history, "error": None},
    )


@router.post("")
async def notifications_create(
    request: Request,
    ntfy_url: str = Form(...),
    topic: str = Form(...),
    note: str = Form(default=""),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    try:
        await create_version(db, ntfy_url, topic, note or None)
    except ApiError as exc:
        # Re-render the form with the error rather than a bare redirect --
        # the owner sees exactly what was rejected and why, without losing
        # the existing current config / history context on screen.
        current = await current_config(db)
        history = await config_history(db)
        return templates.TemplateResponse(
            request,
            "notifications.html",
            {"csrf_token": session["csrf"], "current": current, "history": history, "error": exc.detail},
            status_code=exc.status_code,
        )
    return RedirectResponse(url="/ui/notifications", status_code=303)
