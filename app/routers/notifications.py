"""Notification channel config -- POST/GET /v1/notifications-config.

Owner-token only: the owner decides where the Brain's "notify me" channel
points, same trust posture as doctrine (contracts-v1.md §4: "the one
collection where full trust does not apply"). Immutable rows,
supersede-never-erase -- every write is a new version, never an edit; see
app/notifications.py for the shared validation/versioning logic also used by
the owner UI (app/routers/ui_notifications.py) and read by bootstrap's
"Notifications" subsection (app/routers/bootstrap.py).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.notifications import create_version, current_config
from app.notifications import history as config_history
from app.schemas import NotificationConfigCreateRequest, NotificationConfigGetResponse, NotificationConfigResponse

router = APIRouter(prefix="/v1/notifications-config", tags=["notifications"])


@router.post("", response_model=NotificationConfigResponse, status_code=201)
async def create_notifications_config(
    body: NotificationConfigCreateRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> NotificationConfigResponse:
    row = await create_version(db, body.ntfy_url, body.topic, body.note)
    return NotificationConfigResponse.model_validate(row)


@router.get("", response_model=NotificationConfigGetResponse)
async def get_notifications_config(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> NotificationConfigGetResponse:
    current = await current_config(db)
    rows = await config_history(db)
    return NotificationConfigGetResponse(
        current=None if current is None else NotificationConfigResponse.model_validate(current),
        history=[NotificationConfigResponse.model_validate(r) for r in rows],
    )
