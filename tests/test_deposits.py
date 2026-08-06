"""Checkpoint deposit flow -- POST /v1/deposits (contracts-v1.md §2)."""

from sqlalchemy import select
from ulid import ULID

from app.models import Deposit, Event, Handoff, Machine, OwnerToken, Project
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _machine_headers(db_session) -> dict:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name="test-machine", token_hash=hash_token(token), status="active")
    db_session.add(machine)
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


def _deposit_body(**overrides) -> dict:
    body = {
        "deposit_id": str(ULID()),
        "tool": "claude-code",
        "session": "sess-1",
        "project": "brain",
        "reason": "daily",
        "client_ts": "2026-08-06T12:00:00Z",
        "events": [],
    }
    body.update(overrides)
    return body


def _event(seq: int = 1, kind: str = "note", summary: str = "did a thing", **overrides) -> dict:
    e = {"seq": seq, "ts": "2026-08-06T11:59:00Z", "kind": kind, "summary": summary}
    e.update(overrides)
    return e


def _handoff(**overrides) -> dict:
    h = {
        "stands": "phase 2 deposits endpoint implemented",
        "in_flight": "writing tests",
        "blocked": "",
        "next_steps": "run e2e verification",
    }
    h.update(overrides)
    return h


# --- happy paths ---


async def test_daily_deposit_with_events_happy_path(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        events=[
            _event(seq=1, kind="session.started", summary="started session"),
            _event(seq=2, kind="work.started", summary="began phase 2"),
            _event(seq=3, kind="artifact.produced", summary="opened PR", tags=["git"]),
        ],
        metrics={"tokens_in": 100, "tokens_out": 50},
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["deposit_id"] == body["deposit_id"]
    assert data["replayed"] is False
    assert data["counts"] == {"events": 3, "handoff": False}
    assert data["project"] == {"name": "brain", "stub_created": True}

    events = (await db_session.scalars(select(Event).where(Event.deposit_id == body["deposit_id"]))).all()
    assert len(events) == 3
    assert {e.kind for e in events} == {"session.started", "work.started", "artifact.produced"}

    deposit = await db_session.get(Deposit, body["deposit_id"])
    assert deposit.metrics == {"tokens_in": 100, "tokens_out": 50}


async def test_session_end_with_handoff(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        reason="session_end",
        handoff=_handoff(notes="all good"),
        events=[_event(seq=1, kind="session.ended")],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"] == {"events": 1, "handoff": True}

    handoff = await db_session.scalar(select(Handoff).where(Handoff.deposit_id == body["deposit_id"]))
    assert handoff is not None
    assert handoff.stands == body["handoff"]["stands"]
    assert handoff.next_steps == body["handoff"]["next_steps"]
    assert handoff.notes == "all good"
    assert handoff.project == "brain"


async def test_session_end_with_no_handoff_waiver(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", reason="session_end", no_handoff="trivial session, nothing to report")

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"] == {"events": 0, "handoff": False}

    deposit = await db_session.get(Deposit, body["deposit_id"])
    assert deposit.no_handoff == "trivial session, nothing to report"


async def test_metrics_partial_subsets_accepted(client, db_session):
    headers = await _machine_headers(db_session)

    resp1 = await client.post(
        "/v1/deposits", json=_deposit_body(project="metrics-proj", metrics={"duration": 12.5}), headers=headers
    )
    assert resp1.status_code == 200

    resp2 = await client.post(
        "/v1/deposits", json=_deposit_body(project="metrics-proj", metrics=None), headers=headers
    )
    assert resp2.status_code == 200

    resp3 = await client.post(
        "/v1/deposits",
        json=_deposit_body(project="metrics-proj", metrics={"model": "sonnet", "cost_estimate": 0.02}),
        headers=headers,
    )
    assert resp3.status_code == 200


async def test_auto_stub_project_created_only_once(client, db_session):
    headers = await _machine_headers(db_session)

    resp1 = await client.post(
        "/v1/deposits", json=_deposit_body(project="new-proj", events=[_event()]), headers=headers
    )
    assert resp1.status_code == 200
    assert resp1.json()["project"] == {"name": "new-proj", "stub_created": True}

    project = await db_session.get(Project, "new-proj")
    assert project is not None
    assert project.status == "active"
    assert project.created_at is not None

    resp2 = await client.post(
        "/v1/deposits", json=_deposit_body(project="new-proj", events=[_event()]), headers=headers
    )
    assert resp2.status_code == 200
    assert resp2.json()["project"] == {"name": "new-proj", "stub_created": False}


# --- rejections ---


async def test_session_end_without_handoff_or_waiver_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", reason="session_end")

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "handoff_or_waiver_required"
    assert await db_session.get(Deposit, body["deposit_id"]) is None


async def test_session_end_with_both_handoff_and_waiver_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        reason="session_end",
        handoff=_handoff(),
        no_handoff="contradiction",
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "handoff_and_waiver_conflict"
    assert await db_session.get(Deposit, body["deposit_id"]) is None


async def test_unknown_event_kind_rejected_with_per_event_recovery(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        events=[
            _event(seq=1, kind="note"),
            _event(seq=2, kind="totally.unknown", summary="mystery event"),
        ],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "unknown_event_kind"
    assert error["failing_events"] == [
        {"seq": 2, "kind": "totally.unknown", "recovery": "relabel to 'note', preserve the original kind as a tag, resend"}
    ]

    # whole deposit rejected -- nothing stored, including the valid event
    assert await db_session.get(Deposit, body["deposit_id"]) is None
    assert (await db_session.scalars(select(Event).where(Event.deposit_id == body["deposit_id"]))).all() == []


async def test_payload_over_256kb_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    big_payload = {"blob": "x" * (257 * 1024)}
    body = _deposit_body(project="brain", events=[_event(seq=1, payload=big_payload)])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "payload_too_large"
    assert error["failing_events"][0]["seq"] == 1
    assert error["failing_events"][0]["payload_bytes"] > 256 * 1024


async def test_deposit_with_knowledge_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brand-new-2", knowledge=[{"title": "some lesson"}])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "knowledge_not_implemented"
    # atomicity: the unrelated new-project auto-stub must not have happened either
    assert await db_session.get(Project, "brand-new-2") is None
    assert await db_session.get(Deposit, body["deposit_id"]) is None


async def test_owner_token_rejected_on_deposits(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/deposits", json=_deposit_body(), headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "machine_token_required"


async def test_invalid_deposit_id_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(deposit_id="not-a-ulid")
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_deposit_id"


# --- idempotency & atomicity ---


async def test_idempotent_replay_returns_original_ack_and_stores_nothing_new(client, db_session):
    headers = await _machine_headers(db_session)
    deposit_id = str(ULID())

    first_body = _deposit_body(
        deposit_id=deposit_id, project="brain", events=[_event(seq=1), _event(seq=2)]
    )
    resp1 = await client.post("/v1/deposits", json=first_body, headers=headers)
    assert resp1.status_code == 200
    ack1 = resp1.json()
    assert ack1["replayed"] is False

    # retried with a materially different body under the same deposit_id --
    # idempotency must ignore it entirely
    retried_body = _deposit_body(
        deposit_id=deposit_id, project="brain", tool="a-different-tool", events=[_event(seq=1)]
    )
    resp2 = await client.post("/v1/deposits", json=retried_body, headers=headers)
    assert resp2.status_code == 200
    ack2 = resp2.json()

    assert ack2["replayed"] is True
    assert ack2["deposit_id"] == deposit_id
    assert ack2["received_at"] == ack1["received_at"]
    assert ack2["counts"] == ack1["counts"] == {"events": 2, "handoff": False}
    assert ack2["project"] == ack1["project"]

    events = (await db_session.scalars(select(Event).where(Event.deposit_id == deposit_id))).all()
    assert len(events) == 2  # original count, no duplication and no replacement

    deposits = (await db_session.scalars(select(Deposit).where(Deposit.deposit_id == deposit_id))).all()
    assert len(deposits) == 1
    assert deposits[0].tool == "claude-code"  # original body's value, not the retry's


async def test_invalid_deposit_creates_no_rows_and_no_project_stub(client, db_session):
    """A deposit that fails validation is accepted or rejected as one unit --
    the project stub is created only on acceptance, never on a rejected
    attempt, even though the project name was novel.
    """
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="never-created", events=[_event(kind="bogus.kind")])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    assert await db_session.get(Project, "never-created") is None
    assert await db_session.get(Deposit, body["deposit_id"]) is None
    assert (await db_session.scalars(select(Event).where(Event.deposit_id == body["deposit_id"]))).all() == []


async def test_replay_with_invalid_retry_body_still_returns_original_ack(client, db_session):
    """Idempotency is unconditional: even a retried body that would itself be
    rejected (unknown event kind AND a non-empty knowledge[]) must not be
    validated at all on replay -- the existing deposit_id short-circuits
    straight to the original acknowledgment, storing nothing new.
    """
    headers = await _machine_headers(db_session)
    deposit_id = str(ULID())

    first_body = _deposit_body(deposit_id=deposit_id, project="brain", events=[_event(seq=1), _event(seq=2)])
    resp1 = await client.post("/v1/deposits", json=first_body, headers=headers)
    assert resp1.status_code == 200
    ack1 = resp1.json()
    assert ack1["replayed"] is False

    invalid_retry_body = _deposit_body(
        deposit_id=deposit_id,
        project="brain",
        events=[_event(seq=1, kind="totally.unknown")],
        knowledge=[{"title": "should never be validated, let alone stored"}],
    )
    resp2 = await client.post("/v1/deposits", json=invalid_retry_body, headers=headers)

    assert resp2.status_code == 200
    ack2 = resp2.json()
    assert ack2["replayed"] is True
    assert ack2["deposit_id"] == deposit_id
    assert ack2["received_at"] == ack1["received_at"]
    assert ack2["counts"] == ack1["counts"] == {"events": 2, "handoff": False}
    assert ack2["project"] == ack1["project"]

    events = (await db_session.scalars(select(Event).where(Event.deposit_id == deposit_id))).all()
    assert len(events) == 2  # original rows only -- unchanged

    deposits = (await db_session.scalars(select(Deposit).where(Deposit.deposit_id == deposit_id))).all()
    assert len(deposits) == 1
