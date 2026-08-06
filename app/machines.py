"""Shared machine registry operations (contracts-v1.md §1).

Used by both the API router (app/routers/machines.py) and the owner-gated UI
admin area (app/routers/ui_admin.py) -- kept here once so mint/revoke logic
never drifts between the two surfaces (phase 6 brief: "do NOT duplicate
mint/revoke/approve logic").
"""

from ulid import ULID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Machine
from app.security import generate_machine_token, hash_token


async def list_machines(db: AsyncSession) -> list[Machine]:
    result = await db.scalars(select(Machine).order_by(Machine.created_at))
    return list(result)


async def mint_machine(db: AsyncSession, name: str) -> tuple[Machine, str]:
    """Creates a machine and returns (machine, plaintext_token). The
    plaintext token exists only in this return value -- it is never stored,
    logged, or retrievable again (contracts-v1.md §1: "shown in full exactly
    once, at creation").
    """
    token = generate_machine_token()
    machine = Machine(
        id=str(ULID()),
        name=name,
        token_hash=hash_token(token),
        status="active",
    )
    db.add(machine)
    await db.commit()
    return machine, token


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
