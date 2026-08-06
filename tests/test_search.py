"""Full-text search -- GET /v1/search (contracts-v1.md §6 note, §7)."""

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


def _event(seq: int = 1, kind: str = "note", summary: str = "did a thing", **overrides) -> dict:
    e = {"seq": seq, "ts": "2026-08-06T11:59:00Z", "kind": kind, "summary": summary}
    e.update(overrides)
    return e


def _handoff(**overrides) -> dict:
    h = {
        "stands": "phase 3 library endpoint implemented",
        "in_flight": "writing search tests",
        "blocked": "",
        "next_steps": "run e2e verification",
    }
    h.update(overrides)
    return h


async def _deposit_entry(client, headers, **entry_overrides) -> str:
    body = _deposit_body(project="brain", knowledge=[_knowledge_new(**entry_overrides)])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["knowledge"][0]["id"]


def _document(**overrides) -> dict:
    item = {
        "path": "docs/adr/0003-choose-db.md",
        "kind": "adr",
        "title": "Choose the database",
        "content": "We chose Postgres for its full-text search support.",
    }
    item.update(overrides)
    return item


async def _deposit_document(client, headers, **overrides) -> dict:
    body = _deposit_body(project="brain", documents=[_document(**overrides)])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["documents"][0]


async def test_search_finds_library_entry_by_title_and_body_terms(client, db_session):
    headers = await _machine_headers(db_session)
    entry_id = await _deposit_entry(
        client,
        headers,
        title="Zephyrix Frobnicator Setup Guide",
        body="Somewhere deep in this body sits the term qwibblewomp for body-only matching.",
        project="brain",
    )

    title_resp = await client.get("/v1/search", params={"q": "Zephyrix"}, headers=headers)
    assert title_resp.status_code == 200
    title_ids = {r["id"] for r in title_resp.json()["results"]}
    assert entry_id in title_ids

    body_resp = await client.get("/v1/search", params={"q": "qwibblewomp"}, headers=headers)
    assert body_resp.status_code == 200
    body_ids = {r["id"] for r in body_resp.json()["results"]}
    assert entry_id in body_ids
    hit = next(r for r in body_resp.json()["results"] if r["id"] == entry_id)
    assert hit["type"] == "library"
    assert hit["project"] == "brain"


async def test_search_active_only_by_default_include_history_shows_superseded(client, db_session):
    headers = await _machine_headers(db_session)
    parent_id = await _deposit_entry(client, headers, title="Xanthium Legacy Approach")

    supersede_body = _deposit_body(
        project="brain", knowledge=[_knowledge_new(title="Xanthium Replacement Approach", supersedes=[parent_id])]
    )
    resp = await client.post("/v1/deposits", json=supersede_body, headers=headers)
    assert resp.status_code == 200

    default_resp = await client.get("/v1/search", params={"q": "Legacy"}, headers=headers)
    assert default_resp.status_code == 200
    assert parent_id not in {r["id"] for r in default_resp.json()["results"]}

    history_resp = await client.get(
        "/v1/search", params={"q": "Legacy", "include_history": "true"}, headers=headers
    )
    assert history_resp.status_code == 200
    assert parent_id in {r["id"] for r in history_resp.json()["results"]}


async def test_search_handoff_hit_in_default_scope(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        reason="session_end",
        handoff=_handoff(stands="Quixotic milestone reached on the search endpoint"),
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200

    search_resp = await client.get("/v1/search", params={"q": "Quixotic"}, headers=headers)
    assert search_resp.status_code == 200
    results = search_resp.json()["results"]
    assert any(r["type"] == "handoff" for r in results)


async def test_search_event_hit_only_with_journal_or_all_scope(client, db_session):
    headers = await _machine_headers(db_session)
    body = _deposit_body(
        project="brain",
        events=[_event(seq=1, kind="error.hit", summary="Flibbertigibbet error encountered during boot")],
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200

    default_resp = await client.get("/v1/search", params={"q": "Flibbertigibbet"}, headers=headers)
    assert default_resp.status_code == 200
    assert default_resp.json()["results"] == []

    journal_resp = await client.get(
        "/v1/search", params={"q": "Flibbertigibbet", "scope": "journal"}, headers=headers
    )
    assert journal_resp.status_code == 200
    assert any(r["type"] == "event" for r in journal_resp.json()["results"])

    all_resp = await client.get("/v1/search", params={"q": "Flibbertigibbet", "scope": "all"}, headers=headers)
    assert all_resp.status_code == 200
    assert any(r["type"] == "event" for r in all_resp.json()["results"])


async def test_search_pagination_covers_all_results_without_duplicates(client, db_session):
    headers = await _machine_headers(db_session)
    entry_ids = set()
    for suffix in ("Alpha", "Beta", "Gamma"):
        entry_ids.add(await _deposit_entry(client, headers, title=f"Wobblestone {suffix} Report"))

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous upper bound against an infinite loop bug
        params = {"q": "Wobblestone", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/v1/search", params=params, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) <= 1
        seen.extend(r["id"] for r in data["results"])
        cursor = data["next_cursor"]
        if cursor is None:
            break

    assert set(seen) == entry_ids
    assert len(seen) == len(set(seen))  # no duplicates across pages


async def test_search_accepts_machine_and_owner_tokens_rejects_no_auth(client, db_session):
    machine_headers = await _machine_headers(db_session)
    await _deposit_entry(client, machine_headers, title="Tokenscope Acceptance Fixture")

    owner_headers = await _owner_headers(db_session)

    machine_resp = await client.get("/v1/search", params={"q": "Tokenscope"}, headers=machine_headers)
    assert machine_resp.status_code == 200

    owner_resp = await client.get("/v1/search", params={"q": "Tokenscope"}, headers=owner_headers)
    assert owner_resp.status_code == 200

    no_auth_resp = await client.get("/v1/search", params={"q": "Tokenscope"})
    assert no_auth_resp.status_code == 401
    assert no_auth_resp.json()["error"]["code"] == "missing_token"


# --- decisions scope: mirrored ADRs, latest version per path only (contracts-v1.md §5, §7) ---


async def test_decisions_scope_returns_latest_adr_version_only(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_document(
        client, headers, path="docs/adr/0099-quazorbit.md", title="Quazorbit v1", content="original decision text"
    )
    v2 = await _deposit_document(
        client, headers, path="docs/adr/0099-quazorbit.md", title="Quazorbit v2", content="revised decision text"
    )

    resp = await client.get("/v1/search", params={"q": "Quazorbit", "scope": "decisions"}, headers=headers)
    assert resp.status_code == 200
    results = resp.json()["results"]

    ids = {r["id"] for r in results}
    assert v2["id"] in ids
    # the superseded (older) path version is absent from search entirely
    assert len(results) == 1

    hit = results[0]
    assert hit["type"] == "decision"
    assert hit["snippet"] == "Quazorbit v2"  # snippet is the title
    assert hit["path"] == "docs/adr/0099-quazorbit.md"
    assert hit["version"] == 2
    assert hit["project"] == "brain"


async def test_decisions_scope_excludes_doc_kind_mirrors(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_document(
        client, headers, path="docs/README.md", kind="doc", title="Zylofoo Doc", content="doc body text"
    )

    resp = await client.get("/v1/search", params={"q": "Zylofoo", "scope": "decisions"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# --- default scope completeness: library + decisions + handoffs, no events ---


async def test_default_scope_includes_decisions_excludes_events(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_document(
        client, headers, path="docs/adr/0077-wibblefract.md", title="Wibblefract Decision", content="decision body"
    )
    body = _deposit_body(
        project="brain",
        events=[_event(seq=1, kind="note", summary="Wibblefract mentioned in passing during a journal note")],
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200

    default_resp = await client.get("/v1/search", params={"q": "Wibblefract"}, headers=headers)
    assert default_resp.status_code == 200
    results = default_resp.json()["results"]
    types = {r["type"] for r in results}
    assert "decision" in types
    assert "event" not in types


async def test_all_scope_includes_doc_kind_and_events(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_document(
        client, headers, path="docs/README.md", kind="doc", title="Skreevaltix README", content="doc content"
    )
    body = _deposit_body(
        project="brain",
        events=[_event(seq=1, kind="note", summary="Skreevaltix appeared in the journal too")],
    )
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200

    all_resp = await client.get("/v1/search", params={"q": "Skreevaltix", "scope": "all"}, headers=headers)
    assert all_resp.status_code == 200
    types = {r["type"] for r in all_resp.json()["results"]}
    assert "document" in types
    assert "event" in types

    # but doc-kind mirrors are absent from the default scope
    default_resp = await client.get("/v1/search", params={"q": "Skreevaltix"}, headers=headers)
    default_types = {r["type"] for r in default_resp.json()["results"]}
    assert "document" not in default_types
    assert "event" not in default_types
