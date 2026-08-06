"""Machine registry: mint, list, revoke. Owner token required for all three."""

from ulid import ULID
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.errors import ApiError
from app.models import Machine
from app.schemas import MachineCreateRequest, MachineCreateResponse, MachineListItem, MachineRevokeResponse
from app.security import generate_machine_token, hash_token

router = APIRouter(prefix="/v1/machines", tags=["machines"])


@router.post("", response_model=MachineCreateResponse, status_code=201)
async def create_machine(
    body: MachineCreateRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> MachineCreateResponse:
    token = generate_machine_token()
    machine = Machine(
        id=str(ULID()),
        name=body.name,
        token_hash=hash_token(token),
        status="active",
    )
    db.add(machine)
    await db.commit()
    return MachineCreateResponse(id=machine.id, name=machine.name, token=token)


@router.get("", response_model=list[MachineListItem])
async def list_machines(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> list[Machine]:
    result = await db.scalars(select(Machine).order_by(Machine.created_at))
    return list(result)


@router.post("/{machine_id}/revoke", response_model=MachineRevokeResponse)
async def revoke_machine(
    machine_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> MachineRevokeResponse:
    machine = await db.get(Machine, machine_id)
    if machine is None:
        raise ApiError(404, "machine_not_found", f"No machine with id '{machine_id}'.")

    machine.status = "revoked"  # idempotent: revoking an already-revoked machine is a no-op
    await db.commit()
    return MachineRevokeResponse(id=machine.id, status=machine.status)
