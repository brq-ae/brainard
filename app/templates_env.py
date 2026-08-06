"""Jinja2 template environment for the UI (phase 6 brief).

Autoescape is explicitly forced ON (not just relying on Starlette's
default) -- entries are AI-written content and templates must never emit
raw, attacker-controllable strings into HTML without escaping. The one
sanctioned exception is `render_markdown`'s already-sanitized HTML output,
applied explicitly per-call via the `md` filter below (`{{ body | md }}`),
never a blanket `| safe` on raw text.
"""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.markdown_render import render_markdown

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.autoescape = True  # belt-and-braces: explicit, never left to a default that could change


def _md_filter(text: str | None) -> Markup:
    """Renders markdown to sanitized HTML and marks it safe for the
    template -- the *only* place raw HTML is allowed to reach the response,
    and only after `render_markdown` has already stripped/escaped anything
    dangerous (see app/markdown_render.py).
    """
    return Markup(render_markdown(text or ""))


def _human_ts_filter(value: datetime | None) -> str:
    """Human-readable UTC timestamp, e.g. '2026-08-06 14:32 UTC'. Every
    stored timestamp is already timezone-aware UTC; this only formats it."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["md"] = _md_filter
templates.env.filters["human_ts"] = _human_ts_filter
