"""Shared machine registry operations (contracts-v1.md §1).

Used by both the API router (app/routers/machines.py) and the owner-gated UI
admin area (app/routers/ui_admin.py) -- kept here once so mint/revoke logic
never drifts between the two surfaces (phase 6 brief: "do NOT duplicate
mint/revoke/approve logic").
"""

from typing import Any

from ulid import ULID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Machine
from app.roles import DEFAULT_ROLE, validate_role
from app.security import generate_machine_token, hash_token

_ALLOWED_MACHINE_UPDATE_KEYS = frozenset({"role", "default_project", "name"})


async def list_machines(db: AsyncSession) -> list[Machine]:
    result = await db.scalars(select(Machine).order_by(Machine.created_at))
    return list(result)


async def mint_machine(
    db: AsyncSession,
    name: str,
    *,
    role: str = DEFAULT_ROLE,
    default_project: str | None = None,
) -> tuple[Machine, str]:
    """Creates a machine and returns (machine, plaintext_token). The
    plaintext token exists only in this return value -- it is never stored,
    logged, or retrievable again (contracts-v1.md §1: "shown in full exactly
    once, at creation").

    `role` is validated here (not just at the pydantic layer) so the UI
    form path -- a raw string off an HTML <select>, not pydantic-checked --
    gets the same self-explaining rejection as the API on a forged/bad
    value. `default_project` is a hint only, never validated against the
    project registry (see app/models.py Machine.default_project docstring).
    """
    validate_role(role)
    token = generate_machine_token()
    machine = Machine(
        id=str(ULID()),
        name=name,
        token_hash=hash_token(token),
        status="active",
        role=role,
        default_project=default_project,
    )
    db.add(machine)
    await db.commit()
    return machine, token


def validate_machine_update(data: Any) -> None:
    """Whole-object, self-explaining validation for `PATCH /v1/machines/{id}`
    (and its UI counterpart) -- mirrors app/projects.py's
    `validate_project_update` style. An empty object is a valid no-op.
    """
    if not isinstance(data, dict):
        raise ApiError(
            422,
            "invalid_machine_update",
            "The machine update must be an object with optional `role`/`default_project`/`name` fields. "
            "Recovery: fix the field(s) and resend.",
        )

    unknown_keys = sorted(set(data) - _ALLOWED_MACHINE_UPDATE_KEYS)
    if unknown_keys:
        raise ApiError(
            422,
            "invalid_machine_update",
            f"Unknown field(s) {unknown_keys} in machine update; only {sorted(_ALLOWED_MACHINE_UPDATE_KEYS)} "
            "are recognized. Recovery: fix the field(s) and resend.",
            extra={"unknown_keys": unknown_keys},
        )

    if "role" in data:
        if not isinstance(data["role"], str):
            raise ApiError(422, "invalid_role", "`role` must be a string. Recovery: fix the field and resend.")
        validate_role(data["role"])

    if "name" in data:
        if not isinstance(data["name"], str):
            raise ApiError(
                422,
                "invalid_machine_update",
                "`name` must be a string. Recovery: fix the field and resend.",
            )
        # Blank/whitespace-only is rejected on the *trimmed* value -- a lone
        # space would pass a plain min_length=1 check but isn't a real name.
        # Max length mirrors MachineCreateRequest.name's max_length=255
        # (app/schemas.py) -- PATCH must not accept what POST would reject.
        if not data["name"].strip():
            raise ApiError(
                422,
                "invalid_name",
                "`name` must not be blank (or whitespace-only). Recovery: fix the field and resend.",
            )
        if len(data["name"]) > 255:
            raise ApiError(
                422,
                "invalid_name",
                f"`name` must be at most 255 characters, got {len(data['name'])} characters. "
                "Recovery: fix the field and resend.",
            )

    if "default_project" in data and data["default_project"] is not None:
        if not isinstance(data["default_project"], str):
            raise ApiError(
                422,
                "invalid_machine_update",
                "`default_project` must be a string or null. Recovery: fix the field and resend.",
            )
        # Same 1-255 char bounds as MachineCreateRequest.default_project
        # (app/schemas.py) -- PATCH must not accept what POST would reject.
        if not (1 <= len(data["default_project"]) <= 255):
            raise ApiError(
                422,
                "invalid_default_project",
                "`default_project` must be 1-255 characters if given (or null to clear it), got "
                f"{len(data['default_project'])} characters. Recovery: fix the field and resend.",
            )


def apply_machine_update(machine: Machine, data: dict[str, Any]) -> None:
    """Applies an already-validated `{role?, default_project?, name?}` object.
    Absent keys leave the current value untouched (partial update).
    """
    if "role" in data:
        machine.role = data["role"]
    if "default_project" in data:
        machine.default_project = data["default_project"]
    if "name" in data:
        machine.name = data["name"]


async def update_machine(db: AsyncSession, machine_id: str, data: dict[str, Any]) -> Machine | None:
    """Validates and applies a machine update, returning the updated machine
    (or None if no such machine exists, so callers can 404).
    """
    machine = await db.get(Machine, machine_id)
    if machine is None:
        return None
    validate_machine_update(data)
    apply_machine_update(machine, data)
    await db.commit()
    return machine


async def revoke_machine(db: AsyncSession, machine_id: str) -> Machine | None:
    """Revokes a machine (idempotent: revoking an already-revoked machine is
    a no-op). Returns None if no such machine exists, so callers can 404.
    """
    machine = await db.get(Machine, machine_id)
    if machine is None:
        return None
    machine.status = "revoked"
    await db.commit()
    return machine
