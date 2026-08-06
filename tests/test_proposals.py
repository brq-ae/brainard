"""Doctrine proposals -- deposit flag, GET /v1/proposals,
POST /v1/proposals/{id}/approve|reject (contracts-v1.md §4)."""

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


def _proposal_knowledge(**overrides) -> dict:
    item = {
        "title": "Proposal: add a rule about squash commits",
        "namespace": "reference",
        "body": "Rationale: keeps history readable. Evidence: last 5 PRs were noisy.",
        "doctrine_proposal": True,
    }
    item.update(overrides)
    return item


def _lesson_knowledge(**overrides) -> dict:
    item = {
        "title": "Ordinary lesson, not a proposal",
        "namespace": "lessons",
        "body": "This is an ordinary lesson entry.",
    }
    item.update(overrides)
    return item


async def _deposit_proposal(client, headers, **overrides) -> str:
    body = _deposit_body(knowledge=[_proposal_knowledge(**overrides)])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["knowledge"][0]["id"]


# --- deposit flow: doctrine_proposal flag ---


async def test_deposit_with_doctrine_proposal_flag_stores_flagged_entry(client, db_session):
    headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, headers)

    from app.models import KnowledgeEntry

    entry = await db_session.get(KnowledgeEntry, proposal_id)
    assert entry.is_doctrine_proposal is True
    assert entry.proposal_decision is None
    assert entry.proposal_decided_at is None


async def test_doctrine_proposal_flag_rejects_non_boolean(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(knowledge=[_proposal_knowledge(doctrine_proposal="yes")])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_knowledge_entry"
    assert "doctrine_proposal" in error["failing_items"][0]["reason"]


# --- exclusion from search default/journal/all, inclusion in scope=proposals ---


async def test_proposal_excluded_from_default_search_included_in_proposals_scope(client, db_session):
    headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(
        client, headers, title="Zenithward Commit Squashing Proposal", body="Rationale text here."
    )

    default_resp = await client.get("/v1/search", params={"q": "Zenithward"}, headers=headers)
    assert default_resp.status_code == 200
    assert proposal_id not in {r["id"] for r in default_resp.json()["results"]}

    all_resp = await client.get("/v1/search", params={"q": "Zenithward", "scope": "all"}, headers=headers)
    assert proposal_id not in {r["id"] for r in all_resp.json()["results"]}

    proposals_resp = await client.get("/v1/search", params={"q": "Zenithward", "scope": "proposals"}, headers=headers)
    assert proposals_resp.status_code == 200
    assert proposal_id in {r["id"] for r in proposals_resp.json()["results"]}


async def test_ordinary_entry_not_returned_by_proposals_scope(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(knowledge=[_lesson_knowledge(title="Quaggamire Ordinary Lesson")])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    entry_id = resp.json()["knowledge"][0]["id"]

    proposals_resp = await client.get("/v1/search", params={"q": "Quaggamire", "scope": "proposals"}, headers=headers)
    assert entry_id not in {r["id"] for r in proposals_resp.json()["results"]}

    default_resp = await client.get("/v1/search", params={"q": "Quaggamire"}, headers=headers)
    assert entry_id in {r["id"] for r in default_resp.json()["results"]}


# --- GET /v1/proposals ---


async def test_list_proposals_owner_only(client, db_session):
    machine_headers = await _machine_headers(db_session)
    resp = await client.get("/v1/proposals", headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_list_proposals_shows_active_proposals_with_full_content(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(
        client, machine_headers, title="Full Content Proposal", body="Full body content here."
    )
    await _deposit_proposal(client, machine_headers, title="Another lesson", body="body")
    body = _deposit_body(knowledge=[_lesson_knowledge()])
    await client.post("/v1/deposits", json=body, headers=machine_headers)  # ordinary, should not appear

    owner_headers = await _owner_headers(db_session)
    resp = await client.get("/v1/proposals", headers=owner_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    match = next(p for p in items if p["id"] == proposal_id)
    assert match["title"] == "Full Content Proposal"
    assert match["body"] == "Full body content here."
    assert match["proposal_decision"] is None


# --- approve / reject ---


async def test_approve_proposal_records_decision(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers)

    owner_headers = await _owner_headers(db_session)
    resp = await client.post(f"/v1/proposals/{proposal_id}/approve", headers=owner_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == proposal_id
    assert data["decision"] == "approved"
    assert data["decided_at"] is not None

    from app.models import KnowledgeEntry

    entry = await db_session.get(KnowledgeEntry, proposal_id)
    assert entry.proposal_decision == "approved"
    assert entry.proposal_decided_at is not None


async def test_reject_proposal_records_decision(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers)

    owner_headers = await _owner_headers(db_session)
    resp = await client.post(f"/v1/proposals/{proposal_id}/reject", headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json()["decision"] == "rejected"


async def test_approve_already_decided_proposal_rejected(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers)

    owner_headers = await _owner_headers(db_session)
    first = await client.post(f"/v1/proposals/{proposal_id}/approve", headers=owner_headers)
    assert first.status_code == 200

    second = await client.post(f"/v1/proposals/{proposal_id}/reject", headers=owner_headers)
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "proposal_already_decided"


async def test_approve_non_proposal_id_rejected(client, db_session):
    machine_headers = await _machine_headers(db_session)
    body = _deposit_body(knowledge=[_lesson_knowledge()])
    resp = await client.post("/v1/deposits", json=body, headers=machine_headers)
    entry_id = resp.json()["knowledge"][0]["id"]

    owner_headers = await _owner_headers(db_session)
    approve_resp = await client.post(f"/v1/proposals/{entry_id}/approve", headers=owner_headers)
    assert approve_resp.status_code == 404
    assert approve_resp.json()["error"]["code"] == "proposal_not_found"


async def test_approve_unknown_id_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.post("/v1/proposals/01ARZ3NDEKTSV4RRFFQ69G5FAV/approve", headers=owner_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "proposal_not_found"


async def test_approve_reject_owner_only(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers)

    resp = await client.post(f"/v1/proposals/{proposal_id}/approve", headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


# --- proposals must stay inert against the live library ---


async def test_proposal_cannot_supersede_library_entry_rejected_atomically(client, db_session):
    headers = await _machine_headers(db_session)
    library_body = _deposit_body(knowledge=[_lesson_knowledge(title="A real library entry")])
    library_resp = await client.post("/v1/deposits", json=library_body, headers=headers)
    assert library_resp.status_code == 200
    library_id = library_resp.json()["knowledge"][0]["id"]

    bad_deposit_id = str(ULID())
    body = _deposit_body(
        deposit_id=bad_deposit_id,
        knowledge=[_proposal_knowledge(supersedes=[library_id])],
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "proposal_cannot_supersede_library"
    assert library_id in error["detail"]
    assert error["failing_items"] == [
        {
            "index": 0,
            "supersedes": [library_id],
            "recovery": "file the proposal without supersedes, or supersede only other proposals",
        }
    ]

    from sqlalchemy import select

    from app.models import Deposit, Flag, KnowledgeEntry

    # Nothing stored: whole deposit rejected atomically.
    assert await db_session.get(Deposit, bad_deposit_id) is None
    assert (
        await db_session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.deposit_id == bad_deposit_id))
    ).all() == []

    # The target library entry is untouched -- still active, never flipped to
    # 'superseded', and no fork flag was ever created against it (fork
    # detection can never fire for a proposal-vs-library parent because the
    # illegitimate supersedes reference never makes it past validation).
    target = await db_session.get(KnowledgeEntry, library_id)
    assert target.status == "active"
    assert (await db_session.scalars(select(Flag).where(Flag.related_entry_id == library_id))).all() == []
    assert (await db_session.scalars(select(Flag).where(Flag.entry_id == library_id))).all() == []


async def test_proposal_can_supersede_another_proposal(client, db_session):
    headers = await _machine_headers(db_session)
    old_proposal_id = await _deposit_proposal(client, headers, title="Old proposal draft")

    body = _deposit_body(
        knowledge=[_proposal_knowledge(title="Revised proposal draft", supersedes=[old_proposal_id])]
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200
    new_proposal_id = resp.json()["knowledge"][0]["id"]

    from app.models import KnowledgeEntry

    old_proposal = await db_session.get(KnowledgeEntry, old_proposal_id)
    new_proposal = await db_session.get(KnowledgeEntry, new_proposal_id)
    assert old_proposal.status == "superseded"
    assert new_proposal.status == "active"
    assert new_proposal.is_doctrine_proposal is True


async def test_retire_combined_with_doctrine_proposal_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    entry_id = await _deposit_proposal(client, headers)

    body = _deposit_body(knowledge=[{"retire": entry_id, "reason": "test", "doctrine_proposal": True}])
    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_knowledge_entry"
    assert "doctrine_proposal" in error["failing_items"][0]["reason"]
    assert "retire" in error["failing_items"][0]["reason"]


# --- proposals excluded from duplicate-hint mechanics, both directions ---


async def test_duplicate_hints_never_link_proposals_and_library_entries(client, db_session):
    headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(
        client,
        headers,
        title="Xanthoglossal Widget Calibration Proposal",
        body="Distinctive rare vocabulary: xanthoglossal calibration procedure for widgets.",
        namespace="reference",
    )

    ordinary_body = _deposit_body(
        knowledge=[
            _lesson_knowledge(
                title="Xanthoglossal Widget Calibration Proposal",
                body="Distinctive rare vocabulary: xanthoglossal calibration procedure for widgets.",
                namespace="reference",
            )
        ]
    )
    resp = await client.post("/v1/deposits", json=ordinary_body, headers=headers)
    assert resp.status_code == 200
    ordinary_id = resp.json()["knowledge"][0]["id"]

    from sqlalchemy import select

    from app.models import Flag

    # Direction 1: the proposal never surfaces as a "possible duplicate" hint
    # on the matching ordinary entry.
    ordinary_hints = (
        await db_session.scalars(select(Flag).where(Flag.type == "duplicate", Flag.entry_id == ordinary_id))
    ).all()
    assert ordinary_hints == []

    # Direction 2: the proposal entry itself never receives duplicate hints
    # pointing at library entries (hint generation is skipped for proposals
    # entirely).
    proposal_hints = (
        await db_session.scalars(select(Flag).where(Flag.type == "duplicate", Flag.entry_id == proposal_id))
    ).all()
    assert proposal_hints == []
