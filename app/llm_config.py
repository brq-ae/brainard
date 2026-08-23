"""Shared LLM-provider-config operations (owner-managed, ADR-0010 phase 1:
pluggable librarian runtimes -- the built-in LLM client's configuration).
Used by the owner API (app/routers/llm_config.py) and the owner UI
(app/routers/ui_llm.py) -- kept here once so validation/versioning/masking
logic never drifts between the two write surfaces (same "shared module"
pattern as app/notifications.py, which this module deliberately mirrors
structure-for-structure).

Immutable rows, supersede-never-erase (contracts-v1.md Principles): every
config change is a new version, never an edit.

`base_url` validation mirrors app/notifications.py's `validate_ntfy_url`
rigor: this value is about to be interpolated into an outbound HTTP request
line (app/llm_client.py) built from owner input, so the same
control/format/line-or-paragraph-separator/shell-metacharacter deny-list
applies, for the same reason -- nothing that fails these checks may ever be
stored.

`api_key` validation is the security-critical one here: the key is placed
verbatim into an outbound `Authorization: Bearer <key>` HTTP header
(app/llm_client.py). A key containing a CR/LF (or any other control
character) is a textbook HTTP header/request-splitting injection vector --
rejected outright, no exceptions, regardless of what the value is otherwise
used for.

ADR-0010 decision 3: the API key is NOT encrypted at rest (an encryption
key living in the same env file on the same host is security theatre) --
mitigated instead by (a) `resolve_llm_config`'s env-var override below,
which stores nothing in the database at all, and (b) masking the key in
every API/UI response (never served back in full once stored) -- see
`mask_api_key` and the router-side response builders.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.config import get_settings
from app.errors import ApiError
from app.models import LlmConfig

# base_url is a URL (not a bare token), so an allow-list charset would be
# too restrictive (host, port, path segments all use characters a stricter
# allow-list would reject) -- deny-list instead, same set
# app/notifications.py's validate_ntfy_url uses: whitespace, control
# characters, and shell/markdown metacharacters are never valid in a
# provider base URL.
BASE_URL_MAX_LENGTH = 512
_BASE_URL_FORBIDDEN_CHARS = set(" \t\n\r\v\f`;&|<>(){}[]!*?~^\"'\\$")
# Every Unicode category that can carry an invisible or line-breaking
# payload: Cc (control, incl. C1 0x80-0x9F, e.g. NEL U+0085), Cf (format --
# zero-width/bidi-override chars etc.), Zl (LINE SEPARATOR U+2028), Zp
# (PARAGRAPH SEPARATOR U+2029). Same set app/notifications.py's
# validate_ntfy_url rejects, same reasoning.
_FORBIDDEN_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

# model is a bare identifier, not a URL -- an allow-list is the right shape
# here (mirrors app/notifications.py's topic allow-list). Covers every
# real-world model name seen in the wild: "llama3.1", "gpt-4o-mini",
# "meta-llama/Llama-3.1-8B-Instruct", "gpt-4:free", "qwen2.5:14b-instruct".
MODEL_MAX_LENGTH = 200
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,%d}$" % MODEL_MAX_LENGTH)

# api_key: no charset allow-list (real provider keys use a wide range of
# base64url-ish characters) -- the only hard rule is "no control characters
# or newlines", since this value is placed verbatim into an HTTP header
# (header/request-splitting injection otherwise). See module docstring.
API_KEY_MAX_LENGTH = 500

MAX_INSERT_ATTEMPTS = 3
_RECOVERY_LLM_CONFIG_CONFLICT = "resend the same request unchanged; the version number will be recomputed automatically"


async def current_config(db: AsyncSession) -> LlmConfig | None:
    """The highest-numbered version, if any has ever been written --
    "current" is simply "latest by version" (mirrors
    app/notifications.py's `current_config`).
    """
    return await db.scalar(select(LlmConfig).order_by(LlmConfig.version.desc()).limit(1))


async def history(db: AsyncSession) -> list[LlmConfig]:
    """Every version ever written, newest first -- supersede-never-erase
    means this is a plain full read, not filtered to "current" (mirrors
    app/notifications.py's `history`).
    """
    rows = (await db.scalars(select(LlmConfig).order_by(LlmConfig.version.desc()))).all()
    return list(rows)


async def next_version(db: AsyncSession) -> int:
    """The version number the next write should use -- 1 if none exists
    yet, else one past the current highest (mirrors app/notifications.py's
    `next_version`).
    """
    current = await db.scalar(select(LlmConfig.version).order_by(LlmConfig.version.desc()).limit(1))
    return (current or 0) + 1


def validate_base_url(base_url: str) -> None:
    if len(base_url) > BASE_URL_MAX_LENGTH:
        raise ApiError(
            422,
            "invalid_base_url",
            f"base_url exceeds the {BASE_URL_MAX_LENGTH}-character limit. Recovery: shorten it, resend.",
        )

    if any(unicodedata.category(c) in _FORBIDDEN_UNICODE_CATEGORIES for c in base_url):
        raise ApiError(
            422,
            "invalid_base_url",
            "base_url contains a control, format, or line/paragraph-separator character (e.g. a C0/C1 "
            "control, NEL, U+2028, U+2029) -- never valid in a URL, and a header/request-injection vector "
            "here (this value is used to build an outbound HTTP request line). Recovery: remove it, resend.",
        )

    forbidden = sorted({c for c in base_url if c in _BASE_URL_FORBIDDEN_CHARS})
    if forbidden:
        raise ApiError(
            422,
            "invalid_base_url",
            f"base_url contains disallowed character(s) {forbidden} -- whitespace and shell metacharacters "
            "are never valid here. Recovery: remove them, resend.",
        )

    try:
        parsed = urlparse(base_url)
    except ValueError:
        # Post-CVE-2023-24329 guard in urllib.parse: it raises ValueError
        # (rather than silently mis-parsing) when the netloc contains a
        # character that NFKC-normalizes into a URL-structural character --
        # e.g. U+FF0F (fullwidth solidus) or U+FF20 (fullwidth commercial-at).
        # Those characters are category Po (not Cc/Cf/Zl/Zp) and aren't in
        # the ASCII forbidden-chars set above, so they reach this point
        # undetected; caught here and turned into the same clean 422 as any
        # other malformed URL, never a raw 500.
        raise ApiError(
            422,
            "invalid_base_url",
            f"'{base_url}' is not a valid http(s) URL. Recovery: fix the URL (must start with http:// or "
            "https:// and include a host, using only ordinary ASCII URL characters), resend.",
        ) from None

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ApiError(
            422,
            "invalid_base_url",
            f"'{base_url}' is not a valid http(s) URL. Recovery: fix the URL (must start with http:// or "
            "https:// and include a host), resend.",
        )


def validate_model(model: str) -> None:
    if not model.strip():
        raise ApiError(422, "invalid_model", "model is required and cannot be blank. Recovery: provide a model name, resend.")
    if not _MODEL_PATTERN.fullmatch(model):
        raise ApiError(
            422,
            "invalid_model",
            f"model must be 1-{MODEL_MAX_LENGTH} characters using only letters, digits, and '.', '_', "
            "'-', ':', '/' -- the common model-name charset (e.g. 'llama3.1', 'gpt-4o-mini', "
            "'meta-llama/Llama-3.1-8B-Instruct'). Recovery: fix the model name, resend.",
        )


def validate_api_key(api_key: str) -> None:
    """Called only when an api_key is actually supplied -- it is otherwise
    fully optional (Ollama and other local endpoints need none).
    """
    if not api_key.strip():
        raise ApiError(
            422,
            "invalid_api_key",
            "api_key, if supplied, cannot be blank/whitespace-only. Recovery: omit it entirely, or provide a "
            "real key, resend.",
        )
    if len(api_key) > API_KEY_MAX_LENGTH:
        raise ApiError(
            422,
            "invalid_api_key",
            f"api_key exceeds the {API_KEY_MAX_LENGTH}-character limit. Recovery: shorten it, resend.",
        )
    # Security-critical: this value goes verbatim into an outbound
    # `Authorization: Bearer <key>` header (app/llm_client.py). A
    # CR/LF -- or any other control/format/line-separator character -- is a
    # header/request-splitting injection vector and is rejected outright,
    # same category set as validate_base_url above.
    if any(unicodedata.category(c) in _FORBIDDEN_UNICODE_CATEGORIES for c in api_key):
        raise ApiError(
            422,
            "invalid_api_key",
            "api_key contains a control, format, or line/paragraph-separator character (e.g. a newline, "
            "CR/LF, or C1 control) -- never valid here, and an HTTP header-injection vector (this value is "
            "placed verbatim into an outbound Authorization header). Recovery: remove it, resend.",
        )


async def _insert_config(
    db: AsyncSession, version: int, base_url: str, model: str, api_key: str | None, note: str | None
) -> LlmConfig:
    """The single-row insert attempt, factored out so `create_version`'s
    retry loop below can catch exactly this call's IntegrityError (mirrors
    app/notifications.py's `_insert_config` / app/routers/deposits.py's
    `_insert_deposit` split).
    """
    row = LlmConfig(
        id=str(ULID()),
        version=version,
        base_url=base_url,
        model=model,
        api_key=api_key,
        note=note,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return row


async def create_version(
    db: AsyncSession, base_url: str, model: str, api_key: str | None, note: str | None
) -> LlmConfig:
    """Validates and writes the next version. Raises ApiError(422) on a bad
    base_url/model/api_key -- self-explaining, same recovery style as every
    other validation rejection in the contract.

    Concurrent POSTs computing the same "next version" collide on
    `llm_configs`' unique version index (IntegrityError) -- bounded
    in-server retry, recomputing the version fresh each attempt, same
    pattern as app/notifications.py's `create_version` (itself mirroring
    app/routers/deposits.py's insert-conflict loop). No idempotency key
    here, so no replay branch -- every attempt is either a fresh successful
    insert or a genuine collision to retry.
    """
    validate_base_url(base_url)
    validate_model(model)
    if api_key is not None:
        validate_api_key(api_key)

    for attempt in range(1, MAX_INSERT_ATTEMPTS + 1):
        version = await next_version(db)
        try:
            return await _insert_config(db, version, base_url, model, api_key, note)
        except IntegrityError:
            await db.rollback()
            if attempt < MAX_INSERT_ATTEMPTS:
                continue
            # Pathological contention: every attempt collided. Fail loudly
            # but through the contract's envelope, never a raw 500 -- every
            # rejection is self-explaining with a scripted recovery.
            raise ApiError(
                503,
                "llm_config_conflict_retry",
                "A concurrent write collided with this request repeatedly and in-server retries did not "
                f"resolve it; nothing was stored. Recovery: {_RECOVERY_LLM_CONFIG_CONFLICT}.",
            ) from None

    # Unreachable: the loop above always returns or raises.
    raise AssertionError("unreachable")


# --- Effective config resolution (env override vs. stored DB version) ---


@dataclass(frozen=True)
class EffectiveLlmConfig:
    """The config the built-in librarian (phase 2) will actually use.
    `source` distinguishes where it came from -- "env" (LLM_BASE_URL/
    LLM_MODEL set, nothing stored), "db" (the current stored version), or
    None (nothing configured either way). `version`/`note` are only ever
    set for source="db" (an env override has no version/note).
    """

    base_url: str | None
    model: str | None
    api_key: str | None
    source: Literal["env", "db"] | None
    version: int | None = None
    note: str | None = None


async def resolve_llm_config(db: AsyncSession) -> EffectiveLlmConfig:
    """ADR-0010 decision 3: "an environment variable takes precedence over
    the stored value[s] and stores nothing in the database". Both
    `LLM_BASE_URL` and `LLM_MODEL` must be set for the env override to
    apply -- a partial override (e.g. only LLM_BASE_URL set) falls through
    to the stored DB config instead, same as if neither were set, since a
    half-specified provider isn't usable either way.
    """
    settings = get_settings()
    if settings.llm_base_url and settings.llm_model:
        return EffectiveLlmConfig(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            source="env",
        )

    current = await current_config(db)
    if current is None:
        return EffectiveLlmConfig(base_url=None, model=None, api_key=None, source=None)

    return EffectiveLlmConfig(
        base_url=current.base_url,
        model=current.model,
        api_key=current.api_key,
        source="db",
        version=current.version,
        note=current.note,
    )


# --- Model discovery source resolution (GET {base_url}/models -- app/
# llm_client.py's `list_provider_models`, app/routers/llm_config.py's
# POST /v1/llm-config/models and its UI equivalent) ---


async def resolve_models_source(db: AsyncSession, base_url: str | None, api_key: str | None) -> tuple[str, str | None]:
    """Resolve which base_url/api_key to probe for `GET {base_url}/models`.

    Deliberately all-or-nothing on `base_url`, not independent per-field
    fallback: when the caller supplies a `base_url` (discovering models for
    a provider they haven't saved yet), the `api_key` used is EXACTLY what
    they supplied (None if they left it blank) -- it never silently falls
    back to whatever key happens to be stored for a *different*,
    already-saved provider. Mixing an old provider's secret key into a
    request aimed at a new, owner-typed host would be a real (if narrow)
    key-leak risk, not a convenience worth the trade. Only when `base_url`
    is omitted entirely does resolution fall through to the effective
    stored/env config, where base_url and api_key are guaranteed to be the
    same trusted pairing.

    Raises `ApiError(503, "no_llm_provider_configured", ...)` -- same code
    app/room_ai.py's `run_action` uses for the identical condition -- when
    neither an explicit `base_url` nor an effective one resolves.
    `base_url`/`api_key`, when explicitly supplied, are validated with the
    same rigor as `create_version` (header-injection/URL-injection guards)
    since they are about to be used to build an outbound request exactly
    like a stored config would be.
    """
    if base_url:
        validate_base_url(base_url)
        if api_key:
            validate_api_key(api_key)
        return base_url, (api_key or None)

    effective = await resolve_llm_config(db)
    if not effective.base_url:
        raise ApiError(
            503,
            "no_llm_provider_configured",
            "No LLM provider is configured -- set one up first (owner UI: /ui/llm, or POST /v1/llm-config), "
            "or supply base_url directly in this request.",
        )
    return effective.base_url, effective.api_key


# --- API-key masking (never serve the raw key back, once stored) ---


def mask_api_key(api_key: str | None) -> tuple[bool, str | None]:
    """Returns (api_key_set, hint) -- `hint` is a last-4-characters
    fragment (e.g. "...ab12"), never the full key, and only ever populated
    when a key is actually set. Used by both the API and UI response
    builders (app/routers/llm_config.py, app/routers/ui_llm.py) so the raw
    value is masked in exactly one place.
    """
    if not api_key:
        return False, None
    if len(api_key) <= 4:
        return True, "..." + ("*" * len(api_key))
    return True, "..." + api_key[-4:]
