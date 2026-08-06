"""UI doctrine -- GET /ui/doctrine: current global rules by tier, overlays
per project (with the stale-override marker), version history. Owner
session required.

The stale-override marker closes the phase 4 advisory at the UI layer: an
overlay override whose target rule is now `non_negotiable` (a tier change
made after the override was written -- overlay writes only validate
against the tier *at write time*, so a later global tier bump can silently
strand an override) or no longer exists in the current global doctrine is
visibly marked inactive. See app/doctrine.py's `stale_override_reason`.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.doctrine import current_global, current_overlays_all, stale_override_reason, version_history
from app.templates_env import templates
from app.ui_auth import require_ui_session

router = APIRouter(prefix="/ui/doctrine", tags=["ui"])


@router.get("")
async def doctrine_page(
    request: Request,
    session: dict = Depends(require_ui_session),
    db: AsyncSession = Depends(get_db),
):
    global_row = await current_global(db)
    global_rules = global_row.rules if global_row is not None else []
    global_rules_by_id = {r["id"]: r for r in global_rules}
    non_negotiable = [r for r in global_rules if r["tier"] == "non_negotiable"]
    default_rules = [r for r in global_rules if r["tier"] == "default"]

    overlay_rows = await current_overlays_all(db)
    overlays = []
    for row in overlay_rows:
        overrides = (row.rules or {}).get("overrides", [])
        additions = (row.rules or {}).get("additions", [])
        overrides_annotated = [
            {**o, "stale_reason": stale_override_reason(global_rules_by_id, o["id"])} for o in overrides
        ]
        overlays.append(
            {
                "project": row.project,
                "version": row.version,
                "content": row.content,
                "overrides": overrides_annotated,
                "additions": additions,
            }
        )

    history = await version_history(db)

    return templates.TemplateResponse(
        request,
        "doctrine.html",
        {
            "csrf_token": session["csrf"],
            "global_row": global_row,
            "non_negotiable": non_negotiable,
            "default_rules": default_rules,
            "overlays": overlays,
            "history": history,
        },
    )
