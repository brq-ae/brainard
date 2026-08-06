"""Library entry reads -- GET /v1/library/{id} (contracts-v1.md §3, §7)."""

from ulid import ULID

from app.models import Machine, OwnerToken
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


def _knowledge_new(**overrides) -> dict:
    item = {
        "title": "How to restart the healthcheck loop",
        "namespace": "howto",
        "body": "1. stop the container\n2. docker compose up -d\n3. verify healthz",
    }
    item.update(overrides)
    return item


async def _deposit_one_entry(client, headers, deposit_project: str, **entry_overrides) -> dict:
    """Deposits a single new knowledge entry and returns its full ack item.
    `deposit_project` is the deposit envelope's `project` (auto-stubbed);
    entry-level `project` (§3, optional per-entry) is a separate field
    passed via `entry_overrides` when a test needs it set.
    """
    body = _deposit_body(project=deposit_project, knowledge=[_knowledge_new(**entry_overrides)])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["knowledge"][0]


async def test_get_library_entry_full_fields(client, db_session):
    headers = await _machine_headers(db_session)
    entry_id = (
        await _deposit_one_entry(
            client,
            headers,
            "brain",
            title="Wipe the volume before a migration change",
            namespace="lessons",
            body="Compose keeps stale schema between builds.",
            tags=["postgres"],
            project="brain",
        )
    )["id"]

    resp = await client.get(f"/v1/library/{entry_id}", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == entry_id
    assert data["title"] == "Wipe the volume before a migration change"
    assert data["namespace"] == "lessons"
    assert data["project"] == "brain"
    assert data["tags"] == ["postgres"]
    assert data["status"] == "active"
    assert data["retire_reason"] is None
    assert data["supersedes"] == []
    assert data["body"] == "Compose keeps stale schema between builds."
    assert data["source"]["tool"] == "claude-code"
    assert data["source"]["session"] == "sess-1"
    assert "machine_id" in data["source"]
    assert data["parents"] == []
    assert data["children"] == []
    assert data["duplicate_hints"] == []


async def test_get_library_entry_shows_supersession_chain(client, db_session):
    headers = await _machine_headers(db_session)
    parent_id = (await _deposit_one_entry(client, headers, "brain", title="Old approach"))["id"]

    body = _deposit_body(project="brain", knowledge=[_knowledge_new(title="New approach", supersedes=[parent_id])])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    child_id = resp.json()["knowledge"][0]["id"]

    parent_resp = await client.get(f"/v1/library/{parent_id}", headers=headers)
    assert parent_resp.status_code == 200
    parent_data = parent_resp.json()
    assert parent_data["status"] == "superseded"
    assert parent_data["children"] == [{"id": child_id, "title": "New approach", "status": "active"}]
    assert parent_data["parents"] == []

    child_resp = await client.get(f"/v1/library/{child_id}", headers=headers)
    assert child_resp.status_code == 200
    child_data = child_resp.json()
    assert child_data["parents"] == [{"id": parent_id, "title": "Old approach", "status": "superseded"}]
    assert child_data["children"] == []


async def test_get_library_entry_shows_duplicate_hints(client, db_session):
    headers = await _machine_headers(db_session)
    original_id = (
        await _deposit_one_entry(
            client,
            headers,
            "brain",
            title="Docker Compose Healthcheck Times Out On Cold Start",
            namespace="lessons",
            body="The healthcheck interval was too aggressive for a cold container start.",
        )
    )["id"]

    dup_body = _deposit_body(
        project="brain",
        knowledge=[
            _knowledge_new(
                title="Docker Compose Healthcheck Times Out On Cold Start",
                namespace="lessons",
                body="A slightly different write-up of the same healthcheck timing lesson.",
            )
        ],
    )
    resp = await client.post("/v1/deposits", json=dup_body, headers=headers)
    dup_id = resp.json()["knowledge"][0]["id"]

    dup_resp = await client.get(f"/v1/library/{dup_id}", headers=headers)
    assert dup_resp.status_code == 200
    hints = dup_resp.json()["duplicate_hints"]
    assert len(hints) == 1
    assert hints[0]["entry_id"] == original_id
    assert hints[0]["title"] == "Docker Compose Healthcheck Times Out On Cold Start"
    assert hints[0]["rank"] > 0


async def test_get_library_entry_404_for_unknown_id(client, db_session):
    headers = await _machine_headers(db_session)
    resp = await client.get("/v1/library/01ARZ3NDEKTSV4RRFFQ69G5FAV", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "entry_not_found"


async def test_get_library_entry_accepts_owner_token(client, db_session):
    machine_headers = await _machine_headers(db_session)
    entry_id = (await _deposit_one_entry(client, machine_headers, "brain"))["id"]

    owner_headers = await _owner_headers(db_session)
    resp = await client.get(f"/v1/library/{entry_id}", headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == entry_id


async def test_get_library_entry_rejects_missing_auth(client, db_session):
    resp = await client.get("/v1/library/01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"
