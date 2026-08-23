"""Get-or-create helper for a reserved, tokenless Machine identity -- used
to attribute writes made by in-app, non-session code paths (never over
HTTP, never via `require_machine`) to a stable, well-known row. Extracted
from app/librarian_engine.py (ADR-0010 phase 2, the first caller) so
app/room_ai.py's own reserved identity (ADR-0011) shares the exact same
get-or-create/race-safety semantics rather than a second, drifted copy.

Both callers need the identical shape: a fixed, well-known machine id so
lookups are idempotent across restarts (never a fresh ULID per process), a
`token_hash` set to the hash of a random value immediately discarded (NEVER
a usable bearer token -- satisfies the NOT NULL UNIQUE column only, nothing
can ever authenticate as this row over the API since every write through it
goes through the domain functions directly), and get-or-create semantics
safe under a concurrent race (two callers provisioning the same row at
once -- the loser's IntegrityError just re-fetches the winner's row).
"""

import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Machine
from app.security import hash_token


async def ensure_reserved_machine(
    session_factory: async_sessionmaker[AsyncSession], machine_id: str, machine_name: str
) -> Machine:
    async with session_factory() as session:
        existing = await session.get(Machine, machine_id)
        if existing is not None:
            return existing

    async with session_factory() as session:
        machine = Machine(
            id=machine_id,
            name=machine_name,
            token_hash=hash_token(secrets.token_urlsafe(32)),
            status="active",
            role="solo",
        )
        session.add(machine)
        try:
            await session.commit()
            return machine
        except IntegrityError:
            # Lost a race to another concurrent call provisioning the same
            # row at the same moment -- the row exists either way; fetch
            # what the winner created.
            await session.rollback()

    async with session_factory() as session:
        winner = await session.get(Machine, machine_id)
    assert winner is not None, "the reserved machine must exist by now (created here or by the race winner)"
    return winner
