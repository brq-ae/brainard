"""Machine registry: mint, list, revoke, reactivate, update. Owner token
required for all five.

The actual mint/list/revoke/reactivate/update logic lives in
app/machines.py, shared with the UI admin area (app/routers/ui_admin.py) so
the two surfaces never drift.

`reactivate` is the symmetric counterpart to `revoke` -- revocation is not a
one-way door. This matters generally (a revoked agent machine can be
brought back), and specifically for the one reserved built-in-librarian
machine (app/librarian_engine.py's `LIBRARIAN_MACHINE_ID`), whose Revoke
control now doubles as a real kill switch for the built-in librarian --
without a reactivate path, that would be a trap.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.errors import ApiError
from app.machines import list_machines as _list_machines
from app.machines import mint_machine
from app.machines import reactivate_machine as reactivate_machine_impl
from app.machines import revoke_machine as revoke_machine_impl
from app.machines import update_machine as update_machine_impl
from app.models import Machine
from app.schemas import MachineCreateRequest, MachineCreateResponse, MachineListItem, MachineRevokeResponse

router = APIRouter(prefix="/v1/machines", tags=["machines"])


@router.post("", response_model=MachineCreateResponse, status_code=201)
async def create_machine(
    body: MachineCreateRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> MachineCreateResponse:
    machine, token = await mint_machine(db, body.name, role=body.role, default_project=body.default_project)
    return MachineCreateResponse(
        id=machine.id, name=machine.name, role=machine.role, default_project=machine.default_project, token=token
    )


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


@router.post("/{machine_id}/reactivate", response_model=MachineRevokeResponse)
async def reactivate_machine(
    machine_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> MachineRevokeResponse:
    machine = await reactivate_machine_impl(db, machine_id)
    if machine is None:
        raise ApiError(404, "machine_not_found", f"No machine with id '{machine_id}'.")
    return MachineRevokeResponse(id=machine.id, status=machine.status)


@router.patch("/{machine_id}", response_model=MachineListItem)
async def update_machine(
    machine_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> Machine:
    machine = await update_machine_impl(db, machine_id, body)
    if machine is None:
        raise ApiError(404, "machine_not_found", f"No machine with id '{machine_id}'.")
    return machine
