"""Bootstrap -- GET /v1/bootstrap?project=X (contracts-v1.md §6)."""

from datetime import UTC, datetime

from sqlalchemy import select
from ulid import ULID

from app.models import BootstrapFetch, Machine, OwnerToken, Project
from app.security import generate_machine_token, generate_owner_token, hash_token
from app.routers.bootstrap import (
    SIZE_BUDGET_BYTES,
    TEMPLATES,
    _apply_size_budget,
    _json_bytes,
    _markdown_bytes,
    _operating_instructions_markdown,
)


async def _machine_headers(db_session) -> tuple[dict, str]:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name="test-machine", token_hash=hash_token(token), status="active")
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


def _global_body(**overrides) -> dict:
    body = {
        "content": "# Global doctrine\n\nBe careful out there.",
        "rules": [
            {"id": "G1", "tier": "non_negotiable", "text": "Never assume."},
            {"id": "G2", "tier": "non_negotiable", "text": "Never guess."},
            {"id": "G3", "tier": "default", "text": "Prefer small commits."},
        ],
    }
    body.update(overrides)
    return body


async def _post_global(client, headers, **overrides) -> dict:
    resp = await client.post("/v1/doctrine/global", json=_global_body(**overrides), headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


async def _post_overlay(client, headers, project: str, **body) -> dict:
    payload = {"content": "overlay content", "overrides": [], "additions": []}
    payload.update(body)
    resp = await client.post(f"/v1/doctrine/overlays/{project}", json=payload, headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()


# --- format variants + machine-token-only ---


async def test_bootstrap_markdown_is_default_and_json_via_format_param(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain"}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.startswith("# Bootstrap")

    json_resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    assert json_resp.status_code == 200
    assert json_resp.headers["content-type"].startswith("application/json")
    data = json_resp.json()
    assert "doctrine" in data and "project" in data and "operating_instructions" in data
    assert "templates" in data and "lessons_digest" in data


async def test_bootstrap_owner_token_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain"}, headers=owner_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "machine_token_required"


async def test_bootstrap_missing_auth_rejected(client, db_session):
    resp = await client.get("/v1/bootstrap", params={"project": "brain"})
    assert resp.status_code == 401


# --- five sections present ---


async def test_bootstrap_json_contains_all_five_sections(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    data = resp.json()
    assert data["doctrine"] is not None
    assert data["project"]["name"] == "brain"
    assert isinstance(data["operating_instructions"], str) and len(data["operating_instructions"]) > 0
    assert set(data["templates"]) == {"handoff", "lesson", "howto"}
    assert isinstance(data["lessons_digest"], list)


# --- phase 5: operating instructions stay true to the real API (contracts-v1.md §6) ---


async def test_bootstrap_instructions_mention_documents_compartment(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    assert "documents[]" in text
    assert '"path"' in text
    assert '"kind"' in text
    assert "adr" in text and "doc" in text
    assert "version" in text


async def test_bootstrap_instructions_mention_project_update(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    assert "project_update" in text
    assert "PATCH /v1/projects/{name}" in text
    assert "GET /v1/projects/{name}" in text


async def test_bootstrap_instructions_mention_completed_search_scopes(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    assert "decisions" in text
    assert "library + decisions + handoffs" in text or "decisions + handoffs" in text


# --- patch 2026-08-07: instructions enumerate the full deposit schema ---


async def test_bootstrap_instructions_enumerate_envelope_fields(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    for field in (
        "deposit_id",
        "tool",
        "session",
        "project",
        "reason",
        "client_ts",
        "doctrine_version",
        "metrics",
        "project_update",
    ):
        assert field in text, f"envelope field {field!r} missing from operating instructions"
    assert "tokens_in" in text and "tokens_out" in text and "cost_estimate" in text and "duration" in text


async def test_bootstrap_instructions_enumerate_events_required_fields(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    for field in ("seq", "ts", "kind", "summary", "payload", "tags", "256 KB"):
        assert field in text


async def test_bootstrap_instructions_state_project_cascade_rule_plainly(client, db_session):
    """The new project cascade rule must be stated in plain, unambiguous
    terms: omitting `project` inherits the deposit's project; sending
    explicit null is universal.
    """
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    assert "omit" in text.lower() and "this deposit's project" in text.lower()
    assert "null" in text.lower() and "universal" in text.lower()
    # supersedes is explicitly called out as an array/list, not a scalar
    assert "supersedes" in text and ("a list" in text.lower() or "an array" in text.lower())


async def test_bootstrap_instructions_include_retire_action_shape(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    assert '"retire"' in text
    assert '"reason"' in text


async def test_bootstrap_instructions_mention_handoff_or_waiver_fields(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    assert "no_handoff" in text
    for field in ("stands", "in_flight", "blocked", "next_steps"):
        assert field in text


async def test_bootstrap_instructions_contain_a_minimal_valid_example_deposit(client, db_session):
    """Contracts amendment (2026-08-07): the instructions must include one
    minimal valid example deposit JSON, in a code block, that a client can
    verify against -- not just prose describing the shape.
    """
    import json as _json

    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    text = resp.json()["operating_instructions"]
    assert "```json" in text

    # Extract the fenced json block and confirm it's valid, minimal, and
    # actually satisfies the real deposit validation (envelope-only + one
    # event + one knowledge item with `project` omitted).
    start = text.index("```json") + len("```json")
    end = text.index("```", start)
    example = _json.loads(text[start:end].strip())

    assert set(example) >= {"deposit_id", "tool", "session", "project", "reason", "client_ts"}
    assert example["reason"] == "daily"
    assert "project" not in example["knowledge"][0]  # demonstrates the cascade rule: omitted -> inherits

    from ulid import ULID as _ULID

    example["deposit_id"] = str(_ULID())  # the example reuses a fixed illustrative id; make it fresh for posting
    post_resp = await client.post("/v1/deposits", json=example, headers=headers)
    assert post_resp.status_code == 200, post_resp.json()
    entry = post_resp.json()["knowledge"][0]
    assert entry["action"] == "created"


async def test_bootstrap_project_context_mentions_writable_fields(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain"}, headers=headers)
    text = resp.text
    assert "writable" in text.lower()


async def test_bootstrap_markdown_contains_all_five_section_headings(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain"}, headers=headers)
    text = resp.text
    assert "## 1. Doctrine" in text
    assert "## 2. Project context" in text
    assert "## 3. Operating instructions" in text
    assert "## 4. Templates" in text
    assert "## 5. Lessons digest" in text


# --- compiled doctrine ---


async def test_bootstrap_no_doctrine_is_honest(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    data = resp.json()
    assert data["doctrine"]["has_doctrine"] is False
    assert data["doctrine"]["non_negotiable"] == []
    assert data["doctrine"]["default"] == []
    assert data["version_stamp"] == "none"

    md_resp = await client.get("/v1/bootstrap", params={"project": "brain"}, headers=headers)
    assert "no doctrine configured yet" in md_resp.text.lower()
    assert "G1" not in md_resp.text  # never fakes rules


async def test_bootstrap_compiles_global_only_no_overlay(client, db_session):
    owner_headers = await _owner_headers(db_session)
    await _post_global(client, owner_headers)

    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "no-overlay-project", "format": "json"}, headers=headers)
    data = resp.json()
    assert data["doctrine"]["has_doctrine"] is True
    assert data["version_stamp"] == "global:v1"
    assert {r["id"] for r in data["doctrine"]["non_negotiable"]} == {"G1", "G2"}
    default_by_id = {r["id"]: r for r in data["doctrine"]["default"]}
    assert default_by_id["G3"]["overridden"] is False
    assert default_by_id["G3"]["text"] == "Prefer small commits."


async def test_bootstrap_compiles_overlay_override_marked_and_additions_appended(client, db_session):
    owner_headers = await _owner_headers(db_session)
    await _post_global(client, owner_headers)
    db_session.add(Project(name="brain", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    await _post_overlay(
        client,
        owner_headers,
        "brain",
        overrides=[{"id": "G3", "text": "Actually, batch commits here."}],
        additions=[{"id": "P1", "text": "Always run e2e before merging."}],
    )

    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "brain", "format": "json"}, headers=headers)
    data = resp.json()
    assert data["version_stamp"] == "global:v1+overlay:v1"
    default_by_id = {r["id"]: r for r in data["doctrine"]["default"]}
    assert default_by_id["G3"]["overridden"] is True
    assert default_by_id["G3"]["text"] == "Actually, batch commits here."
    assert data["doctrine"]["overlay_additions"] == [{"id": "P1", "text": "Always run e2e before merging."}]
    # non-negotiables are untouched by the overlay
    assert {r["id"]: r["text"] for r in data["doctrine"]["non_negotiable"]} == {
        "G1": "Never assume.",
        "G2": "Never guess.",
    }

    md_resp = await client.get("/v1/bootstrap", params={"project": "brain"}, headers=headers)
    assert "[project override of G3]" in md_resp.text


# --- project context: auto-stub + latest handoff ---


async def test_bootstrap_unknown_project_auto_stubbed(client, db_session):
    headers, _ = await _machine_headers(db_session)
    assert await db_session.get(Project, "brand-new-via-bootstrap") is None

    resp = await client.get("/v1/bootstrap", params={"project": "brand-new-via-bootstrap", "format": "json"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"]["is_new"] is True
    assert data["project"]["status"] == "active"

    project = await db_session.get(Project, "brand-new-via-bootstrap")
    assert project is not None

    md_resp = await client.get("/v1/bootstrap", params={"project": "brand-new-via-bootstrap-2"}, headers=headers)
    assert "new project" in md_resp.text.lower()


async def test_bootstrap_shows_latest_handoff_not_an_older_one(client, db_session):
    headers, _ = await _machine_headers(db_session)

    async def _deposit_handoff(stands: str):
        body = _deposit_body(
            project="handoff-proj",
            reason="session_end",
            handoff={
                "stands": stands,
                "in_flight": "in flight",
                "blocked": "",
                "next_steps": "next",
            },
        )
        resp = await client.post("/v1/deposits", json=body, headers=headers)
        assert resp.status_code == 200

    await _deposit_handoff("older handoff state")
    await _deposit_handoff("newer handoff state")

    resp = await client.get("/v1/bootstrap", params={"project": "handoff-proj", "format": "json"}, headers=headers)
    data = resp.json()
    assert data["project"]["handoff"]["stands"] == "newer handoff state"


async def test_bootstrap_no_handoff_yet_is_honest(client, db_session):
    headers, _ = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "no-handoff-proj", "format": "json"}, headers=headers)
    assert resp.json()["project"]["handoff"] is None


# --- digest: cap + excludes proposals ---


async def test_bootstrap_digest_excludes_proposals(client, db_session):
    headers, _ = await _machine_headers(db_session)
    lesson_body = _deposit_body(
        project="digest-proj",
        knowledge=[
            {"title": "An ordinary lesson", "namespace": "lessons", "body": "body text", "project": "digest-proj"}
        ],
    )
    proposal_body = _deposit_body(
        project="digest-proj",
        knowledge=[
            {
                "title": "A doctrine proposal",
                "namespace": "reference",
                "body": "proposal body",
                "project": "digest-proj",
                "doctrine_proposal": True,
            }
        ],
    )
    assert (await client.post("/v1/deposits", json=lesson_body, headers=headers)).status_code == 200
    assert (await client.post("/v1/deposits", json=proposal_body, headers=headers)).status_code == 200

    resp = await client.get("/v1/bootstrap", params={"project": "digest-proj", "format": "json"}, headers=headers)
    titles = {item["title"] for item in resp.json()["lessons_digest"]}
    assert "An ordinary lesson" in titles
    assert "A doctrine proposal" not in titles


async def test_bootstrap_digest_capped_at_20(client, db_session):
    headers, _ = await _machine_headers(db_session)
    for i in range(25):
        body = _deposit_body(
            project="cap-proj",
            knowledge=[
                {"title": f"Lesson {i}", "namespace": "lessons", "body": f"body {i}", "project": "cap-proj"}
            ],
        )
        assert (await client.post("/v1/deposits", json=body, headers=headers)).status_code == 200

    resp = await client.get("/v1/bootstrap", params={"project": "cap-proj", "format": "json"}, headers=headers)
    digest = resp.json()["lessons_digest"]
    assert len(digest) == 20  # 25 deposited, hard-capped to 20


# --- fetch logging + version stamp format ---


async def test_bootstrap_fetch_logged_with_versions(client, db_session):
    owner_headers = await _owner_headers(db_session)
    await _post_global(client, owner_headers)
    db_session.add(Project(name="logged-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    await _post_overlay(client, owner_headers, "logged-proj")

    headers, machine_id = await _machine_headers(db_session)
    resp = await client.get("/v1/bootstrap", params={"project": "logged-proj"}, headers=headers)
    assert resp.status_code == 200

    rows = (await db_session.scalars(select(BootstrapFetch).where(BootstrapFetch.project == "logged-proj"))).all()
    assert len(rows) == 1
    assert rows[0].machine_id == machine_id
    assert rows[0].doctrine_global_version == 1
    assert rows[0].doctrine_overlay_version == 1


async def test_bootstrap_version_stamp_formats(client, db_session):
    headers, _ = await _machine_headers(db_session)
    owner_headers = await _owner_headers(db_session)

    none_resp = await client.get("/v1/bootstrap", params={"project": "stamp-none", "format": "json"}, headers=headers)
    assert none_resp.json()["version_stamp"] == "none"

    await _post_global(client, owner_headers)
    global_only_resp = await client.get(
        "/v1/bootstrap", params={"project": "stamp-global-only", "format": "json"}, headers=headers
    )
    assert global_only_resp.json()["version_stamp"] == "global:v1"

    db_session.add(Project(name="stamp-both", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    await _post_overlay(client, owner_headers, "stamp-both")
    both_resp = await client.get("/v1/bootstrap", params={"project": "stamp-both", "format": "json"}, headers=headers)
    assert both_resp.json()["version_stamp"] == "global:v1+overlay:v1"


# --- size budget trimming ---


async def test_bootstrap_typical_response_fits_well_under_budget(client, db_session):
    """Patch 2026-08-07 grew the operating-instructions section (full schema
    enumeration + example JSON). A realistic bootstrap -- global doctrine +
    a project overlay + a handful of digest entries, nothing pathological --
    must still land comfortably under the 32 KB budget with no trimming, so
    the budget mechanics never have to trim ordinary rules to make room for
    the bigger instructions text.
    """
    owner_headers = await _owner_headers(db_session)
    await _post_global(client, owner_headers)
    db_session.add(Project(name="typical-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    await _post_overlay(
        client,
        owner_headers,
        "typical-proj",
        content="# Project overlay\n\nA normal, modestly-sized project overlay document.",
        additions=[{"id": "P1", "text": "Run the test profile before committing."}],
    )

    headers, _ = await _machine_headers(db_session)
    for i in range(10):
        body = _deposit_body(
            project="typical-proj",
            knowledge=[
                {
                    "title": f"Ordinary lesson {i}",
                    "namespace": "lessons",
                    "body": f"A normal, realistically-sized lesson body for entry {i}.",
                }
            ],
        )
        assert (await client.post("/v1/deposits", json=body, headers=headers)).status_code == 200

    resp = await client.get("/v1/bootstrap", params={"project": "typical-proj", "format": "json"}, headers=headers)
    data = resp.json()
    assert len(data["lessons_digest"]) == 10  # nothing trimmed
    assert data["doctrine"]["overlay_content"] is not None  # nothing trimmed

    assert _markdown_bytes(data) <= SIZE_BUDGET_BYTES // 2  # comfortably under half the budget
    assert _json_bytes(data) <= SIZE_BUDGET_BYTES // 2

    md_resp = await client.get("/v1/bootstrap", params={"project": "typical-proj"}, headers=headers)
    assert len(md_resp.content) <= SIZE_BUDGET_BYTES // 2


async def test_bootstrap_trims_digest_before_overlay_content_before_rules(client, db_session):
    owner_headers = await _owner_headers(db_session)
    await _post_global(client, owner_headers)
    db_session.add(Project(name="oversized-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    # A deliberately huge overlay content blob -- bigger than the whole 32 KB
    # budget on its own, so dropping it is the only way to fit regardless of
    # how much the digest shrinks.
    await _post_overlay(client, owner_headers, "oversized-proj", content="OVERLAY-MARKER " + ("x" * 50000))

    headers, _ = await _machine_headers(db_session)
    # Push the digest past the budget too: 20 entries with long bodies, all
    # entry-scoped to this project so they're eligible for the digest.
    for i in range(20):
        body = _deposit_body(
            project="oversized-proj",
            knowledge=[
                {
                    "title": f"Long lesson {i}",
                    "namespace": "lessons",
                    "body": ("word " * 200) + f" unique-{i}",
                    "project": "oversized-proj",
                }
            ],
        )
        assert (await client.post("/v1/deposits", json=body, headers=headers)).status_code == 200

    json_resp = await client.get("/v1/bootstrap", params={"project": "oversized-proj", "format": "json"}, headers=headers)
    assert json_resp.status_code == 200
    data = json_resp.json()

    # The overlay is over budget on its own -- trimming the digest to zero
    # still can't fit it, so it must end up dropped. Digest trimming is
    # attempted first regardless (per the trim order), so it's fully emptied
    # along the way.
    assert data["lessons_digest"] == []
    assert data["doctrine"]["overlay_content"] is None
    # Non-negotiable (and default) rules are never trimmed, regardless.
    assert {r["id"] for r in data["doctrine"]["non_negotiable"]} == {"G1", "G2"}
    assert any(r["id"] == "G3" for r in data["doctrine"]["default"])

    md_resp = await client.get("/v1/bootstrap", params={"project": "oversized-proj"}, headers=headers)
    assert "G1" in md_resp.text
    assert "Never assume." in md_resp.text


def _synthetic_bootstrap_data(digest_count: int) -> dict:
    """Same shape `get_bootstrap` builds, without going through the DB/HTTP
    layer -- lets the boundary test below dial in an exact digest length
    deterministically instead of fighting incidental overhead from a real
    deposit round-trip.
    """
    return {
        "version_stamp": "none",
        "doctrine": {
            "version_stamp": "none",
            "has_doctrine": False,
            "non_negotiable": [],
            "default": [],
            "overlay_additions": [],
            "global_content": None,
            "overlay_content": None,
        },
        "project": {
            "name": "boundary-proj",
            "status": "active",
            "description": None,
            "is_new": False,
            "handoff": None,
        },
        "operating_instructions": _operating_instructions_markdown(),
        "templates": TEMPLATES,
        "lessons_digest": [
            {"id": "a" * 26, "title": "t", "snippet": "s"} for _ in range(digest_count)
        ],
    }


def test_size_budget_binds_json_even_when_markdown_is_under_budget():
    """The regression this guards against: trimming used to measure only the
    markdown rendering. JSON's per-item struct overhead (quoted keys, braces)
    makes it grow faster than markdown per digest entry, so a digest length
    exists where markdown is comfortably under the 32 KB budget while the
    JSON rendering of the exact same data is over it. Before the fix,
    `_apply_size_budget` would return early (markdown-only check) and the
    JSON response would ship oversized.
    """
    data = None
    for n in range(1, 4000):
        candidate = _synthetic_bootstrap_data(n)
        if _markdown_bytes(candidate) <= SIZE_BUDGET_BYTES and _json_bytes(candidate) > SIZE_BUDGET_BYTES:
            data = candidate
            break
    assert data is not None, "could not construct a markdown-under/json-over boundary case"

    trimmed = _apply_size_budget(data, machine_id="m1", project="boundary-proj")

    assert _markdown_bytes(trimmed) <= SIZE_BUDGET_BYTES
    assert _json_bytes(trimmed) <= SIZE_BUDGET_BYTES
    # The digest is what got trimmed to fit JSON -- overlay content was
    # already None (nothing to drop) and rules stay untouched.
    assert len(trimmed["lessons_digest"]) < 4000
