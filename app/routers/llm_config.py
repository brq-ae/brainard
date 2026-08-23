"""LLM provider config -- POST/GET /v1/llm-config, POST /v1/llm-config/test
(ADR-0010 phase 1: pluggable librarian runtimes, built-in LLM client
configuration).

Owner-token only: the owner decides which LLM provider the built-in
librarian (phase 2, not built yet) uses -- same trust posture as
notifications config and doctrine (contracts-v1.md §4: "the one collection
where full trust does not apply"). Immutable rows, supersede-never-erase --
every write is a new version, never an edit; see app/llm_config.py for the
shared validation/versioning/masking logic also used by the owner UI
(app/routers/ui_llm.py).

The api_key is NEVER returned in full by any response here -- see
app/llm_config.py's `mask_api_key` and the response builders below.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.llm_client import test_llm_connection
from app.llm_config import EffectiveLlmConfig, create_version, mask_api_key, resolve_llm_config
from app.llm_config import history as config_history
from app.models import LlmConfig
from app.schemas import (
    LlmConfigCreateRequest,
    LlmConfigGetResponse,
    LlmConfigResponse,
    LlmConfigTestResponse,
    LlmEffectiveConfig,
)

router = APIRouter(prefix="/v1/llm-config", tags=["llm-config"])


def _to_response(row: LlmConfig) -> LlmConfigResponse:
    api_key_set, api_key_hint = mask_api_key(row.api_key)
    return LlmConfigResponse(
        version=row.version,
        base_url=row.base_url,
        model=row.model,
        api_key_set=api_key_set,
        api_key_hint=api_key_hint,
        note=row.note,
        created_at=row.created_at,
    )


def _to_effective_response(effective: EffectiveLlmConfig) -> LlmEffectiveConfig:
    api_key_set, api_key_hint = mask_api_key(effective.api_key)
    return LlmEffectiveConfig(
        base_url=effective.base_url,
        model=effective.model,
        source=effective.source,
        api_key_set=api_key_set,
        api_key_hint=api_key_hint,
        version=effective.version,
        note=effective.note,
    )


@router.post("", response_model=LlmConfigResponse, status_code=201)
async def create_llm_config(
    body: LlmConfigCreateRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> LlmConfigResponse:
    row = await create_version(db, body.base_url, body.model, body.api_key, body.note)
    return _to_response(row)


@router.get("", response_model=LlmConfigGetResponse)
async def get_llm_config(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> LlmConfigGetResponse:
    effective = await resolve_llm_config(db)
    rows = await config_history(db)
    return LlmConfigGetResponse(
        effective=_to_effective_response(effective),
        history=[_to_response(r) for r in rows],
    )


@router.post("/test", response_model=LlmConfigTestResponse)
async def test_llm_config(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> LlmConfigTestResponse:
    """Fires exactly one minimal chat-completion request at the EFFECTIVE
    provider (env override if set, else the current stored version) and
    reports back ok/detail/latency -- never the api_key, never a raw
    traceback on failure (app/llm_client.py handles every failure mode
    cleanly).
    """
    effective = await resolve_llm_config(db)
    result = await test_llm_connection(effective)
    return LlmConfigTestResponse(**result)
