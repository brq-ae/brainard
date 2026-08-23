"""Built-in librarian control -- GET /v1/librarian/runs, POST /v1/librarian/run
(ADR-0010 phase 2). The LLM is mocked throughout (no network calls);
POST /v1/librarian/run runs the engine inline, so mocking
app.librarian_engine.chat_completion_json is sufficient -- see
tests/test_librarian_engine.py for the fuller engine-level coverage this
file doesn't re-test (merge/distinct/stale/caps/abort logic).
"""

import asyncio
from datetime import UTC, datetime

from ulid import ULID

import app.librarian_engine as librarian_engine_module
import app.routers.librarian as librarian_router_module
from app.models import LibrarianRun, LlmConfig, Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _machine_headers(db_session) -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _configure_provider(db_session) -> None:
    db_session.add(
        LlmConfig(
            id=str(ULID()),
            version=1,
            base_url="http://fake-provider.invalid/v1",
            model="fake-model",
            api_key=None,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()


def _install_llm_stub(monkeypatch, responses: list) -> None:
    queue = list(responses)

    async def fake_chat_completion_json(effective, *, system_prompt, user_prompt, max_tokens, timeout):
        assert queue, "chat_completion_json called more times than the test scripted"
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(librarian_engine_module, "chat_completion_json", fake_chat_completion_json)


# --- POST /v1/librarian/run ---


async def test_trigger_run_no_provider_returns_skipped(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    _install_llm_stub(monkeypatch, [])

    resp = await client.post("/v1/librarian/run", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "skipped"
    assert data["id"]
    assert data["started_at"] is not None
    assert data["finished_at"] is not None
    assert data["error"] is None


async def test_trigger_run_with_configured_provider_and_nothing_to_do_returns_ok(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    await _configure_provider(db_session)
    _install_llm_stub(monkeypatch, [])  # nothing to process -> zero LLM calls needed

    resp = await client.post("/v1/librarian/run", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["counts"]["duplicate_flags_seen"] == 0
    assert data["counts"]["lessons_seen"] == 0


async def test_trigger_run_machine_token_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    resp = await client.post("/v1/librarian/run", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_trigger_run_missing_auth_rejected(client, db_session):
    resp = await client.post("/v1/librarian/run")
    assert resp.status_code == 401


# --- GET /v1/librarian/runs ---


async def test_get_runs_lists_most_recent_first(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    _install_llm_stub(monkeypatch, [])

    first = await client.post("/v1/librarian/run", headers=headers)
    second = await client.post("/v1/librarian/run", headers=headers)
    assert first.status_code == 200 and second.status_code == 200

    resp = await client.get("/v1/librarian/runs", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 2
    ids = [r["id"] for r in data["results"]]
    assert ids[0] == second.json()["id"]
    assert ids[1] == first.json()["id"]


async def test_get_runs_empty_when_none_have_run(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.get("/v1/librarian/runs", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    assert resp.json()["next_cursor"] is None


async def test_get_runs_machine_token_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    resp = await client.get("/v1/librarian/runs", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_get_runs_missing_auth_rejected(client, db_session):
    resp = await client.get("/v1/librarian/runs")
    assert resp.status_code == 401


async def test_get_runs_pagination(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    _install_llm_stub(monkeypatch, [])
    for _ in range(3):
        assert (await client.post("/v1/librarian/run", headers=headers)).status_code == 200

    page1 = await client.get("/v1/librarian/runs", params={"limit": 2}, headers=headers)
    assert page1.status_code == 200
    data1 = page1.json()
    assert len(data1["results"]) == 2
    assert data1["next_cursor"] is not None

    page2 = await client.get("/v1/librarian/runs", params={"limit": 2, "cursor": data1["next_cursor"]}, headers=headers)
    data2 = page2.json()
    assert len(data2["results"]) == 1
    assert data2["next_cursor"] is None

    seen_ids = {r["id"] for r in data1["results"]} | {r["id"] for r in data2["results"]}
    assert len(seen_ids) == 3


# --- timeout: a run cancelled by the wait_for wrapper still leaves a
# librarian_runs row (independent review advisory H, cheap half) ---


async def test_run_exceeding_timeout_writes_error_row_and_returns_enveloped_error(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    # The real (derived, now potentially minutes-long -- see
    # tests/test_librarian_inline_timeout.py) limit would make this test
    # itself hang; shrink the EFFECTIVE wrapper timeout to a few
    # milliseconds so `asyncio.wait_for` trips almost immediately against a
    # `run_librarian` that deliberately never finishes.
    monkeypatch.setattr(librarian_router_module, "effective_inline_run_timeout_secs", lambda limits=None: 0.05)

    async def hanging_run_librarian(*, limits, run_id):
        await asyncio.sleep(10)  # far longer than the patched timeout

    monkeypatch.setattr(librarian_router_module, "run_librarian", hanging_run_librarian)

    resp = await client.post("/v1/librarian/run", headers=headers)

    # never a bare 500 -- a clean, enveloped error
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "librarian_run_timeout"
    assert "did not finish" in data["error"]["detail"].lower()
    # names the lever the owner can actually pull -- not just "check logs"
    assert "LIBRARIAN_INLINE_RUN_TIMEOUT_SECS" in data["error"]["detail"]

    rows = (await db_session.execute(LibrarianRun.__table__.select())).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "error"
    assert row.error is not None
    assert "did not finish" in row.error.lower() or "cancelled" in row.error.lower()
    assert "LIBRARIAN_INLINE_RUN_TIMEOUT_SECS" in row.error
    assert row.finished_at is not None
    assert row.started_at is not None
