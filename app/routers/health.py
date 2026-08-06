"""Unauthenticated liveness/readiness check."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import AsyncSessionLocal

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> JSONResponse:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False

    return JSONResponse(
        status_code=200 if database_ok else 503,
        content={"ok": database_ok, "database": "reachable" if database_ok else "unreachable"},
    )
