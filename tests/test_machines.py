"""Machine mint / list / revoke flow."""

import pytest

from app.auth import authenticate
from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.models import OwnerToken
from app.security import generate_owner_token, hash_token


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def test_mint_list_revoke_flow(client, db_session):
    headers = await _owner_headers(db_session)

    # mint: plaintext token returned exactly once
    resp = await client.post("/v1/machines", json={"name": "test-box"}, headers=headers)
    assert resp.status_code == 201
    minted = resp.json()
    assert minted["name"] == "test-box"
    assert minted["token"].startswith("brn_")
    machine_id = minted["id"]
    machine_token = minted["token"]

    # list: never leaks token material
    resp = await client.get("/v1/machines", headers=headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    listed = items[0]
    assert listed["id"] == machine_id
    assert listed["name"] == "test-box"
    assert listed["status"] == "active"
    assert listed["last_seen"] is None
    assert "token" not in listed
    assert "token_hash" not in listed

    # the minted token actually authenticates as that machine
    principal = await authenticate(machine_token, db_session)
    assert principal.kind == "machine"
    assert principal.machine.id == machine_id

    # revoke
    resp = await client.post(f"/v1/machines/{machine_id}/revoke", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"id": machine_id, "status": "revoked"}

    # revoke is idempotent
    resp = await client.post(f"/v1/machines/{machine_id}/revoke", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"id": machine_id, "status": "revoked"}

    # revoked token no longer authenticates (fresh session -- db_session's
    # identity map still holds the pre-revoke object)
    async with AsyncSessionLocal() as fresh_session:
        with pytest.raises(ApiError) as exc_info:
            await authenticate(machine_token, fresh_session)
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "token_revoked"

    # list reflects revoked status
    resp = await client.get("/v1/machines", headers=headers)
    assert resp.json()[0]["status"] == "revoked"


async def test_create_machine_rejects_blank_name(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/machines", json={"name": ""}, headers=headers)
    assert resp.status_code == 422


async def test_revoke_unknown_machine_returns_404(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/machines/01UNKNOWNIDXXXXXXXXXXXXXX/revoke", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "machine_not_found"


# --- roles + default_project (feature: machine roles + prebuilt onboarding prompt) ---


async def test_create_machine_defaults_to_solo_role(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/machines", json={"name": "default-role-box"}, headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "solo"
    assert body["default_project"] is None


async def test_create_machine_with_role_and_default_project_persists(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/machines",
        json={"name": "commander-box", "role": "commander", "default_project": "my-project"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "commander"
    assert body["default_project"] == "my-project"
    machine_id = body["id"]

    # persists -- reflected on GET /v1/machines
    listed = (await client.get("/v1/machines", headers=headers)).json()
    entry = next(m for m in listed if m["id"] == machine_id)
    assert entry["role"] == "commander"
    assert entry["default_project"] == "my-project"


async def test_create_machine_rejects_bad_role(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/machines", json={"name": "bad-role-box", "role": "overlord"}, headers=headers)
    assert resp.status_code == 422


async def test_create_machine_rejects_empty_default_project(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/machines", json={"name": "empty-project-box", "default_project": ""}, headers=headers
    )
    assert resp.status_code == 422


async def test_patch_machine_updates_role_and_default_project(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "patchable-box"}, headers=headers)).json()

    resp = await client.patch(
        f"/v1/machines/{minted['id']}",
        json={"role": "builder", "default_project": "other-project"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "builder"
    assert body["default_project"] == "other-project"

    # partial update -- omitting a field leaves it untouched
    resp2 = await client.patch(f"/v1/machines/{minted['id']}", json={"role": "solo"}, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["role"] == "solo"
    assert resp2.json()["default_project"] == "other-project"

    # explicit null clears default_project
    resp3 = await client.patch(
        f"/v1/machines/{minted['id']}", json={"default_project": None}, headers=headers
    )
    assert resp3.status_code == 200
    assert resp3.json()["default_project"] is None


async def test_patch_machine_rejects_empty_default_project(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "patch-empty-project-box"}, headers=headers)).json()
    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"default_project": ""}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_default_project"


async def test_patch_machine_rejects_overlong_default_project(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "patch-long-project-box"}, headers=headers)).json()
    resp = await client.patch(
        f"/v1/machines/{minted['id']}", json={"default_project": "x" * 256}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_default_project"


async def test_patch_machine_rejects_bad_role(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "patch-bad-role-box"}, headers=headers)).json()
    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"role": "overlord"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_role"


async def test_patch_machine_rejects_unknown_field(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "patch-unknown-field-box"}, headers=headers)).json()
    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"nickname": "renamed"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_machine_update"


# --- rename (feature: change a machine's name via PATCH) ---


async def test_patch_machine_renames_and_returns_updated_name(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "old-name-box"}, headers=headers)).json()

    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"name": "new-name-box"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "new-name-box"

    # persists -- reflected on GET /v1/machines
    listed = (await client.get("/v1/machines", headers=headers)).json()
    entry = next(m for m in listed if m["id"] == minted["id"])
    assert entry["name"] == "new-name-box"


async def test_patch_machine_rename_alongside_role_and_default_project(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "combo-box"}, headers=headers)).json()

    resp = await client.patch(
        f"/v1/machines/{minted['id']}",
        json={"name": "combo-box-renamed", "role": "builder", "default_project": "combo-project"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "combo-box-renamed"
    assert body["role"] == "builder"
    assert body["default_project"] == "combo-project"


async def test_patch_machine_rejects_blank_name(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "blank-name-box"}, headers=headers)).json()
    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"name": ""}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_name"


async def test_patch_machine_rejects_whitespace_only_name(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "whitespace-name-box"}, headers=headers)).json()
    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"name": "   "}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_name"


async def test_patch_machine_rejects_overlong_name(client, db_session):
    headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "long-name-box"}, headers=headers)).json()
    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"name": "x" * 256}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_name"


async def test_patch_machine_rename_requires_owner_token(client, db_session):
    owner_headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "rename-owner-only-box"}, headers=owner_headers)).json()
    machine_headers = {"Authorization": f"Bearer {minted['token']}"}

    resp = await client.patch(f"/v1/machines/{minted['id']}", json={"name": "should-fail"}, headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_patch_unknown_machine_returns_404(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.patch(
        "/v1/machines/01UNKNOWNIDXXXXXXXXXXXXXX", json={"role": "builder"}, headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "machine_not_found"


async def test_machine_token_rejected_on_create_and_patch(client, db_session):
    owner_headers = await _owner_headers(db_session)
    minted = (await client.post("/v1/machines", json={"name": "requester-box"}, headers=owner_headers)).json()
    machine_headers = {"Authorization": f"Bearer {minted['token']}"}

    resp = await client.post("/v1/machines", json={"name": "should-fail"}, headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"

    resp2 = await client.patch(f"/v1/machines/{minted['id']}", json={"role": "builder"}, headers=machine_headers)
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "owner_token_required"
