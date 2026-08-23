"""UI LLM provider config -- GET /ui/llm (effective + history), POST
/ui/llm (owner cookie + CSRF, creates the next version), POST /ui/llm/test
(connectivity test). Mirrors tests/test_ui_notifications.py's structure;
see tests/test_llm_config.py for the API-side equivalent.
"""

import re
from types import SimpleNamespace

import httpx
from ulid import ULID

import app.llm_config as llm_config_module
from app.models import LlmConfig, Machine, OwnerToken
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


def _install_llm_post_stub(monkeypatch, outer_client, stub):
    """See tests/test_llm_config.py's identical helper for the full
    rationale: `httpx.AsyncClient.post` is patched at the class level, so
    it must discriminate the app's own outbound call from the test's own
    `client` fixture (also an `httpx.AsyncClient` instance) making the
    actual `POST /ui/llm/test` request.
    """
    real_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kwargs):
        if self is outer_client:
            return await real_post(self, url, *args, **kwargs)
        return await stub(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def _machine_token(db_session) -> str:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return token


# --- rendering ---


async def test_llm_page_renders_empty_state(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/llm")
    assert resp.status_code == 200
    assert "no llm provider configured" in resp.text.lower()


async def test_llm_page_renders_effective_and_history(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/llm")
    csrf = _extract_csrf(page.text)

    await client.post(
        "/ui/llm",
        data={"base_url": "http://ollama:11434/v1", "model": "llama3.1", "note": "first", "csrf_token": csrf},
    )
    await client.post(
        "/ui/llm",
        data={"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "note": "switched", "csrf_token": csrf},
    )

    resp = await client.get("/ui/llm")
    assert resp.status_code == 200
    assert "gpt-4o-mini" in resp.text  # effective, shown in full
    assert "llama3.1" in resp.text  # history, shown in full
    assert "switched" in resp.text
    assert "v2" in resp.text
    assert "v1" in resp.text
    assert "source: <strong>db</strong>" in resp.text


async def test_llm_page_shows_env_source_and_inert_notice(client, db_session, monkeypatch):
    await _login(client, db_session)
    fake_settings = SimpleNamespace(llm_base_url="http://env-provider:1234/v1", llm_model="env-model", llm_api_key=None)
    monkeypatch.setattr(llm_config_module, "get_settings", lambda: fake_settings)

    resp = await client.get("/ui/llm")
    assert resp.status_code == 200
    assert "env-provider" in resp.text
    assert "source: <strong>env</strong>" in resp.text
    assert "take precedence" in resp.text.lower()


# --- update form (CSRF-gated) ---


async def test_llm_form_creates_next_version(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/llm")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/llm",
        data={"base_url": "http://ollama:11434/v1", "model": "llama3.1", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/llm"

    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 1
    assert rows[0].version == 1
    assert rows[0].model == "llama3.1"


async def test_llm_form_without_csrf_rejected(client, db_session):
    await _login(client, db_session)
    resp = await client.post("/ui/llm", data={"base_url": "http://ollama:11434/v1", "model": "llama3.1"})
    assert resp.status_code == 403

    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 0


async def test_llm_form_invalid_url_shows_error(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/llm")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/llm",
        data={"base_url": "not-a-url", "model": "llama3.1", "csrf_token": csrf},
    )
    assert resp.status_code == 422
    assert "valid http" in resp.text.lower()

    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 0


async def test_llm_form_api_key_never_leaked_in_response(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/llm")
    csrf = _extract_csrf(page.text)

    secret = "sk-topsecretvalue999"
    resp = await client.post(
        "/ui/llm",
        data={"base_url": "http://ollama:11434/v1", "model": "llama3.1", "api_key": secret, "csrf_token": csrf},
        follow_redirects=True,
    )
    assert secret not in resp.text

    page2 = await client.get("/ui/llm")
    assert secret not in page2.text
    assert "999" in page2.text  # masked hint fragment only


# --- XSS: note is owner-supplied free text rendered on this page ---


async def test_llm_page_escapes_script_tag_in_note(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/llm")
    csrf = _extract_csrf(page.text)

    payload = "<script>alert(1)</script>"
    resp = await client.post(
        "/ui/llm",
        data={"base_url": "http://ollama:11434/v1", "model": "llama3.1", "note": payload, "csrf_token": csrf},
        follow_redirects=True,
    )
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


async def test_llm_page_escapes_quote_breakout_in_note(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/llm")
    csrf = _extract_csrf(page.text)

    payload = '"><img src=x onerror=alert(1)>'
    resp = await client.post(
        "/ui/llm",
        data={"base_url": "http://ollama:11434/v1", "model": "llama3.1", "note": payload, "csrf_token": csrf},
        follow_redirects=True,
    )
    assert '"><img src=x onerror=alert(1)>' not in resp.text
    assert "&#34;&gt;&lt;img" in resp.text or "&#39;" in resp.text or "&lt;img" in resp.text


# --- test-connection button ---


async def test_llm_test_connection_button_shows_result_inline(client, db_session, monkeypatch):
    await _login(client, db_session)
    page = await client.get("/ui/llm")
    csrf = _extract_csrf(page.text)

    await client.post(
        "/ui/llm",
        data={"base_url": "http://ollama:11434/v1", "model": "llama3.1", "csrf_token": csrf},
    )

    async def stub(self, url, json=None, headers=None, **kwargs):
        return httpx.Response(200, json={"model": "llama3.1"})

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/ui/llm/test", data={"csrf_token": csrf})
    assert resp.status_code == 200
    assert "connected" in resp.text.lower()


async def test_llm_test_connection_without_csrf_rejected(client, db_session):
    await _login(client, db_session)
    resp = await client.post("/ui/llm/test", data={})
    assert resp.status_code == 403


# --- auth gating ---


async def test_llm_page_unauthenticated_redirects_to_login(client, db_session):
    resp = await client.get("/ui/llm", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_llm_page_machine_token_cannot_reach_ui(client, db_session):
    """The UI is cookie-gated only -- a machine bearer token grants
    nothing here (same posture as tests/test_ui_notifications.py's
    equivalent)."""
    machine_token = await _machine_token(db_session)
    resp = await client.get("/ui/llm", headers={"Authorization": f"Bearer {machine_token}"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    resp_post = await client.post(
        "/ui/llm",
        data={"base_url": "http://ollama:11434/v1", "model": "llama3.1"},
        headers={"Authorization": f"Bearer {machine_token}"},
        follow_redirects=False,
    )
    assert resp_post.status_code == 303
    assert resp_post.headers["location"] == "/ui/login"

    resp_test = await client.post(
        "/ui/llm/test", headers={"Authorization": f"Bearer {machine_token}"}, follow_redirects=False
    )
    assert resp_test.status_code == 303
    assert resp_test.headers["location"] == "/ui/login"
