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
wall-clock time bounded, so blocking the request is an acceptable, much
simpler trade for a single-owner LAN admin action: the response IS the
finished run, with no separate "check back later" step or "running" status
to represent. The wrapper timeout (`effective_inline_run_timeout_secs`
below) is a defensive backstop, for a pathologically slow/misbehaving
provider -- but it must scale with the run's own real budget (up to
`max_llm_calls` SEQUENTIAL judgment calls, each allowed up to
`llm_call_timeout_secs`) or it becomes a false alarm: a handful of slow-but-
successful calls against a local/reasoning model can legitimately take
longer than an old flat default sized for fast hosted APIs. See
app/config.py's `librarian_inline_run_timeout_secs` for the full rationale
and the override knob (`LIBRARIAN_INLINE_RUN_TIMEOUT_SECS`).
"""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.auth import Principal, require_owner
from app.config import get_settings
from app.db import AsyncSessionLocal, get_db
from app.errors import ApiError
from app.librarian_engine import DEFAULT_LIMITS, LibrarianLimits, list_librarian_runs, record_timeout_run, run_librarian
from app.models import LibrarianRun
from app.schemas import LibrarianRunItem, LibrarianRunListResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/librarian", tags=["librarian"])

# The previous flat default (10 minutes) -- now only a FLOOR on the derived
# timeout (see `effective_inline_run_timeout_secs`), so an owner who never
# touches LLM_CALL_TIMEOUT_SECS/max_llm_calls sees no change at all.
INLINE_RUN_TIMEOUT_FLOOR_SECS = 600.0
# A sane cap on the derived timeout even for a large max_llm_calls *
# llm_call_timeout_secs product -- 1 hour is already a long time to block a
# synchronous owner request; beyond this, explicit
# LIBRARIAN_INLINE_RUN_TIMEOUT_SECS is the right lever, not an ever-larger
# automatic derivation.
INLINE_RUN_TIMEOUT_CEILING_SECS = 3600.0


def effective_inline_run_timeout_secs(limits: LibrarianLimits = DEFAULT_LIMITS) -> float:
    """The timeout actually applied to the synchronous `POST
    /v1/librarian/run` request. `LIBRARIAN_INLINE_RUN_TIMEOUT_SECS`, if set,
    wins outright (app/config.py already validates it's positive and
    bounded). Otherwise DERIVED from the run's own real budget --
    `limits.max_llm_calls * limits.call_timeout_secs`, i.e. the worst case
    if every single judgment call in the run took the full configured
    per-call timeout -- clamped to [INLINE_RUN_TIMEOUT_FLOOR_SECS,
    INLINE_RUN_TIMEOUT_CEILING_SECS] so it's never shorter than the old flat
    default and never unreasonably long. A flat number here (the old
    behavior) doesn't scale with either factor: raising
    LLM_CALL_TIMEOUT_SECS for a local reasoning model -- exactly what a real
    deployment needed -- made a flat 600s wrapper disproportionately short,
    since a run can make up to `max_llm_calls` of those calls in sequence.
    """
    override = get_settings().librarian_inline_run_timeout_secs
    if override is not None:
        return override
    derived = limits.max_llm_calls * limits.call_timeout_secs
    return min(max(derived, INLINE_RUN_TIMEOUT_FLOOR_SECS), INLINE_RUN_TIMEOUT_CEILING_SECS)


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
    timeout = effective_inline_run_timeout_secs(DEFAULT_LIMITS)
    try:
        result = await asyncio.wait_for(run_librarian(limits=DEFAULT_LIMITS, run_id=run_id), timeout=timeout)
    except asyncio.TimeoutError:
        # `wait_for` has already cancelled the in-flight `run_librarian` call
        # and waited for it to unwind -- it never reached its own
        # `_record_run`, so without this the timeout would leave no trace in
        # librarian_runs history at all. Record one 'error' row for the
        # run_id we already minted, then report a clean enveloped error --
        # never a bare 500.
        reason = f"the librarian run did not finish within {timeout:.0f}s (LIBRARIAN_INLINE_RUN_TIMEOUT_SECS) and was cancelled"
        await record_timeout_run(AsyncSessionLocal, run_id, started_at, reason)
        raise ApiError(
            503,
            "librarian_run_timeout",
            f"The librarian run did not finish within {timeout:.0f}s and was aborted (recorded in librarian "
            "run history as an error). Recovery: if the configured provider is just slow (e.g. a local or "
            "reasoning model working through many judgment calls), raise LIBRARIAN_INLINE_RUN_TIMEOUT_SECS "
            "and retry; if it seems actually stuck/unreachable, check server logs and fix that first.",
        ) from None

    return LibrarianRunItem(
        id=result.run_id,
        started_at=result.started_at,
        finished_at=result.finished_at,
        status=result.status,
        counts=result.counts,
        error=result.error,
    )
