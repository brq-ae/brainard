"""Owner-only UI session auth (phase 6 brief).

The UI is for the OWNER only -- machine tokens are never accepted here.
Login trades the owner's bearer token (verified the same way as every other
owner-gated endpoint, via `app.auth.authenticate`) for a signed, HttpOnly
session cookie. The session is entirely stateless: no server-side session
table, just an itsdangerous-signed, timestamped payload carrying an `owner`
marker and a per-login CSRF token. This mirrors the rest of the app's
"no unnecessary state" posture and needs no new migration.

CSRF: the synchronizer-token pattern. The CSRF token is minted once at
login and travels *inside* the signed, HttpOnly session cookie -- a
cross-site attacker's page can neither read the cookie (HttpOnly + it's a
same-site-only browser jar) nor forge a valid one (it's signed with a
server-only secret), so it cannot learn the token to embed in a forged
request. Every admin POST form embeds `{{ csrf_token }}` as a hidden field;
`require_csrf` compares it against the session's own token.
"""

import logging
import secrets
from typing import Any

from fastapi import Cookie, Depends, Form, Header, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from app.auth import authenticate
from app.config import get_settings
from app.errors import ApiError

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "brain_ui_session"
# 30 days: the owner token is long and random by design (it is a bearer
# credential on every API call), so re-pasting it daily is friction without
# a security payoff. The cookie is HttpOnly, signed, and scoped to one
# browser; a longer window trades a slightly longer-lived session for not
# tempting the owner into a weaker root credential.
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # ~30 days
_SALT = "brain-ui-session-v1"

settings = get_settings()

if settings.ui_session_secret:
    _SECRET = settings.ui_session_secret
else:
    # Generate-if-missing (documented in .env.example): a LAN, single-owner
    # deployment where a restart simply logs everyone out again (same owner
    # token, one click) is an acceptable trade against a hard failure to
    # boot. Set UI_SESSION_SECRET explicitly for sessions that must survive
    # a restart.
    _SECRET = secrets.token_urlsafe(32)
    logger.warning(
        "UI_SESSION_SECRET not set -- generated a random secret for this process only. "
        "Existing UI sessions will not survive a restart. Set UI_SESSION_SECRET in .env to persist sessions."
    )

_serializer = URLSafeTimedSerializer(_SECRET, salt=_SALT)


class UIAuthRequired(Exception):
    """Raised by `require_ui_session` when no valid session cookie is
    present. Handled by an app-level exception handler (app/main.py) that
    redirects to /ui/login -- kept as a plain exception rather than an
    HTTPException subclass so it can never be confused with an ordinary
    403/401 API error.
    """


def _create_session_payload() -> dict[str, Any]:
    return {"owner": True, "csrf": secrets.token_urlsafe(32)}


def create_session_token() -> str:
    return _serializer.dumps(_create_session_payload())


def verify_session_token(token: str) -> dict[str, Any] | None:
    try:
        payload = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or payload.get("owner") is not True or not payload.get("csrf"):
        return None
    return payload


async def require_ui_session(
    brain_ui_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """FastAPI dependency guarding every protected /ui/* route. Raises
    UIAuthRequired (-> redirect to /ui/login) for a missing, malformed,
    expired, or forged cookie. Never accepts a machine token -- there is no
    code path from a bearer token into this cookie at all.
    """
    if not brain_ui_session:
        raise UIAuthRequired()
    payload = verify_session_token(brain_ui_session)
    if payload is None:
        raise UIAuthRequired()
    return payload


def set_session_cookie(response: Response) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.ui_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


async def verify_owner_token(token: str, db: AsyncSession) -> bool:
    """Verifies a pasted token is the owner token (contracts-v1.md §1) --
    reuses the same `authenticate` used by every bearer-token API route, so
    the UI login never re-implements token verification. A machine token
    authenticates fine against `authenticate` but resolves to kind
    'machine', which is rejected here -- machine tokens are never accepted
    for the UI (phase 6 brief).
    """
    try:
        principal = await authenticate(token, db)
    except ApiError:
        return False
    return principal.kind == "owner"


async def require_csrf(
    session: dict[str, Any] = Depends(require_ui_session),
    csrf_token: str = Form(default=""),
) -> None:
    """Every admin POST form depends on this (in addition to
    `require_ui_session`) to enforce the per-session CSRF token. A
    mismatch or missing token is a plain 403 -- there's nothing to redirect
    to, the form itself was just submitted wrong or forged.
    """
    if not secrets.compare_digest(csrf_token, session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid. Reload the page and retry.")


async def require_csrf_header(
    session: dict[str, Any] = Depends(require_ui_session),
    x_csrf_token: str = Header(default=""),
) -> None:
    """Same CSRF check as `require_csrf`, but reads the token from an
    `X-CSRF-Token` request header instead of a form field -- for the one
    endpoint whose body isn't form-encoded (ADR-0012's raw streamed file
    upload, `POST /ui/rooms/{id}/attachments`: the body IS the file's raw
    bytes, so there is no hidden form field to carry a token in). Fetched
    via JS (app/static/room_attachments.js), never an ordinary HTML form
    submission. Same synchronizer-token comparison as `require_csrf` --
    only the transport differs.
    """
    if not secrets.compare_digest(x_csrf_token, session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid. Reload the page and retry.")
