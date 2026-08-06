"""UI login/logout, session auth gating, and CSRF (phase 6)."""

import re

from ulid import ULID

from app.models import Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _make_owner_token(db_session) -> str:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return token


async def _make_machine_token(db_session) -> str:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return token


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf_token hidden field not found in page"
    return m.group(1)


# --- login ---


async def test_login_with_correct_owner_token_sets_cookie_and_redirects(client, db_session):
    token = await _make_owner_token(db_session)
    resp = await client.post("/ui/login", data={"token": token}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui"
    assert "brain_ui_session" in resp.cookies
    # the cookie is HttpOnly (never readable from JS)
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "httponly" in set_cookie_header.lower()


async def test_login_with_wrong_token_rejected(client, db_session):
    resp = await client.post("/ui/login", data={"token": "not-a-real-token"})
    assert resp.status_code == 401
    assert "brain_ui_session" not in client.cookies
    assert "not recognized" in resp.text.lower()


async def test_login_with_machine_token_rejected(client, db_session):
    machine_token = await _make_machine_token(db_session)
    resp = await client.post("/ui/login", data={"token": machine_token})
    assert resp.status_code == 401
    assert "brain_ui_session" not in client.cookies


async def test_login_page_renders_when_unauthenticated(client, db_session):
    resp = await client.get("/ui/login")
    assert resp.status_code == 200
    assert "owner token" in resp.text.lower()


# --- auth gating ---


async def test_root_redirects_to_ui(client, db_session):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui"


async def test_unauthenticated_ui_routes_redirect_to_login(client, db_session):
    for path in ["/ui", "/ui/library", "/ui/search", "/ui/projects", "/ui/journal", "/ui/doctrine", "/ui/admin/machines", "/ui/admin/proposals"]:
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/ui/login", path


async def test_forged_cookie_rejected(client, db_session):
    client.cookies.set("brain_ui_session", "garbage.not.a.valid.signed.token")
    resp = await client.get("/ui", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_v1_api_routes_unaffected_by_ui_cookie(client, db_session):
    """The UI session cookie must never grant access to bearer-token-only
    /v1/* API routes -- they remain completely unaffected by the UI's
    cookie auth (phase 6 brief).
    """
    token = await _make_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})
    assert "brain_ui_session" in client.cookies

    # No Authorization header sent -- only the UI cookie -- must still 401.
    resp = await client.get("/v1/machines")
    assert resp.status_code == 401


async def test_logged_in_dashboard_accessible(client, db_session):
    token = await _make_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})
    resp = await client.get("/ui")
    assert resp.status_code == 200
    assert "dashboard" in resp.text.lower()


# --- logout ---


async def test_logout_clears_session(client, db_session):
    token = await _make_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})
    dash = await client.get("/ui")
    csrf = _extract_csrf(dash.text)

    resp = await client.post("/ui/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    after = await client.get("/ui", follow_redirects=False)
    assert after.status_code == 303
    assert after.headers["location"] == "/ui/login"


# --- CSRF ---


async def test_post_without_csrf_token_rejected(client, db_session):
    token = await _make_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})

    resp = await client.post("/ui/admin/machines", data={"name": "no-csrf-machine"})
    assert resp.status_code == 403


async def test_post_with_wrong_csrf_token_rejected(client, db_session):
    token = await _make_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})

    resp = await client.post("/ui/admin/machines", data={"name": "bad-csrf-machine", "csrf_token": "wrong-token"})
    assert resp.status_code == 403


async def test_post_with_correct_csrf_token_accepted(client, db_session):
    token = await _make_owner_token(db_session)
    await client.post("/ui/login", data={"token": token})
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/admin/machines", data={"name": "good-csrf-machine", "csrf_token": csrf})
    assert resp.status_code == 201
