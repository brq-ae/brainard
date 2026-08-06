"""UI read-only browse pages: dashboard/library/search/projects/journal/
doctrine return 200 with expected content markers (phase 6). Also covers
the stale-override marker (closing the phase 4 advisory at the UI layer)
and XSS sanitization of AI-written markdown bodies.
"""

from datetime import UTC, datetime

from ulid import ULID

from app.models import Machine, OwnerToken, Project
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _login(client, db_session) -> str:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    await client.post("/ui/login", data={"token": token})
    return token


async def _machine_headers(db_session) -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
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


# --- basic 200s with content markers ---


async def test_dashboard_returns_200_with_markers(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui")
    assert resp.status_code == 200
    assert "Machines" in resp.text
    assert "Pending proposals" in resp.text


async def test_library_list_returns_200(client, db_session):
    machine_headers = await _machine_headers(db_session)
    await client.post(
        "/v1/deposits",
        json=_deposit_body(knowledge=[{"title": "A lesson entry", "namespace": "lessons", "body": "body text"}]),
        headers=machine_headers,
    )
    await _login(client, db_session)
    resp = await client.get("/ui/library")
    assert resp.status_code == 200
    assert "A lesson entry" in resp.text


async def test_library_entry_page_returns_200_with_rendered_body(client, db_session):
    machine_headers = await _machine_headers(db_session)
    resp = await client.post(
        "/v1/deposits",
        json=_deposit_body(knowledge=[{"title": "Entry Title", "namespace": "howto", "body": "**bold body text**"}]),
        headers=machine_headers,
    )
    entry_id = resp.json()["knowledge"][0]["id"]

    await _login(client, db_session)
    page = await client.get(f"/ui/library/{entry_id}")
    assert page.status_code == 200
    assert "Entry Title" in page.text
    assert "<strong>bold body text</strong>" in page.text


async def test_search_page_returns_200_and_finds_results(client, db_session):
    machine_headers = await _machine_headers(db_session)
    await client.post(
        "/v1/deposits",
        json=_deposit_body(
            knowledge=[{"title": "Zorbaxian search target", "namespace": "reference", "body": "distinctive body"}]
        ),
        headers=machine_headers,
    )
    await _login(client, db_session)
    resp = await client.get("/ui/search", params={"q": "Zorbaxian"})
    assert resp.status_code == 200
    assert "Zorbaxian search target" in resp.text


async def test_search_page_blank_query_returns_200(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/search")
    assert resp.status_code == 200


async def test_projects_list_returns_200(client, db_session):
    db_session.add(Project(name="listed-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()
    await _login(client, db_session)
    resp = await client.get("/ui/projects")
    assert resp.status_code == 200
    assert "listed-proj" in resp.text


async def test_project_detail_returns_200_with_facts(client, db_session):
    machine_headers = await _machine_headers(db_session)
    await client.post(
        "/v1/deposits",
        json=_deposit_body(
            project="detail-ui-proj",
            reason="session_end",
            handoff={"stands": "stands text", "in_flight": "flight text", "blocked": "", "next_steps": "next text"},
        ),
        headers=machine_headers,
    )
    await _login(client, db_session)
    resp = await client.get("/ui/projects/detail-ui-proj")
    assert resp.status_code == 200
    assert "detail-ui-proj" in resp.text
    assert "stands text" in resp.text


async def test_project_detail_unknown_404(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/projects/does-not-exist")
    assert resp.status_code == 404


async def test_document_view_returns_200_with_version_history(client, db_session):
    machine_headers = await _machine_headers(db_session)
    for content in ("first version", "second version"):
        resp = await client.post(
            "/v1/deposits",
            json=_deposit_body(
                project="doc-proj",
                documents=[{"path": "docs/adr/0001.md", "kind": "adr", "title": "ADR One", "content": content}],
            ),
            headers=machine_headers,
        )
        assert resp.status_code == 200

    await _login(client, db_session)
    resp = await client.get("/ui/projects/doc-proj/documents/docs/adr/0001.md")
    assert resp.status_code == 200
    assert "ADR One" in resp.text
    assert "second version" in resp.text
    assert "v1" in resp.text and "v2" in resp.text


async def test_journal_returns_200(client, db_session):
    machine_headers = await _machine_headers(db_session)
    await client.post(
        "/v1/deposits",
        json=_deposit_body(
            events=[
                {"seq": 1, "ts": "2026-08-06T12:00:00Z", "kind": "note", "summary": "A distinct journal summary"}
            ]
        ),
        headers=machine_headers,
    )
    await _login(client, db_session)
    resp = await client.get("/ui/journal")
    assert resp.status_code == 200
    assert "A distinct journal summary" in resp.text


async def test_doctrine_page_returns_200(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200
    assert "doctrine" in resp.text.lower()


# --- stale-override marker (closes the phase 4 advisory at the UI layer) ---


async def test_doctrine_stale_override_marked_when_target_becomes_non_negotiable(client, db_session):
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    db_session.add(Project(name="stale-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()

    r = await client.post(
        "/v1/doctrine/global",
        json={"content": "g1", "rules": [{"id": "G1", "tier": "default", "text": "be nice"}]},
        headers=owner_headers,
    )
    assert r.status_code == 201

    r = await client.post(
        "/v1/doctrine/overlays/stale-proj",
        json={"content": "overlay", "overrides": [{"id": "G1", "text": "overridden text"}], "additions": []},
        headers=owner_headers,
    )
    assert r.status_code == 201

    # tier-change scenario: G1 flips to non_negotiable in a later global version
    r = await client.post(
        "/v1/doctrine/global",
        json={"content": "g2", "rules": [{"id": "G1", "tier": "non_negotiable", "text": "be nice, mandatory"}]},
        headers=owner_headers,
    )
    assert r.status_code == 201

    await client.post("/ui/login", data={"token": owner_token})
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200
    assert "inactive" in resp.text.lower()
    assert "non-negotiable" in resp.text.lower()


async def test_doctrine_stale_override_marked_when_target_no_longer_exists(client, db_session):
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    db_session.add(Project(name="gone-proj", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()

    await client.post(
        "/v1/doctrine/global",
        json={"content": "g1", "rules": [{"id": "G1", "tier": "default", "text": "be nice"}]},
        headers=owner_headers,
    )
    await client.post(
        "/v1/doctrine/overlays/gone-proj",
        json={"content": "overlay", "overrides": [{"id": "G1", "text": "overridden text"}], "additions": []},
        headers=owner_headers,
    )
    # G1 dropped entirely from the next global version
    r = await client.post(
        "/v1/doctrine/global",
        json={"content": "g2", "rules": [{"id": "G2", "tier": "default", "text": "a different rule"}]},
        headers=owner_headers,
    )
    assert r.status_code == 201

    await client.post("/ui/login", data={"token": owner_token})
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200
    assert "no longer exists" in resp.text.lower()


# --- XSS: AI-written entry bodies must render escaped/sanitized ---


async def test_library_entry_script_tag_and_javascript_link_are_neutralized(client, db_session):
    machine_headers = await _machine_headers(db_session)
    malicious_body = (
        "# Heading\n\n"
        "Some text with <script>alert('xss')</script> embedded directly.\n\n"
        "[click this](javascript:alert('xss'))\n"
    )
    resp = await client.post(
        "/v1/deposits",
        json=_deposit_body(
            knowledge=[{"title": "Malicious Entry", "namespace": "reference", "body": malicious_body}]
        ),
        headers=machine_headers,
    )
    assert resp.status_code == 200
    entry_id = resp.json()["knowledge"][0]["id"]

    await _login(client, db_session)
    page = await client.get(f"/ui/library/{entry_id}")
    assert page.status_code == 200
    # the raw, executable script tag must never appear unescaped
    assert "<script>alert('xss')</script>" not in page.text
    assert "<script>" not in page.text
    # neutralized javascript: link must never appear as a live href
    assert 'href="javascript:' not in page.text
    # the content is still visible, just escaped -- not silently dropped
    assert "alert" in page.text


async def test_proposal_body_script_tag_neutralized_in_admin(client, db_session):
    machine_headers = await _machine_headers(db_session)
    malicious_body = "Rationale with <script>alert('xss')</script> embedded."
    resp = await client.post(
        "/v1/deposits",
        json=_deposit_body(
            knowledge=[
                {
                    "title": "Malicious Proposal",
                    "namespace": "reference",
                    "body": malicious_body,
                    "doctrine_proposal": True,
                }
            ]
        ),
        headers=machine_headers,
    )
    assert resp.status_code == 200

    await _login(client, db_session)
    page = await client.get("/ui/admin/proposals")
    assert page.status_code == 200
    assert "<script>alert('xss')</script>" not in page.text
    assert "<script>" not in page.text
