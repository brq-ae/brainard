"""UI Agent Chat Rooms (ADR-0006 phase B): rooms list + create form, room
live view, the cookie-authed JSON short-poll endpoint, owner post, owner
close, and XSS handling of untrusted agent message content. Exercises the
same shared logic (app/rooms.py) as the phase A API -- see tests/test_rooms.py
for the API-side equivalent.
"""

import re
from datetime import UTC, datetime, timedelta

from ulid import ULID

from app.models import Machine, OwnerToken, Room, RoomMessage
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
