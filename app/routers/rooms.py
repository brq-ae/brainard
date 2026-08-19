"""Agent Chat Rooms -- Phase A core API (ADR-0006): two-agent rooms, live via
long-poll, with guardrails (done-signal close, hard message cap, owner
close) and a best-effort owner ntfy ping on close. No UI here -- phase B.

Auth: room creation and owner-close are owner-only (same trust posture as
doctrine/notifications-config -- deciding who's allowed to talk and stopping
a room are owner calls); everything else (list/read/post/long-poll) is
machine-or-owner, matching the rest of the session-facing surface.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_machine_or_owner, require_owner
from app.db import get_db
from app.errors import ApiError
from app.rooms import (
    create_room,
    get_member_sides,
    get_member_sides_for_rooms,
    get_members,
    get_members_for_rooms,
    get_recent_messages,
    get_room,
)
from app.rooms import close_room as close_room_op
from app.rooms import list_rooms as list_rooms_op
from app.rooms import poll_messages as poll_messages_op
from app.rooms import post_message as post_message_op
from app.schemas import (
    RoomCloseRequest,
    RoomCloseResponse,
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDetailResponse,
    RoomListItem,
    RoomListResponse,
    RoomMessageOut,
    RoomMessagesPollResponse,
    RoomPostMessageRequest,
    RoomPostMessageResponse,
)

router = APIRouter(prefix="/v1/rooms", tags=["rooms"])


@router.post("", response_model=RoomCreateResponse, status_code=201)
async def create_room_endpoint(
    body: RoomCreateRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomCreateResponse:
    room = await create_room(
        db,
        body.name,
        body.members,
        body.max_messages,
        mode=body.mode,
        topic=body.topic,
        sides=body.sides,
        duration_seconds=body.duration_seconds,
        expires_at=body.expires_at,
    )
    members = await get_members(db, room.id)
    sides = await get_member_sides(db, room.id)
    return RoomCreateResponse(
        id=room.id,
        name=room.name,
        status=room.status,
        members=members,
        max_messages=room.max_messages,
        mode=room.mode,
        topic=room.topic,
        expires_at=room.expires_at,
        sides=sides,
    )


@router.get("", response_model=RoomListResponse)
async def list_rooms_endpoint(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomListResponse:
    rows, next_cursor = await list_rooms_op(db, cursor=cursor, limit=limit)
    room_ids = [r.id for r in rows]
    members_by_room = await get_members_for_rooms(db, room_ids)
    sides_by_room = await get_member_sides_for_rooms(db, room_ids)
    return RoomListResponse(
        results=[
            RoomListItem(
                id=r.id,
                name=r.name,
                status=r.status,
                members=members_by_room.get(r.id, []),
                message_count=r.message_count,
                max_messages=r.max_messages,
                created_at=r.created_at,
                close_reason=r.close_reason,
                mode=r.mode,
                topic=r.topic,
                expires_at=r.expires_at,
                sides=sides_by_room.get(r.id, {}),
            )
            for r in rows
        ],
        next_cursor=next_cursor,
    )


@router.get("/{room_id}", response_model=RoomDetailResponse)
async def get_room_endpoint(
    room_id: str,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomDetailResponse:
    room = await get_room(db, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
    members = await get_members(db, room_id)
    sides = await get_member_sides(db, room_id)
    messages = await get_recent_messages(db, room_id)
    return RoomDetailResponse(
        id=room.id,
        name=room.name,
        status=room.status,
        members=members,
        max_messages=room.max_messages,
        message_count=room.message_count,
        notify_on_close=room.notify_on_close,
        created_at=room.created_at,
        closed_at=room.closed_at,
        close_reason=room.close_reason,
        mode=room.mode,
        topic=room.topic,
        expires_at=room.expires_at,
        sides=sides,
        messages=[RoomMessageOut.model_validate(m) for m in messages],
    )


@router.post("/{room_id}/messages", response_model=RoomPostMessageResponse)
async def post_room_message_endpoint(
    room_id: str,
    body: RoomPostMessageRequest,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomPostMessageResponse:
    message, room = await post_message_op(db, room_id, body.sender, body.text, body.kind)
    return RoomPostMessageResponse(id=message.id, seq=message.seq, room_status=room.status, close_reason=room.close_reason)


@router.post("/{room_id}/close", response_model=RoomCloseResponse)
async def close_room_endpoint(
    room_id: str,
    body: RoomCloseRequest | None = None,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomCloseResponse:
    reason = body.reason if body is not None else None
    room = await close_room_op(db, room_id, reason)
    return RoomCloseResponse(id=room.id, status=room.status, close_reason=room.close_reason, closed_at=room.closed_at)


# The long-poll. Deliberately takes NO `db: AsyncSession = Depends(get_db)`
# -- app/rooms.py's `poll_messages` manages its own short-lived sessions per
# iteration so nothing here holds a session across the wait. See
# app/rooms.py's `poll_messages` docstring for the full reasoning.
@router.get("/{room_id}/messages", response_model=RoomMessagesPollResponse)
async def poll_room_messages_endpoint(
    room_id: str,
    since: int = Query(default=0, ge=0),
    wait: int = Query(default=0, ge=0),
    _principal: Principal = Depends(require_machine_or_owner),
) -> RoomMessagesPollResponse:
    room, messages = await poll_messages_op(room_id, since, wait)
    return RoomMessagesPollResponse(
        room_status=room.status,
        messages=[RoomMessageOut.model_validate(m) for m in messages],
    )
