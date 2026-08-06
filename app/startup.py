"""First-boot owner token provisioning.

(Renamed from app/bootstrap.py in phase 4 to free that name for the real
bootstrap feature -- GET /v1/bootstrap, contracts-v1.md §6 -- which is
unrelated to this first-boot concern.)

On first startup (no owner_token row exists), generate the owner token,
persist only its hash, and print the plaintext exactly once -- it can never
be retrieved again.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import AsyncSessionLocal
from app.models import OwnerToken
from app.security import generate_owner_token, hash_token

_BANNER = """
================================================================================
  THE BRAIN -- OWNER TOKEN (shown once, save it now)

  {token}

  This is the root credential: machine management, doctrine writes, proposal
  approvals, and export all require it. It is stored only as a hash and
  CANNOT be recovered or shown again. If lost, provisioning a new one
  requires direct database access.
================================================================================
"""


async def bootstrap_owner_token() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(OwnerToken).limit(1))
        if existing is not None:
            return

        token = generate_owner_token()
        db.add(OwnerToken(token_hash=hash_token(token)))
        try:
            await db.commit()
        except IntegrityError:
            # Another process won the race to create the singleton row;
            # the token generated here was never persisted, so drop it silently.
            await db.rollback()
            return

    print(_BANNER.format(token=token))
