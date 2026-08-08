"""Flags -- GET /v1/flags, POST /v1/flags/{id}/resolve (contracts-v1.md §3,
ADR-0004)."""

from sqlalchemy import select
from ulid import ULID

from app.models import Flag, Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _machine_headers(db_session, name: str = "test-machine") -> tuple[dict, str]:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name=name, token_hash=hash_token(token), status="active")
    db_session.add(machine)
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}, machine.id


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


async def _deposit_one_entry(client, headers, deposit_project: str, **entry_overrides) -> str:
    body = _deposit_body(project=deposit_project, knowledge=[_knowledge_new(**entry_overrides)])
    resp = await client.post("/v1/deposits", json=body, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["knowledge"][0]["id"]


async def _make_duplicate_flag(client, headers) -> str:
    """Deposits two near-identical entries, returns the id of the *newer*
    one -- the entry that carries the resulting duplicate flag as its
    `entry_id`.
    """
    await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Docker Compose Healthcheck Times Out On Cold Start",
        namespace="lessons",
        body="The healthcheck interval was too aggressive for a cold container start.",
    )
    dup_id = await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Docker Compose Healthcheck Times Out On Cold Start",
        namespace="lessons",
        body="A slightly different write-up of the same healthcheck timing lesson.",
    )
    return dup_id


async def _make_fork_flags(client, headers) -> str:
    """Deposits a parent then two children that both supersede it -- returns
    the id of the second (fork-flagged) child. Titles/bodies are
    deliberately unrelated to each other (and to `_make_duplicate_flag`'s
    vocabulary) so this never incidentally also trips a *duplicate* flag --
    this helper is only meant to produce a clean, single fork flag.
    """
    parent_id = await _deposit_one_entry(
        client, headers, "brain", title="Volcano formation overview", namespace="reference", body="magma chamber pressure buildup qqq111"
    )
    await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Marathon training schedule",
        namespace="reference",
        body="weekly mileage progression tables www333",
        supersedes=[parent_id],
    )
    second_child_id = await _deposit_one_entry(
        client,
        headers,
        "brain",
        title="Sourdough starter maintenance",
        namespace="reference",
        body="flour hydration ratio adjustments vvv555",
        supersedes=[parent_id],
    )
    return second_child_id


# --- auth matrix: GET /v1/flags ---


async def test_list_flags_accepts_machine_and_owner_tokens(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    owner_headers = await _owner_headers(db_session)
    await _make_duplicate_flag(client, machine_headers)

    assert (await client.get("/v1/flags", headers=machine_headers)).status_code == 200
    assert (await client.get("/v1/flags", headers=owner_headers)).status_code == 200


async def test_list_flags_no_token_rejected(client, db_session):
    resp = await client.get("/v1/flags")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


async def test_list_flags_bad_token_rejected(client, db_session):
    resp = await client.get("/v1/flags", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


# --- auth matrix: POST /v1/flags/{id}/resolve ---


async def test_resolve_flag_owner_token_rejected(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    owner_headers = await _owner_headers(db_session)
    dup_id = await _make_duplicate_flag(client, machine_headers)
    flag = (await db_session.scalars(select(Flag).where(Flag.entry_id == dup_id))).one()

    resp = await client.post(f"/v1/flags/{flag.id}/resolve", headers=owner_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "machine_token_required"


async def test_resolve_flag_no_token_rejected(client, db_session):
    resp = await client.post(f"/v1/flags/{'x' * 26}/resolve")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


# --- listing shape, filters, pagination ---


async def test_list_flags_row_shape(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    dup_id = await _make_duplicate_flag(client, machine_headers)

    resp = await client.get("/v1/flags", headers=machine_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    row = data["results"][0]
    assert row["type"] == "duplicate"
    assert row["entry_id"] == dup_id
    assert row["related_entry_id"] is not None
    assert row["detail"]["rank"] > 0
    assert row["created_at"] is not None
    assert row["resolved_at"] is None
    assert row["resolved_by"] is None


async def test_list_flags_unresolved_default_excludes_resolved(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    dup_id = await _make_duplicate_flag(client, machine_headers)
    flag = (await db_session.scalars(select(Flag).where(Flag.entry_id == dup_id))).one()

    resolve_resp = await client.post(f"/v1/flags/{flag.id}/resolve", headers=machine_headers)
    assert resolve_resp.status_code == 200

    default_resp = await client.get("/v1/flags", headers=machine_headers)
    assert default_resp.json()["results"] == []

    explicit_unresolved_resp = await client.get("/v1/flags", params={"unresolved": "true"}, headers=machine_headers)
    assert explicit_unresolved_resp.json()["results"] == []

    all_resp = await client.get("/v1/flags", params={"unresolved": "false"}, headers=machine_headers)
    ids = [r["id"] for r in all_resp.json()["results"]]
    assert flag.id in ids


async def test_list_flags_type_filter(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    dup_id = await _make_duplicate_flag(client, machine_headers)
    fork_child_id = await _make_fork_flags(client, machine_headers)

    dup_resp = await client.get("/v1/flags", params={"type": "duplicate"}, headers=machine_headers)
    dup_entry_ids = {r["entry_id"] for r in dup_resp.json()["results"]}
    assert dup_entry_ids == {dup_id}

    fork_resp = await client.get("/v1/flags", params={"type": "fork"}, headers=machine_headers)
    fork_entry_ids = {r["entry_id"] for r in fork_resp.json()["results"]}
    assert fork_entry_ids == {fork_child_id}


async def test_list_flags_invalid_type_rejected(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/flags", params={"type": "not-a-type"}, headers=machine_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_flag_type"


async def test_list_flags_pagination_newest_first_no_duplicates(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    # Three independent duplicate pairs -> three duplicate flags. Each
    # pair's vocabulary is deliberately unrelated to the other pairs' (no
    # shared words) so no *cross*-pair duplicate flags are incidentally
    # created -- only within-pair near-identical titles/bodies should match.
    pairs = [
        ("Apple orchard maintenance notes", "orchard pruning schedule details"),
        ("Bicycle chain lubrication guide", "bicycle chain lubrication steps"),
        ("Chess opening theory overview", "chess pawn structure analysis"),
    ]
    dup_ids = []
    for title, body in pairs:
        await _deposit_one_entry(client, machine_headers, "brain", title=title, namespace="reference", body=body)
        dup_id = await _deposit_one_entry(
            client, machine_headers, "brain", title=title, namespace="reference", body=body + " extra rewritten commentary"
        )
        dup_ids.append(dup_id)

    seen: list[str] = []
    cursor = None
    for _ in range(20):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/v1/flags", params=params, headers=machine_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) <= 1
        seen.extend(r["id"] for r in data["results"])
        cursor = data["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen))
    seen_entry_ids = set()
    for flag_id in seen:
        flag = await db_session.get(Flag, flag_id)
        seen_entry_ids.add(flag.entry_id)
    assert seen_entry_ids == set(dup_ids)


# --- resolve: happy path, idempotency, 404 ---


async def test_resolve_flag_happy_path(client, db_session):
    machine_headers, machine_id = await _machine_headers(db_session)
    dup_id = await _make_duplicate_flag(client, machine_headers)
    flag = (await db_session.scalars(select(Flag).where(Flag.entry_id == dup_id))).one()

    resp = await client.post(f"/v1/flags/{flag.id}/resolve", headers=machine_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == flag.id
    assert data["resolved_by"] == machine_id
    assert data["resolved_at"] is not None
    assert data["already_resolved"] is False

    await db_session.refresh(flag)
    assert flag.resolved_at is not None
    assert flag.resolved_by == machine_id


async def test_resolve_flag_idempotent_returns_existing_resolution(client, db_session):
    machine_headers, machine_id = await _machine_headers(db_session)
    other_headers, other_machine_id = await _machine_headers(db_session, "other-machine")
    dup_id = await _make_duplicate_flag(client, machine_headers)
    flag = (await db_session.scalars(select(Flag).where(Flag.entry_id == dup_id))).one()

    first = await client.post(f"/v1/flags/{flag.id}/resolve", headers=machine_headers)
    assert first.status_code == 200
    first_resolved_at = first.json()["resolved_at"]

    # A second, different machine re-resolving the same flag gets the
    # *original* resolution back unchanged, 200 -- never a conflict, and
    # never overwritten by the second caller.
    second = await client.post(f"/v1/flags/{flag.id}/resolve", headers=other_headers)
    assert second.status_code == 200
    data = second.json()
    assert data["already_resolved"] is True
    assert data["resolved_by"] == machine_id
    assert data["resolved_by"] != other_machine_id
    assert data["resolved_at"] == first_resolved_at


async def test_resolve_unknown_flag_404(client, db_session):
    machine_headers, _ = await _machine_headers(db_session)
    resp = await client.post(f"/v1/flags/{'0' * 26}/resolve", headers=machine_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "flag_not_found"
