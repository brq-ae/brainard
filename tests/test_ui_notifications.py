"""UI notifications -- GET /ui/notifications (current + history), POST
/ui/notifications (owner cookie + CSRF, creates the next version). Exercises
the same shared logic (app/notifications.py) as the API endpoint -- see
tests/test_notifications.py for the API-side equivalent.
"""

import re

from ulid import ULID

from app.models import Machine, NotificationConfig, OwnerToken
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


# --- rendering ---


async def test_notifications_page_renders_empty_state(client, db_session):
    await _login(client, db_session)
    resp = await client.get("/ui/notifications")
    assert resp.status_code == 200
    assert "no notification channel configured" in resp.text.lower()


async def test_notifications_page_renders_current_and_history(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/notifications")
    csrf = _extract_csrf(page.text)

    await client.post(
        "/ui/notifications",
        data={"ntfy_url": "https://ntfy.sh", "topic": "topic-one", "note": "first", "csrf_token": csrf},
    )
    await client.post(
        "/ui/notifications",
        data={"ntfy_url": "https://ntfy.sh", "topic": "topic-two", "note": "rotated", "csrf_token": csrf},
    )

    resp = await client.get("/ui/notifications")
    assert resp.status_code == 200
    assert "topic-two" in resp.text  # current, shown in full
    assert "topic-one" in resp.text  # history, shown in full
    assert "rotated" in resp.text
    assert "v2" in resp.text
    assert "v1" in resp.text


# --- update form (CSRF-gated) ---


async def test_notifications_form_creates_next_version(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/notifications")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/notifications",
        data={"ntfy_url": "https://ntfy.sh", "topic": "fresh-topic", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/notifications"

    rows = (await db_session.execute(NotificationConfig.__table__.select())).all()
    assert len(rows) == 1
    assert rows[0].version == 1
    assert rows[0].topic == "fresh-topic"


async def test_notifications_form_without_csrf_rejected(client, db_session):
    await _login(client, db_session)
    resp = await client.post("/ui/notifications", data={"ntfy_url": "https://ntfy.sh", "topic": "no-csrf"})
    assert resp.status_code == 403

    rows = (await db_session.execute(NotificationConfig.__table__.select())).all()
    assert len(rows) == 0


async def test_notifications_form_invalid_url_shows_error(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/notifications")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/notifications",
        data={"ntfy_url": "not-a-url", "topic": "some-topic", "csrf_token": csrf},
    )
    assert resp.status_code == 422
    assert "valid http" in resp.text.lower()

    rows = (await db_session.execute(NotificationConfig.__table__.select())).all()
    assert len(rows) == 0


# --- auth gating ---


async def test_notifications_page_unauthenticated_redirects_to_login(client, db_session):
    resp = await client.get("/ui/notifications", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_notifications_page_machine_token_cannot_reach_ui(client, db_session):
    """The UI is cookie-gated only -- a machine bearer token (sent as a
    header, since machines have no session cookie) grants nothing here; the
    request is treated exactly as unauthenticated (phase 6 posture, same as
    tests/test_ui_auth.py's v1-vs-UI isolation tests).
    """
    machine_token = await _machine_token(db_session)
    resp = await client.get(
        "/ui/notifications", headers={"Authorization": f"Bearer {machine_token}"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"

    resp_post = await client.post(
        "/ui/notifications",
        data={"ntfy_url": "https://ntfy.sh", "topic": "x"},
        headers={"Authorization": f"Bearer {machine_token}"},
        follow_redirects=False,
    )
    assert resp_post.status_code == 303
    assert resp_post.headers["location"] == "/ui/login"
