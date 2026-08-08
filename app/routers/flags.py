"""Flags -- GET /v1/flags, POST /v1/flags/{id}/resolve (contracts-v1.md §3;
ADR-0004: fork/duplicate signals are "the librarian's inbox," fully
specified by the contracts themselves).

`GET /v1/flags` accepts machine OR owner token, matching the precedent
already set by GET /v1/library/{id}, GET /v1/search, and GET /v1/projects
(all session-facing reads open to either credential kind). `POST
/v1/flags/{id}/resolve` is machine-token only: resolving is an action
attributed to a machine (`resolved_by` comes straight from the authenticated
token, same reasoning as knowledge[]'s `machine_id`/`source`) -- the owner
has no machine identity to attribute a resolution to.

The actual list/resolve query logic lives in app/flags.py.
"""

from fastapi import APIRouter, Depends, Query

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_machine, require_machine_or_owner
from app.db import get_db
from app.errors import ApiError
from app.flags import VALID_FLAG_TYPES, list_flags, resolve_flag
from app.models import Flag
from app.schemas import FlagListItem, FlagListResponse, FlagResolveResponse

router = APIRouter(prefix="/v1/flags", tags=["flags"])


def _flag_out(f: Flag) -> FlagListItem:
    return FlagListItem(
        id=f.id,
        type=f.type,
        entry_id=f.entry_id,
        related_entry_id=f.related_entry_id,
        detail=f.detail,
        created_at=f.created_at,
        resolved_at=f.resolved_at,
        resolved_by=f.resolved_by,
    )


@router.get("", response_model=FlagListResponse)
async def get_flags(
    unresolved: bool = Query(default=True),
    type: str | None = Query(default=None),
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> FlagListResponse:
    if type is not None and type not in VALID_FLAG_TYPES:
        raise ApiError(
            422,
            "invalid_flag_type",
            f"`type` must be one of {sorted(VALID_FLAG_TYPES)}, got {type!r}.",
        )
    rows, next_cursor = await list_flags(db, unresolved=unresolved, type=type, cursor=cursor, limit=limit)
    return FlagListResponse(results=[_flag_out(f) for f in rows], next_cursor=next_cursor)


@router.post("/{flag_id}/resolve", response_model=FlagResolveResponse)
async def resolve_flag_route(
    flag_id: str,
    principal: Principal = Depends(require_machine),
    db: AsyncSession = Depends(get_db),
) -> FlagResolveResponse:
    flag, already_resolved = await resolve_flag(db, flag_id, principal.machine.id)
    return FlagResolveResponse(
        id=flag.id,
        resolved_at=flag.resolved_at,
        resolved_by=flag.resolved_by,
        already_resolved=already_resolved,
    )
