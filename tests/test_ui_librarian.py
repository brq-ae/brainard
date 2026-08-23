"""UI for the built-in librarian -- GET /ui/librarian, POST /ui/librarian/run
(ADR-0010 phase 2). Mirrors tests/test_ui_llm.py's structure; the LLM is
mocked throughout via app.librarian_engine.chat_completion_json (POST
/ui/librarian/run runs the engine inline, same as the owner API).
"""

import re
from datetime import UTC, datetime

from ulid import ULID

import app.librarian_engine as librarian_engine_module
from app.models import LlmConfig, Machine, OwnerToken, Project
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _login(client, db_session) -> str:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    await client.post("/ui/login", data={"token": token})
    return token


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf_token hidden field not found in page"
    return m.group(1)


async def _machine_token(db_session) -> str:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return token


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


# --- rendering / status ---


async def test_librarian_page_shows_not_configured_state(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/librarian")
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()
    assert "no runs yet" in resp.text.lower()


async def test_librarian_page_shows_configured_provider(client, db_session):
    await _login(client, db_session)
    await _configure_provider(db_session)
    resp = await client.get("/ui/librarian")
    assert resp.status_code == 200
    assert "fake-model" in resp.text


async def test_librarian_page_run_now_button_present(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/librarian")
    assert 'action="/ui/librarian/run"' in resp.text
    assert "Run now" in resp.text


# --- run now (CSRF-gated, inline) ---


async def test_run_now_shows_result_in_runs_list(client, db_session, monkeypatch):
    await _login(client, db_session)
    page = await client.get("/ui/librarian")
    csrf = _extract_csrf(page.text)
    _install_llm_stub(monkeypatch, [])

    resp = await client.post("/ui/librarian/run", data={"csrf_token": csrf}, follow_redirects=True)
    assert resp.status_code == 200
    assert "skipped" in resp.text.lower()
    assert "no runs yet" not in resp.text.lower()


async def test_run_now_without_csrf_rejected(client, db_session):
    await _login(client, db_session)
    resp = await client.post("/ui/librarian/run", data={})
    assert resp.status_code == 403


# --- auth gating ---


async def test_librarian_page_unauthenticated_redirects_to_login(client, db_session):
    resp = await client.get("/ui/librarian", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_librarian_page_machine_token_cannot_reach_ui(client, db_session):
    machine_token = await _machine_token(db_session)
    resp = await client.get("/ui/librarian", headers={"Authorization": f"Bearer {machine_token}"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    resp_post = await client.post(
        "/ui/librarian/run", headers={"Authorization": f"Bearer {machine_token}"}, follow_redirects=False
    )
    assert resp_post.status_code == 303
    assert resp_post.headers["location"] == "/ui/login"


# --- XSS ---


async def test_stale_project_name_escaped_in_run_summary(client, db_session, monkeypatch):
    await _login(client, db_session)
    await _configure_provider(db_session)
    db_session.add(Project(name="<script>alert(1)</script>", status="active", created_at=datetime.now(UTC)))
    await db_session.commit()

    page = await client.get("/ui/librarian")
    csrf = _extract_csrf(page.text)
    _install_llm_stub(monkeypatch, [])  # no flags/lessons exist -> zero LLM calls

    resp = await client.post("/ui/librarian/run", data={"csrf_token": csrf}, follow_redirects=True)
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
