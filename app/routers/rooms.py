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
from app.rooms import assign_group_to_rooms as assign_group_to_rooms_op
from app.rooms import close_room as close_room_op
from app.rooms import delete_message as delete_message_op
from app.rooms import delete_room as delete_room_op
from app.rooms import list_rooms as list_rooms_op
from app.rooms import poll_messages as poll_messages_op
from app.rooms import post_message as post_message_op
from app.rooms import set_requires_owner_open as set_requires_owner_open_op
from app.rooms import switch_room_mode as switch_room_mode_op
from app.schemas import (
    RoomCloseRequest,
    RoomCloseResponse,
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDeleteResponse,
    RoomDetailResponse,
    RoomGroupAssignRequest,
    RoomGroupAssignResponse,
    RoomListItem,
    RoomListResponse,
    RoomMessageDeleteResponse,
    RoomMessageOut,
    RoomMessagesPollResponse,
    RoomModeSwitchRequest,
    RoomModeSwitchResponse,
    RoomOpenGateRequest,
    RoomOpenGateResponse,
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
        group=body.group,
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
        group=room.group_name,
    )


@router.get("", response_model=RoomListResponse)
async def list_rooms_endpoint(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    # ADR-0008: optional exact-match group filter -- omitted means "all
    # groups", not "ungrouped only" (see app/rooms.py's list_rooms docstring).
    group: str | None = None,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomListResponse:
    rows, next_cursor = await list_rooms_op(db, cursor=cursor, limit=limit, group=group)
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
                group=r.group_name,
            )
            for r in rows
        ],
        next_cursor=next_cursor,
    )


@router.post("/assign-group", response_model=RoomGroupAssignResponse)
async def assign_room_group_endpoint(
    body: RoomGroupAssignRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomGroupAssignResponse:
    """ADR-0008: bulk-assigns (or clears, group=null/blank) a group label
    across multiple rooms in one call -- see app/rooms.py's
    `assign_group_to_rooms` for the all-or-nothing validation (unknown ids
    404 before anything is written).

    Registered before `GET /{room_id}` in this file so this static path
    reads clearly next to the other collection-level routes; there is no
    actual routing ambiguity either way since no other route here is a bare
    `POST /{room_id}` that "assign-group" could be mistaken for.
    """
    updated, resolved_group = await assign_group_to_rooms_op(db, body.room_ids, body.group)
    return RoomGroupAssignResponse(updated=updated, group=resolved_group)


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
        agent_uploads_allowed=room.agent_uploads_allowed,
        created_at=room.created_at,
        closed_at=room.closed_at,
        close_reason=room.close_reason,
        mode=room.mode,
        topic=room.topic,
        expires_at=room.expires_at,
        sides=sides,
        group=room.group_name,
        opened_at=room.opened_at,
        requires_owner_open=room.requires_owner_open,
        messages=[RoomMessageOut.model_validate(m) for m in messages],
    )


@router.post("/{room_id}/messages", response_model=RoomPostMessageResponse)
async def post_room_message_endpoint(
    room_id: str,
    body: RoomPostMessageRequest,
    principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomPostMessageResponse:
    # `principal` (not just `body.sender`) is passed through so post_message
    # can reject a machine token claiming `sender="owner"` -- see that
    # function's docstring for the impersonation this closes.
    message, room = await post_message_op(db, room_id, body.sender, body.text, body.kind, principal=principal)
    return RoomPostMessageResponse(id=message.id, seq=message.seq, room_status=room.status, close_reason=room.close_reason)


@router.delete("/{room_id}/messages/{message_id}", response_model=RoomMessageDeleteResponse)
async def delete_room_message_endpoint(
    room_id: str,
    message_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomMessageDeleteResponse:
    """ADR-0015: owner-only tombstone delete of one message -- `require_owner`
    (same dependency guarding `create_room_endpoint`/`close_room_endpoint`/
    `switch_room_mode_endpoint`/`delete_room_endpoint` above) means there is
    no machine-token path to this endpoint at all, not even for the room's
    own members: an agent can never delete a message (decision 1). See
    app/rooms.py's `delete_message` for the tombstone/counter-decrement
    logic; this route only wires request/response shapes to it.
    """
    message, room = await delete_message_op(db, room_id, message_id)
    return RoomMessageDeleteResponse(
        id=message.id, seq=message.seq, deleted_at=message.deleted_at, message_count=room.message_count
    )


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


@router.post("/{room_id}/open-gate", response_model=RoomOpenGateResponse)
async def set_room_open_gate_endpoint(
    room_id: str,
    body: RoomOpenGateRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomOpenGateResponse:
    """ADR-0014 decision 4: owner-only mid-session toggle of the room's
    owner-open requirement -- see app/rooms.py's `set_requires_owner_open`
    for the validation/locking/announcement logic; this route only wires
    the request/response shapes to it.
    """
    room, announcement = await set_requires_owner_open_op(db, room_id, body.required)
    return RoomOpenGateResponse(
        id=room.id,
        requires_owner_open=room.requires_owner_open,
        opened_at=room.opened_at,
        expires_at=room.expires_at,
        announcement=announcement,
    )


@router.post("/{room_id}/mode", response_model=RoomModeSwitchResponse)
async def switch_room_mode_endpoint(
    room_id: str,
    body: RoomModeSwitchRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomModeSwitchResponse:
    """ADR-0009: owner-only mid-session mode switch -- see app/rooms.py's
    `switch_room_mode` for the validation/locking/announcement logic; this
    route only wires the request/response shapes to it.
    """
    room, announcement = await switch_room_mode_op(db, room_id, body.mode, body.topic, body.sides)
    sides = await get_member_sides(db, room.id)
    return RoomModeSwitchResponse(
        id=room.id, mode=room.mode, topic=room.topic, sides=sides, announcement=announcement
    )


@router.delete("/{room_id}", response_model=RoomDeleteResponse)
async def delete_room_endpoint(
    room_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomDeleteResponse:
    """ADR-0008: owner-only hard delete -- works on an open OR closed room
    (delete is independent of a room's open/closed status). 404s for an
    unknown/already-deleted id (app/rooms.py's `delete_room`), so a repeat
    call is self-explaining rather than a silent success.
    """
    deleted_messages, deleted_members = await delete_room_op(db, room_id)
    return RoomDeleteResponse(id=room_id, deleted_messages=deleted_messages, deleted_members=deleted_members)


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
    room, messages, open_gate_notice = await poll_messages_op(room_id, since, wait)
    return RoomMessagesPollResponse(
        room_status=room.status,
        messages=[RoomMessageOut.model_validate(m) for m in messages],
        open_gate_notice=open_gate_notice,
    )
