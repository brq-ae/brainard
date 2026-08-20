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

Member names are the same kind of untrusted content (a room's `members`
strings are owner-supplied at create time -- see app/rooms.py's
`_validate_members` -- but the room view also flows them into the
generated per-member join prompt below, so the same autoescape discipline
applies there too: `room_view.html` renders each `join_prompts[m]` and the
member label through ordinary Jinja2, never `|safe`).

A room's `topic` is the same kind of owner-supplied-but-untrusted content
(ADR-0007) and now renders in two places: room_view.html's header
(`{{ room.topic }}`, plain Jinja2 autoescape, no `|safe`) and inside every
join-prompt box's generated text (`join_prompts[m]`, same rendering as
member names above -- the generator, app.onboarding's
`generate_room_join_prompt`/`_room_session_block`, only ever builds plain
text, so escaping happens once, at render time, same as everywhere else on
this page). The mode label and side labels shown alongside it come from
ROOM_MODES (app/room_modes.py), which is trusted, static, server-authored
text -- not user input -- so no escaping concern applies there.

PHASE C -- JOIN PROMPTS: `_join_prompts_by_member` generates, for each of a
room's two members, the copy-paste prompt (app/onboarding.py's
`generate_room_join_prompt`) that drops that agent into the room's respond-
loop -- the room-view analogue of app/routers/ui_admin.py's
`_prompts_by_machine_id` for the onboarding prompt. Always rendered with
`TOKEN_PLACEHOLDER` standing in for the token: a room member is identified
by a free-text `agent_name` string, not a minted Machine row, so there is
no real per-member token to look up here in the first place (same
placeholder convention, different reason than the onboarding case).

ADR-0007 (PART 2 -- MODES + TIME LIMITS UI): the create-room form gains a
mode dropdown, a topic field, and a time-limit preset (both sourced from
app/room_modes.py's ROOM_MODES -- see `ROOM_MODES_JSON` and
`_sides_for_mode`/`_parse_duration_seconds` below); the live view's header
shows the mode/topic and a live countdown to `expires_at`
(app/static/rooms.js). All of ADR-0007's actual validation (mode, topic,
sides, deadline range) still lives entirely in app/rooms.py's create_room --
this module only resolves the HTML form's shape into that function's plain
keyword arguments and lets its ApiErrors render as the existing clean
422/etc. responses on this page, same as phase A's max_messages handling
already does. `_join_prompts_by_member` now also threads the room's
mode/topic/side/deadline through to `generate_room_join_prompt` (previously
dead/defaulted params flagged by a prior review) so a debate/critique room's
join prompts carry the real stance + topic + deadline text, not just the
generic freeform framing.

ADR-0008 (DELETE + FREE-FORM GROUPS): rooms_list.html gains a per-room
Delete button (POST /ui/rooms/{id}/delete, owner cookie + CSRF, confirmed
client-side via the existing generic `data-confirm` handler in
app/static/main.js -- no new JS file needed), a Group column, a `?group=`
filter (server-side, via app.rooms.list_rooms's own `group` param), and a
bulk "assign selected rooms to a group" form (POST /ui/rooms/assign-group).
The bulk form's room checkboxes live inline in each room row but are NOT
nested inside the bulk `<form>` (HTML forbids nested forms) -- they use the
HTML5 `form="rooms-bulk-form"` attribute to associate with the bulk form
that wraps just the group-name field and submit button, same technique
needed because each row's own Delete button is *also* its own separate,
sibling `<form>`. `group` is owner-supplied free text and, like message
text/sender/topic/member-name above, is untrusted content the moment it
renders anywhere: rendered here only through ordinary Jinja2 autoescape
(never `|safe`), same discipline as everywhere else on this page.
"""

import json

from fastapi import APIRouter, Depends, Form, Query, Request
from markupsafe import Markup
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse

from app.db import get_db
from app.errors import ApiError
from app.models import Room
from app.onboarding import TOKEN_PLACEHOLDER, generate_room_join_prompt, resolve_base_url
from app.room_modes import DEFAULT_MODE, ROOM_MODES
from app.rooms import assign_group_to_rooms as assign_group_to_rooms_op
from app.rooms import close_room as close_room_op
from app.rooms import create_room as create_room_op
from app.rooms import delete_room as delete_room_op
from app.rooms import get_member_sides, get_members, get_members_for_rooms, get_recent_messages, get_room
from app.rooms import list_room_groups as list_room_groups_op
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

# --- ADR-0007: room modes + time limits (Part 2, UI only) ---
#
# The create-room form's mode dropdown and its mode->sides/labels JS handler
# (app/static/room_form.js) are both sourced from this single ROOM_MODES
# import -- never a hardcoded, divergent list of modes in the template or JS.
# ROOM_MODES_JSON is computed once at import time (the data is static/
# trusted, defined entirely in app/room_modes.py -- never user input), and
# wrapped in `Markup` so Jinja2's autoescape (HTML-entity escaping) does not
# corrupt the JSON when it's embedded in a `<script type="application/json">`
# block: HTML parsers treat `<script>` contents as raw text, so an
# HTML-entity-escaped `"` (`&#34;`) would reach `JSON.parse` unescaped and
# fail to parse. This is safe *only* because the payload is fully
# server-controlled static data with no user-supplied substrings (e.g. no
# room topic/agent name is ever included here). As defense-in-depth for a
# future field that might add user-derived data to this payload, every `<`
# is additionally escaped to `<` (a valid JSON escape -- JSON.parse
# still parses it identically to a literal `<`) so a `</script>` sequence
# could never prematurely close the enclosing <script> tag either way.
_ROOM_MODES_PAYLOAD = {
    key: {
        "label": mode_def.label,
        "symmetric": mode_def.symmetric,
        # Ordered [first_side_label, second_side_label] matching
        # RoomMode.sides's tuple order, so the JS can map agent_a/agent_b's
        # form position to the correct side without re-deriving mode
        # semantics -- None for symmetric modes (no sides).
        "side_labels": [mode_def.side_labels[s] for s in mode_def.sides] if mode_def.sides else None,
    }
    for key, mode_def in ROOM_MODES.items()
}
ROOM_MODES_JSON = Markup(json.dumps(_ROOM_MODES_PAYLOAD).replace("<", "\\u003c"))

_CUSTOM_DURATION_UNIT_SECONDS = {"minutes": 60, "hours": 3600}


def _sides_for_mode(mode: str, agent_a: str, agent_b: str) -> dict[str, str] | None:
    """Builds `{agent_name: side}` for asymmetric modes by assigning the
    mode's two distinct side keys (app/room_modes.py's `ROOM_MODES[mode]
    .sides`, e.g. ('for', 'against')) in order to agent_a/agent_b -- i.e.
    the first form field always gets the mode's first side, matching the
    labels app/static/room_form.js renders onto those same two fields.
    Returns None for symmetric/freeform modes (sides ignored) and for an
    unrecognized `mode` string (create_room's own `validate_mode` raises
    the self-explaining error for that case; this just avoids a KeyError
    here so that error is the one the owner sees).
    """
    mode_def = ROOM_MODES.get(mode)
    if mode_def is None or mode_def.sides is None:
        return None
    first_side, second_side = mode_def.sides
    return {agent_a: first_side, agent_b: second_side}


def _parse_duration_seconds(preset: str, custom_value: str, custom_unit: str) -> int | None:
    """Maps the create-room form's time-limit dropdown to `duration_seconds`
    for app.rooms.create_room -- which is the sole owner of range/type
    validation (self-explaining ApiError; ADR-0007 decision leaves
    out-of-range customs to "the domain's _validate_deadline"). This only
    resolves the UI's preset-or-custom selection down to a single int or
    None; it never validates a range itself. Mirrors the existing
    `max_messages` parsing below: an unparseable/empty custom amount maps to
    0 (or "" -> None for a bad preset), a value guaranteed to be rejected by
    the domain's own range check rather than silently guessed at here.
    """
    preset = preset.strip()
    if not preset:
        return None  # "No limit"
    if preset == "custom":
        value = custom_value.strip()
        if not value:
            return 0
        try:
            amount = int(value)
        except ValueError:
            return 0
        unit_seconds = _CUSTOM_DURATION_UNIT_SECONDS.get(custom_unit, 60)
        return amount * unit_seconds
    try:
        return int(preset)
    except ValueError:
        return 0


def _join_prompts_by_member(
    request: Request, room: Room, members: list[str], sides: dict[str, str | None]
) -> dict[str, str]:
    """The generated room-join prompt (app/onboarding.py's
    `generate_room_join_prompt`) for each of the room's members, keyed by
    agent name -- always with `TOKEN_PLACEHOLDER` standing in for the token
    (see this module's docstring, "PHASE C -- JOIN PROMPTS"). Phase A's
    `create_room` enforces exactly two distinct members (app/rooms.py's
    `REQUIRED_MEMBER_COUNT`), so each member's partner is simply "the other
    one of the two" -- there is no 3+-member case to handle here.

    ADR-0007: also passes the room's mode/topic/expires_at and this member's
    own side (from `sides`, app.rooms.get_member_sides) through to
    `generate_room_join_prompt` -- these were previously dead/defaulted
    params (a prior review flagged this), so e.g. a debate room's join
    prompts now actually carry the For/Against stance text, the topic, and
    the deadline line, not just the generic freeform framing.
    """
    base_url = resolve_base_url(request)
    return {
        agent_name: generate_room_join_prompt(
            base_url=base_url,
            room_id=room.id,
            agent_name=agent_name,
            partner_name=next(other for other in members if other != agent_name),
            token=TOKEN_PLACEHOLDER,
            mode=room.mode,
            topic=room.topic,
            side=sides.get(agent_name),
            deadline=room.expires_at,
        )
        for agent_name in members
    }


async def _room_context(db: AsyncSession, request: Request, room_id: str) -> dict | None:
    """Shared fetch for the room view and for re-rendering it with an error
    after a failed post -- returns None if the room doesn't exist so callers
    can 404.
    """
    room = await get_room(db, room_id)
    if room is None:
        return None
    members = await get_members(db, room_id)
    sides = await get_member_sides(db, room_id)
    messages = await get_recent_messages(db, room_id, limit=INITIAL_MESSAGE_LIMIT)
    last_seq = messages[-1].seq if messages else 0
    mode_def = ROOM_MODES[room.mode]
    return {
        "room": room,
        "members": members,
        "sides": sides,
        "mode_label": mode_def.label,
        "side_labels": mode_def.side_labels,
        "messages": messages,
        "last_seq": last_seq,
        "join_prompts": _join_prompts_by_member(request, room, members, sides),
    }


# --- list + create ---


@router.get("")
async def rooms_list(
    request: Request,
    cursor: str | None = Query(default=None),
    # ADR-0008: optional exact-match group filter, server-side via
    # app.rooms.list_rooms's own `group` param -- None/absent means "all
    # groups" (see that function's docstring).
    group: str | None = Query(default=None),
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    rows, next_cursor = await list_rooms_op(db, cursor=cursor, limit=ROOM_LIST_LIMIT, group=group)
    members_by_room = await get_members_for_rooms(db, [r.id for r in rows])
    groups = await list_room_groups_op(db)
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
            "room_modes": ROOM_MODES,
            "room_modes_json": ROOM_MODES_JSON,
            "groups": groups,
            "group_filter": group,
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
    # --- ADR-0007: room modes and time limits ---
    # `mode`/`topic` and the time-limit preset drive app.rooms.create_room's
    # own mode/topic/sides/duration_seconds validation (self-explaining
    # ApiErrors) -- nothing about mode/topic/side/duration validity is
    # re-checked here beyond what's needed to resolve the form's shape into
    # that function's plain parameters. `side_a`/`side_b` are NOT read from
    # the form: app/static/room_form.js only ever changes the two agent
    # fields' *labels* client-side, never submits separate side values, so
    # the sides mapping is derived server-side in `_sides_for_mode` from the
    # mode alone (agent_a always gets the mode's first side, agent_b the
    # second -- the same order the JS labels them in), which also means a
    # JS-disabled submission still produces a correct, non-spoofable sides
    # assignment.
    mode: str = Form(default=DEFAULT_MODE),
    topic: str = Form(default=""),
    duration_preset: str = Form(default=""),
    custom_duration_value: str = Form(default=""),
    custom_duration_unit: str = Form(default="minutes"),
    # ADR-0008: optional free-form group label, same "trim, blank -> None"
    # pre-pass as `topic` above -- app.rooms.create_room's own
    # `_validate_group` re-validates it either way (length cap), same
    # reasoning as the topic field's own duplicated trim.
    group: str = Form(default=""),
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

    cleaned_topic = topic.strip() or None
    cleaned_group = group.strip() or None
    sides = _sides_for_mode(mode, agent_a, agent_b)
    duration_seconds = _parse_duration_seconds(duration_preset, custom_duration_value, custom_duration_unit)

    try:
        room = await create_room_op(
            db,
            name,
            [agent_a, agent_b],
            parsed_max,
            mode=mode,
            topic=cleaned_topic,
            sides=sides,
            duration_seconds=duration_seconds,
            group=cleaned_group,
        )
    except ApiError as exc:
        rows, next_cursor = await list_rooms_op(db, limit=ROOM_LIST_LIMIT)
        members_by_room = await get_members_for_rooms(db, [r.id for r in rows])
        groups = await list_room_groups_op(db)
        return templates.TemplateResponse(
            request,
            "rooms_list.html",
            {
                "csrf_token": session["csrf"],
                "rooms": rows,
                "members_by_room": members_by_room,
                "next_cursor": next_cursor,
                "error": exc.detail,
                "form": {
                    "name": name,
                    "agent_a": agent_a,
                    "agent_b": agent_b,
                    "max_messages": max_messages,
                    "mode": mode,
                    "topic": topic,
                    "duration_preset": duration_preset,
                    "custom_duration_value": custom_duration_value,
                    "custom_duration_unit": custom_duration_unit,
                    "group": group,
                },
                "room_modes": ROOM_MODES,
                "room_modes_json": ROOM_MODES_JSON,
                "groups": groups,
                "group_filter": None,
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
    ctx = await _room_context(db, request, room_id)
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
        ctx = await _room_context(db, request, room_id)
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


# --- ADR-0008: delete + bulk group assignment ---


@router.post("/{room_id}/delete")
async def room_delete(
    room_id: str,
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Owner-only hard delete from the rooms list -- confirmed client-side
    by rooms_list.html's per-row `data-confirm` (handled by the existing
    generic listener in app/static/main.js, no new JS needed). Works on an
    open or closed room; a 404 (unknown/already-deleted id) just becomes an
    ordinary 404 page, same as `room_view`'s own 404 for a missing room.
    """
    try:
        await delete_room_op(db, room_id)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return RedirectResponse(url="/ui/rooms", status_code=303)


@router.post("/assign-group")
async def rooms_assign_group(
    request: Request,
    # Repeated `<input type="checkbox" name="room_ids" ...>` fields --
    # FastAPI/Starlette collects same-named form fields into a list here.
    # An owner submitting with nothing checked lands on the domain's own
    # "non-empty list" ApiError below rather than a silent no-op.
    room_ids: list[str] = Form(default_factory=list),
    group: str = Form(default=""),
    session: dict = Depends(require_ui_session),
    _csrf: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-assigns (or, given a blank group field, clears) a group label
    across every checked room -- see app.rooms.assign_group_to_rooms for the
    all-or-nothing unknown-id validation. Re-renders the rooms list with the
    domain's self-explaining error on failure, same pattern `rooms_create`
    above uses, rather than a bare error page.
    """
    cleaned_group = group.strip() or None
    try:
        await assign_group_to_rooms_op(db, room_ids, cleaned_group)
    except ApiError as exc:
        rows, next_cursor = await list_rooms_op(db, limit=ROOM_LIST_LIMIT)
        members_by_room = await get_members_for_rooms(db, [r.id for r in rows])
        groups = await list_room_groups_op(db)
        return templates.TemplateResponse(
            request,
            "rooms_list.html",
            {
                "csrf_token": session["csrf"],
                "rooms": rows,
                "members_by_room": members_by_room,
                "next_cursor": next_cursor,
                "error": exc.detail,
                "form": {},
                "room_modes": ROOM_MODES,
                "room_modes_json": ROOM_MODES_JSON,
                "groups": groups,
                "group_filter": None,
            },
            status_code=exc.status_code,
        )
    return RedirectResponse(url="/ui/rooms", status_code=303)
