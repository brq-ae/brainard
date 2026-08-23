"""Room AI actions -- POST /v1/rooms/{id}/ai/{action} (ADR-0011 decision 2).

Owner-only: these actions spend the owner's configured LLM provider budget,
same trust posture as room creation/close/mode-switch (app/routers/rooms.py).
Deliberately kept in its OWN router file rather than added to
app/routers/rooms.py -- ADR-0011 is explicit that the existing v1 rooms API
contract is not to be touched by this feature; this file only adds a new
path, it never edits the existing one.

Calls app.room_ai.run_action -- the exact same domain function the owner UI
(app/routers/ui_rooms.py) calls for its own JS-fetched equivalent -- so the
two surfaces can never drift on what an action does; only the credential
(bearer token here, cookie session there) and response envelope differ.
`ApiError` is deliberately left to propagate to the app-level handler
(app/errors.py's `api_error_handler`, registered in app/main.py) rather
than being caught here, so every self-explaining rejection (unknown
action/room, no provider configured, an unusable model response) comes
back in the contract's ordinary `{"error": {...}}` envelope.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.room_ai import run_action
from app.schemas import RoomAiActionResponse

router = APIRouter(prefix="/v1/rooms", tags=["rooms-ai"])


@router.post("/{room_id}/ai/{action}", response_model=RoomAiActionResponse)
async def run_room_ai_action_endpoint(
    room_id: str,
    action: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomAiActionResponse:
    result = await run_action(db, room_id, action)
    return RoomAiActionResponse(
        room_id=room_id,
        action=result.action,
        result=result.result,
        truncated=result.truncated,
        truncated_notice=result.truncated_notice,
    )
