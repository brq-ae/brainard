"""Shared project registry write validation (contracts-v1.md §5).

`{description?, status?}` is writable through two paths -- a deposit's
optional `project_update` envelope field (checkpoint-bound, applied
atomically with the deposit; app/routers/deposits.py) and the owner's
deliberate `PATCH /v1/projects/{name}` (app/routers/projects.py). Kept here
once so the two write paths never drift on what counts as valid.
"""

from typing import Any

from app.errors import ApiError
from app.models import Project

VALID_PROJECT_STATUSES = frozenset({"active", "paused", "done"})
_ALLOWED_KEYS = frozenset({"description", "status"})

_RECOVERY_FIX_AND_RESEND = "fix the listed field(s), resend"


def validate_project_update(data: Any) -> None:
    """Whole-object, self-explaining validation. Raises ApiError naming every
    problem at once -- mirrors the deposits[]/knowledge[] validation style.
    An empty object is valid (no-op update).
    """
    if not isinstance(data, dict):
        raise ApiError(
            422,
            "invalid_project_update",
            "`project_update` must be an object with optional `description`/`status` fields. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
        )

    unknown_keys = sorted(set(data) - _ALLOWED_KEYS)
    if unknown_keys:
        raise ApiError(
            422,
            "invalid_project_update",
            f"Unknown field(s) {unknown_keys} in project update; only {sorted(_ALLOWED_KEYS)} are recognized. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
            extra={"unknown_keys": unknown_keys},
        )

    if "description" in data and data["description"] is not None and not isinstance(data["description"], str):
        raise ApiError(
            422,
            "invalid_project_update",
            f"`description` must be a string or null. Recovery: {_RECOVERY_FIX_AND_RESEND}.",
        )

    if "status" in data and data["status"] not in VALID_PROJECT_STATUSES:
        raise ApiError(
            422,
            "invalid_project_update",
            f"`status` must be one of {sorted(VALID_PROJECT_STATUSES)}, got {data['status']!r}. "
            f"Recovery: {_RECOVERY_FIX_AND_RESEND}.",
        )


def apply_project_update(project: Project, data: dict[str, Any]) -> None:
    """Applies an already-validated `{description?, status?}` object.
    Absent keys leave the current value untouched (partial update).
    """
    if "description" in data:
        project.description = data["description"]
    if "status" in data:
        project.status = data["status"]
