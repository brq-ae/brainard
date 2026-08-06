"""Owner-token first-boot logic (app/startup.py; formerly app/bootstrap.py --
see that module's docstring)."""

import contextlib
import io

from sqlalchemy import select

from app.startup import bootstrap_owner_token
from app.db import AsyncSessionLocal
from app.models import OwnerToken


async def test_first_boot_generates_and_prints_owner_token_once():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await bootstrap_owner_token()
    output = buf.getvalue()

    assert "brnown_" in output
    assert "OWNER TOKEN" in output

    async with AsyncSessionLocal() as db:
        rows = (await db.scalars(select(OwnerToken))).all()
    assert len(rows) == 1


async def test_second_boot_does_not_recreate_or_reprint():
    await bootstrap_owner_token()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await bootstrap_owner_token()
    assert buf.getvalue() == ""

    async with AsyncSessionLocal() as db:
        rows = (await db.scalars(select(OwnerToken))).all()
    assert len(rows) == 1
