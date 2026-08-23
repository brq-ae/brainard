"""UI LLM provider config -- GET /ui/llm (effective config + source + full
immutable history), POST /ui/llm (owner cookie + CSRF, creates the next
version), POST /ui/llm/test (owner cookie + CSRF, connectivity test),
POST /ui/llm/models (owner cookie + CSRF, JS-fetched model discovery --
app/static/llm.js). Owner session required, same as every other /ui/*
admin surface (ADR-0010 phase 1).

Every write/read here calls the exact same shared functions as the owner
API (app.llm_config.create_version / resolve_llm_config /
resolve_models_source, app.llm_client.test_llm_connection /
list_provider_models) -- validation/versioning/masking/test/discovery
logic is never duplicated between the two surfaces (same rule as
app/routers/ui_notifications.py, which this module mirrors structurally).

XSS: base_url/model/note are owner-supplied content rendered on this page,
and the model IDs `POST /ui/llm/models` returns are PROVIDER-supplied --
untrusted the same way a room transcript is (app/room_ai.py). Jinja2
autoescape is forced on globally (app/templates_env.py) and nothing here
uses the `| safe`/`md` escape hatch (see the module docstring reasoning in
app/routers/ui_rooms.py for the same discipline applied to a different
untrusted-input surface); `/models`' JSON response is rendered client-side
by app/static/llm.js via `.textContent`/`document.createElement` only,
mirroring app/static/room_ai.js's discipline for the same reason -- never
`innerHTML`, never a template literal fed into it.
"""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.db import get_db
from app.errors import ApiError
from app.llm_client import LlmModelsError, list_provider_models, test_llm_connection
from app.llm_config import create_version, mask_api_key, resolve_llm_config, resolve_models_source
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
    settings = get_settings()
    return {
        "csrf_token": session["csrf"],
        "effective": effective,
        "eff_api_key_set": eff_api_key_set,
        "eff_api_key_hint": eff_api_key_hint,
        "history": history_view,
        "error": error,
        "test_result": test_result,
        # Env-configured, read-only here (app/config.py's
        # `llm_call_timeout_secs`/`llm_test_timeout_secs`, env
        # LLM_CALL_TIMEOUT_SECS/LLM_TEST_TIMEOUT_SECS) -- changing either
        # requires a restart, same as every other env-sourced setting
        # surfaced in this UI (e.g. /ui/librarian's interval).
        "call_timeout_secs": settings.llm_call_timeout_secs,
        "test_timeout_secs": settings.llm_test_timeout_secs,
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


@router.post("/models")
async def llm_config_models(
    base_url: str = Form(default=""),
    api_key: str = Form(default=""),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """JS-fetched (app/static/llm.js), never a browser navigation -- so the
    "Fetch models" button can populate a picker without discarding whatever
    the owner has already typed into the base_url/model/api_key fields
    (unlike a normal form POST, which would re-render the whole page from
    the saved/effective config). Blank `base_url`/`api_key` are sent as ""
    by the form and normalized to None here, then resolved exactly like
    the owner API (`resolve_models_source`) -- an explicit base_url is
    probed with exactly the api_key given alongside it, never a different,
    already-saved provider's key.

    Any ApiError (no provider configured/given, connection refused/DNS/
    timeout, 401/403, unexpected body shape) is left to propagate to the
    app-level handler (app/errors.py's `api_error_handler`), turning it
    into the same `{"error": {"code", "detail", ...}}` envelope the v1 API
    uses -- the JS reads `error.code`/`error.detail` from that same shape
    either way (same pattern as app/routers/ui_rooms.py's `room_ai_action`).
    """
    resolved_base_url, resolved_api_key = await resolve_models_source(db, base_url or None, api_key or None)
    timeout = get_settings().llm_test_timeout_secs
    try:
        models, truncated = await list_provider_models(resolved_base_url, resolved_api_key, timeout=timeout)
    except LlmModelsError as exc:
        raise ApiError(503, exc.code, str(exc)) from exc
    return JSONResponse({"models": models, "count": len(models), "truncated": truncated})
