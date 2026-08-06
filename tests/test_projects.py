"""Project registry -- project_update (deposit envelope + owner PATCH),
GET /v1/projects, GET /v1/projects/{name}, GET /v1/projects/{name}/handoffs
(contracts-v1.md §5, §7)."""

from datetime import UTC, datetime

from ulid import ULID

from app.models import Machine, OwnerToken, Project
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _machine_headers(db_session, name: str = "test-machine") -> tuple[dict, str]:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name=name, token_hash=hash_token(token), status="active")
    db_session.add(machine)
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}, machine.id


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


def _handoff(**overrides) -> dict:
    h = {"stands": "stands text", "in_flight": "in flight text", "blocked": "", "next_steps": "next steps text"}
    h.update(overrides)
    return h


async def _post_global(client, headers) -> dict:
    body = {
        "content": "# Global doctrine",
        "rules": [{"id": "G1", "tier": "non_negotiable", "text": "Never assume."}],
    }
    resp = await client.post("/v1/doctrine/global", json=body, headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


async def _post_overlay(client, headers, project: str) -> dict:
    body = {"content": "overlay content", "overrides": [], "additions": []}
    resp = await client.post(f"/v1/doctrine/overlays/{project}", json=body, headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


# --- project_update via deposit envelope ---


async def test_project_update_via_deposit_happy_path(client, db_session):
    headers, _ = await _machine_headers(db_session)
    body = _deposit_body(
        project="update-proj", project_update={"description": "A cool project", "status": "paused"}
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    project = await db_session.get(Project, "update-proj")
    assert project.description == "A cool project"
    assert project.status == "paused"


async def test_project_update_applies_atomically_to_new_stub(client, db_session):
    headers, _ = await _machine_headers(db_session)
    assert await db_session.get(Project, "brand-new-with-update") is None

    body = _deposit_body(
        project="brand-new-with-update", project_update={"status": "active", "description": "fresh stub"}
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200

    project = await db_session.get(Project, "brand-new-with-update")
    assert project is not None
    assert project.description == "fresh stub"
    assert project.status == "active"


async def test_project_update_partial_leaves_other_field_untouched(client, db_session):
    headers, _ = await _machine_headers(db_session)
    db_session.add(
        Project(name="partial-update-proj", status="active", description="original", created_at=datetime.now(UTC))
    )
    await db_session.commit()

    body = _deposit_body(project="partial-update-proj", project_update={"status": "done"})
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200

    project = await db_session.get(Project, "partial-update-proj")
    assert project.status == "done"
    assert project.description == "original"  # untouched


async def test_project_update_bad_status_rejected(client, db_session):
    headers, _ = await _machine_headers(db_session)
    body = _deposit_body(project="brain", project_update={"status": "not-a-status"})

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_project_update"
    assert "status" in error["detail"]


async def test_project_update_unknown_key_rejected(client, db_session):
    headers, _ = await _machine_headers(db_session)
    body = _deposit_body(project="brain", project_update={"name": "cannot rename this way"})

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_project_update"
    assert "name" in error["detail"]


async def test_project_update_rejection_rejects_whole_deposit(client, db_session):
    headers, _ = await _machine_headers(db_session)
    body = _deposit_body(project="brain", project_update={"status": "bogus"}, events=[])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    from app.models import Deposit

    assert await db_session.get(Deposit, body["deposit_id"]) is None


# --- PATCH /v1/projects/{name} (owner-only) ---


async def test_patch_project_owner_happy_path(client, db_session):
    db_session.add(Project(name="patch-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    owner_headers = await _owner_headers(db_session)

    resp = await client.patch(
        "/v1/projects/patch-proj", json={"description": "Now with a description", "status": "paused"}, headers=owner_headers
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data == {"name": "patch-proj", "description": "Now with a description", "status": "paused"}

    project = await db_session.get(Project, "patch-proj")
    assert project.description == "Now with a description"
    assert project.status == "paused"


async def test_patch_project_machine_token_rejected(client, db_session):
    db_session.add(Project(name="patch-proj-2", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    machine_headers, _ = await _machine_headers(db_session)

    resp = await client.patch("/v1/projects/patch-proj-2", json={"status": "done"}, headers=machine_headers)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_patch_unknown_project_404(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.patch("/v1/projects/does-not-exist", json={"status": "done"}, headers=owner_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_project"


async def test_patch_project_bad_status_rejected(client, db_session):
    db_session.add(Project(name="patch-proj-3", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    owner_headers = await _owner_headers(db_session)

    resp = await client.patch("/v1/projects/patch-proj-3", json={"status": "nope"}, headers=owner_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_project_update"


# --- GET /v1/projects/{name} ---


async def test_get_project_unknown_404(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/projects/does-not-exist", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_project"


async def test_get_project_detail_facts_machines_counts_handoff_overlay(client, db_session):
    machine_a_headers, machine_a_id = await _machine_headers(db_session, "machine-a")
    machine_b_headers, machine_b_id = await _machine_headers(db_session, "machine-b")
    owner_headers = await _owner_headers(db_session)

    await _post_global(client, owner_headers)
    db_session.add(Project(name="detail-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    await _post_overlay(client, owner_headers, "detail-proj")

    # two machines deposit on this project
    resp_a = await client.post(
        "/v1/deposits",
        json=_deposit_body(
            project="detail-proj",
            knowledge=[{"title": "Lesson one", "namespace": "lessons", "body": "body one", "project": "detail-proj"}],
        ),
        headers=machine_a_headers,
    )
    assert resp_a.status_code == 200

    resp_b = await client.post(
        "/v1/deposits",
        json=_deposit_body(
            project="detail-proj",
            documents=[
                {"path": "docs/adr/0001.md", "kind": "adr", "title": "ADR one", "content": "adr content"},
                {"path": "docs/README.md", "kind": "doc", "title": "readme", "content": "readme content"},
            ],
            reason="session_end",
            handoff=_handoff(stands="latest handoff state"),
        ),
        headers=machine_b_headers,
    )
    assert resp_b.status_code == 200

    resp = await client.get("/v1/projects/detail-proj", headers=machine_a_headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["name"] == "detail-proj"
    assert data["status"] == "active"
    assert data["overlay_version"] == 1

    machine_ids = {m["id"] for m in data["machines"]}
    assert machine_ids == {machine_a_id, machine_b_id}

    assert data["latest_handoff"]["stands"] == "latest handoff state"

    assert data["counts"]["active_library_entries"] == 1
    assert data["counts"]["mirrored_documents"] == {"adr": 1, "doc": 1}
    assert data["counts"]["total_deposits"] == 2


async def test_get_project_no_overlay_no_handoff_is_honest(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.post("/v1/deposits", json=_deposit_body(project="bare-proj"), headers=headers)
    assert resp.status_code == 200

    detail = await client.get("/v1/projects/bare-proj", headers=headers)
    assert detail.status_code == 200
    data = detail.json()
    assert data["overlay_version"] is None
    assert data["latest_handoff"] is None
    assert data["counts"]["mirrored_documents"] == {"adr": 0, "doc": 0}


async def test_get_project_accepts_machine_and_owner_tokens(client, db_session):
    db_session.add(Project(name="token-check-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    machine_headers, _ = await _machine_headers(db_session)
    owner_headers = await _owner_headers(db_session)

    assert (await client.get("/v1/projects/token-check-proj", headers=machine_headers)).status_code == 200
    assert (await client.get("/v1/projects/token-check-proj", headers=owner_headers)).status_code == 200


# --- GET /v1/projects (list) ---


async def test_list_projects_pagination_and_activity_ordering(client, db_session):
    headers, _ = await _machine_headers(db_session)

    # project with no deposits at all -- should still be listed, sorted last
    db_session.add(Project(name="zzz-no-activity", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()

    for name in ("proj-older", "proj-newer"):
        resp = await client.post("/v1/deposits", json=_deposit_body(project=name), headers=headers)
        assert resp.status_code == 200

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/v1/projects", params=params, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) <= 1
        seen.extend(r["name"] for r in data["results"])
        cursor = data["next_cursor"]
        if cursor is None:
            break

    assert set(seen) == {"zzz-no-activity", "proj-older", "proj-newer"}
    assert len(seen) == len(set(seen))
    # newest activity first: proj-newer (deposited second) before proj-older,
    # and the never-deposited-on project sorts last regardless of name.
    assert seen.index("proj-newer") < seen.index("proj-older") < seen.index("zzz-no-activity")


# --- GET /v1/projects/{name}/handoffs ---


async def test_list_project_handoffs_unknown_project_404(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/projects/does-not-exist/handoffs", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_project"


async def test_list_project_handoffs_chain_newest_first_paginated(client, db_session):
    headers, _ = await _machine_headers(db_session)

    for stands in ("first handoff", "second handoff", "third handoff"):
        resp = await client.post(
            "/v1/deposits",
            json=_deposit_body(project="handoff-chain-proj", reason="session_end", handoff=_handoff(stands=stands)),
            headers=headers,
        )
        assert resp.status_code == 200

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/v1/projects/handoff-chain-proj/handoffs", params=params, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) <= 1
        seen.extend(r["stands"] for r in data["results"])
        cursor = data["next_cursor"]
        if cursor is None:
            break

    assert seen == ["third handoff", "second handoff", "first handoff"]  # newest first
