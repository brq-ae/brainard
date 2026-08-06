"""Doctrine -- POST /v1/doctrine/global, POST /v1/doctrine/overlays/{project},
GET /v1/doctrine (contracts-v1.md §4)."""

from ulid import ULID

from app.models import Machine, OwnerToken, Project
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


async def _make_project(db_session, name: str) -> None:
    from datetime import UTC, datetime

    db_session.add(Project(name=name, status="active", created_at=datetime.now(UTC)))
    await db_session.commit()


def _global_body(**overrides) -> dict:
    body = {
        "content": "# Global doctrine\n\nBe careful.",
        "rules": [
            {"id": "G1", "tier": "non_negotiable", "text": "Never assume."},
            {"id": "G2", "tier": "non_negotiable", "text": "Never guess."},
            {"id": "G3", "tier": "default", "text": "Prefer small commits."},
        ],
    }
    body.update(overrides)
    return body


# --- global doctrine ---


async def test_create_global_doctrine_happy_path(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["version"] == 1
    assert data["content"] == "# Global doctrine\n\nBe careful."
    assert len(data["rules"]) == 3
    assert data["created_at"] is not None


async def test_global_doctrine_version_increments_and_is_immutable(client, db_session):
    headers = await _owner_headers(db_session)
    resp1 = await client.post("/v1/doctrine/global", json=_global_body(content="v1 content"), headers=headers)
    assert resp1.status_code == 201
    assert resp1.json()["version"] == 1

    resp2 = await client.post("/v1/doctrine/global", json=_global_body(content="v2 content"), headers=headers)
    assert resp2.status_code == 201
    assert resp2.json()["version"] == 2

    # GET /v1/doctrine reflects only the latest -- but both rows persist (verified via version numbers)
    get_resp = await client.get("/v1/doctrine", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["global"]["version"] == 2
    assert get_resp.json()["global"]["content"] == "v2 content"


async def test_global_doctrine_duplicate_rule_id_rejected(client, db_session):
    headers = await _owner_headers(db_session)
    body = _global_body(
        rules=[
            {"id": "G1", "tier": "non_negotiable", "text": "Never assume."},
            {"id": "G1", "tier": "default", "text": "Duplicate id."},
        ]
    )
    resp = await client.post("/v1/doctrine/global", json=body, headers=headers)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "duplicate_rule_id"
    assert "G1" in error["detail"]


async def test_global_doctrine_invalid_tier_rejected(client, db_session):
    headers = await _owner_headers(db_session)
    body = _global_body(rules=[{"id": "G1", "tier": "sometimes", "text": "bad tier"}])
    resp = await client.post("/v1/doctrine/global", json=body, headers=headers)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_rule_tier"
    assert error["failing_rules"] == [{"id": "G1", "tier": "sometimes"}]


async def test_global_doctrine_owner_only(client, db_session):
    machine_headers = await _machine_headers(db_session)
    resp = await client.post("/v1/doctrine/global", json=_global_body(), headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


# --- overlays ---


async def test_overlay_override_of_default_rule_ok(client, db_session):
    headers = await _owner_headers(db_session)
    await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)
    await _make_project(db_session, "brain")

    resp = await client.post(
        "/v1/doctrine/overlays/brain",
        json={
            "content": "# Brain overlay",
            "overrides": [{"id": "G3", "text": "Actually, prefer large batched commits here."}],
            "additions": [{"id": "P1", "text": "Always run e2e before merging."}],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["project"] == "brain"
    assert data["version"] == 1
    assert data["overrides"] == [{"id": "G3", "text": "Actually, prefer large batched commits here."}]
    assert data["additions"] == [{"id": "P1", "text": "Always run e2e before merging."}]


async def test_overlay_override_of_non_negotiable_rejected_naming_id_and_tier(client, db_session):
    headers = await _owner_headers(db_session)
    await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)
    await _make_project(db_session, "brain")

    resp = await client.post(
        "/v1/doctrine/overlays/brain",
        json={"content": "overlay", "overrides": [{"id": "G1", "text": "Actually assume sometimes."}], "additions": []},
        headers=headers,
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "non_negotiable_override_rejected"
    assert "G1" in error["detail"]
    assert "non_negotiable" in error["detail"]
    assert error["failing_overrides"] == [{"id": "G1", "tier": "non_negotiable"}]


async def test_overlay_override_unknown_id_rejected(client, db_session):
    headers = await _owner_headers(db_session)
    await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)
    await _make_project(db_session, "brain")

    resp = await client.post(
        "/v1/doctrine/overlays/brain",
        json={"content": "overlay", "overrides": [{"id": "G99", "text": "does not exist"}], "additions": []},
        headers=headers,
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "unknown_override_id"
    assert error["unknown_ids"] == ["G99"]


async def test_overlay_addition_collision_with_global_id_rejected(client, db_session):
    headers = await _owner_headers(db_session)
    await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)
    await _make_project(db_session, "brain")

    resp = await client.post(
        "/v1/doctrine/overlays/brain",
        json={"content": "overlay", "overrides": [], "additions": [{"id": "G3", "text": "collides with global"}]},
        headers=headers,
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "addition_id_collision"
    assert error["colliding_ids"] == ["G3"]


async def test_overlay_unknown_project_rejected(client, db_session):
    headers = await _owner_headers(db_session)
    await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)

    resp = await client.post(
        "/v1/doctrine/overlays/never-registered",
        json={"content": "overlay", "overrides": [], "additions": []},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_project"


async def test_overlay_version_increments_per_project(client, db_session):
    headers = await _owner_headers(db_session)
    await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)
    await _make_project(db_session, "brain")

    resp1 = await client.post(
        "/v1/doctrine/overlays/brain", json={"content": "v1", "overrides": [], "additions": []}, headers=headers
    )
    assert resp1.json()["version"] == 1
    resp2 = await client.post(
        "/v1/doctrine/overlays/brain", json={"content": "v2", "overrides": [], "additions": []}, headers=headers
    )
    assert resp2.json()["version"] == 2


async def test_overlay_owner_only(client, db_session):
    machine_headers = await _machine_headers(db_session)
    await _make_project(db_session, "brain")
    resp = await client.post(
        "/v1/doctrine/overlays/brain",
        json={"content": "overlay", "overrides": [], "additions": []},
        headers=machine_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


# --- GET /v1/doctrine ---


async def test_get_doctrine_returns_current_global_and_all_current_overlays(client, db_session):
    headers = await _owner_headers(db_session)
    await client.post("/v1/doctrine/global", json=_global_body(), headers=headers)
    await _make_project(db_session, "brain")
    await _make_project(db_session, "other-proj")

    await client.post(
        "/v1/doctrine/overlays/brain", json={"content": "brain overlay v1", "overrides": [], "additions": []}, headers=headers
    )
    await client.post(
        "/v1/doctrine/overlays/brain", json={"content": "brain overlay v2", "overrides": [], "additions": []}, headers=headers
    )
    await client.post(
        "/v1/doctrine/overlays/other-proj",
        json={"content": "other overlay", "overrides": [], "additions": []},
        headers=headers,
    )

    resp = await client.get("/v1/doctrine", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["global"]["version"] == 1
    overlays_by_project = {o["project"]: o for o in data["overlays"]}
    assert set(overlays_by_project) == {"brain", "other-proj"}
    assert overlays_by_project["brain"]["version"] == 2  # latest only
    assert overlays_by_project["brain"]["content"] == "brain overlay v2"


async def test_get_doctrine_honest_when_no_global_doctrine_exists(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.get("/v1/doctrine", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"global": None, "overlays": []}


async def test_get_doctrine_owner_only(client, db_session):
    machine_headers = await _machine_headers(db_session)
    resp = await client.get("/v1/doctrine", headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"
