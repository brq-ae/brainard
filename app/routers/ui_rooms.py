"""UI Agent Chat Rooms (ADR-0006, phase B): the owner-facing portal on top
of phase A's core rooms API (app/rooms.py, app/routers/rooms.py). Owner
cookie session required throughout (app/ui_auth.py), same posture as every
other /ui/* admin surface -- never a machine bearer token.

GET /ui/rooms (list + "new room" form), POST /ui/rooms (create), GET
/ui/rooms/{id} (live view: header, message stream, owner post box, Stop
button), POST /ui/rooms/{id}/post (owner message), POST /ui/rooms/{id}/close
(owner stop). Every one of those calls the exact same shared functions as
the phase A API (app.rooms.create_room/get_room/get_members/
get_recent_messages/post_message/close_room/list_rooms) -- validation,
guardrails, and versioning logic are never duplicated between the two
surfaces (same rule as app/routers/ui_admin.py's machine mint/revoke).

LIVENESS -- SHORT-POLL ONLY, NOT LONG-POLL:
GET /ui/rooms/{id}/messages is a cookie-authed JSON endpoint that
app/static/rooms.js calls every ~2s with the last-seen `seq`. It calls
app.rooms.poll_messages(..., wait=0) -- an IMMEDIATE return, never the
long-poll wait phase A's agent-facing GET /v1/rooms/{id}/messages uses.
Long-polling from *this* endpoint would be wrong: a browser tab open on the
room view would hold one of the app's few request-handling slots (and, if
poll_messages held a session across the wait -- it doesn't, but the request
handler's own auth-check session is still per-request -- a DB-pool
connection) for up to MAX_WAIT_SECS on every single poll, and a caught-up
tab re-polls constantly. wait=0 keeps every owner-UI request short and
cheap; only phase A's machine-facing long-poll endpoint is allowed to hold
open for liveness.

XSS -- agent (and owner) message `text`/`sender` are untrusted, hostile-
capable content (ADR-0006 consequences: "agent-to-agent messaging is an
injection surface"; an agent could post a `<script>` tag or an
`<img onerror=...>`). Two independent renderers see this content and both
must render it inert:
  1. room_view.html's initial server-rendered transcript interpolates
     `text`/`sender` through ordinary Jinja2 (autoescape ON, forced in
     app/templates_env.py) -- never the `md` filter (that's for
     already-sanitized markdown bodies elsewhere in the UI, e.g. library
     entries) and never `|safe`.
  2. app/static/rooms.js, the only consumer of this endpoint's JSON, always
     appends messages via `document.createElement` + `.textContent` /
     `document.createTextNode` -- never `innerHTML`. See that file's header
     comment for the detail.
This endpoint itself just returns the raw message text as a JSON string
value, which is correct: JSON data is not HTML, and the two renderers above
are what keep it from ever becoming HTML.
"""

from fastapi import APIRouter, Depends, Form, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse

from app.db import get_db
from app.errors import ApiError
from app.rooms import close_room as close_room_op
from app.rooms import create_room as create_room_op
from app.rooms import get_members, get_members_for_rooms, get_recent_messages, get_room
from app.rooms import list_rooms as list_rooms_op
from app.rooms import poll_messages as poll_messages_op
from app.rooms import post_message as post_message_op
from app.templates_env import templates
from app.ui_auth import require_csrf, require_ui_session

router = APIRouter(prefix="/ui/rooms", tags=["ui"])

ROOM_LIST_LIMIT = 20
INITIAL_MESSAGE_LIMIT = 50
# Always wait=0 here -- see module docstring's "LIVENESS" section.
SHORT_POLL_WAIT = 0


async def _room_context(db: AsyncSession, room_id: str) -> dict | None:
    """Shared fetch for the room view and for re-rendering it with an error
    after a failed post -- returns None if the room doesn't exist so callers
    can 404.
    """
    room = await get_room(db, room_id)
    if room is None:
        return None
    members = await get_members(db, room_id)
    messages = await get_recent_messages(db, room_id, limit=INITIAL_MESSAGE_LIMIT)
    last_seq = messages[-1].seq if messages else 0
    return {"room": room, "members": members, "messages": messages, "last_seq": last_seq}


# --- list + create ---


@router.get("")
async def rooms_list(
    request: Request,
    cursor: str | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    rows, next_cursor = await list_rooms_op(db, cursor=cursor, limit=ROOM_LIST_LIMIT)
    members_by_room = await get_members_for_rooms(db, [r.id for r in rows])
    return templates.TemplateResponse(
        request,
        "rooms_list.html",
        {
            "csrf_token": session["csrf"],
            "rooms": rows,
            "members_by_room": members_by_room,
            "next_cursor": next_cursor,
            "error": None,
            "form": {},
        },
    )


@router.post("")
async def rooms_create(
    request: Request,
    name: str = Form(...),
    agent_a: str = Form(...),
    agent_b: str = Form(...),
    max_messages: str = Form(default=""),
    # notify_on_close is accepted from the form for forward-compatibility
    # with the checkbox rendered in rooms_list.html, but phase A's
    # create_room() has no parameter for it -- it always creates rooms with
    # notify_on_close=True (see app/rooms.py's create_room and the Room
    # model's docstring: "always true today (no API surface sets it false in
    # phase A)"). Reusing that domain function unchanged (this task's scope)
    # means this value is deliberately not wired to anything; the checkbox
    # is rendered checked+disabled in the template rather than pretending
    # unchecking it would do something it can't.
    notify_on_close: str = Form(default=""),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    parsed_max: int | None = None
    if max_messages.strip():
        try:
            parsed_max = int(max_messages.strip())
        except ValueError:
            # Not an int at all -- feed the domain's own range validator a
            # value guaranteed to be out of range so it raises the same
            # self-explaining ApiError it would for e.g. 0 or 10001, rather
            # than duplicating that validation here.
            parsed_max = 0

    try:
        room = await create_room_op(db, name, [agent_a, agent_b], parsed_max)
    except ApiError as exc:
        rows, next_cursor = await list_rooms_op(db, limit=ROOM_LIST_LIMIT)
        members_by_room = await get_members_for_rooms(db, [r.id for r in rows])
        return templates.TemplateResponse(
            request,
            "rooms_list.html",
            {
                "csrf_token": session["csrf"],
                "rooms": rows,
                "members_by_room": members_by_room,
                "next_cursor": next_cursor,
                "error": exc.detail,
                "form": {"name": name, "agent_a": agent_a, "agent_b": agent_b, "max_messages": max_messages},
            },
            status_code=exc.status_code,
        )
    return RedirectResponse(url=f"/ui/rooms/{room.id}", status_code=303)


# --- live view ---


@router.get("/{room_id}")
async def room_view(
    room_id: str,
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _room_context(db, room_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"No room with id '{room_id}'.")
    return templates.TemplateResponse(
        request,
        "room_view.html",
        {"csrf_token": session["csrf"], "error": None, **ctx},
    )


# --- JSON short-poll (cookie-authed; see module docstring) ---


@router.get("/{room_id}/messages")
async def room_messages_json(
    room_id: str,
    since: int = Query(default=0, ge=0),
    _session: dict = Depends(require_ui_session),
) -> JSONResponse:
    try:
        room, messages = await poll_messages_op(room_id, since, SHORT_POLL_WAIT)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return JSONResponse(
        {
            "messages": [
                {
                    "seq": m.seq,
                    "sender": m.sender,
                    "text": m.text,
                    "kind": m.kind,
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
            "status": room.status,
            "close_reason": room.close_reason,
            "message_count": room.message_count,
        }
    )


# --- owner post + close ---


@router.post("/{room_id}/post")
async def room_post(
    room_id: str,
    request: Request,
    text: str = Form(...),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    try:
        # sender='owner' -- this counts toward the room's message cap same
        # as any member post (app.rooms.post_message's guardrail logic is
        # sender-agnostic beyond the membership check it skips for 'owner').
        await post_message_op(db, room_id, "owner", text)
    except ApiError as exc:
        ctx = await _room_context(db, room_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail=f"No room with id '{room_id}'.") from exc
        return templates.TemplateResponse(
            request,
            "room_view.html",
            {"csrf_token": session["csrf"], "error": exc.detail, **ctx},
            status_code=exc.status_code,
        )
    return RedirectResponse(url=f"/ui/rooms/{room_id}", status_code=303)


@router.post("/{room_id}/close")
async def room_close(
    room_id: str,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    try:
        await close_room_op(db, room_id, "owner")
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return RedirectResponse(url=f"/ui/rooms/{room_id}", status_code=303)
