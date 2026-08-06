"""UI admin area: machine mint (token shown once) + revoke, proposal
approve/reject (phase 6). Exercises the same shared logic
(app/machines.py, app/proposals.py) as the API endpoints -- see
tests/test_machines.py and tests/test_proposals.py for the API-side
equivalents.
"""

import re

from ulid import ULID

from app.models import Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _login(client, db_session) -> str:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    await client.post("/ui/login", data={"token": token})
    return token


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf_token hidden field not found in page"
    return m.group(1)


async def _machine_headers(db_session) -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


# --- machines ---


async def test_ui_mint_machine_shows_token_once(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/admin/machines", data={"name": "ui-minted-machine", "csrf_token": csrf})
    assert resp.status_code == 201
    assert "brn_" in resp.text
    assert "ui-minted-machine" in resp.text

    token_match = re.search(r"brn_[A-Za-z0-9_-]+", resp.text)
    assert token_match, "minted token not found in response"
    minted_token = token_match.group(0)

    # the minted token actually authenticates against the API
    api_resp = await client.get("/v1/machines", headers={"Authorization": f"Bearer {minted_token}"})
    # a machine token is not owner-scoped -- 403, but that proves it's a
    # real, valid, *authenticating* token (401 would mean it failed to parse)
    assert api_resp.status_code == 403
    assert api_resp.json()["error"]["code"] == "owner_token_required"

    # the token is never shown again on a plain re-list of the page
    listing = await client.get("/ui/admin/machines")
    assert minted_token not in listing.text
    assert "ui-minted-machine" in listing.text


async def test_ui_revoke_machine_works(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    mint_resp = await client.post("/ui/admin/machines", data={"name": "to-be-revoked", "csrf_token": csrf})
    assert mint_resp.status_code == 201
    machine = (await db_session.execute(Machine.__table__.select().where(Machine.name == "to-be-revoked"))).first()
    machine_id = machine.id

    resp = await client.post(f"/ui/admin/machines/{machine_id}/revoke", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/admin/machines"

    listing = await client.get("/ui/admin/machines")
    assert "revoked" in listing.text.lower()

    from app.models import Machine as MachineModel

    refreshed = await db_session.get(MachineModel, machine_id)
    assert refreshed.status == "revoked"


async def test_ui_revoke_unknown_machine_404(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/admin/machines/01UNKNOWNIDXXXXXXXXXXXXXX/revoke", data={"csrf_token": csrf})
    assert resp.status_code == 404


# --- proposals ---


async def _deposit_proposal(client, headers, title="A proposal", body="rationale") -> str:
    resp = await client.post(
        "/v1/deposits",
        json={
            "deposit_id": str(ULID()),
            "tool": "t",
            "session": "s",
            "project": "brain",
            "reason": "manual",
            "client_ts": "2026-08-06T12:00:00Z",
            "events": [],
            "knowledge": [{"title": title, "namespace": "reference", "body": body, "doctrine_proposal": True}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["knowledge"][0]["id"]


async def test_ui_proposal_approve_records_decision(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers, title="Approve me")
    await _login(client, db_session)

    page = await client.get("/ui/admin/proposals")
    assert page.status_code == 200
    assert "Approve me" in page.text
    csrf = _extract_csrf(page.text)

    resp = await client.post(f"/ui/admin/proposals/{proposal_id}/approve", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303

    from app.models import KnowledgeEntry

    entry = await db_session.get(KnowledgeEntry, proposal_id)
    assert entry.proposal_decision == "approved"
    assert entry.proposal_decided_at is not None

    after = await client.get("/ui/admin/proposals")
    assert "approved" in after.text.lower()


async def test_ui_proposal_reject_records_decision(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers, title="Reject me")
    await _login(client, db_session)

    page = await client.get("/ui/admin/proposals")
    csrf = _extract_csrf(page.text)

    resp = await client.post(f"/ui/admin/proposals/{proposal_id}/reject", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303

    from app.models import KnowledgeEntry

    entry = await db_session.get(KnowledgeEntry, proposal_id)
    assert entry.proposal_decision == "rejected"


async def test_ui_proposal_already_decided_rejected(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers)
    await _login(client, db_session)

    page = await client.get("/ui/admin/proposals")
    csrf = _extract_csrf(page.text)

    first = await client.post(f"/ui/admin/proposals/{proposal_id}/approve", data={"csrf_token": csrf})
    assert first.status_code == 303

    second = await client.post(f"/ui/admin/proposals/{proposal_id}/reject", data={"csrf_token": csrf})
    assert second.status_code == 422
