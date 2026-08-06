"""Auth: owner vs machine vs revoked vs garbage tokens."""

import pytest
from ulid import ULID

from app.auth import authenticate
from app.errors import ApiError
from app.models import Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _insert_owner_token(db) -> str:
    token = generate_owner_token()
    db.add(OwnerToken(token_hash=hash_token(token)))
    await db.commit()
    return token


async def _insert_machine(db, status: str = "active") -> tuple[str, Machine]:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name="test-machine", token_hash=hash_token(token), status=status)
    db.add(machine)
    await db.commit()
    return token, machine


# --- authenticate() unit-level ---


async def test_authenticate_recognizes_owner_token(db_session):
    token = await _insert_owner_token(db_session)
    principal = await authenticate(token, db_session)
    assert principal.kind == "owner"


async def test_authenticate_recognizes_machine_token_and_updates_last_seen(db_session):
    token, machine = await _insert_machine(db_session)
    assert machine.last_seen is None

    principal = await authenticate(token, db_session)

    assert principal.kind == "machine"
    assert principal.machine is not None
    assert principal.machine.id == machine.id
    assert principal.machine.last_seen is not None


async def test_authenticate_rejects_revoked_machine(db_session):
    token, _ = await _insert_machine(db_session, status="revoked")
    with pytest.raises(ApiError) as exc_info:
        await authenticate(token, db_session)
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "token_revoked"


async def test_authenticate_rejects_garbage_token(db_session):
    with pytest.raises(ApiError) as exc_info:
        await authenticate("not-a-real-token", db_session)
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "invalid_token"


# --- endpoint-level: header handling ---


async def test_endpoint_rejects_missing_authorization_header(client):
    resp = await client.get("/v1/machines")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


async def test_endpoint_rejects_malformed_authorization_header(client):
    resp = await client.get("/v1/machines", headers={"Authorization": "Basic abc123"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "malformed_authorization"


async def test_endpoint_rejects_garbage_bearer_token(client):
    resp = await client.get("/v1/machines", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_endpoint_rejects_machine_token_on_owner_route(client, db_session):
    token, _ = await _insert_machine(db_session)
    resp = await client.get("/v1/machines", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_endpoint_rejects_revoked_machine_token(client, db_session):
    token, _ = await _insert_machine(db_session, status="revoked")
    resp = await client.get("/v1/machines", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "token_revoked"
