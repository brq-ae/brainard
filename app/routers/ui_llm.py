"""UI LLM provider config -- GET /ui/llm (effective config + source + full
immutable history), POST /ui/llm (owner cookie + CSRF, creates the next
version), POST /ui/llm/test (owner cookie + CSRF, connectivity test).
Owner session required, same as every other /ui/* admin surface (ADR-0010
phase 1).

Every write/read here calls the exact same shared functions as the owner
API (app.llm_config.create_version / resolve_llm_config,
app.llm_client.test_llm_connection) -- validation/versioning/masking/test
logic is never duplicated between the two surfaces (same rule as
app/routers/ui_notifications.py, which this module mirrors structurally).

XSS: base_url/model/note are owner-supplied content rendered on this page.
Jinja2 autoescape is forced on globally (app/templates_env.py) and nothing
here uses the `| safe`/`md` escape hatch -- see the module docstring
reasoning in app/routers/ui_rooms.py for the same discipline applied to a
different untrusted-input surface.
"""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from app.db import get_db
from app.errors import ApiError
from app.llm_client import test_llm_connection
from app.llm_config import create_version, mask_api_key, resolve_llm_config
from app.llm_config import history as config_history
from app.templates_env import templates
from app.ui_auth import require_csrf, require_ui_session

router = APIRouter(prefix="/ui/llm", tags=["ui"])


async def _page_context(db: AsyncSession, session: dict, *, error: str | None = None, test_result: dict | None = None) -> dict:
    effective = await resolve_llm_config(db)
    history = await config_history(db)
    eff_api_key_set, eff_api_key_hint = mask_api_key(effective.api_key)
    history_view = []
    for row in history:
        api_key_set, api_key_hint = mask_api_key(row.api_key)
        history_view.append(
            {
                "version": row.version,
                "base_url": row.base_url,
                "model": row.model,
                "api_key_set": api_key_set,
                "api_key_hint": api_key_hint,
                "note": row.note,
                "created_at": row.created_at,
            }
        )
    return {
        "csrf_token": session["csrf"],
        "effective": effective,
        "eff_api_key_set": eff_api_key_set,
        "eff_api_key_hint": eff_api_key_hint,
        "history": history_view,
        "error": error,
        "test_result": test_result,
    }


@router.get("")
async def llm_config_page(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    context = await _page_context(db, session)
    return templates.TemplateResponse(request, "llm.html", context)


@router.post("")
async def llm_config_create(
    request: Request,
    base_url: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(default=""),
    note: str = Form(default=""),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    try:
        await create_version(db, base_url, model, api_key or None, note or None)
    except ApiError as exc:
        # Re-render the form with the error rather than a bare redirect --
        # the owner sees exactly what was rejected and why, without losing
        # the existing effective config / history context on screen.
        context = await _page_context(db, session, error=exc.detail)
        return templates.TemplateResponse(request, "llm.html", context, status_code=exc.status_code)
    return RedirectResponse(url="/ui/llm", status_code=303)


@router.post("/test")
async def llm_config_test(
    request: Request,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    effective = await resolve_llm_config(db)
    result = await test_llm_connection(effective)
    context = await _page_context(db, session, test_result=result)
    return templates.TemplateResponse(request, "llm.html", context)
