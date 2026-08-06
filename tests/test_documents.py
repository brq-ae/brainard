"""Mirrored ADRs/docs -- documents[] deposit compartment (contracts-v1.md §5)."""

from sqlalchemy import select
from ulid import ULID

from app.models import Deposit, Machine, MirroredDocument, OwnerToken
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


def _document(**overrides) -> dict:
    item = {
        "path": "docs/adr/0003-choose-db.md",
        "kind": "adr",
        "title": "Choose the database",
        "content": "# Choose the database\n\nWe chose Postgres.",
    }
    item.update(overrides)
    return item


# --- happy path ---


async def test_document_mirror_happy_path_creates_version_1(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", documents=[_document()])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"]["documents"] == 1
    ack = data["documents"][0]
    assert ack["path"] == "docs/adr/0003-choose-db.md"
    assert ack["version"] == 1

    doc = await db_session.get(MirroredDocument, ack["id"])
    assert doc is not None
    assert doc.project == "brain"
    assert doc.kind == "adr"
    assert doc.title == "Choose the database"
    assert doc.content == "# Choose the database\n\nWe chose Postgres."
    assert doc.version == 1
    assert doc.deposit_id == body["deposit_id"]
    assert doc.machine_id is not None
    assert doc.created_at is not None


async def test_document_doc_kind_accepted(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        documents=[_document(path="docs/README.md", kind="doc", title="Project README", content="# README")],
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200
    doc_id = resp.json()["documents"][0]["id"]
    doc = await db_session.get(MirroredDocument, doc_id)
    assert doc.kind == "doc"


# --- versioning: redeposit + same-deposit duplicates ---


async def test_redeposit_same_path_creates_next_version_never_overwrites(client, db_session):
    headers = await _machine_headers(db_session)

    first = _deposit_body(project="brain", documents=[_document(content="v1 content")])
    resp1 = await client.post("/v1/deposits", json=first, headers=headers)
    assert resp1.status_code == 200
    v1_id = resp1.json()["documents"][0]["id"]
    assert resp1.json()["documents"][0]["version"] == 1

    second = _deposit_body(project="brain", documents=[_document(content="v2 content, updated")])
    resp2 = await client.post("/v1/deposits", json=second, headers=headers)
    assert resp2.status_code == 200
    v2_id = resp2.json()["documents"][0]["id"]
    assert resp2.json()["documents"][0]["version"] == 2
    assert v2_id != v1_id

    # supersede-never-erase: v1 row is untouched, still readable
    v1_row = await db_session.get(MirroredDocument, v1_id)
    assert v1_row.content == "v1 content"
    assert v1_row.version == 1

    v2_row = await db_session.get(MirroredDocument, v2_id)
    assert v2_row.content == "v2 content, updated"
    assert v2_row.version == 2

    all_versions = (
        await db_session.scalars(
            select(MirroredDocument).where(MirroredDocument.path == "docs/adr/0003-choose-db.md")
        )
    ).all()
    assert len(all_versions) == 2


async def test_same_deposit_duplicate_paths_get_sequential_deterministic_versions(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        documents=[
            _document(content="first in array order"),
            _document(content="second in array order"),
            _document(content="third in array order"),
        ],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 200
    ack = resp.json()["documents"]
    assert [item["version"] for item in ack] == [1, 2, 3]

    rows_by_id = {item["id"]: item for item in ack}
    for item in ack:
        row = await db_session.get(MirroredDocument, item["id"])
        assert row.version == item["version"]
    # array order preserved: first item's content is version 1, etc.
    v1 = await db_session.get(MirroredDocument, ack[0]["id"])
    v2 = await db_session.get(MirroredDocument, ack[1]["id"])
    v3 = await db_session.get(MirroredDocument, ack[2]["id"])
    assert v1.content == "first in array order"
    assert v2.content == "second in array order"
    assert v3.content == "third in array order"


async def test_different_paths_each_start_at_version_1(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        documents=[
            _document(path="docs/adr/0001-a.md"),
            _document(path="docs/adr/0002-b.md"),
        ],
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200
    ack = resp.json()["documents"]
    assert ack[0]["version"] == 1
    assert ack[1]["version"] == 1


# --- validation rejections ---


async def test_document_bad_kind_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", documents=[_document(kind="not-a-kind")])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_document_entry"
    assert error["failing_items"][0]["index"] == 0
    assert "kind" in error["failing_items"][0]["reason"]
    assert await db_session.get(Deposit, body["deposit_id"]) is None


async def test_document_missing_title_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", documents=[_document(title="")])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_document_entry"
    assert "title" in error["failing_items"][0]["reason"]


async def test_document_missing_path_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", documents=[_document(path="")])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_document_entry"
    assert "path" in error["failing_items"][0]["reason"]


async def test_document_oversize_content_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", documents=[_document(content="x" * (1024 * 1024 + 1))])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_document_entry"
    assert "byte cap" in error["failing_items"][0]["reason"]


async def test_document_unknown_keys_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(project="brain", documents=[_document(extra_field="not recognized")])

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_document_entry"
    assert "extra_field" in error["failing_items"][0]["reason"]


# --- atomicity ---


async def test_document_atomicity_bad_batchmate_rejects_whole_deposit(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        documents=[_document(path="docs/adr/0010-good.md"), _document(kind="bogus")],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    assert await db_session.get(Deposit, body["deposit_id"]) is None
    assert (
        await db_session.scalars(select(MirroredDocument).where(MirroredDocument.deposit_id == body["deposit_id"]))
    ).all() == []


async def test_document_bad_knowledge_batchmate_rejects_documents_too(client, db_session):
    """documents[] and knowledge[] share one atomic deposit -- a failure in
    either compartment rejects both.
    """
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        documents=[_document()],
        knowledge=[{"title": "bad", "namespace": "not-a-shelf", "body": "x"}],
    )

    resp = await client.post("/v1/deposits", json=body, headers=headers)

    assert resp.status_code == 422
    assert await db_session.get(Deposit, body["deposit_id"]) is None
    assert (
        await db_session.scalars(select(MirroredDocument).where(MirroredDocument.deposit_id == body["deposit_id"]))
    ).all() == []


# --- idempotency ---


async def test_replayed_deposit_returns_identical_document_ack(client, db_session):
    headers = await _machine_headers(db_session)
    deposit_id = str(ULID())
    body = _deposit_body(deposit_id=deposit_id, project="brain", documents=[_document()])

    resp1 = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp1.status_code == 200
    ack1 = resp1.json()

    resp2 = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp2.status_code == 200
    ack2 = resp2.json()

    assert ack2["replayed"] is True
    assert ack2["documents"] == ack1["documents"]
    assert ack2["counts"]["documents"] == ack1["counts"]["documents"] == 1

    all_versions = (
        await db_session.scalars(select(MirroredDocument).where(MirroredDocument.deposit_id == deposit_id))
    ).all()
    assert len(all_versions) == 1  # replay never re-applies, never re-versions
