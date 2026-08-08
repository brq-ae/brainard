"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.errors import ApiError, api_error_handler
from app.routers import (
    bootstrap,
    deposits,
    doctrine,
    events,
    export,
    flags,
    health,
    library,
    machines,
    projects,
    proposals,
    search,
    ui_admin,
    ui_dashboard,
    ui_doctrine,
    ui_journal,
    ui_library,
    ui_login,
    ui_projects,
    ui_search,
)
from app.startup import bootstrap_owner_token
from app.ui_auth import UIAuthRequired


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await bootstrap_owner_token()
    yield


app = FastAPI(title="The Brain", version="0.1.0", lifespan=lifespan)

app.add_exception_handler(ApiError, api_error_handler)


async def _ui_auth_required_handler(_request: Request, _exc: UIAuthRequired) -> RedirectResponse:
    # No valid owner session cookie (missing/expired/forged) -- bounce to
    # the login page. Never a JSON 401: this handler only ever fires for
    # /ui/* routes, which are pages, not API calls (phase 6 brief: "GET /
    # redirects to /ui/login unless authenticated").
    return RedirectResponse(url="/ui/login", status_code=303)


app.add_exception_handler(UIAuthRequired, _ui_auth_required_handler)

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# --- v1 API (bearer-token only; entirely unaffected by the UI's cookie auth) ---
app.include_router(health.router)
app.include_router(machines.router)
app.include_router(deposits.router)
app.include_router(library.router)
app.include_router(flags.router)
app.include_router(events.router)
app.include_router(search.router)
app.include_router(doctrine.router)
app.include_router(proposals.router)
app.include_router(projects.router)
app.include_router(bootstrap.router)
app.include_router(export.router)

# --- UI (owner cookie session; see app/ui_auth.py) ---
app.include_router(ui_login.router)
app.include_router(ui_dashboard.router)
app.include_router(ui_library.router)
app.include_router(ui_search.router)
app.include_router(ui_projects.router)
app.include_router(ui_journal.router)
app.include_router(ui_doctrine.router)
app.include_router(ui_admin.router)


@app.get("/")
async def root() -> RedirectResponse:
    # Unauthenticated visitors bounce onward to /ui/login via the
    # UIAuthRequired handler above, once they hit any session-gated /ui/*
    # route -- /ui itself included.
    return RedirectResponse(url="/ui", status_code=303)
