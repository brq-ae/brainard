"""GET /v1/events -- filtered journal read (contracts-v1.md §2, §7; phase 8
librarian support)."""

from ulid import ULID

from app.models import Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _machine_headers(db_session, name: str = "test-machine") -> dict:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name=name, token_hash=hash_token(token), status="active")
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


def _event(seq: int = 1, kind: str = "note", summary: str = "did a thing", ts: str = "2026-08-06T11:59:00Z", **overrides) -> dict:
    e = {"seq": seq, "ts": ts, "kind": kind, "summary": summary}
    e.update(overrides)
    return e


async def _deposit_events(client, headers, project: str, events: list[dict]) -> None:
    resp = await client.post("/v1/deposits", json=_deposit_body(project=project, events=events), headers=headers)
    assert resp.status_code == 200, resp.json()


# --- auth matrix ---


async def test_list_events_accepts_machine_and_owner_tokens(client, db_session):
    machine_headers = await _machine_headers(db_session)
    owner_headers = await _owner_headers(db_session)
    await _deposit_events(client, machine_headers, "brain", [_event()])

    assert (await client.get("/v1/events", headers=machine_headers)).status_code == 200
    assert (await client.get("/v1/events", headers=owner_headers)).status_code == 200


async def test_list_events_no_token_rejected(client, db_session):
    resp = await client.get("/v1/events")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


async def test_list_events_bad_token_rejected(client, db_session):
    resp = await client.get("/v1/events", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


# --- row shape: payload omitted by default, tags present ---


async def test_list_events_row_shape_and_payload_omitted_by_default(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_events(
        client,
        headers,
        "brain",
        [_event(seq=1, kind="lesson.candidate", summary="worth writing up", tags=["t1", "t2"], payload={"secret": "stuff"})],
    )

    resp = await client.get("/v1/events", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    row = data["results"][0]
    assert row["kind"] == "lesson.candidate"
    assert row["summary"] == "worth writing up"
    assert row["tags"] == ["t1", "t2"]
    assert row["project"] == "brain"
    assert row["seq"] == 1
    assert "deposit_id" in row and row["deposit_id"]
    assert "id" in row and row["id"]
    assert "ts" in row
    assert row["payload"] is None  # not included unless include_payload=true


async def test_list_events_include_payload_true(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_events(client, headers, "brain", [_event(payload={"k": "v"})])

    resp = await client.get("/v1/events", params={"include_payload": "true"}, headers=headers)
    assert resp.status_code == 200
    row = resp.json()["results"][0]
    assert row["payload"] == {"k": "v"}


# --- filters ---


async def test_list_events_kind_filter(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_events(
        client,
        headers,
        "brain",
        [
            _event(seq=1, kind="note", summary="a note"),
            _event(seq=2, kind="error.hit", summary="an error"),
        ],
    )

    resp = await client.get("/v1/events", params={"kind": "error.hit"}, headers=headers)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["kind"] == "error.hit"


async def test_list_events_unknown_kind_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    resp = await client.get("/v1/events", params={"kind": "not-a-real-kind"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unknown_event_kind"


async def test_list_events_project_filter(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_events(client, headers, "project-a", [_event(summary="in project a")])
    await _deposit_events(client, headers, "project-b", [_event(summary="in project b")])

    resp = await client.get("/v1/events", params={"project": "project-a"}, headers=headers)
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["project"] == "project-a"


async def test_list_events_since_filter(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_events(
        client,
        headers,
        "brain",
        [
            _event(seq=1, summary="older event", ts="2026-08-01T00:00:00Z"),
            _event(seq=2, summary="newer event", ts="2026-08-06T00:00:00Z"),
        ],
    )

    resp = await client.get("/v1/events", params={"since": "2026-08-05T00:00:00Z"}, headers=headers)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["summary"] == "newer event"


# --- ordering + pagination ---


async def test_list_events_newest_first_and_pagination(client, db_session):
    headers = await _machine_headers(db_session)
    await _deposit_events(
        client,
        headers,
        "brain",
        [
            _event(seq=1, summary="first", ts="2026-08-01T00:00:00Z"),
            _event(seq=2, summary="second", ts="2026-08-02T00:00:00Z"),
            _event(seq=3, summary="third", ts="2026-08-03T00:00:00Z"),
        ],
    )

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/v1/events", params=params, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) <= 1
        seen.extend(r["summary"] for r in data["results"])
        cursor = data["next_cursor"]
        if cursor is None:
            break

    assert seen == ["third", "second", "first"]  # newest first


async def test_list_events_default_and_max_limit(client, db_session):
    headers = await _machine_headers(db_session)

    # limit defaults to 50
    resp_default = await client.get("/v1/events", headers=headers)
    assert resp_default.status_code == 200

    # limit is capped at 200
    resp_over = await client.get("/v1/events", params={"limit": 201}, headers=headers)
    assert resp_over.status_code == 422

    resp_at_cap = await client.get("/v1/events", params={"limit": 200}, headers=headers)
    assert resp_at_cap.status_code == 200
