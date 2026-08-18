"""UI Agent Chat Rooms (ADR-0006 phase B): rooms list + create form, room
live view, the cookie-authed JSON short-poll endpoint, owner post, owner
close, and XSS handling of untrusted agent message content. Exercises the
same shared logic (app/rooms.py) as the phase A API -- see tests/test_rooms.py
for the API-side equivalent.
"""

import re

from ulid import ULID

from app.models import Machine, OwnerToken, Room, RoomMessage
from app.security import generate_machine_token, generate_owner_token, hash_token

XSS_SCRIPT = "<script>alert(1)</script>"
XSS_IMG = '<img src=x onerror=alert(1)>'


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


async def _create_room_via_api(client, owner_headers, *, name="room-1", members=None, max_messages=None) -> dict:
    body: dict = {"name": name, "members": members if members is not None else ["agent-a", "agent-b"]}
    if max_messages is not None:
        body["max_messages"] = max_messages
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
