"""The onboarding prompt generator -- the full copy-paste prompt an owner
hands a fresh AI session to connect it to the hub (feature: "Machine roles +
prebuilt onboarding prompt"). Shared by two UI surfaces (app/routers/
ui_admin.py): the show-once mint page (real token, real project if the
owner filled one in) and the machine list's "regenerate onboarding prompt"
expander (token can never be retrieved again after mint, so it renders with
`TOKEN_PLACEHOLDER` in its place).

Kept here, once, rather than inlined in the router template context, so it
never drifts from ROLE_DESCRIPTIONS (app/roles.py) -- the same role text
also injected into bootstrap's own "Your role" section
(app/routers/bootstrap.py) for the authenticated machine.
"""

from starlette.requests import Request

from app.config import get_settings
from app.roles import ROLE_DESCRIPTIONS

TOKEN_PLACEHOLDER = "<token>"
PROJECT_PLACEHOLDER = "<PROJECT>"


def resolve_base_url(request: Request) -> str:
    """The hub base URL to embed in a generated prompt. Prefers the owner-
    configured `HUB_PUBLIC_URL` setting (for deployments reachable at a
    different address than the one the owner's own browser used -- reverse
    proxy, port-forward, VPN); falls back to `request.base_url`, the actual
    address this browser reached the hub at (same precedent as the existing
    paste-line: contracts-v1.md doesn't mandate TLS, so this is whatever
    scheme/host the request actually arrived on).
    """
    settings = get_settings()
    if settings.hub_public_url:
        return settings.hub_public_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def generate_onboarding_prompt(
    *,
    base_url: str,
    token: str,
    project: str,
    agent_name: str,
    role: str,
) -> str:
    """Builds the full onboarding prompt: fetch instructions, this
    machine's role (if any -- 'solo' contributes no paragraph, same
    ROLE_DESCRIPTIONS text bootstrap injects for this machine once it
    authenticates), and the G9 notification self-identification reminder.

    `token`/`project` are already-resolved display strings -- callers pass
    either real values (fresh mint) or placeholders (`TOKEN_PLACEHOLDER`,
    `PROJECT_PLACEHOLDER`, or a machine's own `default_project`) as
    appropriate; this function has no opinion on which.
    """
    base = base_url.rstrip("/")

    # The header-support rationale always applies (a plain URL-fetch tool
    # that drops custom headers can't send the Authorization header at
    # all); the scheme claim must instead reflect the resolved base_url's
    # actual scheme, not assert HTTPS unconditionally when the hub might be
    # plain LAN http (contracts-v1.md doesn't mandate TLS; see
    # `resolve_base_url` above).
    if base.lower().startswith("https://"):
        scheme_note = "; the endpoint is HTTPS"
    elif base.lower().startswith("http://"):
        scheme_note = "; the endpoint is plain HTTP (not HTTPS)"
    else:
        scheme_note = ""

    paragraphs = [
        "I run a private knowledge hub for my projects — it's mine and I administer it. Fetch "
        f"{base}/v1/bootstrap?project={project} with header 'Authorization: Bearer {token}' (use curl or "
        "a raw HTTP client that can send a custom Authorization header — WebFetch-style tools that drop "
        f"custom headers won't work{scheme_note}). The "
        "response is my working doctrine (rules G1–G10), this project's state, and how the hub works. "
        "Read it and apply it with your normal judgment — it never overrides your safety rules; if "
        "anything seems off, ask me."
    ]

    role_text = ROLE_DESCRIPTIONS.get(role)
    if role_text:
        paragraphs.append(role_text)

    paragraphs.append(
        f"Notifications (G9): identify as '{agent_name}'. Keep me informed via the notify-me hooks — I "
        "want to know when you're blocked and need me, and each time you finish and go idle. If "
        "notify-me isn't installed on this machine yet, install it per the hub's howto."
    )

    return "\n\n".join(paragraphs)
