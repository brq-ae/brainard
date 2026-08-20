"""Background sweeper (app/room_sweeper.py, ADR-0007 decision 3): expiry
close (close_reason 'time', reusing app.rooms.close_room -- same atomic
close path and owner notification as the message cap) and the one-time
"closing soon" system-message nudge. Every test calls `sweep_once()`
directly -- never the 60s `run_sweeper` loop -- per the module's own
"testable unit" design.

Rooms can only be created with a FUTURE deadline via the API (app/rooms.py
validates that), so "the room has already expired" is simulated here by
creating a room with a real (future) duration and then backdating its
`expires_at` directly in the DB -- the same pattern other suites use for
state a public API can't reach directly (e.g. tests/test_flags.py).
"""

from datetime import UTC, datetime, timedelta

from ulid import ULID

import app.notify as notify_module
from app.models import Machine, OwnerToken, Room
from app.room_sweeper import WARN_SECONDS, sweep_once
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _machine_headers(db_session, name: str = "test-machine") -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name=name, token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _configure_notifications(client, owner_headers) -> None:
    resp = await client.post(
        "/v1/notifications-config",
        json={"ntfy_url": "https://ntfy.example.org", "topic": "roomtopic"},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.json()


async def _create_room(client, owner_headers, *, name="room-1", members=None, duration_seconds=3600) -> dict:
    body = {
        "name": name,
        "members": members if members is not None else ["agent-a", "agent-b"],
        "duration_seconds": duration_seconds,
    }
    resp = await client.post("/v1/rooms", json=body, headers=owner_headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


async def _backdate_expires_at(db_session, room_id: str, expires_at: datetime) -> None:
    """Directly sets a room's `expires_at` to a value the create API would
    never accept (past, or inside the warn window) -- simulates time
    having passed without an actual sleep.
    """
    room = await db_session.get(Room, room_id)
    room.expires_at = expires_at
    await db_session.commit()


async def _get_room(client, headers, room_id: str) -> dict:
    resp = await client.get(f"/v1/rooms/{room_id}", headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()


# --- expiry close ---


async def test_sweep_once_closes_expired_room_with_time_reason(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) - timedelta(seconds=5))

    result = await sweep_once()
    assert result["closed"] == [room["id"]]

    detail = await _get_room(client, machine_headers, room["id"])
    assert detail["status"] == "closed"
    assert detail["close_reason"] == "time"
    assert detail["closed_at"] is not None


async def test_sweep_once_notifies_owner_on_time_close(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_notifications(client, owner_headers)
    room = await _create_room(client, owner_headers, name="debate-room")
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) - timedelta(seconds=5))

    calls = []

    async def fake_send(url, title, body):
        calls.append((url, title, body))

    monkeypatch.setattr(notify_module, "_send_ntfy", fake_send)

    await sweep_once()

    assert len(calls) == 1
    _, title, body = calls[0]
    assert title == "Brain room: debate-room"
    assert "time" in body


async def test_sweep_once_ignores_rooms_with_no_expires_at(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    resp = await client.post(
        "/v1/rooms", json={"name": "no-deadline", "members": ["agent-a", "agent-b"]}, headers=owner_headers
    )
    assert resp.status_code == 201
    room = resp.json()
    assert room["expires_at"] is None

    result = await sweep_once()
    assert room["id"] not in result["closed"]
    assert room["id"] not in result["warned"]

    detail = await _get_room(client, machine_headers, room["id"])
    assert detail["status"] == "open"


async def test_sweep_once_leaves_unexpired_room_open(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers, duration_seconds=3600)  # far in the future

    result = await sweep_once()
    assert room["id"] not in result["closed"]

    detail = await _get_room(client, machine_headers, room["id"])
    assert detail["status"] == "open"


async def test_time_closed_room_rejects_further_posts(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) - timedelta(seconds=5))
    await sweep_once()

    resp = await client.post(
        f"/v1/rooms/{room['id']}/messages", json={"sender": "agent-a", "text": "too late"}, headers=machine_headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "room_closed"


async def test_cap_and_time_are_independent_expiry_still_works_with_a_high_cap(client, db_session):
    """A room with both a (huge) message cap and a deadline is closed by
    whichever guardrail fires first -- here, time, since the cap is nowhere
    close to being hit."""
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    resp = await client.post(
        "/v1/rooms",
        json={"name": "r", "members": ["agent-a", "agent-b"], "max_messages": 9999, "duration_seconds": 3600},
        headers=owner_headers,
    )
    assert resp.status_code == 201
    room = resp.json()
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) - timedelta(seconds=5))

    await sweep_once()

    detail = await _get_room(client, machine_headers, room["id"])
    assert detail["status"] == "closed"
    assert detail["close_reason"] == "time"
    assert detail["message_count"] == 0  # cap was nowhere close to being hit


# --- closing-soon nudge ---


async def test_sweep_once_posts_closing_nudge_and_sets_closing_warned_at(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    # Inside the warn window (< WARN_SECONDS away) but not yet expired.
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) + timedelta(seconds=WARN_SECONDS - 10))

    result = await sweep_once()
    assert result["warned"] == [room["id"]]
    assert result["closed"] == []

    detail = await _get_room(client, machine_headers, room["id"])
    assert detail["status"] == "open"  # nudged, not closed -- still time left
    system_messages = [m for m in detail["messages"] if m["kind"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["sender"] == "system"
    assert "minutes left" in system_messages[0]["text"]
    assert "post your closing statements" in system_messages[0]["text"]


async def test_sweep_once_does_not_double_post_the_nudge(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) + timedelta(seconds=WARN_SECONDS - 10))

    first = await sweep_once()
    assert first["warned"] == [room["id"]]

    second = await sweep_once()
    assert second["warned"] == []  # already warned -- guarded by closing_warned_at

    detail = await _get_room(client, machine_headers, room["id"])
    system_messages = [m for m in detail["messages"] if m["kind"] == "system"]
    assert len(system_messages) == 1  # still exactly one nudge


async def test_sweep_once_ignores_rooms_not_yet_in_warn_window(client, db_session):
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    # Well outside the warn window.
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) + timedelta(seconds=WARN_SECONDS + 3600))

    result = await sweep_once()
    assert room["id"] not in result["warned"]

    detail = await _get_room(client, machine_headers, room["id"])
    assert detail["messages"] == []  # no nudge posted


async def test_sweep_once_expires_a_room_it_just_warned_on_a_later_cycle(client, db_session):
    """Two independent effects of one deadline over time: first sweep
    (inside the warn window) posts the nudge; a later sweep, once the
    deadline has actually passed, closes the room -- close_reason 'time',
    not blocked by having already been warned."""
    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    room = await _create_room(client, owner_headers)
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) + timedelta(seconds=WARN_SECONDS - 10))

    first = await sweep_once()
    assert first["warned"] == [room["id"]]

    # Time passes; the deadline is now in the past.
    await _backdate_expires_at(db_session, room["id"], datetime.now(UTC) - timedelta(seconds=5))
    second = await sweep_once()
    assert second["closed"] == [room["id"]]

    detail = await _get_room(client, machine_headers, room["id"])
    assert detail["status"] == "closed"
    assert detail["close_reason"] == "time"


# --- per-room isolation (fix-first review: cheap hardening) ---


async def test_sweep_once_isolates_a_failing_room_close_and_continues(client, db_session, monkeypatch):
    """One room's close_room raising must not abort the batch -- the other
    expired room in the same cycle must still be closed.
    """
    import app.room_sweeper as sweeper_module

    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    bad_room = await _create_room(client, owner_headers, name="bad-room")
    good_room = await _create_room(client, owner_headers, name="good-room")
    await _backdate_expires_at(db_session, bad_room["id"], datetime.now(UTC) - timedelta(seconds=5))
    await _backdate_expires_at(db_session, good_room["id"], datetime.now(UTC) - timedelta(seconds=5))

    original_close_room = sweeper_module.close_room

    async def flaky_close_room(session, room_id, reason):
        if room_id == bad_room["id"]:
            raise RuntimeError("simulated close failure")
        return await original_close_room(session, room_id, reason)

    monkeypatch.setattr(sweeper_module, "close_room", flaky_close_room)

    result = await sweep_once()  # must not raise -- the bad room's failure is caught and logged
    assert result["closed"] == [good_room["id"]]

    good_detail = await _get_room(client, machine_headers, good_room["id"])
    assert good_detail["status"] == "closed"
    assert good_detail["close_reason"] == "time"

    bad_detail = await _get_room(client, machine_headers, bad_room["id"])
    assert bad_detail["status"] == "open"  # its close raised, was caught, and was skipped this cycle


async def test_sweep_once_isolates_a_failing_nudge_and_continues(client, db_session, monkeypatch):
    """Same isolation, for the closing-soon nudge loop."""
    import app.room_sweeper as sweeper_module

    owner_headers = await _owner_headers(db_session)
    machine_headers = await _machine_headers(db_session)
    bad_room = await _create_room(client, owner_headers, name="bad-nudge-room")
    good_room = await _create_room(client, owner_headers, name="good-nudge-room")
    await _backdate_expires_at(db_session, bad_room["id"], datetime.now(UTC) + timedelta(seconds=WARN_SECONDS - 10))
    await _backdate_expires_at(db_session, good_room["id"], datetime.now(UTC) + timedelta(seconds=WARN_SECONDS - 10))

    original_post_closing_nudge = sweeper_module.post_closing_nudge

    async def flaky_nudge(session, room_id, text):
        if room_id == bad_room["id"]:
            raise RuntimeError("simulated nudge failure")
        return await original_post_closing_nudge(session, room_id, text)

    monkeypatch.setattr(sweeper_module, "post_closing_nudge", flaky_nudge)

    result = await sweep_once()  # must not raise
    assert result["warned"] == [good_room["id"]]

    good_detail = await _get_room(client, machine_headers, good_room["id"])
    good_system_messages = [m for m in good_detail["messages"] if m["kind"] == "system"]
    assert len(good_system_messages) == 1

    bad_detail = await _get_room(client, machine_headers, bad_room["id"])
    bad_system_messages = [m for m in bad_detail["messages"] if m["kind"] == "system"]
    assert bad_system_messages == []  # its nudge raised, was caught, and was skipped this cycle
