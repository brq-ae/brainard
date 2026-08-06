"""Read-only deposit queries for the UI dashboard (app/routers/ui_dashboard.py).

Distinct from app/routers/deposits.py (the write path, POST /v1/deposits) --
no session-facing read endpoint over raw deposits exists in the API surface
(contracts-v1.md §7), so this is UI-only, same rationale as app/journal.py.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deposit, Event, Handoff, Machine


async def recent_deposits(db: AsyncSession, *, limit: int = 10) -> list[tuple[Deposit, str, int, bool]]:
    """Tuples of (Deposit, machine_name, events_count, has_handoff), most
    recent first. `knowledge`/`documents` counts are read straight off the
    deposit's own stored ack lists (Deposit.knowledge_ack/documents_ack) --
    no extra query needed for those two.
    """
    stmt = (
        select(Deposit, Machine.name.label("machine_name"))
        .join(Machine, Machine.id == Deposit.machine_id)
        .order_by(Deposit.received_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    deposit_ids = [d.deposit_id for d, _ in rows]

    events_counts: dict[str, int] = {}
    handoff_ids: set[str] = set()
    if deposit_ids:
        ev_rows = (
            await db.execute(
                select(Event.deposit_id, func.count()).where(Event.deposit_id.in_(deposit_ids)).group_by(Event.deposit_id)
            )
        ).all()
        events_counts = dict(ev_rows)
        handoff_ids = set((await db.scalars(select(Handoff.deposit_id).where(Handoff.deposit_id.in_(deposit_ids)))).all())

    return [(d, name, events_counts.get(d.deposit_id, 0), d.deposit_id in handoff_ids) for d, name in rows]
