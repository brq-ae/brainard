"""UI doctrine -- GET /ui/doctrine: current global rules by tier, overlays
per project (with the stale-override marker), version history, and (ADR-
0019 decision 4) an owner-only "Copy doctrine" checklist for handing a
deliberately chosen subset of the current global doctrine to an external
LLM conversation that can't reach the Brain. Owner session required
throughout (`require_ui_session`) -- no new gating added by decision 4; see
that decision for why this page's existing posture is already sufficient.

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
from app.doctrine import current_global, current_overlays_all, doctrine_copy_text, stale_override_reason, version_history
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

    # ADR-0019 decision 4: the "Copy doctrine" checklist and its
    # server-rendered initial copy text -- omitted entirely when no global
    # doctrine has ever been posted (`global_row is None`), matching this
    # page's existing `{% if not global_row %}` empty state; the template
    # never references `doctrine_version_label`/`doctrine_briefing` in that
    # case.
    doctrine_version_label = f"global:v{global_row.version}" if global_row is not None else None
    doctrine_briefing = doctrine_copy_text(doctrine_version_label, global_rules) if global_row is not None else ""

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
            # ADR-0019 decision 4: every current global rule, both tiers, in
            # stored order -- the checklist's own iteration source (never a
            # rule id named in the template or in code).
            "all_global_rules": global_rules,
            "doctrine_version_label": doctrine_version_label,
            "doctrine_briefing": doctrine_briefing,
            "overlays": overlays,
            "history": history,
        },
    )
