"""UI login/logout (phase 6 brief). The one owner-gated surface that is
reachable *without* a session cookie -- everything else under /ui/* requires
`require_ui_session` (app/ui_auth.py).
"""

from fastapi import APIRouter, Cookie, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.templates_env import templates
from app.ui_auth import (
    clear_session_cookie,
    require_csrf,
    require_ui_session,
    set_session_cookie,
    verify_owner_token,
    verify_session_token,
)

router = APIRouter(prefix="/ui", tags=["ui"])


@router.get("/login")
async def login_form(request: Request, brain_ui_session: str | None = Cookie(default=None)):
    # Already logged in -- bounce straight to the dashboard rather than
    # showing the login form again.
    if brain_ui_session and verify_session_token(brain_ui_session) is not None:
        return RedirectResponse(url="/ui", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
async def login_submit(
    request: Request,
    token: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # Intentionally no CSRF check here: there is no session yet to bind a
    # token to, and a forged cross-site login submits the *attacker's own*
    # owner token, landing the attacker in their own session -- not
    # credential theft. The pasted token is the only secret in this
    # request, and CSRF can't extract or guess it.
    ok = await verify_owner_token(token, db)
    if not ok:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That token was not recognized as the owner token. Machine tokens are not accepted here."},
            status_code=401,
        )
    response = RedirectResponse(url="/ui", status_code=303)
    set_session_cookie(response)
    return response


@router.post("/logout")
async def logout(
    _session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
):
    response = RedirectResponse(url="/ui/login", status_code=303)
    clear_session_cookie(response)
    return response
