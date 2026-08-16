"""Shared notification-channel-config operations (owner-managed ntfy
channel). Used by the owner API (app/routers/notifications.py), the
bootstrap "Notifications" subsection (app/routers/bootstrap.py, read-only),
and the owner UI (app/routers/ui_notifications.py) -- kept here once so
validation and versioning logic never drifts between the two write surfaces
(same "shared module" pattern as app/machines.py's mint/revoke and
app/doctrine.py's version lookups).

Immutable rows, supersede-never-erase (contracts-v1.md Principles): every
config change is a new version, never an edit.

`ntfy_url`/`topic` validation is deliberately strict (security review
finding, 2026-08-16): both values are interpolated, unescaped, into (a) the
fleet-wide bootstrap markdown every session reads as instructions, and (b)
a raw `curl` command those sessions are told to run verbatim. A crafted
value is therefore both a markdown/prompt-injection vector (e.g. a topic
containing a ``` fence breaks out of the code block and injects arbitrary
prose that reads as legitimate instructions) and a shell-injection vector
(backticks, `$()`, `;`, `|`, etc. in a value that ends up on a command
line). Validation here is the only gate -- there is no output-side
escaping, so nothing that fails these checks may ever be stored.
"""

import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.errors import ApiError
from app.models import NotificationConfig

# ntfy's legal topic charset (https://docs.ntfy.sh/publish/#topics): letters,
# digits, underscore, hyphen. Enforced as a strict allow-list, not a
# deny-list -- a topic is interpolated verbatim into bootstrap markdown
# *inside* a code fence and into a curl command line, so anything outside
# this charset (whitespace, slashes, backticks, code fences, shell
# metacharacters, ...) is rejected outright rather than special-cased. This
# allow-list already excludes every Unicode control/format/separator
# character (Cc/Cf/Zl/Zp categories, incl. C1 controls 0x80-0x9F, NEL
# U+0085, LINE/PARAGRAPH SEPARATOR U+2028/U+2029) -- confirmed by the tests
# below (delta review, 2026-08-16) -- since only ASCII [A-Za-z0-9_-] passes
# `_TOPIC_PATTERN.fullmatch` at all; no separate check is needed here.
TOPIC_MAX_LENGTH = 64
_TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % TOPIC_MAX_LENGTH)

# ntfy_url is a URL, not a bare token, so an allow-list charset would be too
# restrictive (host, port, path segments all use characters a topic
# shouldn't) -- deny-list instead: whitespace, control characters, and shell
# metacharacters are never valid in a channel base URL that gets pasted into
# a curl command line.
NTFY_URL_MAX_LENGTH = 512
_NTFY_URL_FORBIDDEN_CHARS = set(" \t\n\r\v\f`;&|<>(){}[]!*?~^\"'\\$")
# Delta review (2026-08-16): the original check (`ord(c) < 0x20 or == 0x7F`)
# only covered ASCII C0 controls + DEL. Broadened to every Unicode category
# that can carry an invisible/line-breaking payload into the rendered curl
# line: Cc (control -- now including C1 0x80-0x9F, e.g. NEL U+0085), Cf
# (format -- zero-width/bidi-override chars etc.), Zl (LINE SEPARATOR
# U+2028), Zp (PARAGRAPH SEPARATOR U+2029).
_FORBIDDEN_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

MAX_INSERT_ATTEMPTS = 3
_RECOVERY_NOTIFICATIONS_CONFLICT = "resend the same request unchanged; the version number will be recomputed automatically"


async def current_config(db: AsyncSession) -> NotificationConfig | None:
    """The highest-numbered version, if any has ever been written --
    "current" is simply "latest by version", same reasoning as
    app/doctrine.py's `current_global`.
    """
    return await db.scalar(select(NotificationConfig).order_by(NotificationConfig.version.desc()).limit(1))


async def history(db: AsyncSession) -> list[NotificationConfig]:
    """Every version ever written, newest first -- supersede-never-erase
    means this is a plain full read, not filtered to "current" (mirrors
    app/doctrine.py's `version_history`).
    """
    rows = (await db.scalars(select(NotificationConfig).order_by(NotificationConfig.version.desc()))).all()
    return list(rows)


async def next_version(db: AsyncSession) -> int:
    """The version number the next write should use -- 1 if none exists
    yet, else one past the current highest (mirrors app/doctrine.py's
    `next_version`, minus the (kind, project) partitioning this table
    doesn't need -- see NotificationConfig.version's docstring).
    """
    current = await db.scalar(select(NotificationConfig.version).order_by(NotificationConfig.version.desc()).limit(1))
    return (current or 0) + 1


def validate_ntfy_url(ntfy_url: str) -> None:
    if len(ntfy_url) > NTFY_URL_MAX_LENGTH:
        raise ApiError(
            422,
            "invalid_ntfy_url",
            f"ntfy_url exceeds the {NTFY_URL_MAX_LENGTH}-character limit. Recovery: shorten it, resend.",
        )

    if any(unicodedata.category(c) in _FORBIDDEN_UNICODE_CATEGORIES for c in ntfy_url):
        raise ApiError(
            422,
            "invalid_ntfy_url",
            "ntfy_url contains a control, format, or line/paragraph-separator character (e.g. a C0/C1 "
            "control, NEL, U+2028, U+2029) -- never valid in a URL, and a shell/markdown-injection vector "
            "here (this value is interpolated verbatim into a curl command and into fleet-wide bootstrap "
            "markdown). Recovery: remove it, resend.",
        )

    forbidden = sorted({c for c in ntfy_url if c in _NTFY_URL_FORBIDDEN_CHARS})
    if forbidden:
        raise ApiError(
            422,
            "invalid_ntfy_url",
            f"ntfy_url contains disallowed character(s) {forbidden} -- whitespace, backticks, and shell "
            "metacharacters are never valid here (this value is interpolated verbatim into a curl command "
            "and into fleet-wide bootstrap markdown). Recovery: remove them, resend.",
        )

    parsed = urlparse(ntfy_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ApiError(
            422,
            "invalid_ntfy_url",
            f"'{ntfy_url}' is not a valid http(s) URL. Recovery: fix the URL (must start with http:// or "
            "https:// and include a host), resend.",
        )


def validate_topic(topic: str) -> None:
    if not _TOPIC_PATTERN.fullmatch(topic):
        raise ApiError(
            422,
            "invalid_topic",
            f"topic must be 1-{TOPIC_MAX_LENGTH} characters from ntfy's legal charset -- letters, digits, "
            "'_', '-' only. This value is interpolated verbatim into a curl command and into fleet-wide "
            "bootstrap markdown, so anything else (whitespace, slashes, backticks, code fences, shell "
            f"metacharacters, ...) is rejected outright. Recovery: use only [A-Za-z0-9_-], max "
            f"{TOPIC_MAX_LENGTH} chars, resend.",
        )


async def _insert_config(
    db: AsyncSession, version: int, ntfy_url: str, topic: str, note: str | None
) -> NotificationConfig:
    """The single-row insert attempt, factored out so `create_version`'s
    retry loop below can catch exactly this call's IntegrityError (mirrors
    app/routers/deposits.py's `_insert_deposit` / retry-loop split).
    """
    row = NotificationConfig(
        id=str(ULID()),
        version=version,
        ntfy_url=ntfy_url,
        topic=topic,
        note=note,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return row


async def create_version(db: AsyncSession, ntfy_url: str, topic: str, note: str | None) -> NotificationConfig:
    """Validates and writes the next version. Raises ApiError(422) on a bad
    ntfy_url/topic -- self-explaining, same recovery style as every other
    validation rejection in the contract.

    Concurrent POSTs computing the same "next version" collide on
    `notification_configs`' unique version index (IntegrityError) -- bounded
    in-server retry, recomputing the version fresh each attempt, same
    pattern as app/routers/deposits.py's insert-conflict loop. There is no
    idempotency key here (unlike deposits), so there is no replay branch --
    every attempt is either a fresh successful insert or a genuine
    collision to retry.
    """
    validate_ntfy_url(ntfy_url)
    validate_topic(topic)

    for attempt in range(1, MAX_INSERT_ATTEMPTS + 1):
        version = await next_version(db)
        try:
            return await _insert_config(db, version, ntfy_url, topic, note)
        except IntegrityError:
            await db.rollback()
            if attempt < MAX_INSERT_ATTEMPTS:
                continue
            # Pathological contention: every attempt collided. Fail loudly
            # but through the contract's envelope, never a raw 500 -- every
            # rejection is self-explaining with a scripted recovery.
            raise ApiError(
                503,
                "notifications_config_conflict_retry",
                "A concurrent write collided with this request repeatedly and in-server retries did not "
                f"resolve it; nothing was stored. Recovery: {_RECOVERY_NOTIFICATIONS_CONFLICT}.",
            ) from None

    # Unreachable: the loop above always returns or raises.
    raise AssertionError("unreachable")
