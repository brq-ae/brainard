"""Checkpoint deposit flow -- POST /v1/deposits (contracts-v1.md §2)."""

from sqlalchemy import select
from ulid import ULID

from app.models import Deposit, Event, Flag, Handoff, KnowledgeEntry, Machine, OwnerToken, Project
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


def _knowledge_new(**overrides) -> dict:
    item = {
        "title": "How to restart the healthcheck loop",
        "namespace": "howto",
        "body": "1. stop the container\n2. docker compose up -d\n3. verify healthz",
    }
    item.update(overrides)
    return item


def _knowledge_retire(entry_id: str, reason: str = "no longer applicable") -> dict:
    return {"retire": entry_id, "reason": reason}


async def _deposit_one_entry(client, headers, deposit_project: str, **entry_overrides) -> str:
    """Deposits a single new knowledge entry and returns its server id.
    `deposit_project` is the deposit envelope's `project`; entry-level
    `project` (§3, optional per-entry) is a separate field passed via
    `entry_overrides` when a test needs it set.
    """
    body = _deposit_body(project=deposit_project, knowledge=[_knowledge_new(**entry_overrides)])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["knowledge"][0]["id"]


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
    assert data["counts"] == {"events": 3, "handoff": False, "knowledge": 0}
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
    assert data["counts"] == {"events": 1, "handoff": True, "knowledge": 0}

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
    assert data["counts"] == {"events": 0, "handoff": False, "knowledge": 0}

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
    assert ack2["counts"] == ack1["counts"] == {"events": 2, "handoff": False, "knowledge": 0}
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
    assert ack2["counts"] == ack1["counts"] == {"events": 2, "handoff": False, "knowledge": 0}
    assert ack2["project"] == ack1["project"]

    events = (await db_session.scalars(select(Event).where(Event.deposit_id == deposit_id))).all()
    assert len(events) == 2  # original rows only -- unchanged


# --- knowledge[] (contracts-v1.md §3) ---


async def test_knowledge_entry_created_with_tags_and_project(client, db_session):
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name="test-machine", token_hash=hash_token(token), status="active")
    db_session.add(machine)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    body = _deposit_body(
        project="brain",
        knowledge=[
            _knowledge_new(
                title="Wipe the volume before a migration change",
                namespace="lessons",
                body="Compose keeps stale schema in the named volume between builds.",
                tags=["postgres", "migrations"],
                project="brain",
            )
        ],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"]["knowledge"] == 1
    ack = data["knowledge"][0]
    assert ack["action"] == "created"
    assert ack["title"] == "Wipe the volume before a migration change"

    entry = await db_session.get(KnowledgeEntry, ack["id"])
    assert entry is not None
    assert entry.namespace == "lessons"
    assert entry.tags == ["postgres", "migrations"]
    assert entry.project == "brain"
    assert entry.status == "active"
    assert entry.supersedes == []
    assert entry.deposit_id == body["deposit_id"]
    assert entry.tool == body["tool"]
    assert entry.session == body["session"]
    assert entry.machine_id == machine.id
    assert entry.created_at is not None


async def test_knowledge_strict_namespace_validation_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", knowledge=[_knowledge_new(namespace="not-a-real-shelf")])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_knowledge_entry"
    assert error["failing_items"][0]["index"] == 0
    assert "namespace" in error["failing_items"][0]["reason"]
    assert await db_session.get(Deposit, body["deposit_id"]) is None


async def test_knowledge_empty_title_and_body_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        knowledge=[_knowledge_new(title=""), _knowledge_new(body="   ")],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_knowledge_entry"
    assert {item["index"] for item in error["failing_items"]} == {0, 1}
    assert await db_session.get(Deposit, body["deposit_id"]) is None


async def test_knowledge_body_over_1mb_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", knowledge=[_knowledge_new(body="x" * (1024 * 1024 + 1))])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_knowledge_entry"
    assert "byte cap" in error["failing_items"][0]["reason"]


async def test_knowledge_bad_supersedes_reference_rejects_whole_deposit(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brand-new-supersedes",
        knowledge=[
            _knowledge_new(title="Valid entry, but batchmate is bad"),
            _knowledge_new(title="References a ghost", supersedes=["01ARZ3NDEKTSV4RRFFQ69G5FAV"]),
        ],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "unknown_supersedes_reference"
    assert error["failing_items"] == [
        {
            "index": 1,
            "missing": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
            "recovery": "fix or drop the supersedes reference, resend",
        }
    ]
    # whole deposit rejected -- including the unrelated new project stub and
    # the valid batchmate entry
    assert await db_session.get(Project, "brand-new-supersedes") is None
    assert await db_session.get(Deposit, body["deposit_id"]) is None
    assert (
        await db_session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.deposit_id == body["deposit_id"]))
    ).all() == []


async def test_supersession_sets_active_parent_to_superseded(client, db_session):
    headers = await _machine_headers(db_session)
    parent_id = await _deposit_one_entry(client, headers, "brain", title="Old approach")

    body = _deposit_body(
        project="brain", knowledge=[_knowledge_new(title="New approach", supersedes=[parent_id])]
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200
    child_id = resp.json()["knowledge"][0]["id"]

    parent = await db_session.get(KnowledgeEntry, parent_id)
    child = await db_session.get(KnowledgeEntry, child_id)
    assert parent.status == "superseded"
    assert child.status == "active"
    assert child.supersedes == [parent_id]


async def test_superseding_a_retired_parent_leaves_it_retired(client, db_session):
    headers = await _machine_headers(db_session)
    parent_id = await _deposit_one_entry(client, headers, "brain", title="Dead end approach")

    retire_body = _deposit_body(project="brain", knowledge=[_knowledge_retire(parent_id, reason="dead end")])
    retire_resp = await client.post("/v1/deposits", json=retire_body, headers=headers)
    assert retire_resp.status_code == 200

    parent = await db_session.get(KnowledgeEntry, parent_id)
    assert parent.status == "retired"

    supersede_body = _deposit_body(
        project="brain", knowledge=[_knowledge_new(title="Replacement approach", supersedes=[parent_id])]
    )
    resp = await client.post("/v1/deposits", json=supersede_body, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(parent)
    assert parent.status == "retired"  # lineage recorded via supersedes[], status left alone


async def test_retire_action_happy_path(client, db_session):
    headers = await _machine_headers(db_session)
    entry_id = await _deposit_one_entry(client, headers, "brain", title="Obsolete lesson")

    body = _deposit_body(project="brain", knowledge=[_knowledge_retire(entry_id, reason="proven wrong")])
    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    ack = resp.json()["knowledge"][0]
    assert ack["action"] == "retired"
    assert ack["id"] == entry_id

    entry = await db_session.get(KnowledgeEntry, entry_id)
    assert entry.status == "retired"
    assert entry.retire_reason == "proven wrong"


async def test_retire_unknown_target_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", knowledge=[_knowledge_retire("01ARZ3NDEKTSV4RRFFQ69G5FAV")])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_retire_target"
    assert error["failing_items"][0]["reason"] == "no such library entry"


async def test_retire_already_retired_target_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    entry_id = await _deposit_one_entry(client, headers, "brain", title="Retire me twice")

    first = _deposit_body(project="brain", knowledge=[_knowledge_retire(entry_id)])
    assert (await client.post("/v1/deposits", json=first, headers=headers)).status_code == 200

    second = _deposit_body(project="brain", knowledge=[_knowledge_retire(entry_id, reason="again")])
    resp = await client.post("/v1/deposits", json=second, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_retire_target"
    assert "already 'retired'" in error["failing_items"][0]["reason"]


async def test_retire_already_superseded_target_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    parent_id = await _deposit_one_entry(client, headers, "brain", title="Will be superseded")

    supersede_body = _deposit_body(
        project="brain", knowledge=[_knowledge_new(title="Better approach", supersedes=[parent_id])]
    )
    assert (await client.post("/v1/deposits", json=supersede_body, headers=headers)).status_code == 200

    retire_body = _deposit_body(project="brain", knowledge=[_knowledge_retire(parent_id)])
    resp = await client.post("/v1/deposits", json=retire_body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_retire_target"
    assert "already 'superseded'" in error["failing_items"][0]["reason"]


async def test_fork_flag_created_when_two_entries_supersede_same_parent(client, db_session):
    headers = await _machine_headers(db_session)
    parent_id = await _deposit_one_entry(client, headers, "brain", title="Shared parent")

    first_child = _deposit_body(
        project="brain", knowledge=[_knowledge_new(title="First fork", supersedes=[parent_id])]
    )
    resp1 = await client.post("/v1/deposits", json=first_child, headers=headers)
    assert resp1.status_code == 200
    first_child_id = resp1.json()["knowledge"][0]["id"]

    second_child = _deposit_body(
        project="brain", knowledge=[_knowledge_new(title="Second fork", supersedes=[parent_id])]
    )
    resp2 = await client.post("/v1/deposits", json=second_child, headers=headers)
    assert resp2.status_code == 200
    second_child_id = resp2.json()["knowledge"][0]["id"]

    flags = (await db_session.scalars(select(Flag).where(Flag.type == "fork"))).all()
    assert len(flags) == 1
    assert flags[0].entry_id == second_child_id
    assert flags[0].related_entry_id == first_child_id
    assert flags[0].detail["parent_id"] == parent_id

    # forks are accepted, never rejected, and both children are left active as siblings
    first_child = await db_session.get(KnowledgeEntry, first_child_id)
    second_child = await db_session.get(KnowledgeEntry, second_child_id)
    assert first_child.status == "active"
    assert second_child.status == "active"


async def test_duplicate_supersedes_ids_produce_exactly_one_fork_flag(client, db_session):
    """supersedes=[X, X] against a parent that already has an existing
    superseding sibling must produce exactly one fork flag and one clean
    status for the parent -- not one flag per (redundant) occurrence of X.
    """
    headers = await _machine_headers(db_session)
    parent_id = await _deposit_one_entry(client, headers, "brain", title="Shared parent for duplicate supersedes")

    existing_sibling_body = _deposit_body(
        project="brain", knowledge=[_knowledge_new(title="Existing sibling", supersedes=[parent_id])]
    )
    resp1 = await client.post("/v1/deposits", json=existing_sibling_body, headers=headers)
    assert resp1.status_code == 200
    existing_sibling_id = resp1.json()["knowledge"][0]["id"]

    duplicate_supersedes_body = _deposit_body(
        project="brain",
        knowledge=[_knowledge_new(title="New fork with duplicate parent ref", supersedes=[parent_id, parent_id])],
    )
    resp2 = await client.post("/v1/deposits", json=duplicate_supersedes_body, headers=headers)
    assert resp2.status_code == 200
    new_entry_id = resp2.json()["knowledge"][0]["id"]

    fork_flags = (await db_session.scalars(select(Flag).where(Flag.type == "fork"))).all()
    assert len(fork_flags) == 1
    assert fork_flags[0].entry_id == new_entry_id
    assert fork_flags[0].related_entry_id == existing_sibling_id
    assert fork_flags[0].detail["parent_id"] == parent_id

    parent = await db_session.get(KnowledgeEntry, parent_id)
    assert parent.status == "superseded"  # one clean transition, not toggled


async def test_duplicate_flag_created_for_near_identical_entry_same_namespace(client, db_session):
    headers = await _machine_headers(db_session)
    original_id = await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Docker Compose Healthcheck Times Out On Cold Start",
        namespace="lessons",
        body="The healthcheck interval was too aggressive for a cold container start.",
    )

    near_dup_body = _deposit_body(
        project="brain",
        knowledge=[
            _knowledge_new(
                title="Docker Compose Healthcheck Times Out On Cold Start",
                namespace="lessons",
                body="A slightly different write-up of the same healthcheck timing lesson.",
            )
        ],
    )
    resp = await client.post("/v1/deposits", json=near_dup_body, headers=headers)
    assert resp.status_code == 200
    dup_id = resp.json()["knowledge"][0]["id"]

    flags = (await db_session.scalars(select(Flag).where(Flag.type == "duplicate", Flag.entry_id == dup_id))).all()
    assert len(flags) == 1
    assert flags[0].related_entry_id == original_id
    assert flags[0].detail["rank"] > 0

    # never blocks acceptance
    entry = await db_session.get(KnowledgeEntry, dup_id)
    assert entry.status == "active"


async def test_duplicate_flag_created_for_identical_body_under_different_title(client, db_session):
    """A title-only, AND-only similarity query would miss this: the titles
    share no words at all, but the bodies are verbatim identical. The OR
    query built from title terms + top body lexemes must still catch it.
    """
    headers = await _machine_headers(db_session)
    shared_body = (
        "The quantum flux capacitor requires careful calibration before every "
        "cold boot sequence to avoid oscillation drift entirely."
    )
    original_id = await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Zylophonic Widget Setup Guide",
        namespace="lessons",
        body=shared_body,
    )

    resp = await client.post(
        "/v1/deposits",
        json=_deposit_body(
            project="brain",
            knowledge=[
                _knowledge_new(
                    title="A Completely Different Heading Altogether",
                    namespace="lessons",
                    body=shared_body,
                )
            ],
        ),
        headers=headers,
    )
    assert resp.status_code == 200
    dup_id = resp.json()["knowledge"][0]["id"]

    flags = (await db_session.scalars(select(Flag).where(Flag.type == "duplicate", Flag.entry_id == dup_id))).all()
    assert len(flags) == 1
    assert flags[0].related_entry_id == original_id


async def test_duplicate_flag_created_for_title_differing_by_one_word(client, db_session):
    """Titles differing by a single extra word, with otherwise unrelated
    bodies, must still be flagged via the shared title terms.
    """
    headers = await _machine_headers(db_session)
    original_id = await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Docker Compose Healthcheck Timeout Issue",
        namespace="howto",
        body="Increase the start_period to avoid false negatives on cold boot.",
    )

    resp = await client.post(
        "/v1/deposits",
        json=_deposit_body(
            project="brain",
            knowledge=[
                _knowledge_new(
                    title="Docker Compose Healthcheck Timeout Issue Resolved",
                    namespace="howto",
                    body="A totally unrelated body about DNS resolution and network routing steps.",
                )
            ],
        ),
        headers=headers,
    )
    assert resp.status_code == 200
    dup_id = resp.json()["knowledge"][0]["id"]

    flags = (await db_session.scalars(select(Flag).where(Flag.type == "duplicate", Flag.entry_id == dup_id))).all()
    assert len(flags) == 1
    assert flags[0].related_entry_id == original_id


async def test_duplicate_flag_not_created_for_genuinely_unrelated_entries(client, db_session):
    """Noise guard: two entries in the same namespace with no shared
    vocabulary at all must not be flagged as possible duplicates.
    """
    headers = await _machine_headers(db_session)
    await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Zylophonic Widget Setup Guide",
        namespace="lessons",
        body="The quantum flux capacitor requires careful calibration before every cold boot sequence.",
    )

    resp = await client.post(
        "/v1/deposits",
        json=_deposit_body(
            project="brain",
            knowledge=[
                _knowledge_new(
                    title="Marmalade Recipe Troubleshooting Notes",
                    namespace="lessons",
                    body="Adjust the sugar to pectin ratio and simmer longer if the citrus preserves refuse to set.",
                )
            ],
        ),
        headers=headers,
    )
    assert resp.status_code == 200
    entry_id = resp.json()["knowledge"][0]["id"]

    flags = (await db_session.scalars(select(Flag).where(Flag.entry_id == entry_id))).all()
    assert flags == []


async def test_duplicate_flag_not_created_across_namespaces(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Docker Compose Healthcheck Times Out On Cold Start",
        namespace="lessons",
        body="The healthcheck interval was too aggressive for a cold container start.",
    )

    other_namespace_body = _deposit_body(
        project="brain",
        knowledge=[
            _knowledge_new(
                title="Docker Compose Healthcheck Times Out On Cold Start",
                namespace="reference",
                body="A slightly different write-up of the same healthcheck timing lesson.",
            )
        ],
    )
    resp = await client.post("/v1/deposits", json=other_namespace_body, headers=headers)
    assert resp.status_code == 200
    entry_id = resp.json()["knowledge"][0]["id"]

    flags = (await db_session.scalars(select(Flag).where(Flag.entry_id == entry_id))).all()
    assert flags == []


async def test_knowledge_atomicity_rejection_stores_nothing_including_flags(client, db_session):
    """A deposit whose knowledge[] would create forks/duplicates on one item
    but fails whole-deposit validation on another must store nothing at
    all -- not the entry, and not the flags that would otherwise result.
    """
    headers = await _machine_headers(db_session)
    parent_id = await _deposit_one_entry(client, headers, "brain", title="Parent for a would-be fork")

    body = _deposit_body(
        project="brain",
        knowledge=[
            _knowledge_new(title="Would-be fork sibling", supersedes=[parent_id]),
            _knowledge_new(title="Bad batchmate", namespace="not-a-shelf"),
        ],
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    assert await db_session.get(Deposit, body["deposit_id"]) is None
    assert (
        await db_session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.deposit_id == body["deposit_id"]))
    ).all() == []
    assert (await db_session.scalars(select(Flag))).all() == []

    parent = await db_session.get(KnowledgeEntry, parent_id)
    assert parent.status == "active"  # untouched
