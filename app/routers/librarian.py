"""Built-in librarian control -- GET /v1/librarian/runs, POST /v1/librarian/run
(ADR-0010 phase 2: pluggable librarian runtimes, the deterministic built-in
engine). Owner-token only: triggering/observing the built-in curation job is
an administrative action, same trust posture as llm-config
(app/routers/llm_config.py) and notifications config.

POST /v1/librarian/run runs the engine INLINE (awaited within the request,
under a generous overall timeout) rather than firing a background task the
caller has to poll for. `run_librarian`'s own bounds (per-run caps on flags/
lessons/LLM calls, a per-call timeout, and a hard stop after repeated
provider failures -- app/librarian_engine.py) already keep a single run's
wall-clock time bounded and modest in practice, so blocking the request is
an acceptable, much simpler trade for a single-owner LAN admin action: the
response IS the finished run, with no separate "check back later" step or
"running" status to represent. `INLINE_RUN_TIMEOUT_SECS` is a defensive
backstop only, for a pathologically slow/misbehaving provider.
"""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.auth import Principal, require_owner
from app.db import AsyncSessionLocal, get_db
from app.errors import ApiError
from app.librarian_engine import DEFAULT_LIMITS, list_librarian_runs, record_timeout_run, run_librarian
from app.models import LibrarianRun
from app.schemas import LibrarianRunItem, LibrarianRunListResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/librarian", tags=["librarian"])

INLINE_RUN_TIMEOUT_SECS = 600.0  # 10 minutes -- see module docstring


def _run_out(row: LibrarianRun) -> LibrarianRunItem:
    return LibrarianRunItem(
        id=row.id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        counts=row.counts or {},
        error=row.error,
    )


@router.get("/runs", response_model=LibrarianRunListResponse)
async def get_librarian_runs(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> LibrarianRunListResponse:
    rows, next_cursor = await list_librarian_runs(db, cursor=cursor, limit=limit)
    return LibrarianRunListResponse(results=[_run_out(r) for r in rows], next_cursor=next_cursor)


@router.post("/run", response_model=LibrarianRunItem)
async def trigger_librarian_run(
    _owner: Principal = Depends(require_owner),
) -> LibrarianRunItem:
    run_id = str(ULID())
    started_at = datetime.now(UTC)
    try:
        result = await asyncio.wait_for(run_librarian(limits=DEFAULT_LIMITS, run_id=run_id), timeout=INLINE_RUN_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        # `wait_for` has already cancelled the in-flight `run_librarian` call
        # and waited for it to unwind -- it never reached its own
        # `_record_run`, so without this the timeout would leave no trace in
        # librarian_runs history at all. Record one 'error' row for the
        # run_id we already minted, then report a clean enveloped error --
        # never a bare 500.
        reason = f"the librarian run did not finish within {INLINE_RUN_TIMEOUT_SECS:.0f}s and was cancelled"
        await record_timeout_run(AsyncSessionLocal, run_id, started_at, reason)
        raise ApiError(
            503,
            "librarian_run_timeout",
            f"The librarian run did not finish within {INLINE_RUN_TIMEOUT_SECS:.0f}s and was aborted "
            "(recorded in librarian run history as an error). Recovery: check server logs; if the "
            "configured provider is slow or unreachable, fix that first, then retry.",
        ) from None

    return LibrarianRunItem(
        id=result.run_id,
        started_at=result.started_at,
        finished_at=result.finished_at,
        status=result.status,
        counts=result.counts,
        error=result.error,
    )
