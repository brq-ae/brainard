"""Room transcript export (ADR-0011 decision 1): renders a room's full
transcript as markdown or as a structured JSON dict, plus a filename
sanitizer shared by both download endpoints (app/routers/ui_rooms.py's
`GET /ui/rooms/{id}/transcript.md` and `.json`). No model is involved
anywhere in this module -- pure, deterministic formatting.

Filename/header safety: a room's `name` is owner-supplied free text with no
charset restriction at create time (app/rooms.py's `create_room` only
requires it non-empty) -- it is about to become both a downloaded filename
and the value of an HTTP `Content-Disposition` response header, so it gets
the same treatment app/llm_config.py's `validate_base_url`/`validate_api_key`
give a value headed for an outbound HTTP header: strip every
control/format/line-or-paragraph-separator character (the concrete
CRLF-response-splitting vector this is guarding against), then further
restrict to a small, boring, filesystem-safe charset. Unlike those
validators, a bad room name here isn't rejected (the room already exists;
rendering its export must always succeed) -- it degrades to a safe
fallback name instead.

ADR-0012 (stage 3, decision 11: "export lists files, does not bundle
them"): both render functions below also take the room's current
attachments and list them as references -- filename, size, who attached,
when, and the id needed to fetch it (`GET .../attachments/<id>/download`)
-- never the file's bytes. `RoomAttachment.filename` is already sanitized
through `safe_filename_component` at upload/attach time (app/attachments.py)
before it ever reaches the database, but it is routed through
`safe_filename_component` again here regardless, for the same reason this
module already treats `room.name` this way: this function's OWN contract is
"never emit an unsafe value into a rendered export/header", independent of
what any particular caller upstream already guaranteed.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC
from typing import TYPE_CHECKING, Any

from app.models import Room, RoomMessage

if TYPE_CHECKING:
    # Import only for type-checking: app.attachments imports THIS module
    # (`safe_filename_component`, just below), so a runtime import here
    # would be circular. `RoomAttachmentView` is just a type hint below --
    # never constructed or introspected at runtime by this module.
    from app.attachments import RoomAttachmentView

# Same forbidden-Unicode-category set app/llm_config.py uses: Cc (control,
# incl. C1 0x80-0x9F, e.g. NEL U+0085), Cf (format -- zero-width/bidi-
# override chars etc.), Zl (LINE SEPARATOR U+2028), Zp (PARAGRAPH SEPARATOR
# U+2029) -- every category that can carry an invisible or line-breaking
# payload, the concrete header/request-splitting injection vector a raw
# room name could otherwise smuggle into the Content-Disposition header.
_FORBIDDEN_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

# Conservative allowlist for a filename component: ASCII letters/digits and
# a small set of harmless punctuation. Deliberately excludes '/', '\\',
# ':', '"', '*', '?', '<', '>', '|' (filesystem-unsafe on at least one
# common OS, and '"' specifically would let a room name break out of the
# quoted Content-Disposition filename value) and all whitespace except a
# single space (collapsed below). Also drops all non-ASCII: a room name is
# free text with no charset requirement, so round-tripping Unicode cleanly
# isn't worth the complexity here (RFC 5987 filename*) for a plain
# admin-tool download -- a readable, safe fallback is enough.
_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 _.-]+")
FILENAME_MAX_LENGTH = 80
_FALLBACK_FILENAME = "room"


def safe_filename_component(name: str, *, fallback: str = _FALLBACK_FILENAME) -> str:
    """Reduces arbitrary owner-supplied text to a short, plain-ASCII string
    safe to use both as a downloaded filename and inside an HTTP
    `Content-Disposition` header value. CRLF or any other control/format/
    line-separator character can never reach the header this way. Falls
    back to `fallback` if nothing safe survives (e.g. a name that is
    entirely emoji/CJK/control characters) -- an export must always
    succeed, never 500 on a room name the owner already saved.
    """
    stripped = "".join(c for c in name if unicodedata.category(c) not in _FORBIDDEN_UNICODE_CATEGORIES)
    cleaned = _FILENAME_UNSAFE_RE.sub(" ", stripped)
    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    # A leading/trailing '.' is a hidden-file (Unix) / reserved-name risk on
    # some filesystems -- trimmed defensively even though it's already a
    # fairly boring string at this point.
    collapsed = collapsed.strip(". ")
    if not collapsed:
        return fallback
    return collapsed[:FILENAME_MAX_LENGTH].strip(". ") or fallback


def transcript_filename(room: Room, extension: str) -> str:
    return f"{safe_filename_component(room.name)}-transcript.{extension}"


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _message_header(m: RoomMessage) -> str:
    marker = " [system]" if m.kind == "system" else ""
    return f"**{m.sender}**{marker} (seq {m.seq}, {_iso(m.created_at)})"


def _attachment_markdown_line(view: RoomAttachmentView) -> str:
    a = view.attachment
    safe_name = safe_filename_component(a.filename)
    return f'- "{safe_name}" ({view.byte_size} bytes) -- attached by {a.uploaded_by} at {_iso(a.created_at)} -- id: {a.id}'


def _attachment_json(view: RoomAttachmentView) -> dict[str, Any]:
    a = view.attachment
    return {
        "id": a.id,
        "filename": safe_filename_component(a.filename),
        "byte_size": view.byte_size,
        "uploaded_by": a.uploaded_by,
        "attached_at": _iso(a.created_at),
    }


def render_transcript_markdown(
    room: Room,
    members: list[str],
    sides: dict[str, str | None],
    messages: list[RoomMessage],
    attachments: list[RoomAttachmentView] | None = None,
) -> str:
    """Full transcript as markdown: a header block (name, mode/topic,
    members+sides, status, created/closed times, message count) followed by
    an "Attachments" section (ADR-0012 decision 11: listed as references --
    filename, size, who attached, when, fetch id -- never bundled bytes),
    then one block per message, oldest-first (chat reading order, same as
    `app.rooms.get_all_messages`) -- `**sender** (seq, ISO time)` then the
    message text on the following line(s), with `kind='system'` messages
    marked inline. No model involved; this is pure formatting over
    already-stored data.

    `attachments` defaults to `None` (treated as empty) rather than a
    required parameter, so existing callers (and tests) that predate
    ADR-0012 stage 3 keep working unchanged.
    """
    attachments = attachments or []
    member_line = ", ".join(f"{m} ({sides[m]})" if sides.get(m) else m for m in members) or "(none)"
    lines = [
        f"# {room.name}",
        "",
        f"- Mode: {room.mode}",
    ]
    if room.topic:
        lines.append(f"- Topic: {room.topic}")
    lines.append(f"- Members: {member_line}")
    status_line = f"- Status: {room.status}"
    if room.close_reason:
        status_line += f" (reason: {room.close_reason})"
    lines.append(status_line)
    lines.append(f"- Created: {_iso(room.created_at)}")
    if room.closed_at:
        lines.append(f"- Closed: {_iso(room.closed_at)}")
    lines.append(f"- Messages: {len(messages)}")
    lines.append("")
    lines.append("## Attachments")
    lines.append("")
    if not attachments:
        lines.append("*(none)*")
    else:
        lines.extend(_attachment_markdown_line(v) for v in attachments)
    lines.append("")
    lines.append("---")
    lines.append("")

    if not messages:
        lines.append("*(no messages)*")
    else:
        for m in messages:
            lines.append(_message_header(m))
            lines.append(m.text)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_transcript_json(
    room: Room,
    members: list[str],
    sides: dict[str, str | None],
    messages: list[RoomMessage],
    attachments: list[RoomAttachmentView] | None = None,
) -> dict[str, Any]:
    """Structured JSON form: room metadata + an `attachments` array (ADR-0012
    decision 11: references only, never bundled bytes) + a flat `messages`
    array, oldest-first -- machine-consumption counterpart to
    `render_transcript_markdown` above, same underlying data. See that
    function's docstring for the `attachments` parameter's default.
    """
    attachments = attachments or []
    return {
        "room": {
            "id": room.id,
            "name": room.name,
            "mode": room.mode,
            "topic": room.topic,
            "members": [{"name": m, "side": sides.get(m)} for m in members],
            "status": room.status,
            "close_reason": room.close_reason,
            "created_at": _iso(room.created_at),
            "closed_at": _iso(room.closed_at),
            "message_count": len(messages),
        },
        "attachments": [_attachment_json(v) for v in attachments],
        "messages": [
            {
                "seq": m.seq,
                "sender": m.sender,
                "text": m.text,
                "kind": m.kind,
                "created_at": _iso(m.created_at),
            }
            for m in messages
        ],
    }
