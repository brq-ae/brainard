"""Best-effort owner notification on room close (ADR-0006 decisions 5-6), and
on an agent parking against the owner-open gate (ADR-0014 decision 8).

Reads the CURRENT notification_config (app/notifications.py's
`current_config` -- the owner-managed ntfy channel, same one bootstrap's
"Notifications" subsection points sessions at) and POSTs a ping to it.

Best-effort by design: a room close/post is the real, valuable state change
that already committed to the database by the time this runs; a missed ping
is recoverable (the owner can still see the room via the API), but a broken
room operation is not. Every failure mode -- no config configured, network
error, timeout -- is caught here and logged, and NEVER propagates to the
caller.
"""

import logging
import re
import unicodedata

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Room
from app.notifications import current_config

logger = logging.getLogger(__name__)

NOTIFY_TIMEOUT_SECS = 5.0

# Independent review finding: `agent_name` here is `post_message`'s claimed
# `sender` -- a client-supplied, untrusted string (app/schemas.py's
# RoomPostMessageRequest.sender caps its length but not its charset) that
# `notify_owner_open_pending` below interpolates, unescaped, into an ntfy
# HTTP `Title` header and push body the owner reads on his phone. Same bug
# class app/notifications.py's `validate_topic`/`validate_ntfy_url` were
# added to close for the notification-channel config itself: a crafted
# value can carry a CRLF (HTTP header injection/response splitting via the
# `Title` header), other control/format characters, or just enough raw text
# to read as forged instructions rather than an honest agent name. Sanitised
# with the same technique app/room_export.py's `safe_filename_component`
# already uses (strip every Unicode Cc/Cf/Zl/Zp category character, collapse
# whitespace, cap length) rather than validate_topic's strict allow-list --
# an agent name is free-form display text, not a value with a legal charset
# of its own, so stripping the specific categories that are never safe to
# interpolate is the right shape here, not rejecting anything outside
# [A-Za-z0-9_-].
_FORBIDDEN_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
AGENT_NAME_NOTIFY_MAX_LENGTH = 255


def _sanitize_agent_name_for_notification(agent_name: str | None) -> str | None:
    """Reduces a claimed `sender`/`agent_name` to a short, control-character-
    free string safe to interpolate into an ntfy title/body. Returns None
    (same as an unknown/poll-triggered agent name) if nothing safe survives
    -- `notify_owner_open_pending` already renders None as the honest "An
    agent" fallback, so a name that sanitises to nothing degrades to that
    same wording rather than sending an empty/misleading name.
    """
    if agent_name is None:
        return None
    stripped = "".join(c for c in agent_name if unicodedata.category(c) not in _FORBIDDEN_UNICODE_CATEGORIES)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    if not collapsed:
        return None
    return collapsed[:AGENT_NAME_NOTIFY_MAX_LENGTH]


async def _send_ntfy(url: str, title: str, body: str) -> None:
    """The actual outbound POST, factored out from `notify_room_closed` so
    tests can monkeypatch just this one call (mirrors the
    `_insert_config`/`_insert_deposit` "factor out the one risky call" style
    used elsewhere for retry-loop testing).
    """
    async with httpx.AsyncClient(timeout=NOTIFY_TIMEOUT_SECS) as http_client:
        response = await http_client.post(url, content=body.encode("utf-8"), headers={"Title": title, "Priority": "default"})
        response.raise_for_status()


async def notify_room_closed(db: AsyncSession, room: Room) -> None:
    """Fires a "room closed" ntfy ping if a channel is configured and the
    room opted in (`notify_on_close`). Never raises.
    """
    if not room.notify_on_close:
        return
    try:
        config = await current_config(db)
        if config is None:
            logger.info(
                "room %s ('%s') closed (%s) -- no notification channel configured, skipping ping",
                room.id,
                room.name,
                room.close_reason,
            )
            return
        url = f"{config.ntfy_url}/{config.topic}"
        title = f"Brain room: {room.name}"
        body = f"Room '{room.name}' closed ({room.close_reason}) after {room.message_count} messages."
        await _send_ntfy(url, title, body)
    except Exception:
        # Best-effort: any failure here (no config, DNS/connection error,
        # timeout, non-2xx response the client library raises for, ...) is
        # logged and swallowed -- it must never break the room operation
        # (close/post) that triggered it.
        logger.exception(
            "best-effort room-close notification failed for room %s ('%s') -- room operation still succeeded",
            room.id,
            room.name,
        )


async def notify_owner_open_pending(db: AsyncSession, room: Room, agent_name: str | None) -> None:
    """ADR-0014 decision 8: best-effort ntfy ping fired when an agent parks
    on a room that requires the owner to post first and hasn't yet. Same
    "read current_config, POST to it, swallow every failure" shape as
    `notify_room_closed` above -- the only difference is the trigger and the
    message. The one-shot guard (`Room.owner_open_reminder_sent_at`) is the
    CALLER's responsibility (app/rooms.py's `_maybe_ping_owner_room_not_opened`,
    which sets it under the room's row lock before ever calling this) -- this
    function only sends, unconditionally, same division of labor
    `notify_room_closed` has with its own callers (which decide whether a
    close/post ought to notify at all; this only does the sending).

    `agent_name` is the claimed `sender` when known (`post_message`'s
    rejection -- decision 1/2) or None when not (`poll_messages`'s early
    return -- decision 3): a long-poll read carries no sender at all (see
    that function's docstring), so a poll-triggered park can only ever say
    "an agent", never name one. Stated plainly, per the ADR's own "honest
    limitation" framing, rather than the message pretending to know more
    than it does.

    `agent_name` is untrusted, client-supplied text -- sanitised here (see
    `_sanitize_agent_name_for_notification`'s docstring) before it is ever
    interpolated into the title/body, independent of whatever validation
    the caller applied to the original `sender`.
    """
    try:
        config = await current_config(db)
        if config is None:
            logger.info(
                "room %s ('%s') has an agent waiting for the owner to open it -- no notification channel "
                "configured, skipping ping",
                room.id,
                room.name,
            )
            return
        url = f"{config.ntfy_url}/{config.topic}"
        title = f"Brain room waiting: {room.name}"
        who = _sanitize_agent_name_for_notification(agent_name) or "An agent"
        body = f"{who} is waiting in room '{room.name}' for you to post the first message."
        await _send_ntfy(url, title, body)
    except Exception:
        # Best-effort: same posture as notify_room_closed above -- must
        # never turn a room's real rejection/poll response into a 500.
        logger.exception(
            "best-effort owner-open-pending notification failed for room %s ('%s') -- request still handled "
            "normally",
            room.id,
            room.name,
        )
