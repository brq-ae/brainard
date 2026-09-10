"""Machine roles -- Commander/Builder division of labor (doctrine rule G10).

Single source of truth for role text, used by BOTH the onboarding prompt
generator (app/onboarding.py, feeds the UI mint page and the "regenerate
onboarding prompt" view) and the bootstrap operating-instructions injector
(app/routers/bootstrap.py's "Your role" subsection) -- kept here once so the
two surfaces can never drift on what a role means.

'solo' (the default) carries no description and never gets a "Your role"
section injected into bootstrap, nor a role paragraph in a generated prompt
-- most machines run a single undivided session and G10 doesn't apply to
them.
"""

from app.errors import ApiError

VALID_ROLES = frozenset({"solo", "commander", "builder"})
DEFAULT_ROLE = "solo"

ROLE_DESCRIPTIONS: dict[str, str] = {
    "commander": (
        "You are the Commander for this project. You plan, brainstorm, instruct the Builder, review its "
        "work, and interface with the owner on decisions. You own ALL writes to the hub -- the "
        "decision/ADR mirror, session handoffs, and lessons -- and ensure every significant decision is "
        "recorded and agreed with the owner before the Builder implements. The Builder reads the hub but "
        "never deposits; when it surfaces something worth recording, you deposit it. You are also the "
        "Builder's escalation path: when it brings you a question, decision, or blocker, field it and "
        "decide what genuinely needs the owner and what you can resolve yourself -- a Builder that "
        "escalates to you should not be left waiting."
    ),
    "builder": (
        "You are the Builder for this project. You write code, run tests, publish/deploy, and make the "
        "git commits. Ground yourself in the hub -- read the doctrine, docs, decision log, and lessons, "
        "and search before non-trivial work -- but do NOT deposit anything (no handoffs, no decision "
        "mirror, no lessons). The Commander owns all hub writes; when you learn something worth recording, "
        "surface it in your reply for the Commander to deposit. When anything is unclear, ambiguous, or "
        "needs a decision, you still stop rather than guess (G1) -- but escalate to the Commander, not "
        "straight to the owner's window; the Commander is your escalation path and decides what actually "
        "needs the owner. If the Commander is unresponsive long enough that continuing to wait stops being "
        "reasonable, take it to the owner directly and say plainly that you're doing so because the "
        "Commander didn't respond. Per G10, both 'don't deposit' and this escalation path are the doctrine "
        "being exercised, not a conflict to flag."
    ),
}


def validate_role(role: str) -> None:
    """Self-explaining rejection for an unrecognized role -- same envelope
    shape as every other rejection in this codebase (app/errors.py).
    """
    if role not in VALID_ROLES:
        raise ApiError(
            422,
            "invalid_role",
            f"`role` must be one of {sorted(VALID_ROLES)}, got {role!r}. Recovery: fix the field and resend.",
        )
