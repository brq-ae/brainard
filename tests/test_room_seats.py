"""Room seat assignment/binding (ADR-0018): optional owner assignment at
room-creation time, claim-on-first-write for an unassigned seat, enforcement
on post_message/add_room_attachment under the room's row lock, the owner-only
release/reassign action, the `project` field, and the concurrency guarantee
that two machines racing to claim the same open seat cannot both succeed.
"""

import asyncio
import time

from sqlalchemy import select
from ulid import ULID

from app.auth import Principal
from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.models import Machine, OwnerToken, Room, RoomMember
from app.rooms import post_message as post_message_op
from app.rooms import set_room_member_seat as set_room_member_seat_op
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _register_machine(db_session, name: str = "test-machine", status: str = "active") -> tuple[str, dict]:
    """Returns (machine_id, headers)."""
    token = generate_machine_token()
    machine_id = str(ULID())
    db_session.add(Machine(id=machine_id, name=name, token_hash=hash_token(token), status=status))
    await db_session.commit()
    return machine_id, {"Authorization": f"Bearer {token}"}


async def _create_room(
    client, owner_headers, *, name="room-1", members=None, seats=None, project=None, expect_status=201
) -> dict:
    body: dict = {"name": name, "members": members if members is not None else ["agent-a", "agent-b"]}
    if seats is not None:
        body["seats"] = seats
    if project is not None:
        body["project"] = project
    resp = await client.post("/v1/rooms", json=body, headers=owner_headers)
    assert resp.status_code == expect_status, resp.json()
    return resp.json()


async def _open_room(client, owner_headers, room_id: str, *, text="starting now") -> dict:
    resp = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": "owner", "text": text}, headers=owner_headers
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


async def _post(client, headers, room_id, sender, text="hi", *, expect_status=200):
    resp = await client.post(
        f"/v1/rooms/{room_id}/messages", json={"sender": sender, "text": text}, headers=headers
    )
    assert resp.status_code == expect_status, resp.json()
    return resp.json() if expect_status < 400 else resp.json()


def _pdf_bytes(body: bytes = b"hello") -> bytes:
    return b"%PDF-1.4\n" + body + b"\n%%EOF"


async def _upload(client, headers, room_id, *, filename="doc.pdf", sender="agent-a", expect_status=201):
    resp = await client.post(
        f"/v1/rooms/{room_id}/attachments",
        params={"filename": filename, "sender": sender},
        content=_pdf_bytes(),
        headers=headers,
    )
    assert resp.status_code == expect_status, resp.json()
    return resp.json()


# --- assignment at create time ---


async def test_create_room_with_seat_assignment_sets_bound_machine_id(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    assert room["seats"]["agent-a"] == machine_a_id
    assert room["seats"]["agent-b"] is None


async def test_create_room_without_seats_leaves_both_open(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers)
    assert room["seats"] == {"agent-a": None, "agent-b": None}


async def test_create_room_assigning_both_seats_to_distinct_machines(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    machine_b_id, _ = await _register_machine(db_session, "machine-b")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id, "agent-b": machine_b_id})
    assert room["seats"] == {"agent-a": machine_a_id, "agent-b": machine_b_id}


async def test_create_room_seat_assignment_to_unknown_machine_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, seats={"agent-a": "01NOSUCHMACHINE"}, expect_status=422)
    assert room["error"]["code"] == "unknown_seat_machine"


async def test_create_room_seat_assignment_to_revoked_machine_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_id, _ = await _register_machine(db_session, "revoked-machine", status="revoked")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_id}, expect_status=422)
    assert room["error"]["code"] == "seat_machine_not_active"


async def test_create_room_seat_assignment_for_unknown_agent_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_id, _ = await _register_machine(db_session)
    room = await _create_room(client, owner_headers, seats={"nonexistent-agent": machine_id}, expect_status=422)
    assert room["error"]["code"] == "invalid_room_seats"


# --- enforcement: assigned seat (post_message) ---


async def test_assigned_seat_accepts_its_own_machine(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, machine_a_headers = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_a_headers
    )
    assert resp.status_code == 200, resp.json()


async def test_assigned_seat_refuses_a_different_machine_on_its_very_first_message(client, db_session):
    """ADR-0018 decision 3/9: the whole point of assignment -- the wrong
    machine is refused OUTRIGHT, not given a one-shot claim.
    """
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    _, wrong_machine_headers = await _register_machine(db_session, "wrong-machine")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=wrong_machine_headers
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "seat_bound_to_other_machine"
    assert "agent-a" in body["error"]["detail"]
    assert "not your room" in body["error"]["detail"]
    assert "tell your operator" in body["error"]["detail"]


async def test_assigned_seat_directive_wording_verbatim(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    _, wrong_machine_headers = await _register_machine(db_session, "wrong-machine")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=wrong_machine_headers
    )
    assert resp.json()["error"]["detail"] == (
        "The seat 'agent-a' in room 'room-1' is bound to a different machine token than the one you "
        "authenticated with. Stop: do not post, do not attach, and do not retry -- this is not a transient "
        "error. This is not your room. If you believe this is wrong (a restarted agent, a re-minted machine "
        "token, or you were handed the wrong seat), do not act on it yourself: tell your operator. Only the "
        "room's owner can release or reassign this seat."
    )


# --- enforcement: unassigned seat claims on first write, then refuses others ---


async def test_unassigned_seat_binds_on_first_write(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, machine_a_headers = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers)
    await _open_room(client, owner_headers, room["id"])

    await _post(client, machine_a_headers, room["id"], "agent-a")

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["seats"]["agent-a"] == machine_a_id


async def test_unassigned_seat_refuses_a_different_machine_after_first_claim(client, db_session):
    owner_headers = await _owner_headers(db_session)
    _, machine_a_headers = await _register_machine(db_session, "machine-a")
    _, machine_b_headers = await _register_machine(db_session, "machine-b")
    room = await _create_room(client, owner_headers)
    await _open_room(client, owner_headers, room["id"])

    await _post(client, machine_a_headers, room["id"], "agent-a")  # claims it

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "impostor"}, headers=machine_b_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "seat_bound_to_other_machine"


async def test_unassigned_seat_same_machine_can_post_repeatedly(client, db_session):
    owner_headers = await _owner_headers(db_session)
    _, machine_a_headers = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers)
    await _open_room(client, owner_headers, room["id"])

    await _post(client, machine_a_headers, room["id"], "agent-a", "first")
    await _post(client, machine_a_headers, room["id"], "agent-a", "second")  # same machine, no refusal


# --- owner exemption ---


async def test_owner_post_never_subject_to_seat_binding(client, db_session):
    """ADR-0018 decision 6: keyed on principal.kind, not the `sender`
    string -- the owner posting as a member's agent_name (a legitimate
    testing path this codebase already allows, app/rooms.py's post_message
    docstring) never binds or is refused by a seat.
    """
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "owner testing as agent-a"}, headers=owner_headers
    )
    assert resp.status_code == 200, resp.json()

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    # The owner's post did NOT touch the assignment.
    assert detail.json()["seats"]["agent-a"] == machine_a_id


async def test_owner_sender_literal_still_never_subject_to_binding(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers)
    await _open_room(client, owner_headers, room["id"], text="msg 2")
    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["seats"] == {"agent-a": None, "agent-b": None}


# --- reads/polling stay fully open ---


async def test_poll_unaffected_by_seat_binding_for_any_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, machine_a_headers = await _register_machine(db_session, "machine-a")
    _, other_machine_headers = await _register_machine(db_session, "some-other-machine")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    # A machine token with NO relationship to this seat at all can still
    # poll/read the room -- ADR-0008's observer pattern, unaffected.
    resp = await client.get(f"/v1/rooms/{room['id']}/messages", params={"since": 0}, headers=other_machine_headers)
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 1  # the owner's opening message

    resp2 = await client.get(f"/v1/rooms/{room['id']}", headers=other_machine_headers)
    assert resp2.status_code == 200


async def test_list_rooms_unaffected_by_seat_binding(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    _, other_machine_headers = await _register_machine(db_session, "some-other-machine")
    await _create_room(client, owner_headers, name="assigned-room", seats={"agent-a": machine_a_id})

    resp = await client.get("/v1/rooms", headers=other_machine_headers)
    assert resp.status_code == 200


# --- release/reassign ---


async def test_release_seat_via_api_clears_binding(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})

    resp = await client.post(
        f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": None}, headers=owner_headers
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["bound_machine_id"] is None

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["seats"]["agent-a"] is None


async def test_release_then_different_machine_can_claim(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, machine_a_headers = await _register_machine(db_session, "machine-a")
    _, machine_b_headers = await _register_machine(db_session, "machine-b")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    # machine-b refused while assigned to machine-a.
    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_b_headers
    )
    assert resp.status_code == 403

    await client.post(f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": None}, headers=owner_headers)

    # Now open -- machine-b can claim it.
    resp2 = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi again"}, headers=machine_b_headers
    )
    assert resp2.status_code == 200, resp2.json()

    # And machine-a (the ORIGINAL assignee) is now the one refused.
    resp3 = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "wait, me?"}, headers=machine_a_headers
    )
    assert resp3.status_code == 403


async def test_reassign_seat_directly_to_new_machine(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, machine_a_headers = await _register_machine(db_session, "machine-a")
    machine_c_id, machine_c_headers = await _register_machine(db_session, "machine-c")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    resp = await client.post(
        f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": machine_c_id}, headers=owner_headers
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["bound_machine_id"] == machine_c_id

    # machine-a (the OLD assignee) is now refused.
    resp_a = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "still me?"}, headers=machine_a_headers
    )
    assert resp_a.status_code == 403

    # machine-c (the new assignee) can post.
    resp_c = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "hi"}, headers=machine_c_headers
    )
    assert resp_c.status_code == 200, resp_c.json()


async def test_reassign_to_unknown_machine_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers)
    resp = await client.post(
        f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": "01NOSUCH"}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unknown_seat_machine"


async def test_reassign_to_revoked_machine_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    revoked_id, _ = await _register_machine(db_session, "revoked-one", status="revoked")
    room = await _create_room(client, owner_headers)
    resp = await client.post(
        f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": revoked_id}, headers=owner_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "seat_machine_not_active"


async def test_seat_action_requires_owner_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    _, machine_headers = await _register_machine(db_session)
    room = await _create_room(client, owner_headers)
    resp = await client.post(
        f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": None}, headers=machine_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_seat_action_unknown_member_404s(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers)
    resp = await client.post(
        f"/v1/rooms/{room['id']}/members/no-such-agent/seat", json={"machine_id": None}, headers=owner_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "room_member_not_found"


async def test_seat_action_posts_system_announcement(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await client.post(f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": None}, headers=owner_headers)

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    system_messages = [m for m in detail.json()["messages"] if m["kind"] == "system"]
    assert any("released by the owner" in m["text"] and "agent-a" in m["text"] for m in system_messages)


async def test_seat_action_on_closed_room_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers)
    await client.post(f"/v1/rooms/{room['id']}/close", headers=owner_headers)
    resp = await client.post(
        f"/v1/rooms/{room['id']}/members/agent-a/seat", json={"machine_id": None}, headers=owner_headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "room_closed"


# --- attachments: same enforcement, under the same lock ---


async def test_assigned_seat_attachment_accepts_its_own_machine(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, machine_a_headers = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])
    await _upload(client, machine_a_headers, room["id"], sender="agent-a")


async def test_assigned_seat_attachment_refuses_a_different_machine(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    _, wrong_machine_headers = await _register_machine(db_session, "wrong-machine")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])

    body = await _upload(client, wrong_machine_headers, room["id"], sender="agent-a", expect_status=403)
    assert body["error"]["code"] == "seat_bound_to_other_machine"


async def test_unassigned_seat_attachment_binds_on_first_write_then_refuses_others(client, db_session):
    owner_headers = await _owner_headers(db_session)
    _, machine_a_headers = await _register_machine(db_session, "machine-a")
    _, machine_b_headers = await _register_machine(db_session, "machine-b")
    room = await _create_room(client, owner_headers)
    await _open_room(client, owner_headers, room["id"])

    await _upload(client, machine_a_headers, room["id"], sender="agent-a", filename="first.pdf")

    body = await _upload(
        client, machine_b_headers, room["id"], sender="agent-a", filename="second.pdf", expect_status=403
    )
    assert body["error"]["code"] == "seat_bound_to_other_machine"


async def test_assigned_seat_attach_from_brain_refuses_a_different_machine(client, db_session):
    """The third content-entry point found during implementation --
    'Attach from Brain' links an existing Brain document into a room under
    the same row lock as an upload, and is subject to the identical seat
    bind-or-check (ADR-0018 decision 5's addendum).
    """
    owner_headers = await _owner_headers(db_session)
    machine_a_id, machine_a_headers = await _register_machine(db_session, "machine-a")
    _, wrong_machine_headers = await _register_machine(db_session, "wrong-machine")

    source_room = await _create_room(client, owner_headers, name="brain-source", members=["s1", "s2"])
    upload_resp = await _upload(client, owner_headers, source_room["id"], sender="owner", filename="doc.pdf")
    save_resp = await client.post(
        f"/v1/rooms/{source_room['id']}/attachments/{upload_resp['id']}/save",
        json={"project": "seat-binding-attach-from-brain"},
        headers=owner_headers,
    )
    assert save_resp.status_code == 200, save_resp.json()
    document_id = save_resp.json()["document_id"]

    target_room = await _create_room(client, owner_headers, name="brain-target", seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, target_room["id"])

    ok_resp = await client.post(
        f"/v1/rooms/{target_room['id']}/attach-from-brain",
        json={"document_id": document_id, "sender": "agent-a"},
        headers=machine_a_headers,
    )
    assert ok_resp.status_code == 201, ok_resp.json()

    # A second document, attached by the WRONG machine, must be refused.
    upload_resp2 = await _upload(client, owner_headers, source_room["id"], sender="owner", filename="doc2.pdf")
    save_resp2 = await client.post(
        f"/v1/rooms/{source_room['id']}/attachments/{upload_resp2['id']}/save",
        json={"project": "seat-binding-attach-from-brain-2"},
        headers=owner_headers,
    )
    document_id2 = save_resp2.json()["document_id"]

    bad_resp = await client.post(
        f"/v1/rooms/{target_room['id']}/attach-from-brain",
        json={"document_id": document_id2, "sender": "agent-a"},
        headers=wrong_machine_headers,
    )
    assert bad_resp.status_code == 403
    assert bad_resp.json()["error"]["code"] == "seat_bound_to_other_machine"


async def test_owner_attachment_upload_as_agent_name_never_bound(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_a_id, _ = await _register_machine(db_session, "machine-a")
    room = await _create_room(client, owner_headers, seats={"agent-a": machine_a_id})
    await _open_room(client, owner_headers, room["id"])
    await _upload(client, owner_headers, room["id"], sender="agent-a")  # owner posting as agent-a: exempt

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["seats"]["agent-a"] == machine_a_id  # unchanged


# --- revoked machines excluded from the assignable roster ---


async def test_list_active_machines_excludes_revoked(db_session):
    from app.machines import list_active_machines

    active_id, _ = await _register_machine(db_session, "active-one", status="active")
    revoked_id, _ = await _register_machine(db_session, "revoked-one", status="revoked")

    active_machines = await list_active_machines(db_session)
    ids = {m.id for m in active_machines}
    assert active_id in ids
    assert revoked_id not in ids


async def test_list_active_machines_disambiguates_same_name_revoked_and_active(db_session):
    """Mirrors the live deployment's own 'Rankati - Commander LXC109 -
    Capital NUC' shape: a revoked row and an active row sharing the exact
    same display name -- only the active one may ever be listed.
    """
    from app.machines import list_active_machines

    shared_name = "Rankati - Commander LXC109 - Capital NUC"
    _, _ = await _register_machine(db_session, shared_name, status="revoked")
    active_id, _ = await _register_machine(db_session, shared_name, status="active")

    active_machines = await list_active_machines(db_session)
    matching = [m for m in active_machines if m.name == shared_name]
    assert len(matching) == 1
    assert matching[0].id == active_id


# --- project field ---


async def test_create_room_with_project_round_trips(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers, project="bernard-ai")
    assert room["project"] == "bernard-ai"

    detail = await client.get(f"/v1/rooms/{room['id']}", headers=owner_headers)
    assert detail.json()["project"] == "bernard-ai"

    listing = await client.get("/v1/rooms", headers=owner_headers)
    listed = next(r for r in listing.json()["results"] if r["id"] == room["id"])
    assert listed["project"] == "bernard-ai"


async def test_create_room_without_project_defaults_to_none(client, db_session):
    owner_headers = await _owner_headers(db_session)
    room = await _create_room(client, owner_headers)
    assert room["project"] is None


async def test_create_room_blank_project_is_none(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "r", "members": ["agent-a", "agent-b"], "project": "   "}, headers=owner_headers
    )
    assert resp.status_code == 201
    assert resp.json()["project"] is None


async def test_create_room_project_is_trimmed(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={"name": "r", "members": ["agent-a", "agent-b"], "project": "  bernard-ai  "},
        headers=owner_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["project"] == "bernard-ai"


async def test_create_room_project_too_long_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={"name": "r", "members": ["agent-a", "agent-b"], "project": "x" * 256},
        headers=owner_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_room_project"


# --- concurrency: two machines racing to claim the same unassigned seat ---

COMMIT_DELAY = 0.4
HEAD_START = 0.05


def _delayed_commit_session(delay: float):
    session = AsyncSessionLocal()
    original_commit = session.commit

    async def slow_commit():
        await asyncio.sleep(delay)
        await original_commit()

    session.commit = slow_commit
    return session


async def test_two_machines_racing_to_claim_same_unassigned_seat_cannot_both_succeed(client, db_session):
    """ADR-0018 decisions 3/5: the room's row lock serializes the bind-or-
    check, so whichever of two genuinely concurrent posts (as the SAME,
    still-open seat, from two DIFFERENT machines) acquires the lock first
    claims the seat; the other, on acquiring the lock afterward, must see
    the seat already bound to the winner and be refused -- never both
    winning, never a lost/overwritten claim. Forces genuine Postgres-level
    lock contention (not just decoupled timing luck) via a delayed commit,
    same technique as this suite's other row-lock race tests
    (tests/test_rooms.py).
    """
    owner_headers = await _owner_headers(db_session)
    machine_x_id, _ = await _register_machine(db_session, "machine-x")
    machine_y_id, _ = await _register_machine(db_session, "machine-y")
    room = await _create_room(client, owner_headers)
    room_id = room["id"]

    async with AsyncSessionLocal() as setup_session:
        await post_message_op(setup_session, room_id, "owner", "starting now", "message", principal=Principal(kind="owner"))

    principal_x = Principal(kind="machine", machine=await db_session.get(Machine, machine_x_id))
    principal_y = Principal(kind="machine", machine=await db_session.get(Machine, machine_y_id))

    winner_session = _delayed_commit_session(COMMIT_DELAY)
    loser_session = AsyncSessionLocal()

    async def run_x():
        async with winner_session:
            return await post_message_op(winner_session, room_id, "agent-a", "hello from x", "message", principal=principal_x)

    async def run_y():
        await asyncio.sleep(HEAD_START)
        async with loser_session:
            try:
                return await post_message_op(loser_session, room_id, "agent-a", "hello from y", "message", principal=principal_y)
            except ApiError as exc:
                return exc

    start = time.monotonic()
    x_result, y_result = await asyncio.gather(run_x(), run_y())
    elapsed = time.monotonic() - start

    # Genuine blocking, not decoupled timing luck.
    assert elapsed >= COMMIT_DELAY

    # x acquired the lock first (no delay on its own side beyond the
    # deliberately slowed commit) -- it wins the claim.
    x_message, x_room = x_result
    assert x_message.sender == "agent-a"

    assert isinstance(y_result, ApiError)
    assert y_result.code == "seat_bound_to_other_machine"

    async with AsyncSessionLocal() as check_session:
        member = await check_session.scalar(
            select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.agent_name == "agent-a")
        )
        # exactly one machine ends up bound -- machine-x, the winner.
        assert member.bound_machine_id == machine_x_id
