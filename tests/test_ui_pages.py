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


# --- ADR-0019 decision 4: "Copy doctrine" checklist -- external_safe,
# owner-only, starts empty for today's (unflagged) rules, round-trips
# without a migration (`rules` is an unconstrained JSONB column,
# app/models.py's DoctrineVersion -- see app/schemas.py's DoctrineRuleIn
# docstring for why no alembic revision is needed). ---


async def test_doctrine_checklist_starts_empty_with_todays_unflagged_rules(client, db_session):
    """None of the owner's rules have ever had `external_safe` posted (the
    field didn't exist before this feature) -- every checkbox must render
    unchecked, and the hidden copy text must carry no rule lines (framing
    only), exactly ADR-0019's stated "accepted, temporary cost."
    """
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    r = await client.post(
        "/v1/doctrine/global",
        json={
            "content": "g1",
            "rules": [
                {"id": "G1", "tier": "non_negotiable", "text": "Never assume."},
                {"id": "G2", "tier": "default", "text": "Prefer small commits."},
            ],
        },
        headers=owner_headers,
    )
    assert r.status_code == 201

    await client.post("/ui/login", data={"token": owner_token})
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200
    assert 'id="doctrine-checklist"' in resp.text
    assert "checked" not in resp.text.split('id="doctrine-checklist"', 1)[1].split("</ul>", 1)[0]

    briefing = resp.text.split('id="doctrine-briefing-text"', 1)[1].split("</pre>", 1)[0]
    assert "Never assume." not in briefing
    assert "Prefer small commits." not in briefing
    assert "global:v1" in briefing  # version is still stated even with nothing selected


async def test_doctrine_checklist_preselects_only_external_safe_rules(client, db_session):
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    r = await client.post(
        "/v1/doctrine/global",
        json={
            "content": "g1",
            "rules": [
                {"id": "G1", "tier": "non_negotiable", "text": "Never assume.", "external_safe": True},
                {"id": "G2", "tier": "default", "text": "Prefer small commits.", "external_safe": False},
                {"id": "G4", "tier": "default", "text": "Deposit a handoff at session end."},  # unflagged
            ],
        },
        headers=owner_headers,
    )
    assert r.status_code == 201

    await client.post("/ui/login", data={"token": owner_token})
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200

    checklist = resp.text.split('id="doctrine-checklist"', 1)[1].split("</ul>", 1)[0]

    def _input_tag_for(rule_id: str) -> str:
        marker = f'data-rule-id="{rule_id}"'
        idx = checklist.index(marker)
        tag_start = checklist.rindex("<input", 0, idx)
        tag_end = checklist.index(">", idx)
        return checklist[tag_start : tag_end + 1]

    assert "checked" in _input_tag_for("G1")  # external_safe: true
    assert "checked" not in _input_tag_for("G2")  # external_safe: false
    assert "checked" not in _input_tag_for("G4")  # no external_safe key at all -- fail closed

    briefing = resp.text.split('id="doctrine-briefing-text"', 1)[1].split("</pre>", 1)[0]
    assert "- G1: Never assume." in briefing
    assert "Prefer small commits." not in briefing
    assert "Deposit a handoff" not in briefing


async def test_doctrine_checklist_and_copy_button_owner_only(client, db_session):
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    await client.post(
        "/v1/doctrine/global",
        json={"content": "g1", "rules": [{"id": "G1", "tier": "non_negotiable", "text": "Never assume."}]},
        headers=owner_headers,
    )

    # No cookie session at all -- require_ui_session redirects to /ui/login,
    # same as every other /ui/* page; no new gating was added for this.
    resp = await client.get("/ui/doctrine", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    # Same guarantee, demonstrated the same way as
    # test_ui_rooms.py's test_rooms_list_machine_token_cannot_reach_ui: a
    # valid machine bearer token is not a UI cookie session either --
    # require_ui_session doesn't accept it as a substitute.
    machine_headers = await _machine_headers(db_session)
    resp = await client.get("/ui/doctrine", headers=machine_headers, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_doctrine_checklist_omitted_when_no_global_doctrine(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200
    assert "doctrine-checklist" not in resp.text
    assert "doctrine-briefing-text" not in resp.text


async def test_doctrine_copy_button_uses_shared_data_copy_target_handler(client, db_session):
    """No second copy path (ADR-0019 decision 5) -- the button is an
    ordinary [data-copy-target] button pointing at the hidden element, same
    markup shape as every other copy button on this site.
    """
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    await client.post(
        "/v1/doctrine/global",
        json={"content": "g1", "rules": [{"id": "G1", "tier": "non_negotiable", "text": "Never assume."}]},
        headers=owner_headers,
    )

    await client.post("/ui/login", data={"token": owner_token})
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200
    assert 'data-copy-target="doctrine-briefing-text"' in resp.text
    assert '<script src="/static/doctrine_copy.js"></script>' in resp.text
    assert '<script src="/static/main.js">' in resp.text  # unmodified shared handler still loaded


async def test_doctrine_rule_text_xss_escaped_in_checklist_and_copy_text(client, db_session):
    """A rule's `text` is owner-authored via the API, but still untrusted
    content by this UI's own discipline for everything it renders -- must
    never appear as raw, executable markup in either the checkbox label or
    the `data-rule-text` attribute the copy text is built from.
    """
    from markupsafe import escape as markupsafe_escape

    xss_payload = "<script>alert(1)</script>"
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    await client.post(
        "/v1/doctrine/global",
        json={
            "content": "g1",
            "rules": [{"id": "G1", "tier": "default", "text": xss_payload, "external_safe": True}],
        },
        headers=owner_headers,
    )

    await client.post("/ui/login", data={"token": owner_token})
    resp = await client.get("/ui/doctrine")
    assert resp.status_code == 200
    assert xss_payload not in resp.text
    assert str(markupsafe_escape(xss_payload)) in resp.text


# --- ADR-0019: external_safe round-trips through a doctrine POST without a
# migration (`rules` is an unconstrained JSONB column already, so a new key
# needs no alembic revision -- the round trip through storage and back out
# into the UI checklist IS the evidence). ---


async def test_external_safe_round_trips_through_doctrine_post_and_storage(client, db_session):
    from sqlalchemy import select

    from app.models import DoctrineVersion

    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    r = await client.post(
        "/v1/doctrine/global",
        json={
            "content": "g1",
            "rules": [{"id": "G1", "tier": "non_negotiable", "text": "Never assume.", "external_safe": True}],
        },
        headers=owner_headers,
    )
    assert r.status_code == 201

    # Read the raw stored JSONB column back directly -- no schema/migration
    # involved, `rules` already accepts an arbitrary dict shape.
    row = (await db_session.execute(select(DoctrineVersion).where(DoctrineVersion.kind == "global"))).scalar_one()
    assert row.rules[0]["external_safe"] is True

    # And back out through the UI checklist, which is what actually reads
    # this field (app/routers/ui_doctrine.py's `all_global_rules`).
    await client.post("/ui/login", data={"token": owner_token})
    resp = await client.get("/ui/doctrine")
    assert "- G1: Never assume." in resp.text.split('id="doctrine-briefing-text"', 1)[1].split("</pre>", 1)[0]


async def test_doctrine_global_post_omitting_external_safe_still_works_old_caller_compatible(client, db_session):
    """Additive/backward-compatible (ADR-0019 Consequences): an existing
    caller of POST /v1/doctrine/global that never heard of `external_safe`
    keeps working exactly as before, defaulting every rule closed.
    """
    owner_token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(owner_token)))
    await db_session.commit()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    r = await client.post(
        "/v1/doctrine/global",
        json={"content": "g1", "rules": [{"id": "G1", "tier": "non_negotiable", "text": "Never assume."}]},
        headers=owner_headers,
    )
    assert r.status_code == 201
    assert r.json()["rules"][0]["id"] == "G1"  # unaffected -- old response shape intact


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
