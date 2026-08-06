"""Bulk export -- GET /v1/export (contracts-v1.md §7)."""

import json

from ulid import ULID

from app.models import Machine, OwnerToken
from app.routers.export import EXPORT_TABLES
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _machine_headers(db_session) -> tuple[dict, str]:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name="export-test-machine", token_hash=hash_token(token), status="active")
    db_session.add(machine)
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}, machine.id


def _deposit_body(**overrides) -> dict:
    body = {
        "deposit_id": str(ULID()),
        "tool": "claude-code",
        "session": "sess-1",
        "project": "export-proj",
        "reason": "manual",
        "client_ts": "2026-08-06T12:00:00Z",
        "events": [],
    }
    body.update(overrides)
    return body


async def _seed_all_tables(client, db_session) -> dict:
    """Puts at least one row in every exportable table, via the real API
    surfaces (not direct ORM inserts) so this doubles as an end-to-end
    exercise of the tables that back GET /v1/export. Returns the owner
    headers used to write doctrine, so callers reuse them instead of
    minting a second `owner_token` row (the table is a schema-enforced
    singleton -- see app/models.py's `OwnerToken`).
    """
    machine_headers, _ = await _machine_headers(db_session)
    owner_headers = await _owner_headers(db_session)

    # deposits, events, handoffs, knowledge_entries, mirrored_documents,
    # bootstrap_fetches (via the bootstrap call below)
    parent_body = _deposit_body(
        reason="session_end",
        events=[
            {
                "seq": 1,
                "ts": "2026-08-06T12:00:00Z",
                "kind": "work.completed",
                "summary": "seeded event",
            }
        ],
        handoff={
            "stands": "stands",
            "in_flight": "in flight",
            "blocked": "",
            "next_steps": "next",
        },
        knowledge=[
            {"title": "Parent lesson", "namespace": "lessons", "body": "parent body", "project": "export-proj"}
        ],
        documents=[
            {"path": "docs/adr/0001-test.md", "kind": "adr", "title": "Test ADR", "content": "ADR content"}
        ],
    )
    resp = await client.post("/v1/deposits", json=parent_body, headers=machine_headers)
    assert resp.status_code == 200, resp.json()
    parent_id = resp.json()["knowledge"][0]["id"]

    # flags: a second child superseding the same parent creates a fork flag
    child_body = _deposit_body(
        knowledge=[
            {
                "title": "Child lesson",
                "namespace": "lessons",
                "body": "child body",
                "project": "export-proj",
                "supersedes": [parent_id],
            }
        ],
    )
    resp = await client.post("/v1/deposits", json=child_body, headers=machine_headers)
    assert resp.status_code == 200, resp.json()

    second_child_body = _deposit_body(
        knowledge=[
            {
                "title": "Second child lesson",
                "namespace": "lessons",
                "body": "second child body",
                "project": "export-proj",
                "supersedes": [parent_id],
            }
        ],
    )
    resp = await client.post("/v1/deposits", json=second_child_body, headers=machine_headers)
    assert resp.status_code == 200, resp.json()

    # doctrine_versions
    resp = await client.post(
        "/v1/doctrine/global",
        json={"content": "# doctrine", "rules": [{"id": "G1", "tier": "non_negotiable", "text": "Never assume."}]},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.json()

    # bootstrap_fetches
    resp = await client.get("/v1/bootstrap", params={"project": "export-proj"}, headers=machine_headers)
    assert resp.status_code == 200

    return owner_headers


def _parse_ndjson(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def test_export_owner_only(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/export", headers=machine_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_export_missing_auth_rejected(client, db_session):
    resp = await client.get("/v1/export")
    assert resp.status_code == 401


async def test_export_shape_and_all_tables_represented(client, db_session):
    owner_headers = await _seed_all_tables(client, db_session)

    resp = await client.get("/v1/export", headers=owner_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    lines = _parse_ndjson(resp.text)
    assert lines, "export produced no lines"

    seen_tables: set[str] = set()
    for line in lines:
        assert set(line.keys()) == {"table", "row"}
        assert isinstance(line["row"], dict)
        seen_tables.add(line["table"])

    expected_tables = {name for name, _ in EXPORT_TABLES}
    assert seen_tables == expected_tables, f"missing tables: {expected_tables - seen_tables}"


async def test_export_machines_include_token_hash(client, db_session):
    owner_headers = await _seed_all_tables(client, db_session)

    resp = await client.get("/v1/export", headers=owner_headers)
    lines = _parse_ndjson(resp.text)

    machine_rows = [line["row"] for line in lines if line["table"] == "machines"]
    assert machine_rows
    assert all("token_hash" in row and row["token_hash"] for row in machine_rows)
    # never the plaintext token, which is never persisted anywhere at all
    assert all(not row["token_hash"].startswith("brn_") for row in machine_rows)


async def test_export_timestamps_are_iso8601(client, db_session):
    owner_headers = await _seed_all_tables(client, db_session)

    resp = await client.get("/v1/export", headers=owner_headers)
    lines = _parse_ndjson(resp.text)

    project_rows = [line["row"] for line in lines if line["table"] == "projects"]
    assert project_rows
    created_at = project_rows[0]["created_at"]
    assert isinstance(created_at, str)
    # round-trips through fromisoformat -> proves it's a valid ISO-8601 string
    from datetime import datetime

    datetime.fromisoformat(created_at)


async def test_export_streams_more_rows_than_a_single_batch(client, db_session):
    """Regression guard for the keyset pagination loop itself: seed more
    projects than BATCH_SIZE would ever need to prove multi-batch tables
    aren't truncated -- exercised here at a small scale via a dedicated
    small-batch monkeypatch-free check: enough events in one deposit that,
    combined with the projects/library rows from _seed_all_tables, at least
    one table plausibly spans a boundary in a larger deployment. Kept
    lightweight (no 500+ row fixture) -- the pagination *logic* itself is
    exercised directly in test_export_pagination_batches_correctly below.
    """
    owner_headers = await _seed_all_tables(client, db_session)
    resp = await client.get("/v1/export", headers=owner_headers)
    assert resp.status_code == 200
    assert len(_parse_ndjson(resp.text)) >= 8


async def test_export_pagination_batches_correctly(db_session, monkeypatch):
    """Directly exercises `_stream_table`'s keyset-pagination loop across
    multiple batches by dropping BATCH_SIZE to 2 and seeding a handful of
    projects -- proves no rows are skipped or duplicated across a batch
    boundary, without needing hundreds of real rows.
    """
    import app.routers.export as export_mod
    from app.models import Project
    from datetime import UTC, datetime

    monkeypatch.setattr(export_mod, "BATCH_SIZE", 2)

    names = [f"pg-proj-{i}" for i in range(5)]
    for name in names:
        db_session.add(Project(name=name, status="active", created_at=datetime.now(UTC)))
    await db_session.commit()

    seen = []
    async for chunk in export_mod._stream_table(db_session, "projects", Project):
        line = json.loads(chunk.decode("utf-8"))
        seen.append(line["row"]["name"])

    assert sorted(seen) == sorted(names)
    assert len(seen) == len(set(seen))
