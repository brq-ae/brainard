"""Agent Chat Rooms -- Phase A core API (ADR-0006): create/list/detail/post/
close/long-poll, guardrails (done-signal, hard cap, owner close), and the
best-effort owner notification on close.
"""

import asyncio
import time

from ulid import ULID

import app.notify as notify_module
from app.models import Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _machine_headers(db_session, name: str = "test-machine") -> dict:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name=name, token_hash=hash_token(token), status="active")
    db_session.add(machine)
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _create_room(client, owner_headers, *, name="room-1", members=None, max_messages=None) -> dict:
    body: dict = {"name": name, "members": members if members is not None else ["agent-a", "agent-b"]}
    if max_messages is not None:
        body["max_messages"] = max_messages
    resp = await client.post("/v1/rooms", json=body, headers=owner_headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


async def _configure_notifications(client, owner_headers) -> None:
    resp = await client.post(
        "/v1/notifications-config",
        json={"ntfy_url": "https://ntfy.example.org", "topic": "roomtopic"},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.json()


# --- create: validation ---


async def test_create_room_happy_path(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, name="commander-builder", members=["agent-a", "agent-b"])
    assert room["name"] == "commander-builder"
    assert room["status"] == "open"
    assert sorted(room["members"]) == ["agent-a", "agent-b"]
    assert room["max_messages"] == 100  # default


async def test_create_room_rejects_one_member(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post("/v1/rooms", json={"name": "r", "members": ["only-one"]}, headers=owner_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_members"


async def test_create_room_rejects_three_members(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post("/v1/rooms", json={"name": "r", "members": ["a", "b", "c"]}, headers=owner_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_members"


async def test_create_room_rejects_duplicate_members(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post("/v1/rooms", json={"name": "r", "members": ["same", "same"]}, headers=owner_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "duplicate_room_members"


async def test_create_room_rejects_empty_member_name(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post("/v1/rooms", json={"name": "r", "members": ["a", "   "]}, headers=owner_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_members"


async def test_create_room_rejects_bad_max_messages_zero(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["a", "b"], "max_messages": 0}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_max_messages"


async def test_create_room_rejects_bad_max_messages_too_large(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["a", "b"], "max_messages": 10001}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_max_messages"


async def test_create_room_accepts_custom_max_messages(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, max_messages=5)
    assert room["max_messages"] == 5


async def test_create_room_rejects_empty_name(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post("/v1/rooms", json={"name": "  ", "members": ["a", "b"]}, headers=owner_headers)
    assert resp.status_code == 422


# --- list / detail ---


async def test_list_rooms_newest_first(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room1 = await _create_room(client, owner_headers, name="first", members=["a1", "a2"])
    room2 = await _create_room(client, owner_headers, name="second", members=["b1", "b2"])

    resp = await client.get("/v1/rooms", headers=machine_headers)
    assert resp.status_code == 200
    data = resp.json()
    ids = [r["id"] for r in data["results"]]
    assert ids.index(room2["id"]) < ids.index(room1["id"])
    by_id = {r["id"]: r for r in data["results"]}
    assert sorted(by_id[room1["id"]]["members"]) == ["a1", "a2"]
    assert by_id[room1["id"]]["message_count"] == 0
    assert by_id[room1["id"]]["close_reason"] is None


async def test_get_room_detail_includes_members_and_messages(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hello"}, headers=machine_headers
    )

    resp = await client.get(f"/v1/rooms/{room['id']}", headers=machine_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert sorted(data["members"]) == ["agent-a", "agent-b"]
    assert data["status"] == "open"
    assert data["message_count"] == 1
    assert len(data["messages"]) == 1
    assert data["messages"][0]["text"] == "hello"
    assert data["messages"][0]["seq"] == 1


async def test_get_room_detail_404_for_unknown_room(client, db_session):
    machine_headers = await _machine_headers(db_session)
    resp = await client.get("/v1/rooms/notaroom", headers=machine_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "room_not_found"


# --- posting messages ---


async def test_post_message_as_member_ok(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi there"}, headers=machine_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["seq"] == 1
    assert data["room_status"] == "open"
    assert data["close_reason"] is None


async def test_post_message_as_owner_ok(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "owner", "text": "stepping in"}, headers=owner_headers
    )
    assert resp.status_code == 200
    assert resp.json()["seq"] == 1


async def test_post_message_non_member_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "stranger", "text": "hi"}, headers=machine_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sender_not_room_member"


async def test_post_message_to_closed_room_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "too late"}, headers=machine_headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "room_closed"


async def test_post_message_empty_text_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "   "}, headers=machine_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "empty_message_text"


async def test_post_message_oversize_text_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    huge_text = "x" * (32 * 1024 + 1)
    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": huge_text}, headers=machine_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "message_text_too_large"


async def test_post_message_invalid_kind_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages",
        json={"sender": "agent-a", "text": "hi", "kind": "system"},
        headers=machine_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_message_kind"


async def test_seq_monotonic_and_gap_free(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    seqs = []
    for i in range(6):
        sender = "agent-a" if i % 2 == 0 else "agent-b"
        resp = await client.post(
            f"/v1/rooms/{room['id']}/messages", json={"sender": sender, "text": f"msg {i}"}, headers=machine_headers
        )
        assert resp.status_code == 200
        seqs.append(resp.json()["seq"])

    assert seqs == [1, 2, 3, 4, 5, 6]


# --- guardrails ---


async def test_done_signal_closes_room(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "all done", "kind": "done"}, headers=machine_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["room_status"] == "closed"
    assert data["close_reason"] == "done"

    detail = (await client.get(f"/v1/rooms/{room['id']}", headers=machine_headers)).json()
    assert detail["status"] == "closed"
    assert detail["close_reason"] == "done"
    assert detail["closed_at"] is not None


async def test_cap_auto_closes_room(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], max_messages=2)

    resp1 = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "one"}, headers=machine_headers
    )
    assert resp1.json()["room_status"] == "open"

    resp2 = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-b", "text": "two"}, headers=machine_headers
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["room_status"] == "closed"
    assert data2["close_reason"] == "cap"

    # A third post is now rejected -- the cap really is a hard backstop.
    resp3 = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "three"}, headers=machine_headers
    )
    assert resp3.status_code == 409


async def test_owner_close_is_idempotent(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp1 = await client.post(f"/v1/rooms/{room['id']}/close", json={"reason": "stall"}, headers=owner_headers)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["status"] == "closed"
    assert data1["close_reason"] == "stall"

    # Closing again returns the same state without error.
    resp2 = await client.post(f"/v1/rooms/{room['id']}/close", json={"reason": "owner"}, headers=owner_headers)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "closed"
    assert data2["close_reason"] == "stall"  # unchanged -- idempotent, not overwritten


async def test_owner_close_defaults_reason_to_owner(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json()["close_reason"] == "owner"


async def test_close_rejects_bad_reason(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/v1/rooms/{room['id']}/close", json={"reason": "nonsense"}, headers=owner_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_close_reason"


# --- owner notification on close (mocked) ---


async def test_notification_fired_on_owner_close(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="watched-room", members=["agent-a", "agent-b"])

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)
    assert resp.status_code == 200

    assert len(calls) == 1
    url, title, body = calls[0]
    assert url == "https://ntfy.example.org/roomtopic"
    assert title == "Brain room: watched-room"
    assert "watched-room" in body
    assert "owner" in body
    assert "closed" in body


async def test_notification_fired_on_guardrail_close(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="capped-room", members=["agent-a", "agent-b"], max_messages=1)

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers
    )
    assert resp.status_code == 200
    assert resp.json()["close_reason"] == "cap"

    assert len(calls) == 1
    _, title, body = calls[0]
    assert title == "Brain room: capped-room"
    assert "cap" in body


async def test_close_succeeds_even_if_notify_raises(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    async def broken_send(url, title, body):
        raise RuntimeError("simulated ntfy outage")

    monkeypatch.setattr(notify_module, "_send_ntfy", broken_send)

    resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


async def test_close_succeeds_with_no_notification_channel_configured(client, db_session):
    """No POST /v1/notifications-config was ever made -- current_config()
    returns None, and the close must still succeed (best-effort, never
    breaks the room operation)."""
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


# --- long-poll ---


async def test_long_poll_returns_immediately_when_messages_exist(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await client.post(f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers)

    start = time.monotonic()
    resp = await client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 10}, headers=machine_headers)
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["room_status"] == "open"
    assert elapsed < 2  # must not have waited out any part of the 10s budget


async def test_long_poll_returns_nothing_new_when_since_is_current(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    post_resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers
    )
    last_seq = post_resp.json()["seq"]

    resp = await client.get(
        f"/v1/rooms/{room['id']}/messages", params={"since": last_seq, "wait": 0}, headers=machine_headers
    )
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


async def test_long_poll_returns_empty_after_wait_when_no_new_messages(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    start = time.monotonic()
    resp = await client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 2}, headers=machine_headers)
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == []
    assert data["room_status"] == "open"
    assert elapsed >= 1.5  # actually waited out roughly the requested budget


async def test_long_poll_wait_is_capped_at_30(client, db_session):
    import app.rooms as rooms_module

    assert rooms_module.MAX_WAIT_SECS == 30


async def test_long_poll_returns_promptly_when_message_posted_during_wait(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    async def poster():
        await asyncio.sleep(1.2)
        resp = await client.post(
            f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "arrived"}, headers=machine_headers
        )
        assert resp.status_code == 200

    start = time.monotonic()
    poll_result, _ = await asyncio.gather(
        client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 15}, headers=machine_headers),
        poster(),
    )
    elapsed = time.monotonic() - start

    assert poll_result.status_code == 200
    data = poll_result.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["text"] == "arrived"
    # Returned well before the full 15s budget -- picked up on the next
    # ~1s poll tick after the concurrent post landed, not at the timeout.
    assert elapsed < 6


async def test_long_poll_returns_when_room_closes_during_wait(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    async def closer():
        await asyncio.sleep(1.2)
        resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)
        assert resp.status_code == 200

    start = time.monotonic()
    poll_result, _ = await asyncio.gather(
        client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 15}, headers=machine_headers),
        closer(),
    )
    elapsed = time.monotonic() - start

    assert poll_result.status_code == 200
    data = poll_result.json()
    assert data["messages"] == []
    assert data["room_status"] == "closed"
    assert elapsed < 6


async def test_long_poll_404_for_unknown_room(client, db_session):
    machine_headers = await _machine_headers(db_session)
    resp = await client.get("/v1/rooms/notaroom/messages", params={"since": 0, "wait": 0}, headers=machine_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "room_not_found"


# --- auth matrix ---


async def test_create_room_requires_owner_token(client, db_session):
    machine_headers = await _machine_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["a", "b"]}, headers=machine_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_create_room_requires_auth(client, db_session):
    resp = await client.post("/v1/rooms", json={"name": "r", "members": ["a", "b"]})
    assert resp.status_code == 401


async def test_close_room_requires_owner_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_list_rooms_accepts_machine_and_owner(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    assert (await client.get("/v1/rooms", headers=machine_headers)).status_code == 200
    assert (await client.get("/v1/rooms", headers=owner_headers)).status_code == 200


async def test_list_rooms_requires_auth(client, db_session):
    resp = await client.get("/v1/rooms")
    assert resp.status_code == 401


async def test_post_message_accepts_machine_and_owner(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp1 = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "via machine"}, headers=machine_headers
    )
    assert resp1.status_code == 200
    resp2 = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "owner", "text": "via owner"}, headers=owner_headers
    )
    assert resp2.status_code == 200


async def test_post_message_requires_auth(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    resp = await client.post(f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"})
    assert resp.status_code == 401


async def test_get_room_detail_requires_auth(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    resp = await client.get(f"/v1/rooms/{room['id']}")
    assert resp.status_code == 401


async def test_long_poll_requires_auth(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    resp = await client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 0})
    assert resp.status_code == 401
