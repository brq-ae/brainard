"""Machine registry: mint, list, revoke. Owner token required for all three.

The actual mint/list/revoke logic lives in app/machines.py, shared with the
UI admin area (app/routers/ui_admin.py) so the two surfaces never drift.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.errors import ApiError
from app.machines import list_machines as _list_machines
from app.machines import mint_machine
from app.machines import revoke_machine as revoke_machine_impl
from app.models import Machine
from app.schemas import MachineCreateRequest, MachineCreateResponse, MachineListItem, MachineRevokeResponse

router = APIRouter(prefix="/v1/machines", tags=["machines"])


@router.post("", response_model=MachineCreateResponse, status_code=201)
async def create_machine(
    body: MachineCreateRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> MachineCreateResponse:
    machine, token = await mint_machine(db, body.name)
    return MachineCreateResponse(id=machine.id, name=machine.name, token=token)


@router.get("", response_model=list[MachineListItem])
async def list_machines(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> list[Machine]:
    return await _list_machines(db)


@router.post("/{machine_id}/revoke", response_model=MachineRevokeResponse)
async def revoke_machine(
    machine_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> MachineRevokeResponse:
    machine = await revoke_machine_impl(db, machine_id)
    if machine is None:
        raise ApiError(404, "machine_not_found", f"No machine with id '{machine_id}'.")
    return MachineRevokeResponse(id=machine.id, status=machine.status)
