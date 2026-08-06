"""Bearer-token authentication: hash lookup distinguishes owner vs machine
tokens; revoked machines and unrecognized/malformed tokens are rejected.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.errors import ApiError
from app.models import Machine, OwnerToken
from app.security import hash_token


@dataclass(frozen=True)
class Principal:
    kind: Literal["owner", "machine"]
    machine: Machine | None = None


async def authenticate(token: str, db: AsyncSession) -> Principal:
    """Resolve a bearer token to its principal. Raises ApiError(401) for any
    unrecognized or revoked token.
    """
    token_hash = hash_token(token)

    owner = await db.scalar(select(OwnerToken).where(OwnerToken.token_hash == token_hash))
    if owner is not None:
        return Principal(kind="owner")

    machine = await db.scalar(select(Machine).where(Machine.token_hash == token_hash))
    if machine is not None:
        if machine.status == "revoked":
            raise ApiError(401, "token_revoked", "This machine token has been revoked.")
        machine.last_seen = datetime.now(UTC)
        await db.commit()
        return Principal(kind="machine", machine=machine)

    raise ApiError(401, "invalid_token", "The provided token is not recognized.")


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise ApiError(401, "missing_token", "An Authorization: Bearer <token> header is required.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "malformed_authorization", "Authorization header must be 'Bearer <token>'.")
    return token.strip()


async def get_bearer_token(authorization: str | None = Header(default=None)) -> str:
    return _extract_bearer_token(authorization)


async def require_owner(
    token: str = Depends(get_bearer_token),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    principal = await authenticate(token, db)
    if principal.kind != "owner":
        raise ApiError(403, "owner_token_required", "This endpoint requires the owner token.")
    return principal


async def require_machine(
    token: str = Depends(get_bearer_token),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    principal = await authenticate(token, db)
    if principal.kind != "machine":
        raise ApiError(403, "machine_token_required", "This endpoint requires a machine token.")
    return principal
