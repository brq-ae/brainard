"""The checkpoint deposit -- POST /v1/deposits (contracts-v1.md §2).

One atomic batch, fully accepted or fully rejected. Machine-token only: this
is the first session-facing route, so it's also the first real exercise of
`require_machine`.
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.auth import Principal, require_machine
from app.db import get_db
from app.errors import ApiError
from app.models import Deposit, Event, Handoff, Project
from app.schemas import (
    DepositCounts,
    DepositProjectInfo,
    DepositRequest,
    DepositResponse,
    EventIn,
)

router = APIRouter(prefix="/v1/deposits", tags=["deposits"])

# Fixed nine-kind event vocabulary (contracts-v1.md §2). Append-only: new
# kinds land here only after a doctrine update, hub first per the rollout rule.
VALID_EVENT_KINDS = frozenset(
    {
        "session.started",
        "session.ended",
        "work.started",
        "work.completed",
        "artifact.produced",
        "decision.made",
        "error.hit",
        "lesson.candidate",
        "note",
    }
)

MAX_PAYLOAD_BYTES = 256 * 1024

_RECOVERY_UNKNOWN_KIND = "relabel to 'note', preserve the original kind as a tag, resend"
_RECOVERY_OVERSIZED_PAYLOAD = "trim the payload below 256 KB (or drop it and summarize in `summary`/`tags`), resend"


def _is_valid_ulid(value: str) -> bool:
    try:
        ULID.from_str(value)
    except ValueError:
        return False
    return True


def _payload_size(payload: dict | None) -> int:
    if payload is None:
        return 0
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _validate_events(events: list[EventIn]) -> None:
    """Whole-deposit, self-explaining validation for the events[] compartment.
    Raises ApiError listing every failing event; never rejects on just the
    first failure, so one resend can fix everything at once.
    """
    unknown_kind = [
        {"seq": e.seq, "kind": e.kind, "recovery": _RECOVERY_UNKNOWN_KIND}
        for e in events
        if e.kind not in VALID_EVENT_KINDS
    ]
    if unknown_kind:
        raise ApiError(
            422,
            "unknown_event_kind",
            f"{len(unknown_kind)} event(s) use a kind outside the fixed vocabulary; "
            "the whole deposit was rejected. See `failing_events` for the scripted recovery.",
            extra={"failing_events": unknown_kind},
        )

    oversized = [
        {"seq": e.seq, "payload_bytes": _payload_size(e.payload), "recovery": _RECOVERY_OVERSIZED_PAYLOAD}
        for e in events
        if _payload_size(e.payload) > MAX_PAYLOAD_BYTES
    ]
    if oversized:
        raise ApiError(
            422,
            "payload_too_large",
            f"{len(oversized)} event(s) carry a payload over the 256 KB cap; "
            "the whole deposit was rejected. See `failing_events` for the scripted recovery.",
            extra={"failing_events": oversized},
        )


def _validate_handoff_or_waiver(body: DepositRequest) -> None:
    if body.reason != "session_end":
        return
    if body.handoff is None and body.no_handoff is None:
        raise ApiError(
            422,
            "handoff_or_waiver_required",
            "reason 'session_end' requires either a `handoff` note or a `no_handoff: \"<reason>\"` waiver; "
            "silence is rejected. Recovery: resend with one of the two present.",
        )
    if body.handoff is not None and body.no_handoff is not None:
        raise ApiError(
            422,
            "handoff_and_waiver_conflict",
            "`handoff` and `no_handoff` were both present -- a contradiction. "
            "Recovery: resend with exactly one of the two.",
        )


def _validate_knowledge(body: DepositRequest) -> None:
    if body.knowledge:
        raise ApiError(
            422,
            "knowledge_not_implemented",
            f"the `knowledge[]` compartment ({len(body.knowledge)} entry(ies) submitted) is not implemented "
            "until phase 3; the whole deposit was rejected rather than silently dropping it. "
            "Recovery: resend without `knowledge`, or hold the entries client-side until phase 3 ships.",
        )


async def _events_count(db: AsyncSession, deposit_id: str) -> int:
    return await db.scalar(select(func.count()).select_from(Event).where(Event.deposit_id == deposit_id))


async def _handoff_stored(db: AsyncSession, deposit_id: str) -> bool:
    handoff_id = await db.scalar(select(Handoff.id).where(Handoff.deposit_id == deposit_id))
    return handoff_id is not None


async def _build_ack(db: AsyncSession, deposit: Deposit, *, replayed: bool) -> DepositResponse:
    events_count = await _events_count(db, deposit.deposit_id)
    handoff_stored = await _handoff_stored(db, deposit.deposit_id)
    return DepositResponse(
        deposit_id=deposit.deposit_id,
        received_at=deposit.received_at,
        replayed=replayed,
        counts=DepositCounts(events=events_count, handoff=handoff_stored),
        project=DepositProjectInfo(name=deposit.project, stub_created=deposit.stub_created),
    )


async def _insert_deposit(db: AsyncSession, body: DepositRequest, principal: Principal) -> Deposit:
    """Performs the atomic insert: project stub (if new), deposit, events,
    handoff -- all in one transaction, committed once. Any failure before the
    commit leaves nothing behind.
    """
    received_at = datetime.now(UTC)

    project = await db.get(Project, body.project)
    stub_created = False
    if project is None:
        project = Project(name=body.project, status="active", created_at=received_at)
        db.add(project)
        stub_created = True
        # Flush now: without an explicit relationship() between Project and
        # Deposit/Event/Handoff, the unit of work does not infer the FK
        # dependency order across mapped classes on its own, so the deposit
        # insert below could otherwise be attempted before the stub row
        # exists. Still one transaction -- commit happens once, at the end.
        await db.flush()

    deposit = Deposit(
        deposit_id=body.deposit_id,
        machine_id=principal.machine.id,
        tool=body.tool,
        session=body.session,
        project=body.project,
        reason=body.reason,
        client_ts=body.client_ts,
        doctrine_version=body.doctrine_version,
        metrics=body.metrics.model_dump(exclude_none=True) if body.metrics else None,
        no_handoff=body.no_handoff,
        received_at=received_at,
        stub_created=stub_created,
    )
    db.add(deposit)
    await db.flush()  # same reasoning: events/handoff FK to deposits.deposit_id

    for e in body.events:
        db.add(
            Event(
                id=str(ULID()),
                deposit_id=deposit.deposit_id,
                project=body.project,
                seq=e.seq,
                ts=e.ts,
                kind=e.kind,
                summary=e.summary,
                payload=e.payload,
                tags=e.tags,
            )
        )

    if body.handoff is not None:
        db.add(
            Handoff(
                id=str(ULID()),
                deposit_id=deposit.deposit_id,
                project=body.project,
                stands=body.handoff.stands,
                in_flight=body.handoff.in_flight,
                blocked=body.handoff.blocked,
                next_steps=body.handoff.next_steps,
                notes=body.handoff.notes,
                received_at=received_at,
            )
        )

    await db.commit()
    return deposit


@router.post("", response_model=DepositResponse, status_code=200)
async def create_deposit(
    body: DepositRequest,
    principal: Principal = Depends(require_machine),
    db: AsyncSession = Depends(get_db),
) -> DepositResponse:
    if not _is_valid_ulid(body.deposit_id):
        raise ApiError(422, "invalid_deposit_id", f"deposit_id '{body.deposit_id}' is not a valid ULID.")

    existing = await db.get(Deposit, body.deposit_id)
    if existing is not None:
        # Idempotent replay: same deposit_id already accepted, regardless of
        # what the retried body contains. Store nothing new; return the
        # original acknowledgment plus the replayed marker.
        return await _build_ack(db, existing, replayed=True)

    _validate_knowledge(body)
    _validate_events(body.events)
    _validate_handoff_or_waiver(body)

    try:
        deposit = await _insert_deposit(db, body, principal)
    except IntegrityError:
        # Lost a race against a concurrent identical retry that committed
        # first. Same outcome as a normal replay: nothing new stored, return
        # the (now-existing) original acknowledgment.
        await db.rollback()
        existing = await db.get(Deposit, body.deposit_id)
        if existing is not None:
            return await _build_ack(db, existing, replayed=True)
        raise

    return await _build_ack(db, deposit, replayed=False)
