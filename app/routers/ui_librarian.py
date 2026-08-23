"""UI for the built-in librarian -- GET /ui/librarian (enabled/configured
status + recent run history), POST /ui/librarian/run (owner cookie + CSRF,
runs the engine inline and redirects back showing the result). ADR-0010
phase 2.

A separate page from /ui/llm (app/routers/ui_llm.py) rather than an
extension of it: /ui/llm is about *configuring* the LLM provider (base_url/
model/api_key, immutable version history) while this page is about the
librarian *job itself* (is it enabled, has it run, what did it do) --
distinct concerns, same split as notifications-config vs. rooms elsewhere
in this app. Links both ways are unnecessary; both are one click away via
the top nav (app/templates/base.html).

Every read/write here calls the exact same shared functions as the owner
API (app.librarian_engine.list_librarian_runs / run_librarian) -- logic is
never duplicated between the two surfaces, same rule as app/routers/
ui_llm.py.

XSS: `error` (provider/engine failure text, which can echo back
provider-supplied detail) and stale-project names inside `counts` are
rendered on this page. Jinja2 autoescape is forced on globally
(app/templates_env.py) and nothing here uses `| safe`.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from app.config import get_settings
from app.db import get_db
from app.librarian_engine import DEFAULT_LIMITS, list_librarian_runs, run_librarian
from app.llm_config import resolve_llm_config
from app.templates_env import templates
from app.ui_auth import require_csrf, require_ui_session

router = APIRouter(prefix="/ui/librarian", tags=["ui"])

RUNS_PAGE_SIZE = 20


async def _page_context(db: AsyncSession, session: dict) -> dict:
    settings = get_settings()
    effective = await resolve_llm_config(db)
    runs, _ = await list_librarian_runs(db, limit=RUNS_PAGE_SIZE)
    return {
        "csrf_token": session["csrf"],
        "enabled": settings.librarian_enabled,
        "configured": bool(effective.base_url and effective.model),
        "effective": effective,
        "interval_secs": settings.librarian_interval_secs,
        "runs": runs,
    }


@router.get("")
async def librarian_page(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    context = await _page_context(db, session)
    return templates.TemplateResponse(request, "librarian.html", context)


@router.post("/run")
async def librarian_run_now(
    request: Request,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    # Inline, same as the owner API's POST /v1/librarian/run -- see that
    # router's module docstring for why. The owner's browser simply waits
    # for the page to redirect once the run finishes.
    await run_librarian(limits=DEFAULT_LIMITS)
    return RedirectResponse(url="/ui/librarian", status_code=303)
