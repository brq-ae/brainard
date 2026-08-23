"""Room transcript export (ADR-0011 decision 1) -- app/room_export.py's
pure formatting functions/filename sanitizer, and its two UI download
endpoints (app/routers/ui_rooms.py's GET .../transcript.md and .json). No
model is involved anywhere in this file.
"""

import re
from datetime import UTC, datetime

from ulid import ULID

from app.models import Machine, OwnerToken, Room, RoomMessage
from app.room_export import render_transcript_json, render_transcript_markdown, safe_filename_component, transcript_filename
from app.security import generate_machine_token, generate_owner_token, hash_token

# --- shared fixtures/helpers (same shape as tests/test_ui_rooms.py) ---


async def _create_owner_token(db_session) -> str:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return token


async def _owner_headers(db_session) -> dict:
    token = await _create_owner_token(db_session)
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers_and_login(client, db_session) -> dict:
    token = await _create_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})
    return {"Authorization": f"Bearer {token}"}


async def _machine_headers(db_session, name: str = "test-machine") -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name=name, token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _create_room_via_api(client, owner_headers, *, name="export-room", members=None, **extra) -> dict:
    body: dict = {"name": name, "members": members if members is not None else ["agent-a", "agent-b"]}
    body.update(extra)
    resp = await client.post("/v1/rooms", json=body, headers=owner_headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


async def _post_message(client, machine_headers, room_id, *, sender="agent-a", text="hi", kind="message"):
    resp = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": sender, "text": text, "kind": kind}, headers=machine_headers
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


# --- markdown formatter ---


def test_render_transcript_markdown_shape():
    room = Room(
        id="r1",
        name="Test Room",
        status="closed",
        max_messages=100,
        message_count=2,
        mode="debate",
        topic="Cats vs dogs",
        created_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 8, 6, 13, 0, tzinfo=UTC),
        close_reason="owner",
    )
    members = ["alice", "bob"]
    sides = {"alice": "for", "bob": "against"}
    messages = [
        RoomMessage(
            id="m1", room_id="r1", seq=1, sender="alice", text="Cats are great",
            kind="message", created_at=datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
        ),
        RoomMessage(
            id="m2", room_id="r1", seq=2, sender="system", text="Mode switched",
            kind="system", created_at=datetime(2026, 8, 6, 12, 2, tzinfo=UTC),
        ),
    ]

    md = render_transcript_markdown(room, members, sides, messages)

    assert md.startswith("# Test Room")
    assert "- Mode: debate" in md
    assert "- Topic: Cats vs dogs" in md
    assert "alice (for)" in md and "bob (against)" in md
    assert "- Status: closed (reason: owner)" in md
    assert "- Messages: 2" in md
    assert "**alice** (seq 1, 2026-08-06T12:01:00+00:00)" in md
    assert "Cats are great" in md
    assert "**system** [system] (seq 2" in md
    assert "Mode switched" in md


def test_render_transcript_markdown_zero_messages_exports_cleanly():
    room = Room(
        id="r1", name="Empty room", status="open", max_messages=100, message_count=0,
        mode="freeform", topic=None, created_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    md = render_transcript_markdown(room, ["a", "b"], {"a": None, "b": None}, [])
    assert "- Messages: 0" in md
    assert "(no messages)" in md


def test_render_transcript_json_shape():
    room = Room(
        id="r1", name="JSON room", status="open", max_messages=100, message_count=1,
        mode="freeform", topic=None, created_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    messages = [
        RoomMessage(
            id="m1", room_id="r1", seq=1, sender="a", text="hello",
            kind="message", created_at=datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
        )
    ]

    data = render_transcript_json(room, ["a", "b"], {"a": None, "b": None}, messages)

    assert data["room"]["id"] == "r1"
    assert data["room"]["name"] == "JSON room"
    assert data["room"]["mode"] == "freeform"
    assert data["room"]["message_count"] == 1
    assert data["room"]["members"] == [{"name": "a", "side": None}, {"name": "b", "side": None}]
    assert data["messages"] == [
        {"seq": 1, "sender": "a", "text": "hello", "kind": "message", "created_at": "2026-08-06T12:01:00+00:00"}
    ]


def test_render_transcript_json_zero_messages():
    room = Room(
        id="r1", name="Empty", status="open", max_messages=100, message_count=0,
        mode="freeform", topic=None, created_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    data = render_transcript_json(room, [], {}, [])
    assert data["messages"] == []
    assert data["room"]["message_count"] == 0


# --- filename sanitization ---


def test_safe_filename_component_strips_quotes_slashes_backslashes():
    result = safe_filename_component('He said "hi"/there\\here')
    assert '"' not in result
    assert "/" not in result
    assert "\\" not in result


def test_safe_filename_component_strips_crlf_and_control_chars():
    result = safe_filename_component("line1\r\nline2\x00\x1f")
    assert "\r" not in result
    assert "\n" not in result
    assert "\x00" not in result
    assert "\x1f" not in result


def test_safe_filename_component_unicode_only_falls_back():
    assert safe_filename_component("日本語の部屋") == "room"


def test_safe_filename_component_blank_falls_back():
    assert safe_filename_component("   ") == "room"
    assert safe_filename_component("") == "room"


def test_safe_filename_component_ordinary_name_preserved():
    assert safe_filename_component("My Debate Room") == "My Debate Room"


def test_transcript_filename_sanitized_and_suffixed():
    room = Room(
        id="r1", name='Weird "Room"/Na\r\nme\\Here😀', status="open", max_messages=100,
        message_count=0, mode="freeform", created_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    filename = transcript_filename(room, "md")
    assert filename.endswith("-transcript.md")
    assert '"' not in filename
    assert "\r" not in filename and "\n" not in filename
    assert "/" not in filename and "\\" not in filename


# --- UI endpoints ---


async def test_transcript_md_endpoint_returns_full_transcript(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, name="dl-room")
    await _post_message(client, machine_headers, room["id"], sender="agent-a", text="hello world")

    resp = await client.get(f"/ui/rooms/{room['id']}/transcript.md")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert "dl-room" in disposition
    assert disposition.endswith('.md"')
    assert "# dl-room" in resp.text
    assert "hello world" in resp.text


async def test_transcript_json_endpoint_returns_full_transcript(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, name="json-dl-room")
    await _post_message(client, machine_headers, room["id"], sender="agent-a", text="hello json")

    resp = await client.get(f"/ui/rooms/{room['id']}/transcript.json")

    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    data = resp.json()
    assert data["room"]["name"] == "json-dl-room"
    assert data["messages"][0]["text"] == "hello json"


async def test_transcript_md_zero_messages_exports_cleanly(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="empty-export-room")

    resp = await client.get(f"/ui/rooms/{room['id']}/transcript.md")

    assert resp.status_code == 200
    assert "no messages" in resp.text.lower()


async def test_transcript_json_zero_messages_exports_cleanly(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="empty-json-room")

    resp = await client.get(f"/ui/rooms/{room['id']}/transcript.json")

    assert resp.status_code == 200
    assert resp.json()["messages"] == []


async def test_transcript_endpoints_404_for_unknown_room(client, db_session):
    await _owner_headers_and_login(client, db_session)

    resp_md = await client.get("/ui/rooms/not-a-real-room/transcript.md")
    assert resp_md.status_code == 404
    resp_json = await client.get("/ui/rooms/not-a-real-room/transcript.json")
    assert resp_json.status_code == 404


async def test_transcript_filename_sanitizes_dangerous_room_name_end_to_end(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    weird_name = 'Weird "Room"/Na\r\nme\\Here😀'
    room = await _create_room_via_api(client, owner_headers, name=weird_name)

    resp = await client.get(f"/ui/rooms/{room['id']}/transcript.md")

    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    m = re.search(r'filename="([^"]*)"', disposition)
    assert m, disposition
    filename = m.group(1)
    assert '"' not in filename
    assert "/" not in filename and "\\" not in filename


async def test_transcript_md_requires_ui_session_redirects_to_login(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)

    resp = await client.get(f"/ui/rooms/{room['id']}/transcript.md", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_transcript_json_requires_ui_session_redirects_to_login(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)

    resp = await client.get(f"/ui/rooms/{room['id']}/transcript.json", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_transcript_md_machine_token_cannot_reach_ui(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)
    machine_headers = await _machine_headers(db_session)

    resp = await client.get(
        f"/ui/rooms/{room['id']}/transcript.md", headers=machine_headers, follow_redirects=False
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_transcript_json_machine_token_cannot_reach_ui(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room_via_api(client, owner_headers)
    machine_headers = await _machine_headers(db_session)

    resp = await client.get(
        f"/ui/rooms/{room['id']}/transcript.json", headers=machine_headers, follow_redirects=False
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


# --- room view: export panel present ---


async def test_room_view_shows_export_panel_with_copy_button_and_download_links(client, db_session):
    owner_headers = await _owner_headers_and_login(client, db_session)
    room = await _create_room_via_api(client, owner_headers, name="export-panel-room")

    resp = await client.get(f"/ui/rooms/{room['id']}")

    assert resp.status_code == 200
    assert 'data-copy-target="transcript-markdown"' in resp.text
    assert f"/ui/rooms/{room['id']}/transcript.md" in resp.text
    assert f"/ui/rooms/{room['id']}/transcript.json" in resp.text
    assert "# export-panel-room" in resp.text  # the embedded markdown for the copy button


async def test_room_view_export_panel_xss_message_text_escaped(client, db_session):
    """The embedded transcript markdown (for the copy button) goes through
    ordinary Jinja2 autoescape like everything else on this page -- a
    hostile message must never appear as raw, executable markup.
    """
    owner_headers = await _owner_headers_and_login(client, db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room_via_api(client, owner_headers, name="export-xss-room")
    await _post_message(client, machine_headers, room["id"], text="<script>alert(1)</script>")

    resp = await client.get(f"/ui/rooms/{room['id']}")

    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
