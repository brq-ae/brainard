"""Agent Chat Rooms -- Phase A core API (ADR-0006): create/list/detail/post/
close/long-poll, guardrails (done-signal, hard cap, owner close), and the
best-effort owner notification on close.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from ulid import ULID

import app.notify as notify_module
from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.models import Machine, OwnerToken, Room, RoomMember, RoomMessage
from app.rooms import delete_room as delete_room_op
from app.rooms import post_closing_nudge as post_closing_nudge_op
from app.rooms import post_message as post_message_op
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


async def _create_room(
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
    expires_at=None,
    group=None,
    expect_status=201,
) -> dict:
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
    if expires_at is not None:
        body["expires_at"] = expires_at
    if group is not None:
        body["group"] = group
    resp = await client.post("/v1/rooms", json=body, headers=owner_headers)
    assert resp.status_code == expect_status, resp.json()
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


# --- create: modes and time limits (ADR-0007) ---


async def test_create_room_freeform_is_still_the_default_and_unchanged(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    assert room["mode"] == "freeform"
    assert room["topic"] is None
    assert room["expires_at"] is None
    assert room["sides"] == {"agent-a": None, "agent-b": None}


async def test_create_room_debate_requires_topic(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={"name": "r", "members": ["agent-a", "agent-b"], "mode": "debate", "sides": {"agent-a": "for", "agent-b": "against"}},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "missing_room_topic"


async def test_create_room_debate_rejects_blank_topic(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={
            "name": "r",
            "members": ["agent-a", "agent-b"],
            "mode": "debate",
            "topic": "   ",
            "sides": {"agent-a": "for", "agent-b": "against"},
        },
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "missing_room_topic"


async def test_create_room_debate_requires_both_sides_assigned(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={"name": "r", "members": ["agent-a", "agent-b"], "mode": "debate", "topic": "cats vs dogs"},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_sides"


async def test_create_room_debate_rejects_same_side_twice(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={
            "name": "r",
            "members": ["agent-a", "agent-b"],
            "mode": "debate",
            "topic": "cats vs dogs",
            "sides": {"agent-a": "for", "agent-b": "for"},
        },
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_sides"


async def test_create_room_debate_rejects_sides_for_unknown_member(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={
            "name": "r",
            "members": ["agent-a", "agent-b"],
            "mode": "debate",
            "topic": "cats vs dogs",
            "sides": {"agent-a": "for", "someone-else": "against"},
        },
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_sides"


async def test_create_room_debate_with_proper_sides_succeeds(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(
        client,
        owner_headers,
        members=["agent-a", "agent-b"],
        mode="debate",
        topic="cats vs dogs",
        sides={"agent-a": "for", "agent-b": "against"},
    )
    assert room["mode"] == "debate"
    assert room["topic"] == "cats vs dogs"
    assert room["sides"] == {"agent-a": "for", "agent-b": "against"}


async def test_create_room_critique_with_proper_sides_succeeds(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(
        client,
        owner_headers,
        members=["agent-a", "agent-b"],
        mode="critique",
        topic="the new schema",
        sides={"agent-a": "proposer", "agent-b": "critic"},
    )
    assert room["mode"] == "critique"
    assert room["sides"] == {"agent-a": "proposer", "agent-b": "critic"}


async def test_create_room_collaborate_ignores_sides_if_given(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(
        client,
        owner_headers,
        members=["agent-a", "agent-b"],
        mode="collaborate",
        topic="ship the v2 API",
        sides={"agent-a": "for", "agent-b": "against"},  # nonsensical for a symmetric mode -- ignored, not rejected
    )
    assert room["mode"] == "collaborate"
    assert room["sides"] == {"agent-a": None, "agent-b": None}


async def test_create_room_brainstorm_without_sides_succeeds(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], mode="brainstorm", topic="growth ideas")
    assert room["mode"] == "brainstorm"
    assert room["sides"] == {"agent-a": None, "agent-b": None}


async def test_create_room_rejects_bad_mode(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["agent-a", "agent-b"], "mode": "nonsense"}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_mode"


# --- create: deadline (duration_seconds / expires_at) ---


async def test_create_room_computes_expires_at_from_duration(client, db_session):
    owner_headers = await _owner_headers(db_session)
    before = datetime.now(UTC)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], duration_seconds=1800)
    after = datetime.now(UTC)

    assert room["expires_at"] is not None
    expires_at = datetime.fromisoformat(room["expires_at"])
    assert before + timedelta(seconds=1800) <= expires_at <= after + timedelta(seconds=1800)


async def test_create_room_no_deadline_by_default(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    assert room["expires_at"] is None


async def test_create_room_rejects_zero_duration(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["agent-a", "agent-b"], "duration_seconds": 0}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_deadline"


async def test_create_room_rejects_negative_duration(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["agent-a", "agent-b"], "duration_seconds": -5}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_deadline"


async def test_create_room_rejects_duration_over_30_days(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={"name": "r", "members": ["agent-a", "agent-b"], "duration_seconds": 31 * 24 * 3600},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_deadline"


async def test_create_room_rejects_both_duration_and_expires_at(client, db_session):
    owner_headers = await _owner_headers(db_session)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = await client.post(
        "/v1/rooms",
        json={
            "name": "r",
            "members": ["agent-a", "agent-b"],
            "duration_seconds": 3600,
            "expires_at": future,
        },
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_deadline"


async def test_create_room_accepts_explicit_future_expires_at(client, db_session):
    owner_headers = await _owner_headers(db_session)
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], expires_at=future)
    assert room["expires_at"] is not None
    assert datetime.fromisoformat(room["expires_at"]) > datetime.now(UTC)


async def test_create_room_rejects_past_expires_at(client, db_session):
    owner_headers = await _owner_headers(db_session)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["agent-a", "agent-b"], "expires_at": past}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_deadline"


async def test_create_room_rejects_expires_at_too_far_out(client, db_session):
    owner_headers = await _owner_headers(db_session)
    too_far = (datetime.now(UTC) + timedelta(days=31)).isoformat()
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["agent-a", "agent-b"], "expires_at": too_far}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_deadline"


async def test_cap_and_time_are_independent_cap_still_works_with_a_long_deadline(client, db_session):
    """A room with both a message cap and a (far-future) deadline set is
    closed by whichever guardrail fires first -- here, the cap, long before
    the deadline is anywhere close."""
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(
        client, owner_headers, members=["agent-a", "agent-b"], max_messages=1, duration_seconds=30 * 24 * 3600
    )

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers
    )
    assert resp.status_code == 200
    assert resp.json()["close_reason"] == "cap"


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


# --- ADR-0008: room delete + free-form groups ---


# --- create: group ---


async def test_create_room_with_group_sets_group(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], group="schema-debates")
    assert room["group"] == "schema-debates"


async def test_create_room_without_group_defaults_to_none(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    assert room["group"] is None


async def test_create_room_blank_group_is_none(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], group="   ")
    assert room["group"] is None


async def test_create_room_group_is_trimmed(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], group="  padded  ")
    assert room["group"] == "padded"


async def test_create_room_group_too_long_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={"name": "r", "members": ["agent-a", "agent-b"], "group": "x" * 101},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_group"


# --- list: group filter ---


async def test_list_rooms_includes_group_field(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], group="alpha-group")

    resp = await client.get("/v1/rooms", headers=machine_headers)
    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()["results"]}
    assert by_id[room["id"]]["group"] == "alpha-group"


async def test_list_rooms_group_filter_returns_only_matching(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room_a = await _create_room(client, owner_headers, name="a", members=["a1", "a2"], group="group-a")
    room_b = await _create_room(client, owner_headers, name="b", members=["b1", "b2"], group="group-b")
    room_c = await _create_room(client, owner_headers, name="c", members=["c1", "c2"])  # ungrouped

    resp = await client.get("/v1/rooms", params={"group": "group-a"}, headers=machine_headers)
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["results"]]
    assert ids == [room_a["id"]]
    assert room_b["id"] not in ids
    assert room_c["id"] not in ids


async def test_list_rooms_no_group_filter_returns_all(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _create_room(client, owner_headers, name="a", members=["a1", "a2"], group="group-a")
    await _create_room(client, owner_headers, name="b", members=["b1", "b2"])

    resp = await client.get("/v1/rooms", headers=machine_headers)
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2


async def test_get_room_detail_includes_group(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], group="detail-group")

    resp = await client.get(f"/v1/rooms/{room['id']}", headers=machine_headers)
    assert resp.status_code == 200
    assert resp.json()["group"] == "detail-group"


# --- bulk group assignment ---


async def test_assign_group_sets_group_on_multiple_rooms(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room1 = await _create_room(client, owner_headers, name="r1", members=["a1", "a2"])
    room2 = await _create_room(client, owner_headers, name="r2", members=["b1", "b2"])

    resp = await client.post(
        "/v1/rooms/assign-group",
        json={"room_ids": [room1["id"], room2["id"]], "group": "batch-group"},
        headers=owner_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["updated"] == 2
    assert data["group"] == "batch-group"

    detail1 = await client.get(f"/v1/rooms/{room1['id']}", headers=owner_headers)
    detail2 = await client.get(f"/v1/rooms/{room2['id']}", headers=owner_headers)
    assert detail1.json()["group"] == "batch-group"
    assert detail2.json()["group"] == "batch-group"


async def test_assign_group_null_clears_group(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], group="had-a-group")

    resp = await client.post(
        "/v1/rooms/assign-group",
        json={"room_ids": [room["id"]], "group": None},
        headers=owner_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["group"] is None

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["group"] is None


async def test_assign_group_blank_clears_group(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], group="had-a-group")

    resp = await client.post(
        "/v1/rooms/assign-group",
        json={"room_ids": [room["id"]], "group": "   "},
        headers=owner_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["group"] is None


async def test_assign_group_requires_owner_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        "/v1/rooms/assign-group",
        json={"room_ids": [room["id"]], "group": "nope"},
        headers=machine_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_assign_group_requires_auth(client, db_session):
    resp = await client.post("/v1/rooms/assign-group", json={"room_ids": ["whatever"], "group": "nope"})
    assert resp.status_code == 401


async def test_assign_group_rejects_unknown_room_id(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        "/v1/rooms/assign-group",
        json={"room_ids": [room["id"], "not-a-real-room"], "group": "some-group"},
        headers=owner_headers,
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "unknown_room_ids"
    assert "not-a-real-room" in body["error"]["unknown_ids"]

    # All-or-nothing: the known room's group must be untouched by the
    # rejected call.
    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["group"] is None


async def test_assign_group_rejects_empty_room_ids(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms/assign-group", json={"room_ids": [], "group": "some-group"}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_ids"


async def test_assign_group_rejects_group_too_long(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        "/v1/rooms/assign-group",
        json={"room_ids": [room["id"]], "group": "x" * 101},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_group"


# --- delete ---


async def test_delete_room_owner_hard_deletes_room_and_cascades(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers
    )
    await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-b", "text": "hey"}, headers=machine_headers
    )

    resp = await client.delete(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == room["id"]
    assert data["deleted"] is True
    assert data["deleted_messages"] == 2
    assert data["deleted_members"] == 2

    # Gone via the API too.
    get_resp = await client.get(f"/v1/rooms/{room['id']}", headers=machine_headers)
    assert get_resp.status_code == 404

    # And genuinely gone from the database -- not a soft-delete/status flip.
    assert await db_session.get(Room, room["id"]) is None
    remaining_members = (
        await db_session.execute(select(RoomMember).where(RoomMember.room_id == room["id"]))
    ).all()
    remaining_messages = (
        await db_session.execute(select(RoomMessage).where(RoomMessage.room_id == room["id"]))
    ).all()
    assert remaining_members == []
    assert remaining_messages == []


async def test_delete_room_works_on_closed_room(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    close_resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)
    assert close_resp.status_code == 200

    resp = await client.delete(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert resp.status_code == 200
    assert await db_session.get(Room, room["id"]) is None


async def test_delete_room_requires_owner_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.delete(f"/v1/rooms/{room['id']}", headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"

    # Untouched by the rejected attempt.
    assert await db_session.get(Room, room["id"]) is not None


async def test_delete_room_requires_auth(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.delete(f"/v1/rooms/{room['id']}")
    assert resp.status_code == 401


async def test_delete_room_404_for_unknown_room(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.delete("/v1/rooms/not-a-real-room", headers=owner_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "room_not_found"


async def test_delete_room_404_when_already_deleted(client, db_session):
    """Deleting the same room id twice is a clear 404 the second time, not
    a silent success -- ADR-0008 hard delete has no "already gone" no-op.
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    first = await client.delete(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert first.status_code == 200
    second = await client.delete(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "room_not_found"


async def test_delete_room_does_not_affect_other_rooms(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room1 = await _create_room(client, owner_headers, name="keep-me", members=["a1", "a2"])
    room2 = await _create_room(client, owner_headers, name="delete-me", members=["b1", "b2"])
    await client.post(
        f"/v1/rooms/{room1['id']}/messages", json={"sender": "a1", "text": "still here"}, headers=machine_headers
    )

    resp = await client.delete(f"/v1/rooms/{room2['id']}", headers=owner_headers)
    assert resp.status_code == 200

    still_there = await client.get(f"/v1/rooms/{room1['id']}", headers=machine_headers)
    assert still_there.status_code == 200
    assert still_there.json()["message_count"] == 1


# --- delete_room row-lock race safety (fix-first review finding) ---
#
# delete_room now takes the room's row lock (SELECT ... FOR UPDATE, same
# pattern close_room/post_message/post_closing_nudge already use) before
# deleting anything. These tests force GENUINE Postgres-level lock
# contention -- not just decoupled timing luck -- by delaying one side's
# COMMIT while it holds the lock, so the other side's own FOR UPDATE
# provably blocks at the database level until the winner commits (asserted
# via elapsed time). Both interleavings (delete_room wins the lock first /
# loses it) are exercised against both post_message(kind='done') and
# post_closing_nudge -- the two other writers that touch a room's row and
# its children. The invariant under test either way: no raw
# IntegrityError/500, no orphaned row, and the loser observes a clean
# 404/None once the winner's commit lands.

COMMIT_DELAY = 0.4
HEAD_START = 0.05
RACE_TRIALS = 3


def _delayed_commit_session(delay: float):
    """A real AsyncSessionLocal() session whose .commit() sleeps `delay`
    seconds before actually committing -- used to hold a just-acquired
    FOR UPDATE row lock open long enough that a genuinely concurrent
    caller's own lock acquisition provably blocks on it.
    """
    session = AsyncSessionLocal()
    original_commit = session.commit

    async def slow_commit():
        await asyncio.sleep(delay)
        await original_commit()

    session.commit = slow_commit
    return session


async def test_delete_room_race_delete_wins_against_post_message_done(client, db_session):
    """Interleaving A: delete_room acquires the lock first and holds it
    (via the delayed commit) while a concurrent post_message('done') tries
    to post -- the poster must genuinely block, then see a clean 404 once
    delete has committed. Never a 500/IntegrityError, never an orphaned
    message.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_delete():
            async with winner_session:
                return await delete_room_op(winner_session, room_id)

        async def run_post():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                try:
                    return await post_message_op(loser_session, room_id, "agent-a", "goodbye", "done")
                except ApiError as exc:
                    return exc

        start = time.monotonic()
        delete_result, post_result = await asyncio.gather(run_delete(), run_post())
        elapsed = time.monotonic() - start

        # Genuine blocking, not decoupled timing luck: the loser could not
        # have finished before the winner released the lock.
        assert elapsed >= COMMIT_DELAY

        messages_deleted, members_deleted = delete_result
        assert messages_deleted == 0
        assert members_deleted == 2
        assert isinstance(post_result, ApiError)
        assert post_result.code == "room_not_found"

        # Genuinely gone; no orphaned rows from the raced post.
        assert await db_session.get(Room, room_id) is None
        remaining = (
            await db_session.execute(
                RoomMessage.__table__.select().where(RoomMessage.__table__.c.room_id == room_id)
            )
        ).all()
        assert remaining == []


async def test_delete_room_race_post_message_done_wins_against_delete(client, db_session):
    """Interleaving B: post_message('done') acquires the lock first and
    holds it while delete_room tries to delete -- delete_room must
    genuinely block, then (once the message is committed) proceed to
    delete the room INCLUDING that just-inserted message, with no FK
    violation.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_post():
            async with winner_session:
                return await post_message_op(winner_session, room_id, "agent-a", "goodbye", "done")

        async def run_delete():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await delete_room_op(loser_session, room_id)

        start = time.monotonic()
        post_result, delete_result = await asyncio.gather(run_post(), run_delete())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        # post_message really landed (message inserted, room closed by the
        # done-signal) before delete swept it up.
        message, room_after_post = post_result
        assert message.seq == 1
        assert room_after_post.status == "closed"

        messages_deleted, members_deleted = delete_result
        assert messages_deleted == 1  # the raced-in 'done' message, cleanly swept up -- no FK violation
        assert members_deleted == 2

        assert await db_session.get(Room, room_id) is None


async def test_delete_room_race_delete_wins_against_post_closing_nudge(client, db_session):
    """Same interleaving A, against post_closing_nudge instead of
    post_message: the nudge's own lock acquisition finds the room already
    gone -> a clean None, never an exception.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_delete():
            async with winner_session:
                return await delete_room_op(winner_session, room_id)

        async def run_nudge():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await post_closing_nudge_op(loser_session, room_id, "closing soon")

        start = time.monotonic()
        delete_result, nudge_result = await asyncio.gather(run_delete(), run_nudge())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY
        messages_deleted, members_deleted = delete_result
        assert messages_deleted == 0
        assert members_deleted == 2
        assert nudge_result is None  # clean no-op: room gone by the time the lock was acquired

        assert await db_session.get(Room, room_id) is None


async def test_delete_room_race_post_closing_nudge_wins_against_delete(client, db_session):
    """Same interleaving B, against post_closing_nudge instead of
    post_message: the nudge's system message really lands first, then
    delete sweeps it up with no FK violation.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_nudge():
            async with winner_session:
                return await post_closing_nudge_op(winner_session, room_id, "closing soon")

        async def run_delete():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await delete_room_op(loser_session, room_id)

        start = time.monotonic()
        nudge_result, delete_result = await asyncio.gather(run_nudge(), run_delete())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY
        assert nudge_result is not None  # the nudge really landed before delete ran

        messages_deleted, members_deleted = delete_result
        assert messages_deleted == 1  # the raced-in nudge message, cleanly swept up -- no FK violation
        assert members_deleted == 2

        assert await db_session.get(Room, room_id) is None


async def test_delete_room_race_delete_already_gone_is_clean_404(client, db_session):
    """Baseline (not itself a race, but the reviewer's other requested
    check): deleting a room that's already been deleted -- via the same
    locked path -- is a clean 404, never a 500.
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    async with AsyncSessionLocal() as session1:
        await delete_room_op(session1, room["id"])

    async with AsyncSessionLocal() as session2:
        try:
            await delete_room_op(session2, room["id"])
            assert False, "expected ApiError"
        except ApiError as exc:
            assert exc.code == "room_not_found"
            assert exc.status_code == 404
