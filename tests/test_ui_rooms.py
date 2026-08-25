"""UI Agent Chat Rooms (ADR-0006 phase B): rooms list + create form, room
live view, the cookie-authed JSON short-poll endpoint, owner post, owner
close, and XSS handling of untrusted agent message content. Exercises the
same shared logic (app/rooms.py) as the phase A API -- see tests/test_rooms.py
for the API-side equivalent.
"""

import re
from datetime import UTC, datetime, timedelta

from ulid import ULID

from app.models import Machine, OwnerToken, Room, RoomMember, RoomMessage
from app.security import generate_machine_token, generate_owner_token, hash_token

XSS_SCRIPT = "<script>alert(1)</script>"
XSS_IMG = '<img src=x onerror=alert(1)>'
# Attribute/quote-breakout payloads -- distinct from XSS_IMG above in that
# these specifically probe for a *missing* or partial escape (e.g. only `<`/
# `>` escaped but not quotes) letting the payload break out of an HTML
# attribute context rather than element content.
XSS_ATTR_BREAKOUT = '"><img src=x onerror=alert(1)>'
XSS_QUOTE_BREAKOUT = "agent'\"onmouseover=alert(1)x='"


async def _create_owner_token(db_session) -> str:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return token


async def _login(client, db_session) -> str:
    token = await _create_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})
    return token


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf_token hidden field not found in page"
    return m.group(1)


async def _owner_headers(db_session) -> dict:
    token = await _create_owner_token(db_session)
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers_and_login(client, db_session) -> dict:
    """For tests that need both a bearer-token client (to drive the phase-A
    /v1/rooms API for setup -- room creation/close there are owner-only) and
    a UI cookie session (to exercise /ui/rooms). `owner_token` is a
    fixed-id singleton row (app/models.py's OwnerToken), so a test cannot
    call `_owner_headers` and `_login` separately in the same test -- that
    would try to insert the singleton row twice. This derives both the
    bearer headers and the UI cookie session from the *same* one token.
    """
    token = await _create_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})
    return {"Authorization": f"Bearer {token}"}


async def _machine_headers(db_session, name: str = "test-machine") -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name=name, token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _create_room_via_api(
    client,
    owner_headers,
    *,
    name="room-1",
    members=None,
    max_messages=None,
    mode=None,
    topic=None,
    sides=None,
    duration_seconds=None,
    group=None,
) -> dict:
    """Room setup via the phase-A /v1/rooms API -- used by UI tests that need
    a room already in a particular mode/topic/sides/deadline state to then
    exercise the *view* (room_view.html) independently of the create-*form*
    path (which has its own dedicated tests below, under "create room form:
    modes + sides + time limits").
    """
    body: dict = {"name": name, "members": members if members is not None else ["agent-a", "agent-b"]}
    if max_messages is not None:
        body["max_messages"] = max_messages
    if mode is not None:
        body["mode"] = mode
    if topic is not None:
        body["topic"] = topic
    if sides is not None:
        body["sides"] = sides
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    if group is not None:
        body["group"] = group
    resp = await client.post("/v1/rooms", json=body, headers=owner_headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


async def _post_message_via_api(client, machine_headers, room_id, *, sender="agent-a", text="hi", kind="message"):
    resp = await client.post(
        f"/v1/rooms/{room_id}/messages",
        json={"sender": sender, "text": text, "kind": kind},
        headers=machine_headers,
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


# --- rooms list ---


async def test_rooms_list_page_renders_rooms(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    await _create_room_via_api(client, owner_headers, name="commander-builder", members=["agent-a", "agent-b"])

    resp = await client.get("/ui/rooms")
    assert resp.status_code == 200
    assert "commander-builder" in resp.text
    assert "agent-a" in resp.text
    assert "agent-b" in resp.text
    assert "open" in resp.text
    assert "0/100" in resp.text  # message_count/max_messages


async def test_rooms_list_page_empty_state(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/rooms")
    assert resp.status_code == 200
    assert "no rooms yet" in resp.text.lower()


async def test_rooms_list_unauthenticated_redirects_to_login(client, db_session):
    resp = await client.get("/ui/rooms", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_rooms_list_machine_token_cannot_reach_ui(client, db_session):
    machine_token = await _machine_headers(db_session)
    resp = await client.get("/ui/rooms", headers=machine_token, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


# --- create room form ---


async def test_create_room_form_creates_and_redirects(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "ui-created-room",
            "agent_a": "alpha",
            "agent_b": "beta",
            "max_messages": "50",
            "notify_on_close": "on",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/ui/rooms/")
    room_id = location.rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room is not None
    assert room.name == "ui-created-room"
    assert room.max_messages == 50
    assert room.status == "open"
    assert room.notify_on_close is True  # always True in phase A regardless of the checkbox


async def test_create_room_form_default_max_messages(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={"name": "defaults", "agent_a": "a1", "agent_b": "a2", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]
    room = await db_session.get(Room, room_id)
    assert room.max_messages == 100


async def test_create_room_form_rejects_duplicate_members_with_error(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={"name": "dupe", "agent_a": "same", "agent_b": "same", "csrf_token": csrf},
    )
    assert resp.status_code == 422
    assert "duplicate" in resp.text.lower() or "same agent twice" in resp.text.lower()


async def test_create_room_form_without_csrf_rejected(client, db_session):
    await _login(client, db_session)
    resp = await client.post(
        "/ui/rooms",
        data={"name": "no-csrf", "agent_a": "a", "agent_b": "b"},
    )
    assert resp.status_code == 403

    rows = (await db_session.execute(Room.__table__.select())).all()
    assert len(rows) == 0


# --- create room form: modes, sides, time limits (ADR-0007, Part 2 UI) ---


async def test_create_room_form_freeform_default_mode_and_topic(client, db_session):
    """Freeform is the default and works with no topic at all -- unchanged
    from phase A behavior, just asserted explicitly against the new mode
    field this time.
    """
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={"name": "freeform-room", "agent_a": "a1", "agent_b": "a2", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]
    room = await db_session.get(Room, room_id)
    assert room.mode == "freeform"
    assert room.topic is None


async def test_create_room_form_debate_mode_sets_topic_and_sides(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "debate-room",
            "agent_a": "alice",
            "agent_b": "bob",
            "mode": "debate",
            "topic": "Cats are better than dogs",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room.mode == "debate"
    assert room.topic == "Cats are better than dogs"

    # The form's two agent-name fields are labeled by side, in order, by
    # app/static/room_form.js -- the server independently assigns the
    # mode's two distinct sides in that same order (app/routers/ui_rooms.py's
    # `_sides_for_mode`), agent_a first: 'for' to alice, 'against' to bob.
    detail = await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)
    assert detail.status_code == 200
    assert detail.json()["sides"] == {"alice": "for", "bob": "against"}


async def test_create_room_form_critique_mode_sets_topic_and_sides(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "critique-room",
            "agent_a": "alice",
            "agent_b": "bob",
            "mode": "critique",
            "topic": "A new caching layer design",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room.mode == "critique"
    assert room.topic == "A new caching layer design"

    detail = await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)
    assert detail.status_code == 200
    assert detail.json()["sides"] == {"alice": "proposer", "bob": "critic"}


async def test_create_room_form_collaborate_mode_is_symmetric_no_sides(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "collaborate-room",
            "agent_a": "alice",
            "agent_b": "bob",
            "mode": "collaborate",
            "topic": "Draft the release notes",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room.mode == "collaborate"

    detail = await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)
    assert detail.json()["sides"] == {"alice": None, "bob": None}


async def test_create_room_form_brainstorm_mode_is_symmetric_no_sides(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "brainstorm-room",
            "agent_a": "alice",
            "agent_b": "bob",
            "mode": "brainstorm",
            "topic": "Names for the new feature",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room.mode == "brainstorm"

    detail = await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)
    assert detail.json()["sides"] == {"alice": None, "bob": None}


async def test_create_room_form_non_freeform_blank_topic_shows_clean_error(client, db_session):
    """Non-freeform modes require a topic (enforced by app.rooms._validate_topic).
    Submitting one blank must render the domain's self-explaining error
    cleanly on the rooms-list page, never a 500.
    """
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "debate-no-topic",
            "agent_a": "a1",
            "agent_b": "a2",
            "mode": "debate",
            "topic": "",
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 422
    assert "topic" in resp.text.lower()
    assert "New room" in resp.text  # the form itself is still rendered, not a bare error page

    rows = (await db_session.execute(Room.__table__.select())).all()
    assert len(rows) == 0


async def test_create_room_form_time_preset_maps_to_duration_seconds(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    before = datetime.now(UTC)
    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "timed-room",
            "agent_a": "a1",
            "agent_b": "a2",
            "duration_preset": "3600",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    after = datetime.now(UTC)
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room.expires_at is not None
    expires_at = room.expires_at if room.expires_at.tzinfo else room.expires_at.replace(tzinfo=UTC)
    assert before + timedelta(seconds=3600) <= expires_at <= after + timedelta(seconds=3600)


async def test_create_room_form_custom_time_limit_maps_to_duration_seconds(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    before = datetime.now(UTC)
    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "custom-timed-room",
            "agent_a": "a1",
            "agent_b": "a2",
            "duration_preset": "custom",
            "custom_duration_value": "2",
            "custom_duration_unit": "hours",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    after = datetime.now(UTC)
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room.expires_at is not None
    expires_at = room.expires_at if room.expires_at.tzinfo else room.expires_at.replace(tzinfo=UTC)
    # 2 hours = 7200 seconds
    assert before + timedelta(seconds=7200) <= expires_at <= after + timedelta(seconds=7200)


async def test_create_room_form_no_limit_leaves_expires_at_null(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={
            "name": "no-limit-room",
            "agent_a": "a1",
            "agent_b": "a2",
            "duration_preset": "",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]

    room = await db_session.get(Room, room_id)
    assert room.expires_at is None


# --- room live view ---


async def test_room_view_renders_messages(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text="hello there")

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "agent-a" in resp.text
    assert "hello there" in resp.text
    assert "Post as owner" in resp.text
    assert "Stop room" in resp.text


async def test_room_view_closed_room_shows_banner_and_hides_controls(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers)
    close_resp = await client.post(f"/v1/rooms/{room['id']}/close", json={"reason": "owner"}, headers=owner_headers)
    assert close_resp.status_code == 200

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Room closed" in resp.text
    assert 'id="room-post-panel" style="display:none"' in resp.text
    assert 'id="room-stop-panel" style="display:none"' in resp.text


async def test_room_view_missing_room_404s(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/rooms/nonexistent-id")
    assert resp.status_code == 404


# --- room live view: mode/topic header + countdown (ADR-0007, Part 2 UI) ---


async def test_room_view_header_shows_mode_and_topic(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(
        client,
        owner_headers,
        name="debate-header",
        members=["agent-a", "agent-b"],
        mode="debate",
        topic="Tabs vs spaces",
        sides={"agent-a": "for", "agent-b": "against"},
    )

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Debate" in resp.text
    assert "Tabs vs spaces" in resp.text
    assert "(For)" in resp.text
    assert "(Against)" in resp.text


async def test_room_view_header_shows_freeform_mode_label(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="freeform-header")

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Freeform" in resp.text


async def test_room_view_countdown_present_when_deadline_set(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="deadline-room", duration_seconds=1800)

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert 'id="room-countdown"' in resp.text
    assert 'data-expires-at="' in resp.text
    assert 'data-expires-at=""' not in resp.text


async def test_room_view_countdown_absent_when_no_deadline(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="no-deadline-room")

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert 'id="room-countdown"' not in resp.text
    assert 'data-expires-at=""' in resp.text


async def test_room_view_closed_room_shows_time_close_reason(client, db_session):
    """The sweeper (app/room_sweeper.py, out of scope for this UI part) uses
    the same app.rooms.close_room path exercised here directly via the v1
    API's close endpoint (which accepts 'time' as a valid reason) -- the
    live view's existing generic close_reason rendering must show it
    (ADR-0007: "Closed/expired rooms show the close_reason (incl. 'time')").
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="time-closed-room", duration_seconds=1800)
    close_resp = await client.post(f"/v1/rooms/{room['id']}/close", json={"reason": "time"}, headers=owner_headers)
    assert close_resp.status_code == 200

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Room closed" in resp.text
    assert "time" in resp.text.lower()


# --- JSON short-poll endpoint ---


async def test_room_messages_json_returns_new_since_seq(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text="first")
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-b", text="second")

    resp = await client.get(f"/ui/rooms/{room['id']}/messages?since=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "open"
    assert data["message_count"] == 2
    assert [m["text"] for m in data["messages"]] == ["first", "second"]
    assert data["messages"][0]["sender"] == "agent-a"
    assert data["messages"][0]["seq"] == 1
    assert data["messages"][0]["kind"] == "message"
    assert "created_at" in data["messages"][0]

    resp2 = await client.get(f"/ui/rooms/{room['id']}/messages?since=1")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert [m["text"] for m in data2["messages"]] == ["second"]


async def test_room_messages_json_immediate_no_new_messages(client, db_session):
    """wait=0 (short-poll) means an empty result returns immediately rather
    than blocking -- this call must not hang the test.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers)

    resp = await client.get(f"/ui/rooms/{room['id']}/messages?since=0")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


async def test_room_messages_json_unauthenticated_redirects(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)

    resp = await client.get(f"/ui/rooms/{room['id']}/messages?since=0", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_room_messages_json_machine_token_cannot_reach_ui(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)
    machine_token = await _machine_headers(db_session)

    resp = await client.get(
        f"/ui/rooms/{room['id']}/messages?since=0", headers=machine_token, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


# --- owner post ---


async def test_owner_post_via_ui_appends_message(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers)

    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/post",
        data={"text": "hello from the owner", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ui/rooms/{room['id']}"

    rows = (
        await db_session.execute(
            RoomMessage.__table__.select().where(RoomMessage.__table__.c.room_id == room["id"])
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].sender == "owner"
    assert rows[0].text == "hello from the owner"

    # Owner posts count toward the cap like any other message.
    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["message_count"] == 1


async def test_owner_post_via_ui_without_csrf_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers)

    resp = await client.post(f"/ui/rooms/{room['id']}/post", data={"text": "no csrf"})
    assert resp.status_code == 403

    rows = (
        await db_session.execute(
            RoomMessage.__table__.select().where(RoomMessage.__table__.c.room_id == room["id"])
        )
    ).all()
    assert len(rows) == 0


async def test_owner_post_via_ui_to_closed_room_shows_error(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers)
    await client.post(f"/v1/rooms/{room['id']}/close", json={"reason": "owner"}, headers=owner_headers)

    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/post",
        data={"text": "too late", "csrf_token": csrf},
    )
    assert resp.status_code == 409
    assert "closed" in resp.text.lower()


# --- owner close ---


async def test_close_via_ui_closes_room(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers)

    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/close",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ui/rooms/{room['id']}"

    db_room = await db_session.get(Room, room["id"])
    assert db_room.status == "closed"
    assert db_room.close_reason == "owner"


async def test_close_via_ui_without_csrf_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers)

    resp = await client.post(f"/ui/rooms/{room['id']}/close", data={})
    assert resp.status_code == 403

    db_room = await db_session.get(Room, room["id"])
    assert db_room.status == "open"


# --- XSS: hostile agent message content must render inert ---


async def test_xss_message_json_carries_raw_text_as_data(client, db_session):
    """The JSON endpoint is data, not HTML -- it must carry the exact raw
    text (this is correct; JSON string values are not executable). The
    protection against XSS lives in the *renderers* (server template
    autoescape, and the JS's textContent-only DOM writes), tested below.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text=XSS_SCRIPT)
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text=XSS_IMG)

    resp = await client.get(f"/ui/rooms/{room['id']}/messages?since=0")
    assert resp.status_code == 200
    data = resp.json()
    texts = [m["text"] for m in data["messages"]]
    assert XSS_SCRIPT in texts
    assert XSS_IMG in texts


async def test_xss_server_rendered_view_escapes_message_content(client, db_session):
    """The initial server-rendered transcript (room_view.html) must escape
    hostile message text via Jinja2 autoescape -- the raw `<script>`/`<img
    onerror>` markup must never appear verbatim in the HTML response.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text=XSS_SCRIPT)
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text=XSS_IMG)

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200

    # The raw, executable forms must never appear in the HTML.
    assert "<script>alert(1)</script>" not in resp.text
    assert "<img src=x onerror=alert(1)>" not in resp.text
    # The escaped forms must be present instead (autoescape did its job).
    assert "&lt;script&gt;" in resp.text
    assert "&lt;img" in resp.text


# --- join prompts (ADR-0006, phase C) ---


async def test_room_view_renders_join_prompt_per_member(client, db_session):
    from app.onboarding import TOKEN_PLACEHOLDER

    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Join prompts" in resp.text
    # One join-prompt box per member, each still carrying the token
    # placeholder (a room member's real machine token can't be retrieved
    # here -- see app/routers/ui_rooms.py's module docstring). The prompt
    # text is rendered through Jinja2 autoescape same as everything else on
    # this page, so the literal '<token>' placeholder appears HTML-escaped.
    import html

    assert resp.text.count(html.escape(TOKEN_PLACEHOLDER)) >= 2
    assert "You are &#39;agent-a&#39;; the other participant is &#39;agent-b&#39;." in resp.text
    assert "You are &#39;agent-b&#39;; the other participant is &#39;agent-a&#39;." in resp.text
    assert "Paste to agent-a; it authenticates with its own machine token." in resp.text
    assert "Paste to agent-b; it authenticates with its own machine token." in resp.text
    assert f"/v1/rooms/{room['id']}/messages" in resp.text


async def test_room_join_prompt_xss_member_name_escaped(client, db_session):
    """A room member name is owner-supplied but still untrusted content that
    flows into the generated join prompt (app/onboarding.py's
    `generate_room_join_prompt`) -- both the member label and every mention
    of the name inside the prompt body must render escaped, never as raw
    executable markup. Covers a <script> tag, an <img onerror> tag, and two
    attribute/quote-breakout payloads (a bare '"><img ...' close-and-inject,
    and a mixed-quote 'onmouseover=' payload) -- each must render escaped in
    the join-prompt boxes, never in its raw executable form.
    """
    # MarkupSafe's escape (not stdlib html.escape) to match Jinja2 autoescape
    # exactly -- the two disagree on quote encoding (`&#34;`/`&#39;` vs.
    # `&quot;`/`&#x27;`), which matters here since two of the payloads below
    # contain quote characters.
    from markupsafe import escape as markupsafe_escape

    owner_headers = await _owner_headers_and_login(client, db_session)

    for i, payload in enumerate((XSS_SCRIPT, XSS_IMG, XSS_ATTR_BREAKOUT, XSS_QUOTE_BREAKOUT)):
        room = await _create_room_via_api(
            client, owner_headers, name=f"xss-room-{i}", members=[payload, "agent-b"]
        )

        resp = await client.get(f"/ui/rooms/{room['id']}")
        assert resp.status_code == 200
        assert payload not in resp.text
        assert str(markupsafe_escape(payload)) in resp.text


# --- ADR-0007: mode/topic/side/deadline now flow into join prompts too ---


async def test_room_join_prompt_debate_contains_stance_topic_and_deadline(client, db_session):
    """Fixes the previously-dead mode/topic/side/deadline params flagged by
    a prior review: a debate room's join prompts must actually contain the
    For/Against stance text (app/room_modes.py's `_debate_role_text`), the
    topic, the closing-statement instruction, and a deadline line -- not
    just the generic freeform framing.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(
        client,
        owner_headers,
        name="debate-prompt-room",
        members=["alice", "bob"],
        mode="debate",
        topic="Cats vs dogs",
        sides={"alice": "for", "bob": "against"},
        duration_seconds=1800,
    )

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "This is a Debate session." in resp.text
    assert "You argue FOR the proposition: Cats vs dogs" in resp.text
    assert "You argue AGAINST the proposition: Cats vs dogs" in resp.text
    assert "closing statement" in resp.text.lower()
    assert "Deadline: the room closes at" in resp.text


async def test_room_view_topic_xss_escaped_in_header_and_join_prompts(client, db_session):
    """A room topic is owner-supplied but untrusted content (ADR-0007) that
    now renders in two places: the live-view header and inside every
    join-prompt box's generated role text. Both must render it escaped --
    the raw, executable form must never appear anywhere on the page.
    """
    from markupsafe import escape as markupsafe_escape

    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(
        client,
        owner_headers,
        name="xss-topic-room",
        members=["agent-a", "agent-b"],
        mode="debate",
        topic=XSS_SCRIPT,
        sides={"agent-a": "for", "agent-b": "against"},
    )

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert XSS_SCRIPT not in resp.text

    escaped = str(markupsafe_escape(XSS_SCRIPT))
    assert escaped in resp.text

    # Split the page at the "Join prompts" section so the header portion and
    # the join-prompt-boxes portion are each checked independently.
    marker = "Join prompts"
    assert marker in resp.text
    header_part, join_part = resp.text.split(marker, 1)

    assert XSS_SCRIPT not in header_part
    assert escaped in header_part  # topic shown in the header, escaped

    assert XSS_SCRIPT not in join_part
    assert escaped in join_part  # topic shown inside the join-prompt boxes' role text, escaped


# --- ADR-0013: room page layout + join-prompt copy buttons ---


def _fake_room(**overrides) -> Room:
    """An unsaved Room ORM instance for direct template-rendering tests
    below -- used only to exercise room_view.html's Jinja logic with member
    counts/shapes the *domain* layer (app/rooms.py's REQUIRED_MEMBER_COUNT
    == 2, out of this change's scope) doesn't currently allow to be created
    through the real create-room path. No DB round-trip involved.
    """
    defaults = dict(
        id="room-template-fake",
        name="Template Fixture Room",
        status="open",
        max_messages=100,
        message_count=0,
        notify_on_close=True,
        created_at=datetime.now(UTC),
        closed_at=None,
        close_reason=None,
        mode="freeform",
        topic=None,
        expires_at=None,
        closing_warned_at=None,
        group_name=None,
    )
    defaults.update(overrides)
    return Room(**defaults)


def _render_room_view(*, room, members, sides, side_labels, join_prompts, messages=(), mode_label="Freeform") -> str:
    """Renders room_view.html directly (bypassing the /ui/rooms/{id} route
    entirely) so the new per-participant copy-button loop (ADR-0013
    decision 2) can be exercised with a member list shape the domain layer
    doesn't currently produce (more than two members) -- this is a template
    logic test, not an end-to-end one.
    """
    from types import SimpleNamespace

    from app.routers.ui_rooms import ROOM_MODES, ROOM_MODES_JSON
    from app.templates_env import templates

    fake_request = SimpleNamespace(url=SimpleNamespace(path="/ui/rooms/room-template-fake"))
    template = templates.env.get_template("room_view.html")
    return template.render(
        request=fake_request,
        csrf_token="test-csrf-token",
        room=room,
        members=members,
        sides=sides,
        side_labels=side_labels,
        mode_label=mode_label,
        messages=list(messages),
        last_seq=0,
        join_prompts=join_prompts,
        room_modes=ROOM_MODES,
        room_modes_json=ROOM_MODES_JSON,
        transcript_md="",
        llm_configured=False,
        room_ai_actions=[],
        room_ai_namespaces=[],
        project_names=[],
        error=None,
        deposited=False,
    )


def test_room_view_copy_buttons_generated_dynamically_for_more_than_two_members_with_mixed_roles():
    """Proves the new "Copy join prompt" row (room_view.html) is a genuine
    loop over `members` (ADR-0013 decision 2, "not hardcoded to two"), not
    a fixed pair -- three participants here, two carrying a side/role and
    one without, so the per-member role-vs-fallback label logic (decision
    3) is also exercised in the same room rather than across two rooms that
    could coincidentally each look like a fixed pair.
    """
    room = _fake_room(message_count=7)
    members = ["cmdr", "builder", "observer"]
    sides = {"cmdr": "lead", "builder": "build", "observer": None}
    side_labels = {"lead": "Commander", "build": "Builder"}
    join_prompts = {m: f"JOIN PROMPT FOR {m}" for m in members}

    html_out = _render_room_view(room=room, members=members, sides=sides, side_labels=side_labels, join_prompts=join_prompts)

    # One button per member -- three, not a hardcoded two -- each pointing
    # at the same hidden token-reveal element the bottom "Join prompts"
    # section defines for that member (no duplicated hidden text block).
    assert html_out.count('data-copy-target="join-prompt-1"') == 2  # new row + bottom section
    assert html_out.count('data-copy-target="join-prompt-2"') == 2
    assert html_out.count('data-copy-target="join-prompt-3"') == 2

    assert "Copy join prompt — Commander" in html_out
    assert "Copy join prompt — Builder" in html_out
    # `observer` has no side -> ordinal fallback, per decision 3.
    assert "Copy join prompt — Agent 3" in html_out


def test_room_view_copy_button_labels_fall_back_to_agent_ordinal_without_roles():
    """A room with no roles at all (freeform, `sides` all falsy) must label
    every button "Agent N", never the bare member name and never a blank
    label -- the fallback branch of decision 3.
    """
    room = _fake_room(message_count=0)
    members = ["alpha", "beta", "gamma", "delta"]
    sides = {m: None for m in members}
    join_prompts = {m: f"JOIN PROMPT FOR {m}" for m in members}

    html_out = _render_room_view(room=room, members=members, sides=sides, side_labels=None, join_prompts=join_prompts)

    assert "Copy join prompt — Agent 1" in html_out
    assert "Copy join prompt — Agent 2" in html_out
    assert "Copy join prompt — Agent 3" in html_out
    assert "Copy join prompt — Agent 4" in html_out


async def test_room_view_copy_button_labels_use_role_in_real_debate_room(client, db_session):
    """End-to-end (real /ui/rooms/{id} route, real create-room API) sibling
    of the two template-level tests above: a two-member debate room's
    buttons must read the side label ("For"/"Against"), not "Agent 1"/
    "Agent 2".
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(
        client,
        owner_headers,
        name="debate-copy-buttons",
        members=["alice", "bob"],
        mode="debate",
        topic="Cats vs dogs",
        sides={"alice": "for", "bob": "against"},
    )

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Copy join prompt — For" in resp.text
    assert "Copy join prompt — Against" in resp.text
    assert "Copy join prompt — Agent 1" not in resp.text
    assert "Copy join prompt — Agent 2" not in resp.text


async def test_room_view_copy_button_labels_fall_back_in_real_freeform_room(client, db_session):
    """End-to-end sibling: a freeform (no-roles) two-member room falls back
    to "Agent 1"/"Agent 2" through the real route, same as the template-
    level test above.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="freeform-copy-buttons", members=["agent-a", "agent-b"])

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Copy join prompt — Agent 1" in resp.text
    assert "Copy join prompt — Agent 2" in resp.text


async def test_room_view_copy_button_click_target_matches_bottom_section_join_prompt(client, db_session):
    """Each new button's data-copy-target must resolve to the SAME hidden
    element the bottom "Join prompts" section already defines -- reusing
    the shared [data-copy-target] handler (ADR-0013 decision 4) rather than
    a second copy-to-clipboard path or a duplicated hidden text block.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="copy-target-room", members=["agent-a", "agent-b"])

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    text = resp.text

    assert text.count('data-copy-target="join-prompt-1"') == 2
    assert text.count('data-copy-target="join-prompt-2"') == 2
    # Exactly one hidden token-reveal element per member id -- the new row
    # does not render a second copy of the join-prompt text.
    assert text.count('id="join-prompt-1"') == 1
    assert text.count('id="join-prompt-2"') == 1


async def test_room_view_section_order_matches_adr_0013(client, db_session):
    """Top-to-bottom order per ADR-0013 decision 1: room info (unchanged,
    not separately markered here), join-prompt copy buttons, export, AI
    actions, the owner-controls cluster (post/stop/switch-mode, moved as a
    unit), transcript (now collapsible), then the full join-prompts section
    at the very bottom.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="order-room")

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    text = resp.text

    copy_buttons_idx = text.index('id="room-copy-buttons-panel"')
    export_idx = text.index("<h2>Export</h2>")
    ai_idx = text.index('id="room-ai-panel"')
    post_idx = text.index('id="room-post-panel"')
    stop_idx = text.index('id="room-stop-panel"')
    switch_mode_idx = text.index('id="room-switch-mode-panel"')
    transcript_idx = text.index('id="room-transcript-panel"')
    join_prompts_idx = text.index(">Join prompts<")

    assert (
        copy_buttons_idx
        < export_idx
        < ai_idx
        < post_idx
        < stop_idx
        < switch_mode_idx
        < transcript_idx
        < join_prompts_idx
    )


async def test_room_view_transcript_collapsible_open_by_default_with_live_count(client, db_session):
    """ADR-0013 decision 7: the transcript becomes a <details> that
    defaults open, with a live message count in its header -- "Transcript
    (N messages)" -- reusing the room's actual message_count.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text="hi")
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-b", text="there")

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    text = resp.text

    m = re.search(r'<details class="panel" id="room-transcript-panel"([^>]*)>', text)
    assert m, "transcript panel must be rendered as a <details> element"
    assert "open" in m.group(1), "transcript must default to open, not collapsed"

    assert '<span id="room-transcript-count">2</span>' in text
    assert re.search(r"Transcript \(<span[^>]*>2</span>\s*messages\)", text), (
        "transcript header must show a live 'Transcript (N messages)' count"
    )


async def test_room_view_transcript_count_element_reused_not_duplicated(client, db_session):
    """The transcript header's count and the page-header count must be two
    *different* DOM elements (distinct ids) both driven from the same
    room.message_count value server-side, and the SAME live-update path
    client-side (app/static/rooms.js) -- not a second, independently
    tracked counter that could drift (ADR-0013 decision 7's "does not add
    a second counter that could drift from it").
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text="hi")

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    text = resp.text

    assert '<span id="room-count">1</span>' in text
    assert '<span id="room-transcript-count">1</span>' in text


def test_rooms_js_transcript_count_updated_alongside_room_count():
    """Static-source check on app/static/rooms.js: the new transcript-
    header count element must be looked up once (getElementById) and
    updated from the exact same `data.message_count` handling block that
    already drives #room-count (ADR-0013 decision 7's "hook the header
    there" -- app/static/rooms.js:151-153 in the ADR/task description),
    not a second poll or a second branch that could fall out of sync.
    """
    from pathlib import Path

    js_path = Path(__file__).resolve().parent.parent / "app" / "static" / "rooms.js"
    source = js_path.read_text()

    assert 'getElementById("room-transcript-count")' in source

    # The two elements must be updated inside the same
    # `if (typeof data.message_count === "number")` conditional, not two
    # separate ones -- extract that block and check both assignments live
    # inside it.
    match = re.search(
        r'if \(typeof data\.message_count === "number"\) \{(.*?)\n\s*\}', source, re.DOTALL
    )
    assert match, "expected a single typeof-data.message_count guard block in rooms.js"
    block = match.group(1)
    assert "countEl.textContent" in block
    assert "transcriptCountEl.textContent" in block


def test_main_js_copy_handler_three_tier_fallback_order():
    """Static-source check on app/static/main.js (no JS test runner in this
    Python-based repo -- see test_rooms_js_renders_via_textcontent_not_innerhtml
    for the established pattern this follows). ADR-0013 decision 5: the
    shared [data-copy-target] handler's fallback order must be
    navigator.clipboard.writeText -> hidden-textarea execCommand('copy') ->
    window.prompt last resort -- and the copied-confirmation must be driven
    by each tier's actual result, never fired unconditionally before the
    copy is known to have worked.
    """
    from pathlib import Path

    js_path = Path(__file__).resolve().parent.parent / "app" / "static" / "main.js"
    source = js_path.read_text()

    clipboard_idx = source.index("navigator.clipboard")
    exec_command_idx = source.index("document.execCommand")
    prompt_idx = source.index("window.prompt(")
    assert clipboard_idx < exec_command_idx < prompt_idx, (
        "fallback tiers must appear in order: clipboard API, then execCommand, then window.prompt"
    )

    # Tier 2's hidden textarea must never be visible or affect layout, and
    # must always be cleaned up.
    assert "createElement(\"textarea\")" in source
    assert "document.body.appendChild(textarea)" in source
    assert "document.body.removeChild(textarea)" in source
    assert "finally" in source

    # The success/failure confirmation must be gated on the real result of
    # each tier -- not called unconditionally right after the copy attempt.
    assert "showSuccess" in source
    assert "showFailure" in source
    assert 'navigator.clipboard.writeText(text).then(showSuccess, fallToExecCommandThenPrompt)' in source
    assert "if (execCommandCopy())" in source, "execCommand's boolean return value must gate success/failure"

    # The failure path still falls through to the manual-copy dialog
    # (decision 5's "last resort"), it doesn't just give up.
    assert re.search(r"showFailure\(\);\s*\n\s*window\.prompt\(", source), (
        "on execCommand failure, must show the failed state and still offer the window.prompt fallback"
    )


def test_rooms_js_renders_via_textcontent_not_innerhtml():
    """Static-source check on app/static/rooms.js: the message-append path
    must use textContent/createTextNode (inert against markup) and must
    never assign untrusted content via innerHTML. This is the concrete,
    checkable form of the #1 review concern for this feature.
    """
    from pathlib import Path

    js_path = Path(__file__).resolve().parent.parent / "app" / "static" / "rooms.js"
    source = js_path.read_text()

    # Strip whole-line `//` comments before checking: the file's own header
    # comment discusses innerHTML by name (explaining why it's avoided), so
    # the real assertion -- no *code* line ever assigns to .innerHTML --
    # must look only at code lines, not prose.
    code_only = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))

    assert ".innerHTML" not in code_only, "rooms.js must never assign to .innerHTML for message content"
    assert "textContent" in source
    assert "createTextNode" in source
    assert "createElement" in source


# --- ADR-0008: room delete + free-form groups (UI) ---


# --- create form: group field ---


async def test_create_room_form_with_group_sets_group(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={"name": "grouped-room", "agent_a": "a1", "agent_b": "a2", "group": "schema-debates", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]
    room = await db_session.get(Room, room_id)
    assert room.group_name == "schema-debates"


async def test_create_room_form_blank_group_leaves_it_null(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms",
        data={"name": "ungrouped-room", "agent_a": "a1", "agent_b": "a2", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    room_id = resp.headers["location"].rsplit("/", 1)[-1]
    room = await db_session.get(Room, room_id)
    assert room.group_name is None


# --- rooms list: group column + filter ---


async def test_rooms_list_shows_group_column(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    await _create_room_via_api(client, owner_headers, name="grouped", members=["agent-a", "agent-b"], group="alpha")

    resp = await client.get("/ui/rooms")
    assert resp.status_code == 200
    assert "alpha" in resp.text


async def test_rooms_list_group_filter_shows_only_matching_group(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    await _create_room_via_api(client, owner_headers, name="in-group-a", members=["a1", "a2"], group="group-a")
    await _create_room_via_api(client, owner_headers, name="in-group-b", members=["b1", "b2"], group="group-b")
    await _create_room_via_api(client, owner_headers, name="ungrouped-room", members=["c1", "c2"])

    resp = await client.get("/ui/rooms", params={"group": "group-a"})
    assert resp.status_code == 200
    assert "in-group-a" in resp.text
    assert "in-group-b" not in resp.text
    assert "ungrouped-room" not in resp.text


async def test_rooms_list_no_filter_shows_all_groups(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    await _create_room_via_api(client, owner_headers, name="in-group-a", members=["a1", "a2"], group="group-a")
    await _create_room_via_api(client, owner_headers, name="ungrouped-room", members=["c1", "c2"])

    resp = await client.get("/ui/rooms")
    assert resp.status_code == 200
    assert "in-group-a" in resp.text
    assert "ungrouped-room" in resp.text


# --- rooms list: group XSS ---


async def test_rooms_list_group_xss_escaped(client, db_session):
    from markupsafe import escape as markupsafe_escape

    owner_headers = await _owner_headers_and_login(client, db_session)
    await _create_room_via_api(
        client, owner_headers, name="xss-group-room", members=["agent-a", "agent-b"], group=XSS_SCRIPT
    )

    resp = await client.get("/ui/rooms")
    assert resp.status_code == 200
    assert XSS_SCRIPT not in resp.text
    assert str(markupsafe_escape(XSS_SCRIPT)) in resp.text


# --- bulk group assignment ---


async def test_bulk_assign_group_sets_group_on_multiple_rooms(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room1 = await _create_room_via_api(client, owner_headers, name="r1", members=["a1", "a2"])
    room2 = await _create_room_via_api(client, owner_headers, name="r2", members=["b1", "b2"])

    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms/assign-group",
        data={"room_ids": [room1["id"], room2["id"]], "group": "bulk-group", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/rooms"

    db_room1 = await db_session.get(Room, room1["id"])
    db_room2 = await db_session.get(Room, room2["id"])
    assert db_room1.group_name == "bulk-group"
    assert db_room2.group_name == "bulk-group"


async def test_bulk_assign_group_blank_clears_group(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(
        client, owner_headers, name="r1", members=["a1", "a2"], group="had-a-group"
    )

    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms/assign-group",
        data={"room_ids": [room["id"]], "group": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db_room = await db_session.get(Room, room["id"])
    assert db_room.group_name is None


async def test_bulk_assign_group_without_csrf_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post("/ui/rooms/assign-group", data={"room_ids": [room["id"]], "group": "nope"})
    assert resp.status_code == 403

    db_room = await db_session.get(Room, room["id"])
    assert db_room.group_name is None


async def test_bulk_assign_group_unknown_id_shows_clean_error(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/rooms/assign-group",
        data={"room_ids": ["not-a-real-room"], "group": "some-group", "csrf_token": csrf},
    )
    assert resp.status_code == 404
    assert "not-a-real-room" in resp.text


async def test_bulk_assign_group_machine_token_cannot_reach_ui(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    machine_token = await _machine_headers(db_session)

    resp = await client.post(
        "/ui/rooms/assign-group",
        data={"room_ids": [room["id"]], "group": "nope"},
        headers=machine_token,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    db_room = await db_session.get(Room, room["id"])
    assert db_room.group_name is None


# --- delete ---


async def test_ui_delete_room_removes_room_and_cascades(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    await _post_message_via_api(client, machine_headers, room["id"], sender="agent-a", text="hello")

    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/rooms"

    assert await db_session.get(Room, room["id"]) is None
    remaining_messages = (
        await db_session.execute(RoomMessage.__table__.select().where(RoomMessage.__table__.c.room_id == room["id"]))
    ).all()
    remaining_members = (
        await db_session.execute(RoomMember.__table__.select().where(RoomMember.__table__.c.room_id == room["id"]))
    ).all()
    assert remaining_messages == []
    assert remaining_members == []


async def test_ui_delete_room_button_present_with_confirm(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="delete-candidate", members=["agent-a", "agent-b"])
    await _post_message_via_api(client, await _machine_headers(db_session), room["id"], sender="agent-a", text="one")

    resp = await client.get("/ui/rooms")
    assert resp.status_code == 200
    assert f"/ui/rooms/{room['id']}/delete" in resp.text
    assert "data-confirm=" in resp.text
    assert "permanently" in resp.text.lower()


async def test_ui_delete_room_without_csrf_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/ui/rooms/{room['id']}/delete", data={})
    assert resp.status_code == 403
    assert await db_session.get(Room, room["id"]) is not None


async def test_ui_delete_room_unknown_id_404s(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/rooms/not-a-real-room/delete", data={"csrf_token": csrf})
    assert resp.status_code == 404


async def test_ui_delete_room_works_on_closed_room(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    close_resp = await client.post(f"/v1/rooms/{room['id']}/close", json={"reason": "owner"}, headers=owner_headers)
    assert close_resp.status_code == 200

    page = await client.get("/ui/rooms")
    csrf = _extract_csrf(page.text)
    resp = await client.post(f"/ui/rooms/{room['id']}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert await db_session.get(Room, room["id"]) is None


async def test_ui_delete_room_machine_token_cannot_reach_ui(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    machine_token = await _machine_headers(db_session)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/delete", data={}, headers=machine_token, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    assert await db_session.get(Room, room["id"]) is not None


# --- ADR-0009: mid-session mode switch (UI) ---


async def test_room_view_shows_switch_mode_control_when_open(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "Switch mode" in resp.text
    assert f'action="/ui/rooms/{room["id"]}/switch-mode"' in resp.text


async def test_room_view_hides_switch_mode_control_when_closed(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert 'id="room-switch-mode-panel" style="display:none"' in resp.text


async def test_ui_switch_mode_updates_room_and_header_reflects_it(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])

    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/switch-mode",
        data={"mode": "debate", "topic": "cats vs dogs", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ui/rooms/{room['id']}"

    db_room = await db_session.get(Room, room["id"])
    assert db_room.mode == "debate"
    assert db_room.topic == "cats vs dogs"

    # agent-a is the first member (create order) -> the mode's first side
    # ('for'); agent-b -> the second ('against') -- server-derived, not
    # taken from the form (see _sides_for_mode).
    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["sides"] == {"agent-a": "for", "agent-b": "against"}

    # The header (re-rendered fresh on the redirect-followed GET) reflects
    # the new mode/topic/sides.
    view = await client.get(f"/ui/rooms/{room['id']}")
    assert "Debate" in view.text
    assert "cats vs dogs" in view.text
    assert "(For)" in view.text
    assert "(Against)" in view.text

    # The system announcement appears in the transcript too.
    assert "Mode switched to Debate." in view.text


async def test_ui_switch_mode_without_csrf_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/ui/rooms/{room['id']}/switch-mode", data={"mode": "debate", "topic": "x"})
    assert resp.status_code == 403

    db_room = await db_session.get(Room, room["id"])
    assert db_room.mode == "freeform"


async def test_ui_switch_mode_non_freeform_without_topic_shows_clean_error(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])

    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/switch-mode",
        data={"mode": "debate", "topic": "", "csrf_token": csrf},
    )
    assert resp.status_code == 422
    assert "topic" in resp.text.lower()

    db_room = await db_session.get(Room, room["id"])
    assert db_room.mode == "freeform"  # untouched by the rejected switch


async def test_ui_switch_mode_closed_room_shows_clean_error(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)

    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/switch-mode",
        data={"mode": "debate", "topic": "cats vs dogs", "csrf_token": csrf},
    )
    assert resp.status_code == 409
    assert "closed" in resp.text.lower()


async def test_ui_switch_mode_machine_token_cannot_reach_ui(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, members=["agent-a", "agent-b"])
    machine_token = await _machine_headers(db_session)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/switch-mode",
        data={"mode": "debate", "topic": "x"},
        headers=machine_token,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    db_room = await db_session.get(Room, room["id"])
    assert db_room.mode == "freeform"


async def test_ui_switch_mode_topic_xss_escaped_in_header_and_announcement(client, db_session):
    """A room topic set via a mode switch is owner-supplied but still
    untrusted content, same as at create time (ADR-0007) -- it now also
    renders inside the switch's own system announcement (ADR-0009). Both
    the header and the transcript (which shows the announcement like any
    other message) must render it escaped, never raw. Covers the same
    4-payload matrix as test_room_join_prompt_xss_member_name_escaped: a
    <script> tag, an <img onerror> tag, and two attribute/quote-breakout
    payloads (a bare '"><img ...' close-and-inject, and a mixed-quote
    'onmouseover=' payload).
    """
    from markupsafe import escape as markupsafe_escape

    owner_headers = await _owner_headers_and_login(client, db_session)

    for i, payload in enumerate((XSS_SCRIPT, XSS_IMG, XSS_ATTR_BREAKOUT, XSS_QUOTE_BREAKOUT)):
        room = await _create_room_via_api(
            client, owner_headers, name=f"switch-xss-room-{i}", members=["agent-a", "agent-b"]
        )

        page = await client.get(f"/ui/rooms/{room['id']}")
        csrf = _extract_csrf(page.text)

        resp = await client.post(
            f"/ui/rooms/{room['id']}/switch-mode",
            data={"mode": "debate", "topic": payload, "csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        view = await client.get(f"/ui/rooms/{room['id']}")
        assert view.status_code == 200
        assert payload not in view.text

        escaped = str(markupsafe_escape(payload))
        assert escaped in view.text

        # Split at "Transcript" so the header portion (room.topic, rendered
        # plain via Jinja2 autoescape) and the transcript portion (the
        # announcement message, rendered exactly like any other message row)
        # are each checked independently -- both must carry the escaped
        # form, neither the raw one.
        marker = "Transcript"
        assert marker in view.text
        header_part, transcript_part = view.text.split(marker, 1)

        assert payload not in header_part
        assert escaped in header_part  # topic shown in the header, escaped

        assert payload not in transcript_part
        assert escaped in transcript_part  # topic shown inside the announcement message, escaped
        assert "Mode switched to Debate." in transcript_part


# --- ADR-0012: room file attachments -- owner UI ---


def _pdf_bytes(body: bytes = b"hello") -> bytes:
    return b"%PDF-1.4\n" + body + b"\n%%EOF"


async def _api_upload(client, owner_headers, room_id, *, filename="doc.pdf", sender="owner", content=None):
    resp = await client.post(
        f"/v1/rooms/{room_id}/attachments",
        params={"filename": filename, "sender": sender},
        content=content if content is not None else _pdf_bytes(),
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()


async def _ui_upload(client, room_id, csrf_token, *, filename="doc.pdf", content=None):
    return await client.post(
        f"/ui/rooms/{room_id}/attachments",
        params={"filename": filename},
        content=content if content is not None else _pdf_bytes(),
        headers={"X-CSRF-Token": csrf_token},
    )


async def test_room_view_files_panel_shows_attachment_details(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="files-panel-room")
    await _api_upload(client, owner_headers, room["id"], filename="report.pdf", content=_pdf_bytes(b"panel-body"))

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "report.pdf" in resp.text
    assert f"{len(_pdf_bytes(b'panel-body'))} bytes" in resp.text
    assert "attached by owner" in resp.text
    assert "No attachments yet." not in resp.text


async def test_room_view_files_panel_empty_state(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="files-panel-empty")

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert "No attachments yet." in resp.text


async def test_room_view_attachment_filename_xss_inert_end_to_end(client, db_session):
    """Uploading a file whose CLIENT-supplied name is an XSS payload: the
    sanitizer (app/room_export.py's safe_filename_component, applied at
    upload time) is the primary defense, and the raw payload must never
    appear anywhere in the rendered page either way.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="filename-xss-room")
    malicious = XSS_SCRIPT + ".pdf"
    uploaded = await _api_upload(client, owner_headers, room["id"], filename=malicious)
    assert XSS_SCRIPT not in uploaded["filename"]

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert XSS_SCRIPT not in resp.text
    assert "<script>" not in resp.text.lower().replace(" ", "")


async def test_room_view_attachment_filename_xss_inert_via_template_autoescape(client, db_session):
    """Defense in depth, independent of the sanitizer: a `RoomAttachment`
    row inserted directly with a filename the sanitizer would never itself
    produce (simulating anything that ever bypassed it) must still render
    inert -- proving room_view.html's rendering of `a.attachment.filename`
    is ordinary Jinja2 autoescape, never `|safe`, same discipline as every
    other untrusted field on this page.
    """
    from datetime import UTC, datetime as dt

    from app.models import AttachmentBlob, RoomAttachment

    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="filename-xss-template-room")

    blob = AttachmentBlob(sha256="b" * 64, byte_size=10, created_at=dt.now(UTC))
    db_session.add(blob)
    await db_session.flush()
    db_session.add(
        RoomAttachment(
            id=str(ULID()),
            room_id=room["id"],
            blob_sha256=blob.sha256,
            filename=XSS_IMG,
            uploaded_by="owner",
            created_at=dt.now(UTC),
        )
    )
    await db_session.commit()

    resp = await client.get(f"/ui/rooms/{room['id']}")
    assert resp.status_code == 200
    assert XSS_IMG not in resp.text

    from markupsafe import escape as markupsafe_escape

    assert str(markupsafe_escape(XSS_IMG)) in resp.text


async def test_ui_upload_attachment_succeeds(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-upload-room")
    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await _ui_upload(client, room["id"], csrf, filename="uploaded.pdf")
    assert resp.status_code == 201
    assert resp.json()["filename"] == "uploaded.pdf"

    view = await client.get(f"/ui/rooms/{room['id']}")
    assert "uploaded.pdf" in view.text


async def test_ui_upload_attachment_without_csrf_header_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-upload-no-csrf")
    resp = await client.post(
        f"/ui/rooms/{room['id']}/attachments", params={"filename": "doc.pdf"}, content=_pdf_bytes()
    )
    assert resp.status_code == 403


async def test_ui_upload_attachment_machine_token_cannot_reach_ui(client, db_session):
    # Deliberately `_owner_headers` (bearer-only), NOT `_owner_headers_and_login`
    # -- this client must carry NO session cookie at all, so the machine
    # bearer token below is the only credential on the request; a leftover
    # owner cookie from an earlier login on this same client would let the
    # request through for the wrong reason and defeat the point of this test.
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-upload-machine")
    resp = await client.post(
        f"/ui/rooms/{room['id']}/attachments",
        params={"filename": "doc.pdf"},
        content=_pdf_bytes(),
        headers={**machine_headers, "X-CSRF-Token": "irrelevant"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_ui_download_attachment_headers(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-download-room")
    payload = _pdf_bytes(b"ui-download-body")
    uploaded = await _api_upload(client, owner_headers, room["id"], filename="ui-report.pdf", content=payload)

    resp = await client.get(f"/ui/rooms/{room['id']}/attachments/{uploaded['id']}/download")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="ui-report.pdf"'
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert resp.content == payload


async def test_ui_delete_attachment_via_form(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-delete-room")
    uploaded = await _api_upload(client, owner_headers, room["id"], filename="to-delete.pdf")
    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/attachments/{uploaded['id']}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    view = await client.get(f"/ui/rooms/{room['id']}")
    # Gone from the files panel specifically (a precise check, not a bare
    # substring one -- ADR-0012 stage 3 now legitimately posts a system
    # message into the transcript on the same page that names the deleted
    # file, see the assertions below).
    assert f'data-attachment-id="{uploaded["id"]}"' not in view.text
    assert "No attachments yet." in view.text
    # The removal is announced in the transcript (ADR-0012 stage 3 item 2)
    # so an agent long-polling the room learns about it.
    assert "Attachment removed:" in view.text
    assert "to-delete.pdf" in view.text  # named in the system message(s)


async def test_ui_delete_attachment_without_csrf_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-delete-no-csrf")
    uploaded = await _api_upload(client, owner_headers, room["id"])
    resp = await client.post(f"/ui/rooms/{room['id']}/attachments/{uploaded['id']}/delete", data={})
    assert resp.status_code == 403


async def test_ui_save_attachment_to_brain_leaves_room_copy_readable(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-save-room")
    payload = _pdf_bytes(b"ui-save-body")
    uploaded = await _api_upload(client, owner_headers, room["id"], filename="ui-save.pdf", content=payload)
    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/attachments/{uploaded['id']}/save",
        data={"project": "ui-save-project", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ui/rooms/{room['id']}?deposited=1"

    # Room copy unaffected -- still listed, still downloadable.
    view = await client.get(f"/ui/rooms/{room['id']}")
    assert "ui-save.pdf" in view.text
    download = await client.get(f"/ui/rooms/{room['id']}/attachments/{uploaded['id']}/download")
    assert download.status_code == 200
    assert download.content == payload


async def test_ui_attach_search_returns_documents_with_a_blob(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    source_room = await _create_room_via_api(client, owner_headers, name="ui-search-source")
    # A space-separated filename, not a hyphenated one: Postgres's default
    # text search config indexes a hyphenated "searchable-spec.pdf" as ONE
    # compound lexeme (verified against this exact stack: `to_tsvector`
    # doesn't split it), so a plain single-word query would never match it
    # -- unrelated to this endpoint's own logic, just how FTS tokenizes
    # hyphens+dots. Space-separated words tokenize individually instead.
    uploaded = await _api_upload(client, owner_headers, source_room["id"], filename="brainard widget spec.pdf")
    save_resp = await client.post(
        f"/v1/rooms/{source_room['id']}/attachments/{uploaded['id']}/save",
        json={"project": "ui-attach-search-project"},
        headers=owner_headers,
    )
    assert save_resp.status_code == 200, save_resp.json()

    resp = await client.get(f"/ui/rooms/{source_room['id']}/attach-search?q=widget")
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert any(r["title"] == "brainard widget spec.pdf" for r in results)


async def test_ui_attach_from_brain_via_form(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    source_room = await _create_room_via_api(client, owner_headers, name="ui-attach-source")
    uploaded = await _api_upload(client, owner_headers, source_room["id"], filename="reusable.pdf")
    save_resp = await client.post(
        f"/v1/rooms/{source_room['id']}/attachments/{uploaded['id']}/save",
        json={"project": "ui-attach-from-brain-project"},
        headers=owner_headers,
    )
    document_id = save_resp.json()["document_id"]

    target_room = await _create_room_via_api(client, owner_headers, name="ui-attach-target")
    page = await client.get(f"/ui/rooms/{target_room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{target_room['id']}/attach-from-brain",
        data={"document_id": document_id, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    view = await client.get(f"/ui/rooms/{target_room['id']}")
    assert "reusable.pdf" in view.text


async def test_ui_agent_uploads_checkbox_reflects_state(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-checkbox-room")

    on = await client.get(f"/ui/rooms/{room['id']}")
    assert 'name="allowed" value="1" checked' in on.text

    off_resp = await client.post(f"/v1/rooms/{room['id']}/agent-uploads", json={"allowed": False}, headers=owner_headers)
    assert off_resp.status_code == 200

    off = await client.get(f"/ui/rooms/{room['id']}")
    assert 'name="allowed" value="1" checked' not in off.text


async def test_ui_toggle_agent_uploads_via_form(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-toggle-room")
    page = await client.get(f"/ui/rooms/{room['id']}")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        f"/ui/rooms/{room['id']}/agent-uploads", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["agent_uploads_allowed"] is False

    view = await client.get(f"/ui/rooms/{room['id']}")
    assert "Agent file uploads are now disabled in this room." in view.text


async def test_ui_toggle_agent_uploads_without_csrf_rejected(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="ui-toggle-no-csrf")
    resp = await client.post(f"/ui/rooms/{room['id']}/agent-uploads", data={})
    assert resp.status_code == 403
