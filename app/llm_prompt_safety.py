"""Shared helpers for single-shot LLM judgment calls that wrap untrusted,
session/agent-written content in a prompt -- extracted from
app/librarian_engine.py (ADR-0010 phase 2) so it and app/room_ai.py
(ADR-0011) share exactly one implementation of the per-call-nonce
delimiter mitigation and the defensive JSON-object extraction, rather than
two copies that could silently drift. Behavior is unchanged from the
librarian's original -- this is a pure relocation.

Why a nonce and not a fixed tag name: content interpolated into a prompt
(a library entry body, a room transcript message) is written by ordinary
sessions/agents, not owner-reviewed, and is placed verbatim into the
prompt. A fixed tag name (e.g. `<entry_a_body>`) can simply be typed by
whoever wrote that content -- an embedded literal `</entry_a_body>` forges
a fake close and lets attacker text escape the intended boundary and
imitate the rest of the prompt's structure. A nonce generated fresh, AT
CALL TIME, defeats this: the content was written before this nonce ever
existed, so it cannot contain a matching closing tag except by an
astronomically unlikely guess (2^64 possibilities). `strip_boundary_token`
below is the belt-and-braces second layer for that residual case.
"""

import json
import re
import secrets

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def new_prompt_nonce() -> str:
    """A short, unpredictable-per-call token (16 hex chars, 64 bits) used to
    build one call's delimiter tag names (e.g. `<transcript-{nonce}>`).
    """
    return secrets.token_hex(8)


def strip_boundary_token(text: str | None, nonce: str) -> str | None:
    """Defense in depth (belt and braces): removes any literal occurrence
    of THIS call's nonce from content before it is interpolated into the
    prompt. The nonce is generated after the content already exists, so a
    real collision is cryptographically implausible -- this only guards the
    residual case (a coincidental match, or a future change that makes the
    nonce more guessable) without weakening the primary defense above.
    """
    if not text:
        return text
    return text.replace(nonce, "[boundary-token-removed]")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def extract_json_object(content: str) -> dict | None:
    """Strips a common ```json fence wrapper, then greedily grabs the
    outermost {...} block -- small local models routinely wrap or
    precede/follow strict JSON with commentary despite being asked not to.
    Returns None (never raises) on anything that doesn't parse as a JSON
    object -- a malformed response is an ordinary, expected outcome every
    caller must handle conservatively, never an exception path.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
