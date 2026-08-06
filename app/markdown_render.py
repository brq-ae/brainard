"""Server-side markdown rendering for the UI (phase 6 brief).

Library bodies, mirrored documents, and proposal bodies are AI-written
content and treated as untrusted for the browser context -- the raw
markdown source could legitimately contain `<script>` tags or
`javascript:` links (whether malicious or just a careless copy-paste from
somewhere). `render_markdown` below is the *only* path any of that content
takes to the browser: convert markdown -> HTML with `markdown`, then
sanitize the resulting HTML with `bleach` against a small allowlist before
Jinja marks it safe. No raw HTML from the source ever reaches the response
unescaped, and only http(s)/mailto links survive -- `javascript:`,
`data:`, etc. are stripped.
"""

import bleach
import markdown as _markdown

_MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

# Deliberately small: enough for the doctrine templates + typical
# lesson/howto/reference prose (headings, lists, code blocks, tables,
# emphasis, links) without opening the door to arbitrary embedded HTML.
_ALLOWED_TAGS = frozenset(
    {
        "p", "br", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "strong", "em", "del", "code", "pre", "blockquote",
        "ul", "ol", "li",
        "a",
        "table", "thead", "tbody", "tr", "th", "td",
    }
)

_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "th": ["align"],
    "td": ["align"],
}

# Only these URL schemes survive on href/src -- specifically excludes
# `javascript:`, `data:`, `vbscript:`, etc.
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

_cleaner = bleach.sanitizer.Cleaner(
    tags=_ALLOWED_TAGS,
    attributes=_ALLOWED_ATTRS,
    protocols=_ALLOWED_PROTOCOLS,
    strip=False,  # escape disallowed tags as visible text rather than silently dropping them
)


def render_markdown(text: str) -> str:
    """Markdown source -> sanitized HTML, safe to mark `| safe` in a
    template. Never raises on malformed/malicious input -- worst case, the
    input round-trips as escaped plain text.
    """
    if not text:
        return ""
    raw_html = _markdown.markdown(text, extensions=_MARKDOWN_EXTENSIONS)
    return _cleaner.clean(raw_html)
