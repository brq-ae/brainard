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
