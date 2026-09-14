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


def _human_ts_filter(value: datetime | None) -> Markup:
    """Human-readable UTC timestamp, wrapped for client-side local-timezone
    display. Storage/API/agent-facing output is completely untouched by
    this -- this filter only ever affects the owner-facing HTML the browser
    renders.

    Emits e.g. '<time datetime="2026-08-06T14:32:00+00:00" data-local-ts>2026-08-06 14:32 UTC</time>'.
    The `datetime` attribute carries the exact UTC instant in ISO-8601 --
    the machine-readable value app/static/localtime.js reads to convert the
    element's visible text to the viewer's own timezone on page load (and,
    for room transcript rows appended live by app/static/rooms.js, on
    arrival too). The element's own text is the *same* human-readable UTC
    string this filter has always produced -- with JS disabled, or before
    localtime.js runs, the owner sees exactly today's rendering, never a
    blank element or a raw ISO string. `data-local-ts` is the marker
    localtime.js's querySelectorAll looks for.
    """
    if value is None:
        return Markup("—")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    iso = value.isoformat()
    text = value.strftime("%Y-%m-%d %H:%M UTC")
    return Markup('<time datetime="{}" data-local-ts>{}</time>').format(iso, text)


templates.env.filters["md"] = _md_filter
templates.env.filters["human_ts"] = _human_ts_filter
