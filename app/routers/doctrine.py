"""Doctrine -- POST /v1/doctrine/global, POST /v1/doctrine/overlays/{project},
GET /v1/doctrine (contracts-v1.md §4). Owner-token only: the one collection
where full trust does not apply -- no AI may alter the rulebook.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.auth import Principal, require_owner
from app.db import get_db
from app.doctrine import current_global, current_overlays_all, next_version
from app.errors import ApiError
from app.models import DoctrineVersion, Project
from app.schemas import (
    DoctrineAdditionIn,
    DoctrineGetResponse,
    DoctrineGlobalRequest,
    DoctrineGlobalResponse,
    DoctrineOverlayRequest,
    DoctrineOverlayResponse,
    DoctrineOverrideIn,
    DoctrineRuleIn,
)

router = APIRouter(prefix="/v1/doctrine", tags=["doctrine"])

# Two-tier rules (§4): non-negotiable is immutable everywhere; default may be
# overridden by a project overlay naming the id. Spelled with an underscore
# (not a hyphen) as the literal wire/storage value throughout this module.
VALID_TIERS = frozenset({"non_negotiable", "default"})

_RECOVERY_FIX_AND_RESEND = "fix the listed field(s), resend"


def _validate_global_rules(rules: list[DoctrineRuleIn]) -> None:
    """Whole-request, self-explaining validation: rule ids must be unique and
    every tier must be one of the two recognized values. Every failure is
    reported at once -- mirrors the deposits[] validation style.
    """
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for r in rules:
        if r.id in seen:
            duplicate_ids.add(r.id)
        seen.add(r.id)

    bad_tier = [{"id": r.id, "tier": r.tier} for r in rules if r.tier not in VALID_TIERS]

    if duplicate_ids:
        raise ApiError(
            422,
            "duplicate_rule_id",
            f"Rule id(s) {sorted(duplicate_ids)} appear more than once; every rule id must be unique. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
            extra={"duplicate_ids": sorted(duplicate_ids)},
        )
    if bad_tier:
        raise ApiError(
            422,
            "invalid_rule_tier",
            f"{len(bad_tier)} rule(s) use a tier outside {sorted(VALID_TIERS)}. Recovery: {_RECOVERY_FIX_AND_RESEND}.",
            extra={"failing_rules": bad_tier},
        )


@router.post("/global", response_model=DoctrineGlobalResponse, status_code=201)
async def create_global_doctrine(
    body: DoctrineGlobalRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> DoctrineGlobalResponse:
    _validate_global_rules(body.rules)

    version = await next_version(db, "global", None)
    row = DoctrineVersion(
        id=str(ULID()),
        kind="global",
        project=None,
        version=version,
        content=body.content,
        rules=[r.model_dump() for r in body.rules],
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()

    return DoctrineGlobalResponse(version=row.version, content=row.content, rules=row.rules, created_at=row.created_at)


@router.post("/overlays/{project}", response_model=DoctrineOverlayResponse, status_code=201)
async def create_overlay_doctrine(
    project: str,
    body: DoctrineOverlayRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> DoctrineOverlayResponse:
    # Unlike deposits/bootstrap, an unknown project is rejected here, not
    # auto-stubbed -- the owner writing an overlay is a deliberate act
    # against a project that is supposed to already exist.
    project_row = await db.get(Project, project)
    if project_row is None:
        raise ApiError(
            404,
            "unknown_project",
            f"No project named '{project}' is registered. Recovery: mention the project in a machine "
            "deposit first (auto-stub is deliberate there) or wait for it to exist, then resend the overlay.",
        )

    _validate_overlay_ids(body.overrides, body.additions)

    global_row = await current_global(db)
    global_rules = {r["id"]: r for r in (global_row.rules if global_row is not None else [])}

    unknown_override_ids = sorted(o.id for o in body.overrides if o.id not in global_rules)
    if unknown_override_ids:
        raise ApiError(
            422,
            "unknown_override_id",
            f"Override id(s) {unknown_override_ids} do not exist in the current global doctrine. "
            f"Recovery: fix the id(s), or add them to global doctrine first, resend.",
            extra={"unknown_ids": unknown_override_ids},
        )

    non_negotiable_overrides = [
        {"id": o.id, "tier": global_rules[o.id]["tier"]} for o in body.overrides if global_rules[o.id]["tier"] == "non_negotiable"
    ]
    if non_negotiable_overrides:
        named = ", ".join(f"{r['id']} (tier: {r['tier']})" for r in non_negotiable_overrides)
        raise ApiError(
            422,
            "non_negotiable_override_rejected",
            f"Rule(s) {named} are tier 'non_negotiable' and cannot be overridden by a project overlay -- "
            "non-negotiables are immutable everywhere. Recovery: drop the override(s), resend.",
            extra={"failing_overrides": non_negotiable_overrides},
        )

    colliding_additions = sorted(a.id for a in body.additions if a.id in global_rules)
    if colliding_additions:
        raise ApiError(
            422,
            "addition_id_collision",
            f"Addition id(s) {colliding_additions} already exist in global doctrine; addition ids must be new. "
            f"Recovery: rename the id(s), resend.",
            extra={"colliding_ids": colliding_additions},
        )

    version = await next_version(db, "overlay", project)
    row = DoctrineVersion(
        id=str(ULID()),
        kind="overlay",
        project=project,
        version=version,
        content=body.content,
        rules={
            "overrides": [o.model_dump() for o in body.overrides],
            "additions": [a.model_dump() for a in body.additions],
        },
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()

    return DoctrineOverlayResponse(
        project=project,
        version=row.version,
        content=row.content,
        overrides=row.rules["overrides"],
        additions=row.rules["additions"],
        created_at=row.created_at,
    )


def _validate_overlay_ids(overrides: list[DoctrineOverrideIn], additions: list[DoctrineAdditionIn]) -> None:
    """Self-consistency within this one submission: override ids unique
    among themselves, addition ids unique among themselves -- mirrors the
    "rule ids unique" requirement enforced on global doctrine.
    """
    override_ids = [o.id for o in overrides]
    addition_ids = [a.id for a in additions]
    dup_overrides = sorted({i for i in override_ids if override_ids.count(i) > 1})
    dup_additions = sorted({i for i in addition_ids if addition_ids.count(i) > 1})

    if dup_overrides:
        raise ApiError(
            422,
            "duplicate_rule_id",
            f"Override id(s) {dup_overrides} appear more than once in this submission. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
            extra={"duplicate_ids": dup_overrides},
        )
    if dup_additions:
        raise ApiError(
            422,
            "duplicate_rule_id",
            f"Addition id(s) {dup_additions} appear more than once in this submission. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
            extra={"duplicate_ids": dup_additions},
        )


@router.get("", response_model=DoctrineGetResponse)
async def get_doctrine(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> DoctrineGetResponse:
    global_row = await current_global(db)
    global_resp = (
        None
        if global_row is None
        else DoctrineGlobalResponse(version=global_row.version, content=global_row.content, rules=global_row.rules, created_at=global_row.created_at)
    )

    # Current (latest-by-version) overlay per project -- shared query, see
    # app/doctrine.py's `current_overlays_all`.
    overlay_rows = await current_overlays_all(db)

    overlays = [
        DoctrineOverlayResponse(
            project=r.project,
            version=r.version,
            content=r.content,
            overrides=(r.rules or {}).get("overrides", []),
            additions=(r.rules or {}).get("additions", []),
            created_at=r.created_at,
        )
        for r in overlay_rows
    ]

    return DoctrineGetResponse(**{"global": global_resp}, overlays=overlays)
