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
from app.attachments import blob_path
from app.auth import Principal
from app.config import get_settings
from app.models import AttachmentBlob, Machine, OwnerToken, Room, RoomAttachment, RoomMember, RoomMessage
from app.rooms import close_room as close_room_op
from app.rooms import delete_room as delete_room_op
from app.rooms import post_closing_nudge as post_closing_nudge_op
from app.rooms import post_message as post_message_op
from app.rooms import switch_room_mode as switch_room_mode_op
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


async def test_post_message_machine_token_cannot_claim_owner_sender(client, db_session):
    """Independent-review Fix 1 (BLOCKER): `sender` alone carries no
    identity -- before this fix, ANY valid machine token could pass
    `sender=owner` and have the message recorded (and rendered in the
    transcript) as coming from the owner. The claim is now bound to the
    AUTHENTICATED principal, so a machine token is rejected outright,
    before the (irrelevant here) membership check ever runs.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "owner", "text": "not really the owner"}, headers=machine_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_sender_requires_owner_token"

    # Nothing was posted -- a subsequent genuine owner post is still seq 1.
    real_post = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "owner", "text": "actually the owner"}, headers=owner_headers
    )
    assert real_post.status_code == 200
    assert real_post.json()["seq"] == 1


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


# --- ADR-0012 stage 2: delete_room's attachment blob-reclaim fix ---
#
# RoomAttachment.room_id is ondelete="CASCADE" (a documented, narrow
# exception -- see that model's docstring): before this fix, delete_room
# dropped a room's attachment reference rows via that DB-level cascade but
# never ran the reference-counted blob sweep, orphaning any blob only that
# room referenced (row AND bytes left on disk with nothing pointing at
# them). These three tests exercise the real fix end to end, through the
# real v1 upload/save endpoints, not by hand-writing rows.


def _pdf_bytes(body: bytes = b"hello") -> bytes:
    return b"%PDF-1.4\n" + body + b"\n%%EOF"


async def test_delete_room_reclaims_an_orphaned_blob(client, db_session):
    """The concrete bug: a blob referenced by ONLY the deleted room must be
    gone -- both the attachment_blobs row and the file on disk -- right
    after the delete, with no grace period to wait out (a hard delete
    forfeits the grace-period cushion on its own reference; see
    RoomAttachment's docstring).
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    upload_resp = await client.post(
        f"/v1/rooms/{room['id']}/attachments",
        params={"filename": "doc.pdf", "sender": "owner"},
        content=_pdf_bytes(b"delete-room-reclaim"),
        headers=owner_headers,
    )
    assert upload_resp.status_code == 201, upload_resp.json()

    attachment_row = await db_session.get(RoomAttachment, upload_resp.json()["id"])
    sha256_hex = attachment_row.blob_sha256
    path = blob_path(get_settings().attachment_storage_dir, sha256_hex)
    assert path.exists()

    delete_resp = await client.delete(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert delete_resp.status_code == 200

    assert (await db_session.get(AttachmentBlob, sha256_hex)) is None
    assert not path.exists()


async def test_delete_room_does_not_reclaim_blob_shared_with_another_live_room(client, db_session):
    """The other half of decision 4: two rooms deduped to the same blob --
    deleting ONE of them must never delete the file the other room still
    needs.
    """
    owner_headers = await _owner_headers(db_session)
    room_a = await _create_room(client, owner_headers, name="room-a", members=["a1", "a2"])
    room_b = await _create_room(client, owner_headers, name="room-b", members=["b1", "b2"])
    payload = _pdf_bytes(b"shared-between-two-live-rooms")

    up_a = await client.post(
        f"/v1/rooms/{room_a['id']}/attachments",
        params={"filename": "a.pdf", "sender": "owner"},
        content=payload,
        headers=owner_headers,
    )
    assert up_a.status_code == 201
    up_b = await client.post(
        f"/v1/rooms/{room_b['id']}/attachments",
        params={"filename": "b.pdf", "sender": "owner"},
        content=payload,
        headers=owner_headers,
    )
    assert up_b.status_code == 201

    row_a = await db_session.get(RoomAttachment, up_a.json()["id"])
    sha256_hex = row_a.blob_sha256
    path = blob_path(get_settings().attachment_storage_dir, sha256_hex)
    assert path.exists()

    delete_resp = await client.delete(f"/v1/rooms/{room_a['id']}", headers=owner_headers)
    assert delete_resp.status_code == 200

    # room_b (untouched, still open) still references the exact same blob.
    assert (await db_session.get(AttachmentBlob, sha256_hex)) is not None
    assert path.exists()


async def test_delete_room_does_not_reclaim_blob_referenced_by_a_brain_document(client, db_session):
    """Decision 3/4: once "Save to Brain" links a blob to a MirroredDocument
    row, it must survive forever -- deleting the room that originally held
    the file must never take the blob down with it.
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    upload_resp = await client.post(
        f"/v1/rooms/{room['id']}/attachments",
        params={"filename": "keep.pdf", "sender": "owner"},
        content=_pdf_bytes(b"saved-to-brain-then-room-deleted"),
        headers=owner_headers,
    )
    assert upload_resp.status_code == 201
    attachment_id = upload_resp.json()["id"]

    save_resp = await client.post(
        f"/v1/rooms/{room['id']}/attachments/{attachment_id}/save",
        json={"project": "delete-room-reclaim-test"},
        headers=owner_headers,
    )
    assert save_resp.status_code == 200, save_resp.json()

    row = await db_session.get(RoomAttachment, attachment_id)
    sha256_hex = row.blob_sha256
    path = blob_path(get_settings().attachment_storage_dir, sha256_hex)

    delete_resp = await client.delete(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert delete_resp.status_code == 200

    assert (await db_session.get(AttachmentBlob, sha256_hex)) is not None
    assert path.exists()


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

# These races post as an "agent-a" sender, i.e. a machine-authenticated
# call -- post_message now requires a `principal` to bind the `sender`
# claim to (fix for the sender="owner" impersonation bug); the concurrency
# invariant under test here is unrelated to that identity check, so a
# single fixed machine principal is reused across all of them.
_RACE_MACHINE_PRINCIPAL = Principal(kind="machine", machine=Machine(id=str(ULID())))


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
                    return await post_message_op(loser_session, room_id, "agent-a", "goodbye", "done", principal=_RACE_MACHINE_PRINCIPAL)
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
                return await post_message_op(winner_session, room_id, "agent-a", "goodbye", "done", principal=_RACE_MACHINE_PRINCIPAL)

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


# --- ADR-0009: mid-session mode switch ---


async def _get_latest_message(client, headers, room_id) -> dict:
    detail = await client.get(f"/v1/rooms/{room_id}", headers=headers)
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert messages
    return messages[-1]


async def test_switch_mode_freeform_to_debate_sets_sides_and_posts_announcement(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode",
        json={
            "mode": "debate",
            "topic": "cats vs dogs",
            "sides": {"agent-a": "for", "agent-b": "against"},
        },
        headers=owner_headers,
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["mode"] == "debate"
    assert data["topic"] == "cats vs dogs"
    assert data["sides"] == {"agent-a": "for", "agent-b": "against"}
    assert "Mode switched to Debate." in data["announcement"]
    assert "Topic: cats vs dogs." in data["announcement"]
    assert "agent-a (For): You argue FOR the proposition: cats vs dogs." in data["announcement"]
    assert "agent-b (Against): You argue AGAINST the proposition: cats vs dogs." in data["announcement"]

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.status_code == 200
    detail_data = detail.json()
    assert detail_data["status"] == "open"  # a switch never itself closes the room
    assert detail_data["mode"] == "debate"
    assert detail_data["sides"] == {"agent-a": "for", "agent-b": "against"}

    latest = await _get_latest_message(client, owner_headers, room["id"])
    assert latest["kind"] == "system"
    assert latest["sender"] == "system"
    assert latest["text"] == data["announcement"]


async def test_switch_mode_debate_to_critique_reassigns_sides(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(
        client,
        owner_headers,
        members=["agent-a", "agent-b"],
        mode="debate",
        topic="cats vs dogs",
        sides={"agent-a": "for", "agent-b": "against"},
    )

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode",
        json={
            "mode": "critique",
            "topic": "the new schema",
            "sides": {"agent-a": "critic", "agent-b": "proposer"},
        },
        headers=owner_headers,
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["mode"] == "critique"
    assert data["sides"] == {"agent-a": "critic", "agent-b": "proposer"}
    assert "agent-a (Critic):" in data["announcement"]
    assert "agent-b (Proposer):" in data["announcement"]


async def test_switch_mode_to_symmetric_non_freeform_posts_shared_stance(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(
        client,
        owner_headers,
        members=["agent-a", "agent-b"],
        mode="debate",
        topic="cats vs dogs",
        sides={"agent-a": "for", "agent-b": "against"},
    )

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode",
        json={"mode": "collaborate", "topic": "ship the v2 API"},
        headers=owner_headers,
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["mode"] == "collaborate"
    assert data["sides"] == {"agent-a": None, "agent-b": None}  # sides cleared
    assert "Both agents:" in data["announcement"]
    assert "Collaborate with the other agent to ship the v2 API." in data["announcement"]


async def test_switch_mode_to_freeform_clears_topic_and_sides(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(
        client,
        owner_headers,
        members=["agent-a", "agent-b"],
        mode="debate",
        topic="cats vs dogs",
        sides={"agent-a": "for", "agent-b": "against"},
    )

    resp = await client.post(f"/v1/rooms/{room['id']}/mode", json={"mode": "freeform"}, headers=owner_headers)
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["mode"] == "freeform"
    assert data["topic"] is None
    assert data["sides"] == {"agent-a": None, "agent-b": None}
    assert data["announcement"] == "Mode switched to Freeform."


async def test_switch_mode_non_freeform_without_topic_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode",
        json={"mode": "debate", "sides": {"agent-a": "for", "agent-b": "against"}},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "missing_room_topic"

    # Untouched by the rejected switch.
    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["mode"] == "freeform"


async def test_switch_mode_asymmetric_without_proper_sides_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode",
        json={"mode": "debate", "topic": "cats vs dogs"},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_sides"

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["mode"] == "freeform"


async def test_switch_mode_rejects_bad_mode(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode", json={"mode": "nonsense"}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_mode"


async def test_switch_mode_closed_room_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode",
        json={"mode": "debate", "topic": "cats vs dogs", "sides": {"agent-a": "for", "agent-b": "against"}},
        headers=owner_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "room_closed"


async def test_switch_mode_unknown_room_404(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms/not-a-real-room/mode", json={"mode": "freeform"}, headers=owner_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "room_not_found"


async def test_switch_mode_requires_owner_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/v1/rooms/{room['id']}/mode", json={"mode": "freeform"}, headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["mode"] == "freeform"  # unaffected by the rejected attempt


async def test_switch_mode_requires_auth(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(f"/v1/rooms/{room['id']}/mode", json={"mode": "freeform"})
    assert resp.status_code == 401


async def test_switch_mode_announcement_counts_toward_cap_but_never_trips_it(client, db_session):
    """ADR-0009 decision 3: the announcement counts as a message toward the
    cap, but the switch itself must never trip the cap-auto-close -- same
    posture as post_closing_nudge's own sweeper message.
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], max_messages=1)

    resp = await client.post(
        f"/v1/rooms/{room['id']}/mode",
        json={"mode": "debate", "topic": "cats vs dogs", "sides": {"agent-a": "for", "agent-b": "against"}},
        headers=owner_headers,
    )
    assert resp.status_code == 200, resp.json()

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    data = detail.json()
    assert data["message_count"] == 1  # reached the cap...
    assert data["status"] == "open"  # ...but the room was NOT auto-closed
    assert data["close_reason"] is None


# --- switch_room_mode row-lock race safety (same forced-interleaving
# technique as delete_room's own race tests above) ---


async def test_switch_mode_race_switch_wins_against_post_message(client, db_session):
    """Interleaving A: switch_room_mode acquires the lock first and holds it
    (via the delayed commit) while a concurrent post_message tries to post
    -- the poster must genuinely block, then see the switch's already-
    updated mode/sides once it proceeds. Never a 500/IntegrityError, never a
    lost update.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_switch():
            async with winner_session:
                return await switch_room_mode_op(
                    winner_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                )

        async def run_post():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await post_message_op(loser_session, room_id, "agent-a", "hello", "message", principal=_RACE_MACHINE_PRINCIPAL)

        start = time.monotonic()
        switch_result, post_result = await asyncio.gather(run_switch(), run_post())
        elapsed = time.monotonic() - start

        # Genuine blocking, not decoupled timing luck.
        assert elapsed >= COMMIT_DELAY

        room_after_switch, announcement = switch_result
        assert room_after_switch.mode == "debate"
        message, room_after_post = post_result
        assert room_after_post.status == "open"
        # The post landed strictly after the switch's announcement (seq 1),
        # so it must be seq 2 -- proving the lock serialized the two writes
        # rather than letting them interleave.
        assert message.seq == 2

        async with AsyncSessionLocal() as check_session:
            final_room = await check_session.get(Room, room_id)
            assert final_room.mode == "debate"
            assert final_room.message_count == 2


async def test_switch_mode_race_post_message_wins_against_switch(client, db_session):
    """Interleaving B: post_message acquires the lock first and holds it
    while switch_room_mode tries to switch -- switch must genuinely block,
    then proceed once the post has committed, seeing the up-to-date
    message_count/seq (no lost update).
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_post():
            async with winner_session:
                return await post_message_op(winner_session, room_id, "agent-a", "hello", "message", principal=_RACE_MACHINE_PRINCIPAL)

        async def run_switch():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await switch_room_mode_op(
                    loser_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                )

        start = time.monotonic()
        post_result, switch_result = await asyncio.gather(run_post(), run_switch())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        message, room_after_post = post_result
        assert message.seq == 1
        room_after_switch, announcement = switch_result
        assert room_after_switch.mode == "debate"

        async with AsyncSessionLocal() as check_session:
            final_room = await check_session.get(Room, room_id)
            assert final_room.mode == "debate"
            assert final_room.message_count == 2  # the post + the switch's announcement, no lost update


async def test_switch_mode_race_switch_wins_against_close(client, db_session):
    """Interleaving A against close_room: switch_room_mode holds the lock
    first, so a concurrent owner close must genuinely block, then close the
    room in its now-switched state.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_switch():
            async with winner_session:
                return await switch_room_mode_op(
                    winner_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                )

        async def run_close():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await close_room_op(loser_session, room_id, "owner")

        start = time.monotonic()
        switch_result, close_result = await asyncio.gather(run_switch(), run_close())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        room_after_switch, _announcement = switch_result
        assert room_after_switch.mode == "debate"
        assert close_result.status == "closed"
        assert close_result.close_reason == "owner"


async def test_switch_mode_race_close_wins_against_switch(client, db_session):
    """Interleaving B against close_room: an owner close holds the lock
    first -- the concurrent switch must genuinely block, then see the
    room already closed and reject cleanly (409 room_closed), never a
    500 and never a mode change applied to a closed room.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_close():
            async with winner_session:
                return await close_room_op(winner_session, room_id, "owner")

        async def run_switch():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                try:
                    return await switch_room_mode_op(
                        loser_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                    )
                except ApiError as exc:
                    return exc

        start = time.monotonic()
        close_result, switch_result = await asyncio.gather(run_close(), run_switch())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        assert close_result.status == "closed"
        assert isinstance(switch_result, ApiError)
        assert switch_result.code == "room_closed"

        async with AsyncSessionLocal() as check_session:
            final_room = await check_session.get(Room, room_id)
            assert final_room.mode == "freeform"  # the rejected switch never applied


async def test_switch_mode_race_switch_wins_against_post_closing_nudge(client, db_session):
    """Interleaving A against post_closing_nudge: switch_room_mode holds the
    lock first (posting its announcement, seq 1) -- the sweeper's nudge must
    genuinely block, then post its own message (seq 2) once the switch has
    committed. Neither writer's `message_count` increment is lost.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_switch():
            async with winner_session:
                return await switch_room_mode_op(
                    winner_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                )

        async def run_nudge():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await post_closing_nudge_op(loser_session, room_id, "closing soon")

        start = time.monotonic()
        switch_result, nudge_result = await asyncio.gather(run_switch(), run_nudge())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        room_after_switch, _announcement = switch_result
        assert room_after_switch.mode == "debate"
        assert nudge_result is not None  # room was still open when the nudge's lock was acquired
        assert nudge_result.seq == 2  # strictly after the switch's own announcement (seq 1)

        async with AsyncSessionLocal() as check_session:
            final_room = await check_session.get(Room, room_id)
            assert final_room.mode == "debate"
            assert final_room.message_count == 2  # both writers' increments landed, no lost update
            assert final_room.closing_warned_at is not None


async def test_switch_mode_race_post_closing_nudge_wins_against_switch(client, db_session):
    """Interleaving B against post_closing_nudge: the sweeper's nudge holds
    the lock first (posting its own message, seq 1) -- switch_room_mode
    must genuinely block, then proceed once the nudge has committed, seeing
    the up-to-date message_count/seq for its own announcement (seq 2). No
    lost update either way.
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

        async def run_switch():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await switch_room_mode_op(
                    loser_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                )

        start = time.monotonic()
        nudge_result, switch_result = await asyncio.gather(run_nudge(), run_switch())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        assert nudge_result is not None
        assert nudge_result.seq == 1
        room_after_switch, _announcement = switch_result
        assert room_after_switch.mode == "debate"

        async with AsyncSessionLocal() as check_session:
            final_room = await check_session.get(Room, room_id)
            assert final_room.mode == "debate"
            assert final_room.message_count == 2  # the nudge + the switch's announcement, no lost update


async def test_switch_mode_race_switch_wins_against_delete_room(client, db_session):
    """Interleaving A against delete_room: switch_room_mode holds the lock
    first (posting its announcement, seq 1) -- the concurrent delete must
    genuinely block, then delete the room's now fully-switched state
    INCLUDING the announcement message, with no orphaned rows and no FK
    violation.
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
        room_id = room["id"]

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_switch():
            async with winner_session:
                return await switch_room_mode_op(
                    winner_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                )

        async def run_delete():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await delete_room_op(loser_session, room_id)

        start = time.monotonic()
        switch_result, delete_result = await asyncio.gather(run_switch(), run_delete())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        room_after_switch, _announcement = switch_result
        assert room_after_switch.mode == "debate"

        messages_deleted, members_deleted = delete_result
        assert messages_deleted == 1  # the switch's own announcement, cleanly swept up -- no FK violation
        assert members_deleted == 2

        assert await db_session.get(Room, room_id) is None
        remaining = (
            await db_session.execute(
                RoomMessage.__table__.select().where(RoomMessage.__table__.c.room_id == room_id)
            )
        ).all()
        assert remaining == []


async def test_switch_mode_race_delete_room_wins_against_switch(client, db_session):
    """Interleaving B against delete_room: delete_room holds the lock first
    and fully removes the room -- the concurrent switch must genuinely
    block, then see the room gone and report a clean 404 room_not_found
    (never a spurious invalid_room_sides -- the room-existence check inside
    switch_room_mode must win over the sides-validation check that would
    otherwise fire against an empty, post-delete membership list -- and
    never a 500).
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

        async def run_switch():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                try:
                    return await switch_room_mode_op(
                        loser_session, room_id, "debate", "cats vs dogs", {"agent-a": "for", "agent-b": "against"}
                    )
                except ApiError as exc:
                    return exc

        start = time.monotonic()
        delete_result, switch_result = await asyncio.gather(run_delete(), run_switch())
        elapsed = time.monotonic() - start

        assert elapsed >= COMMIT_DELAY

        messages_deleted, members_deleted = delete_result
        assert messages_deleted == 0
        assert members_deleted == 2

        assert isinstance(switch_result, ApiError)
        assert switch_result.code == "room_not_found"
        assert switch_result.status_code == 404

        assert await db_session.get(Room, room_id) is None
