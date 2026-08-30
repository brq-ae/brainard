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
import app.rooms as rooms_module
from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.attachments import blob_path
from app.auth import Principal
from app.config import get_settings
from app.models import AttachmentBlob, Machine, OwnerToken, Room, RoomAttachment, RoomMember, RoomMessage
from app.rooms import _maybe_ping_owner_room_not_opened as maybe_ping_owner_room_not_opened_op
from app.rooms import close_room as close_room_op
from app.rooms import delete_message as delete_message_op
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


async def _open_room(client, owner_headers, room_id: str, *, text="starting now") -> dict:
    """ADR-0014: rooms default to `requires_owner_open=True`, so most tests
    that want an agent to be able to post at all need the owner to post
    first. This is the one, single owner message that opens the room (seq
    1) -- callers that care about exact seq numbers for their OWN posts
    account for this one extra message.
    """
    resp = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "owner", "text": text}, headers=owner_headers
    )
    assert resp.status_code == 200, resp.json()
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


async def test_create_room_duration_is_deferred_until_the_room_opens(client, db_session):
    """ADR-0014 decision 7: a relative `duration_seconds` is NOT resolved
    into `expires_at` at create time (every new room defaults to
    `requires_owner_open=True`, and the timer starts on open, not
    creation) -- `expires_at` stays null until the owner's first message.
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], duration_seconds=1800)
    assert room["expires_at"] is None

    before = datetime.now(UTC)
    await _open_room(client, owner_headers, room["id"])
    after = datetime.now(UTC)

    detail = (await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)).json()
    assert detail["expires_at"] is not None
    expires_at = datetime.fromisoformat(detail["expires_at"])
    assert before + timedelta(seconds=1800) <= expires_at <= after + timedelta(seconds=1800)
    assert detail["opened_at"] is not None


async def test_create_room_explicit_expires_at_is_not_deferred(client, db_session):
    """Unlike `duration_seconds`, an explicit `expires_at` is a wall-clock
    moment the owner asked for directly -- stored and enforced exactly as
    given, even though the room may still be waiting for the owner when it
    arrives (ADR-0014 decision 7).
    """
    owner_headers = await _owner_headers(db_session)
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], expires_at=future)
    assert room["expires_at"] is not None
    assert datetime.fromisoformat(room["expires_at"]) == datetime.fromisoformat(future)


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
    the deadline is anywhere close. max_messages=2 (not 1): the owner's own
    opening message (ADR-0014) already counts toward the cap, so the cap is
    sized to still leave room for one agent post to be the one that trips it.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(
        client, owner_headers, members=["agent-a", "agent-b"], max_messages=2, duration_seconds=30 * 24 * 3600
    )
    await _open_room(client, owner_headers, room["id"])

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

    # The owner's own opening message doubles as the message under test here
    # (ADR-0014: a room defaults to requiring one before agents may post).
    await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "owner", "text": "hello"}, headers=owner_headers
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
    await _open_room(client, owner_headers, room["id"])  # ADR-0014: agents wait for this

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi there"}, headers=machine_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["seq"] == 2  # 1 is the owner's opening message
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
    await _open_room(client, owner_headers, room["id"])  # else the owner-open gate fires first, not membership

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "stranger", "text": "hi"}, headers=machine_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sender_not_room_member"


async def test_post_message_non_member_rejected_by_gate_before_room_opens(client, db_session):
    """ADR-0014 decision 1: the gate runs BEFORE the membership check --
    a non-member gets the same "wait for the owner" directive a real member
    would on a room that isn't open yet, not a misleading
    "sender_not_room_member" that implies posting as the right name would
    have worked right now.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "stranger", "text": "hi"}, headers=machine_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "room_not_opened"


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
    await _open_room(client, owner_headers, room["id"])  # consumes seq 1

    seqs = []
    for i in range(6):
        sender = "agent-a" if i % 2 == 0 else "agent-b"
        resp = await client.post(
            f"/v1/rooms/{room['id']}/messages", json={"sender": sender, "text": f"msg {i}"}, headers=machine_headers
        )
        assert resp.status_code == 200
        seqs.append(resp.json()["seq"])

    assert seqs == [2, 3, 4, 5, 6, 7]


# --- guardrails ---


async def test_done_signal_closes_room(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await _open_room(client, owner_headers, room["id"])

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
    # max_messages=3: the owner's own opening message (ADR-0014) already
    # counts toward the cap, leaving 2 more slots for the agent-a/agent-b
    # exchange this test is actually about.
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], max_messages=3)
    await _open_room(client, owner_headers, room["id"])

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
    # max_messages=2: the owner's own opening message (ADR-0014) counts
    # toward the cap, leaving exactly one slot for the agent post that's
    # meant to be the one that trips it.
    room = await _create_room(client, owner_headers, name="capped-room", members=["agent-a", "agent-b"], max_messages=2)
    await _open_room(client, owner_headers, room["id"])

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
    opened = await _open_room(client, owner_headers, room["id"])
    await client.post(f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers)

    start = time.monotonic()
    resp = await client.get(
        f"/v1/rooms/{room['id']}/messages", params={"since": opened["seq"], "wait": 10}, headers=machine_headers
    )
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["room_status"] == "open"
    assert data["open_gate_notice"] is None
    assert elapsed < 2  # must not have waited out any part of the 10s budget


async def test_long_poll_returns_nothing_new_when_since_is_current(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await _open_room(client, owner_headers, room["id"])
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
    opened = await _open_room(client, owner_headers, room["id"])

    start = time.monotonic()
    resp = await client.get(
        f"/v1/rooms/{room['id']}/messages", params={"since": opened["seq"], "wait": 2}, headers=machine_headers
    )
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == []
    assert data["room_status"] == "open"
    assert elapsed >= 1.5  # actually waited out roughly the requested budget


async def test_long_poll_returns_immediately_before_the_room_opens(client, db_session):
    """ADR-0014 decision 3: unlike the "actually waits" case above, a room
    that still requires the owner to post returns on the very first
    iteration -- no sleep, no waiting out any part of `wait`.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    start = time.monotonic()
    resp = await client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 15}, headers=machine_headers)
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == []
    assert data["room_status"] == "open"
    assert data["open_gate_notice"] is not None
    assert "not posted" in data["open_gate_notice"]
    assert elapsed < 1  # must NOT have entered the sleep loop at all


async def test_long_poll_wait_is_capped_at_30(client, db_session):
    import app.rooms as rooms_module

    assert rooms_module.MAX_WAIT_SECS == 30


async def test_long_poll_returns_promptly_when_message_posted_during_wait(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    opened = await _open_room(client, owner_headers, room["id"])

    async def poster():
        await asyncio.sleep(1.2)
        resp = await client.post(
            f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "arrived"}, headers=machine_headers
        )
        assert resp.status_code == 200

    start = time.monotonic()
    poll_result, _ = await asyncio.gather(
        client.get(
            f"/v1/rooms/{room['id']}/messages", params={"since": opened["seq"], "wait": 15}, headers=machine_headers
        ),
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
    opened = await _open_room(client, owner_headers, room["id"])

    async def closer():
        await asyncio.sleep(1.2)
        resp = await client.post(f"/v1/rooms/{room['id']}/close", json={}, headers=owner_headers)
        assert resp.status_code == 200

    start = time.monotonic()
    poll_result, _ = await asyncio.gather(
        client.get(
            f"/v1/rooms/{room['id']}/messages", params={"since": opened["seq"], "wait": 15}, headers=machine_headers
        ),
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
    await _open_room(client, owner_headers, room["id"])

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
    await _open_room(client, owner_headers, room["id"])
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
    assert data["deleted_messages"] == 3  # the owner's opening message + the two agent replies
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
    await _open_room(client, owner_headers, room1["id"])
    await client.post(
        f"/v1/rooms/{room1['id']}/messages", json={"sender": "a1", "text": "still here"}, headers=machine_headers
    )

    resp = await client.delete(f"/v1/rooms/{room2['id']}", headers=owner_headers)
    assert resp.status_code == 200

    still_there = await client.get(f"/v1/rooms/{room1['id']}", headers=machine_headers)
    assert still_there.status_code == 200
    assert still_there.json()["message_count"] == 2  # the owner's opening message + "still here"


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
_RACE_OWNER_PRINCIPAL = Principal(kind="owner")


async def _open_room_direct(room_id: str) -> None:
    """ADR-0014: these lock-contention races are about post_message/
    delete_room/post_closing_nudge/switch_room_mode interleaving, not about
    the owner-open gate -- so every room used in a race below is opened
    first (a real owner post, on its own throwaway session/commit) so the
    later agent-sender post under test actually reaches the row lock
    instead of being rejected by the gate before it ever gets there.
    """
    async with AsyncSessionLocal() as session:
        await post_message_op(session, room_id, "owner", "starting now", "message", principal=_RACE_OWNER_PRINCIPAL)


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
        await _open_room_direct(room_id)

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
        assert messages_deleted == 1  # the owner's opening message from _open_room_direct
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
        await _open_room_direct(room_id)

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
        assert message.seq == 2  # 1 is the owner's opening message from _open_room_direct
        assert room_after_post.status == "closed"

        messages_deleted, members_deleted = delete_result
        assert messages_deleted == 2  # the owner's opening message + the raced-in 'done', no FK violation
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
        await _open_room_direct(room_id)  # seq 1; the room must be open before agent-a may post

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
        # seq 1 is the opening message, seq 2 the switch's announcement --
        # the post landed strictly after both, so it must be seq 3, proving
        # the lock serialized the writes rather than letting them interleave.
        assert message.seq == 3

        async with AsyncSessionLocal() as check_session:
            final_room = await check_session.get(Room, room_id)
            assert final_room.mode == "debate"
            assert final_room.message_count == 3


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
        await _open_room_direct(room_id)  # seq 1; the room must be open before agent-a may post

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
        assert message.seq == 2  # 1 is the opening message from _open_room_direct
        room_after_switch, announcement = switch_result
        assert room_after_switch.mode == "debate"

        async with AsyncSessionLocal() as check_session:
            final_room = await check_session.get(Room, room_id)
            assert final_room.mode == "debate"
            assert final_room.message_count == 3  # open + the post + the switch's announcement, no lost update


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


# --- ADR-0014: rooms wait for the owner's first message ---


async def test_owner_post_opens_the_room_and_sets_opened_at(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    detail_before = (await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)).json()
    assert detail_before["opened_at"] is None
    assert detail_before["requires_owner_open"] is True

    before = datetime.now(UTC)
    opened = await _open_room(client, owner_headers, room["id"])
    after = datetime.now(UTC)
    assert opened["seq"] == 1

    detail_after = (await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)).json()
    assert detail_after["opened_at"] is not None
    opened_at = datetime.fromisoformat(detail_after["opened_at"])
    assert before <= opened_at <= after


async def test_agent_post_rejected_before_open_carries_directive_message(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "let's begin"}, headers=machine_headers
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "room_not_opened"
    detail = body["error"]["detail"].lower()
    # A directive to stop, not a bare/retryable rejection (ADR-0014 decision 2).
    assert "has not posted" in detail
    assert "do not begin" in detail
    assert "not start on the topic" in detail
    assert "stop and wait" in detail
    assert "not a retryable error" in detail


async def test_agent_post_accepted_after_owner_opens(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await _open_room(client, owner_headers, room["id"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "let's begin"}, headers=machine_headers
    )
    assert resp.status_code == 200, resp.json()


async def test_opt_out_lets_agents_start_immediately(client, db_session):
    """ADR-0014 decision 4: turning `requires_owner_open` off before the
    owner ever posts lets agents begin right away -- without pretending the
    owner posted a message that never happened (`opened_at` stays null).
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    gate_resp = await client.post(
        f"/v1/rooms/{room['id']}/open-gate", json={"required": False}, headers=owner_headers
    )
    assert gate_resp.status_code == 200, gate_resp.json()
    gate_data = gate_resp.json()
    assert gate_data["requires_owner_open"] is False
    assert gate_data["opened_at"] is None
    assert "may begin from the topic" in gate_data["announcement"]

    post_resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "starting now"}, headers=machine_headers
    )
    assert post_resp.status_code == 200, post_resp.json()
    assert post_resp.json()["seq"] == 2  # seq 1 is the toggle's own system announcement

    detail = (await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)).json()
    assert detail["opened_at"] is None  # the owner still never posted
    assert detail["requires_owner_open"] is False


async def test_opt_out_requires_owner_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/open-gate", json={"required": False}, headers=machine_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_mid_room_toggle_both_directions_post_system_messages(client, db_session):
    """ADR-0014 decision 4, mirroring `set_agent_uploads_allowed`: flipping
    the switch mid-room (after the room already opened normally) posts a
    system announcement each time, in both directions, and never re-gates
    agents already mid-conversation once toggled back on.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await _open_room(client, owner_headers, room["id"])  # seq 1 -- the room is genuinely open already

    off_resp = await client.post(
        f"/v1/rooms/{room['id']}/open-gate", json={"required": False}, headers=owner_headers
    )
    assert off_resp.status_code == 200, off_resp.json()
    off_data = off_resp.json()
    assert off_data["requires_owner_open"] is False
    assert "turned off" in off_data["announcement"]

    on_resp = await client.post(
        f"/v1/rooms/{room['id']}/open-gate", json={"required": True}, headers=owner_headers
    )
    assert on_resp.status_code == 200, on_resp.json()
    on_data = on_resp.json()
    assert on_data["requires_owner_open"] is True
    assert "now requires the owner" in on_data["announcement"]

    detail = (await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)).json()
    system_messages = [m for m in detail["messages"] if m["kind"] == "system"]
    assert [m["text"] for m in system_messages] == [off_data["announcement"], on_data["announcement"]]

    # Toggling back ON doesn't re-gate a room that already genuinely opened.
    post_resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "still going"}, headers=machine_headers
    )
    assert post_resp.status_code == 200, post_resp.json()


async def test_toggle_off_on_a_never_opened_room_resolves_pending_duration(client, db_session):
    """ADR-0014 decisions 4/7: turning the requirement off on a room that
    never received an owner message is itself decision 4's "treated as
    opening it" case -- `opened_at` stays null (the owner never posted) but
    a deferred `pending_duration_seconds` resolves into `expires_at` at the
    toggle's own timestamp, not the owner's.
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(
        client, owner_headers, members=["agent-a", "agent-b"], duration_seconds=1800
    )
    assert room["expires_at"] is None

    before = datetime.now(UTC)
    gate_resp = await client.post(
        f"/v1/rooms/{room['id']}/open-gate", json={"required": False}, headers=owner_headers
    )
    after = datetime.now(UTC)
    assert gate_resp.status_code == 200, gate_resp.json()
    gate_data = gate_resp.json()
    assert gate_data["opened_at"] is None
    assert gate_data["expires_at"] is not None
    expires_at = datetime.fromisoformat(gate_data["expires_at"])
    assert before + timedelta(seconds=1800) <= expires_at <= after + timedelta(seconds=1800)


async def test_poll_returns_immediately_before_the_room_opens_and_does_not_block(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])

    start = time.monotonic()
    resp = await client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 20}, headers=machine_headers)
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == []
    assert data["room_status"] == "open"
    assert data["open_gate_notice"] is not None
    assert elapsed < 1  # never entered the sleep loop, regardless of the 20s `wait` requested


async def test_poll_on_deleted_room_returns_clear_stop_not_bare_404(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    await _open_room(client, owner_headers, room["id"])

    delete_resp = await client.delete(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert delete_resp.status_code == 200

    poll_resp = await client.get(
        f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 0}, headers=machine_headers
    )
    assert poll_resp.status_code == 404
    body = poll_resp.json()
    assert body["error"]["code"] == "room_not_found"
    detail = body["error"]["detail"].lower()
    assert "gone" in detail
    assert "stop polling" in detail


async def test_park_ping_fires_once_under_repeated_polling(client, db_session, monkeypatch):
    """ADR-0014 decision 8, and the ADR's rejected alternative "notify on
    every poll cycle": four straight polls of a still-unopened room must
    fire exactly ONE ntfy ping, not one per poll.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="parked-room", members=["agent-a", "agent-b"])

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    for _ in range(4):
        resp = await client.get(
            f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 0}, headers=machine_headers
        )
        assert resp.status_code == 200
        assert resp.json()["open_gate_notice"] is not None

    assert len(calls) == 1
    _, title, body = calls[0]
    assert title == "Brain room waiting: parked-room"
    # The poll path carries no sender -- honestly names the room, not an agent.
    assert "an agent" in body.lower()
    assert "parked-room" in body


async def test_park_ping_names_the_agent_when_triggered_by_a_rejected_post(client, db_session, monkeypatch):
    """The other trigger (decision 8): post_message's rejection knows the
    claimed sender and names it, unlike the poll trigger above.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="parked-room-named", members=["agent-a", "agent-b"])

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers
    )
    assert resp.status_code == 403

    assert len(calls) == 1
    _, _, body = calls[0]
    assert "agent-a" in body


async def test_park_ping_one_shot_across_both_poll_and_post_triggers(client, db_session, monkeypatch):
    """Decision 8's one-shot guard is per-room, not per-trigger-kind: once
    either trigger has fired, the other is a silent no-op.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="parked-room-both", members=["agent-a", "agent-b"])

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    poll_resp = await client.get(
        f"/v1/rooms/{room['id']}/messages", params={"since": 0, "wait": 0}, headers=machine_headers
    )
    assert poll_resp.status_code == 200

    post_resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_headers
    )
    assert post_resp.status_code == 403

    assert len(calls) == 1  # the poll's ping already fired; the post's own trigger is a silent no-op


async def test_park_ping_does_not_fire_for_a_non_member_sender(client, db_session, monkeypatch):
    """Independent review finding: before the fix, the ping fired BEFORE the
    membership check, so any bearer machine token could force a push by
    naming an arbitrary, non-member `sender` -- it never had to actually be
    one of this room's two members. `sender in await get_members(...)` now
    gates the ping (app/rooms.py's `post_message`); the 403 rejection itself
    is unchanged and still fires for a non-member exactly as it does for a
    real member (ADR-0014 decision 1's own documented reasoning: a
    non-member gets the same "wait for the owner" directive, not a
    confusing `sender_not_room_member`).
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="parked-room-nonmember", members=["agent-a", "agent-b"])

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages",
        json={"sender": "not-a-member-at-all", "text": "hi"},
        headers=machine_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "room_not_opened"

    assert calls == []  # no ping -- the claimed sender is not a real member of this room


async def test_park_ping_overlong_sender_is_rejected_before_it_ever_reaches_the_ping(client, db_session, monkeypatch):
    """Independent review finding: `sender` had no `max_length`
    (app/schemas.py's RoomPostMessageRequest). An arbitrarily long client-
    supplied string reaching the ping/notification path is now stopped at
    the API boundary -- pydantic's 422 fires before `post_message` (and
    therefore before any ping) ever runs.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="parked-room-overlong", members=["agent-a", "agent-b"])

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    overlong_sender = "agent-a" + "x" * 300  # well past the 255-char cap, still prefixed with a real member name
    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages",
        json={"sender": overlong_sender, "text": "hi"},
        headers=machine_headers,
    )
    assert resp.status_code == 422

    assert calls == []  # rejected before post_message (and the ping) ever ran


async def test_park_ping_sanitises_newlines_and_control_characters_in_the_notified_name(
    client, db_session, monkeypatch
):
    """Independent review finding: `agent_name` (the claimed `sender`) is
    interpolated unescaped into the ntfy title/body (app/notify.py). A
    member whose `agent_name` itself carries a newline/control character --
    `app/rooms.py`'s `_validate_members` only trims leading/trailing
    whitespace, it does not reject interior control characters -- must not
    be able to forge structure (e.g. inject a fake extra line, or break the
    ntfy `Title` HTTP header) in what the owner reads.
    `_sanitize_agent_name_for_notification` (app/notify.py) strips every
    Unicode Cc/Cf/Zl/Zp-category character and collapses whitespace before
    the name is ever interpolated.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    await _configure_notifications(client, owner_headers)
    hostile_name = "agent-a\r\nTitle: FAKE OWNER ALERT\r\n\ncall me at 555-0100 --agent-b"
    room = await _create_room(
        client, owner_headers, name="parked-room-hostile-name", members=[hostile_name, "agent-b"]
    )

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": hostile_name, "text": "hi"}, headers=machine_headers
    )
    assert resp.status_code == 403

    assert len(calls) == 1
    _, title, body = calls[0]
    # No raw control character/newline survives into either the header
    # value or the body -- the injected "\r\nTitle: ...\r\n\n" sequence is
    # deleted outright (same technique app/room_export.py's
    # `safe_filename_component` already uses for this same forbidden-
    # category set), collapsing what would have been a forged second HTTP
    # header / blank-line break into ordinary, contiguous, single-line text.
    for forbidden in ("\r", "\n"):
        assert forbidden not in title
        assert forbidden not in body
    assert "agent-aTitle: FAKE OWNER ALERTcall me at 555-0100 --agent-b" in body
    assert "parked-room-hostile-name" in body


async def test_park_ping_race_two_concurrent_triggers_send_exactly_one_ping(client, db_session, monkeypatch):
    """Fix 3 (independent review): `_maybe_ping_owner_room_not_opened`
    (app/rooms.py:488-535) looks atomic by inspection -- its own short-lived
    session, `SELECT ... FOR UPDATE` + `populate_existing`, a re-check of
    the full gate condition under the lock, then set-and-commit -- and the
    sequential tests above (`test_park_ping_fires_once_under_repeated_polling`,
    `test_park_ping_one_shot_across_both_poll_and_post_triggers`) only prove
    correctness when one trigger completes before the next starts. Neither
    proves the one-shot guard actually holds when two triggers are
    GENUINELY concurrent, i.e. both already inside the function, one
    blocked on the other's row lock.

    This forces that race directly at the function under test rather than
    through two HTTP requests (which can't be made to interleave on
    demand): same `_delayed_commit_session` + `asyncio.gather` +
    elapsed-time-assertion shape this file's other race tests already use
    (`test_delete_message_race_...`, `test_concurrent_owner_posts_...`), but
    since this function opens its OWN `AsyncSessionLocal()` internally
    (it takes no `db` parameter -- see its own docstring's "CRITICAL"-style
    reasoning for `poll_messages`' equivalent, and this function's "own
    short-lived session" note), the delayed session is injected by
    monkeypatching `app.rooms.AsyncSessionLocal` itself to hand out two
    specific, pre-built sessions (one delayed-commit, one plain) in a fixed
    order -- the first call gets the delayed one, the second gets the plain
    one, mirroring exactly which call is the lock's first holder.
    """
    owner_headers = await _owner_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="parked-room-race", members=["agent-a", "agent-b"])
    room_row = await db_session.get(Room, room["id"])

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    winner_session = _delayed_commit_session(COMMIT_DELAY)
    loser_session = AsyncSessionLocal()
    sessions_in_call_order = [winner_session, loser_session]

    def fake_session_local():
        return sessions_in_call_order.pop(0)

    monkeypatch.setattr(rooms_module, "AsyncSessionLocal", fake_session_local)

    async def run_first():
        # No head start: reaches `AsyncSessionLocal()` (and so acquires the
        # row lock) first, then holds it for COMMIT_DELAY via the delayed
        # commit -- same "winner goes first, unstarted" shape the other
        # race tests use.
        await maybe_ping_owner_room_not_opened_op(room_row, agent_name="agent-a")

    async def run_second():
        await asyncio.sleep(HEAD_START)
        await maybe_ping_owner_room_not_opened_op(room_row, agent_name="agent-b")

    start = time.monotonic()
    await asyncio.gather(run_first(), run_second())
    elapsed = time.monotonic() - start

    # Genuine blocking, not decoupled timing luck: the second call could not
    # have finished before the first released its row lock.
    assert elapsed >= COMMIT_DELAY

    # Exactly one ping, from the trigger that actually won the row lock --
    # not one per trigger, regardless of how many arrive concurrently.
    assert len(calls) == 1
    _, _, body = calls[0]
    assert "agent-a" in body
    assert "agent-b" not in body

    async with AsyncSessionLocal() as check_session:
        final_room = await check_session.get(Room, room["id"])
        assert final_room.owner_open_reminder_sent_at is not None


async def test_concurrent_owner_posts_cannot_double_open_or_corrupt_state(client, db_session):
    """ADR-0014 decisions 1/7 concurrency: two racing owner posts both land
    as real messages (the owner may legitimately post twice), but only the
    FIRST to acquire the room's row lock may open the room --
    `opened_at`/`expires_at` must reflect that one winner only, never a
    double-open or a second, later resolution of `pending_duration_seconds`
    that would silently push the deadline out further.
    """
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], duration_seconds=1800)
    room_id = room["id"]
    assert room["expires_at"] is None

    winner_session = _delayed_commit_session(COMMIT_DELAY)
    loser_session = AsyncSessionLocal()

    async def run_first():
        async with winner_session:
            return await post_message_op(
                winner_session, room_id, "owner", "first", "message", principal=_RACE_OWNER_PRINCIPAL
            )

    async def run_second():
        await asyncio.sleep(HEAD_START)
        async with loser_session:
            return await post_message_op(
                loser_session, room_id, "owner", "second", "message", principal=_RACE_OWNER_PRINCIPAL
            )

    before = datetime.now(UTC)
    start = time.monotonic()
    first_result, second_result = await asyncio.gather(run_first(), run_second())
    elapsed = time.monotonic() - start
    after = datetime.now(UTC)

    # Genuine blocking, not decoupled timing luck: the loser could not have
    # finished before the winner released the lock.
    assert elapsed >= COMMIT_DELAY

    first_message, first_room_after = first_result
    second_message, second_room_after = second_result
    assert first_message.seq == 1
    assert second_message.seq == 2

    async with AsyncSessionLocal() as check_session:
        final_room = await check_session.get(Room, room_id)
        assert final_room.message_count == 2

        assert final_room.opened_at is not None
        final_opened_at = final_room.opened_at
        if final_opened_at.tzinfo is None:
            final_opened_at = final_opened_at.replace(tzinfo=UTC)
        assert before <= final_opened_at <= after

        # Resolved exactly once, from the WINNER's timestamp -- never
        # re-resolved (and never pushed later) by the second post.
        assert final_room.pending_duration_seconds is None
        assert final_room.expires_at is not None
        final_expires_at = final_room.expires_at
        if final_expires_at.tzinfo is None:
            final_expires_at = final_expires_at.replace(tzinfo=UTC)
        expected = final_opened_at + timedelta(seconds=1800)
        assert abs((final_expires_at - expected).total_seconds()) < 1

    # Both posts genuinely landed and both observed the room as open
    # throughout -- no corruption, no spurious close.
    assert first_room_after.status == "open"
    assert second_room_after.status == "open"


# --- ADR-0015: deleting individual messages ---


async def test_delete_message_overwrites_text_and_leaves_everything_else_intact(client, db_session):
    """The crux of the ADR: `text` is genuinely overwritten in the database
    row (not just hidden behind a flag), while id/seq/sender/kind/created_at
    survive unchanged.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    room_id = room["id"]
    await _open_room(client, owner_headers, room_id)

    secret = "sk-super-secret-token-should-not-persist-12345"
    posted = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": secret}, headers=machine_headers
    )
    assert posted.status_code == 200, posted.json()
    message_id = posted.json()["id"]
    original_seq = posted.json()["seq"]

    resp = await client.delete(f"/v1/rooms/{room_id}/messages/{message_id}", headers=owner_headers)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["id"] == message_id
    assert body["seq"] == original_seq
    assert body["deleted"] is True
    assert body["deleted_at"] is not None

    row = await db_session.get(RoomMessage, message_id)
    assert row.text == "[message deleted by owner]"
    assert row.deleted_at is not None
    assert row.seq == original_seq
    assert row.sender == "agent-a"
    assert row.kind == "message"
    assert row.room_id == room_id

    # No copy of the secret lingers anywhere in the row -- every string
    # column, not just `text`.
    for column in RoomMessage.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, str):
            assert secret not in value


async def test_delete_message_owner_only_machine_token_forbidden(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    room_id = room["id"]
    await _open_room(client, owner_headers, room_id)
    posted = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "keep me intact"}, headers=machine_headers
    )
    message_id = posted.json()["id"]

    resp = await client.delete(f"/v1/rooms/{room_id}/messages/{message_id}", headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"

    row = await db_session.get(RoomMessage, message_id)
    assert row.text == "keep me intact"
    assert row.deleted_at is None


async def test_delete_message_ui_route_machine_token_cannot_reach_it(client, db_session):
    """Confirms there is no machine-token path through the UI surface
    either -- app/ui_auth.py's require_ui_session never accepts a bearer
    token at all (cookie-only), so a machine token here just gets the same
    redirect-to-login every other UI route gives it.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    room_id = room["id"]
    await _open_room(client, owner_headers, room_id)
    posted = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "still here"}, headers=machine_headers
    )
    message_id = posted.json()["id"]

    resp = await client.post(
        f"/ui/rooms/{room_id}/messages/{message_id}/delete", headers=machine_headers, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    row = await db_session.get(RoomMessage, message_id)
    assert row.text == "still here"
    assert row.deleted_at is None


async def test_delete_message_unknown_message_is_clean_404(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers)

    resp = await client.delete(f"/v1/rooms/{room['id']}/messages/not-a-real-message-id", headers=owner_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "room_message_not_found"


async def test_delete_message_unknown_room_is_clean_404(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.delete("/v1/rooms/not-a-real-room/messages/not-a-real-message-id", headers=owner_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "room_not_found"


async def test_delete_message_idempotent_repeat_delete_does_not_double_decrement(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, max_messages=10)
    room_id = room["id"]
    await _open_room(client, owner_headers, room_id)  # message_count == 1
    posted = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "x"}, headers=machine_headers
    )
    message_id = posted.json()["id"]  # message_count == 2

    first = await client.delete(f"/v1/rooms/{room_id}/messages/{message_id}", headers=owner_headers)
    assert first.status_code == 200, first.json()
    assert first.json()["message_count"] == 1

    second = await client.delete(f"/v1/rooms/{room_id}/messages/{message_id}", headers=owner_headers)
    assert second.status_code == 200, second.json()
    assert second.json()["message_count"] == 1  # unchanged -- not decremented a second time
    assert second.json()["deleted_at"] == first.json()["deleted_at"]


async def test_delete_message_frees_cap_slot_and_lets_room_outlive_its_nominal_cap(client, db_session):
    """ADR-0015 decision 3's own worked example: delete one message before
    the cap trips, and the room accepts one more real message than
    `max_messages` would otherwise have allowed.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, max_messages=3)
    room_id = room["id"]
    await _open_room(client, owner_headers, room_id)  # seq 1, count 1, open (1 < 3)

    msg2 = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "will be deleted"}, headers=machine_headers
    )
    assert msg2.status_code == 200, msg2.json()
    assert msg2.json()["room_status"] == "open"  # count 2, 2 < 3
    message_id = msg2.json()["id"]

    del_resp = await client.delete(f"/v1/rooms/{room_id}/messages/{message_id}", headers=owner_headers)
    assert del_resp.status_code == 200, del_resp.json()
    assert del_resp.json()["message_count"] == 1

    detail = (await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)).json()
    assert detail["message_count"] == 1
    assert detail["status"] == "open"  # never closed/reopened by the delete

    msg3 = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "third, real"}, headers=machine_headers
    )
    assert msg3.status_code == 200, msg3.json()
    assert msg3.json()["room_status"] == "open"  # count 2, 2 < 3

    # A 4th real message -- more than max_messages=3 would ordinarily allow
    # -- still gets in, because the deleted one freed a slot; this is the
    # one that finally trips the cap.
    msg4 = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-b", "text": "fourth, trips the cap"}, headers=machine_headers
    )
    assert msg4.status_code == 200, msg4.json()
    assert msg4.json()["room_status"] == "closed"
    assert msg4.json()["close_reason"] == "cap"


async def test_delete_message_on_closed_room_works_but_never_reopens_it(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, max_messages=2)
    room_id = room["id"]
    await _open_room(client, owner_headers, room_id)  # count 1, open
    msg2 = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "closes the room"}, headers=machine_headers
    )
    assert msg2.json()["room_status"] == "closed"
    assert msg2.json()["close_reason"] == "cap"
    message_id = msg2.json()["id"]

    del_resp = await client.delete(f"/v1/rooms/{room_id}/messages/{message_id}", headers=owner_headers)
    assert del_resp.status_code == 200, del_resp.json()
    assert del_resp.json()["message_count"] == 1  # decremented even though the room is closed

    detail = (await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)).json()
    assert detail["status"] == "closed"  # NOT reopened
    assert detail["close_reason"] == "cap"
    assert detail["message_count"] == 1

    # Still closed to new posts -- deletion is housekeeping, not resurrection.
    post_after = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "too late"}, headers=machine_headers
    )
    assert post_after.status_code == 409
    assert post_after.json()["error"]["code"] == "room_closed"


async def test_delete_owner_opening_message_leaves_room_open_gate_intact(client, db_session):
    """ADR-0014's `opened_at` records a FACT (the owner posted once), not a
    live count -- deleting the very message that opened the room must not
    un-open it or re-trip the owner-open gate for agents.
    """
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"])
    room_id = room["id"]
    opened = await _open_room(client, owner_headers, room_id)
    message_id = opened["id"]

    detail_before = (await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)).json()
    assert detail_before["opened_at"] is not None
    opened_at_before = detail_before["opened_at"]
    assert detail_before["requires_owner_open"] is True

    del_resp = await client.delete(f"/v1/rooms/{room_id}/messages/{message_id}", headers=owner_headers)
    assert del_resp.status_code == 200, del_resp.json()

    detail_after = (await client.get(f"/v1/rooms/{room_id}", headers=owner_headers)).json()
    assert detail_after["opened_at"] == opened_at_before  # untouched -- never re-nulled
    assert detail_after["status"] == "open"
    assert detail_after["requires_owner_open"] is True

    # The gate is still satisfied -- an agent can still post.
    post_resp = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "agent-a", "text": "still allowed"}, headers=machine_headers
    )
    assert post_resp.status_code == 200, post_resp.json()


async def test_delete_message_race_concurrent_deletes_of_same_message_never_double_decrement(client, db_session):
    """Same forced-interleaving technique as this file's other row-lock
    race tests (_delayed_commit_session + asyncio.gather): two genuinely
    concurrent deletes of the SAME message must serialize on the room's row
    lock, so exactly one decrement happens, never two -- and the counter
    never goes negative (ADR-0015 decision 8).
    """
    owner_headers = await _owner_headers(db_session)

    for _ in range(RACE_TRIALS):
        room = await _create_room(client, owner_headers, members=["agent-a", "agent-b"], max_messages=10)
        room_id = room["id"]
        await _open_room_direct(room_id)  # count 1

        async with AsyncSessionLocal() as setup_session:
            target_message, _room = await post_message_op(
                setup_session, room_id, "agent-a", "delete me twice", "message", principal=_RACE_MACHINE_PRINCIPAL
            )
            message_id = target_message.id  # count 2

        winner_session = _delayed_commit_session(COMMIT_DELAY)
        loser_session = AsyncSessionLocal()

        async def run_delete_a():
            async with winner_session:
                return await delete_message_op(winner_session, room_id, message_id)

        async def run_delete_b():
            await asyncio.sleep(HEAD_START)
            async with loser_session:
                return await delete_message_op(loser_session, room_id, message_id)

        start = time.monotonic()
        result_a, result_b = await asyncio.gather(run_delete_a(), run_delete_b())
        elapsed = time.monotonic() - start

        # Genuine blocking, not decoupled timing luck.
        assert elapsed >= COMMIT_DELAY

        message_a, _room_a = result_a
        message_b, _room_b = result_b
        assert message_a.deleted_at is not None
        # The loser observed the winner's already-committed tombstone
        # (re-read fresh under the lock), not a stale pre-lock snapshot.
        assert message_b.deleted_at == message_a.deleted_at

        async with AsyncSessionLocal() as check_session:
            room_row = await check_session.get(Room, room_id)
            # Exactly one decrement from the two starting posts (count 2),
            # never two -- and never negative.
            assert room_row.message_count == 1
            assert room_row.message_count >= 0

            message_row = await check_session.get(RoomMessage, message_id)
            assert message_row.text == "[message deleted by owner]"
