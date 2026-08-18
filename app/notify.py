"""Best-effort owner notification on room close (ADR-0006 decisions 5-6).

Reads the CURRENT notification_config (app/notifications.py's
`current_config` -- the owner-managed ntfy channel, same one bootstrap's
"Notifications" subsection points sessions at) and POSTs a close ping to it.

Best-effort by design: a room close/post is the real, valuable state change
that already committed to the database by the time this runs; a missed ping
is recoverable (the owner can still see the room via the API), but a broken
room operation is not. Every failure mode -- no config configured, network
error, timeout -- is caught here and logged, and NEVER propagates to the
caller.
"""

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Room
from app.notifications import current_config

logger = logging.getLogger(__name__)

NOTIFY_TIMEOUT_SECS = 5.0


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
