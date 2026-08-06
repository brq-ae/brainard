"""The checkpoint deposit -- POST /v1/deposits (contracts-v1.md §2).

One atomic batch, fully accepted or fully rejected. Machine-token only: this
is the first session-facing route, so it's also the first real exercise of
`require_machine`.
"""

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.auth import Principal, require_machine
from app.db import get_db
from app.errors import ApiError
from app.models import Deposit, Event, Flag, Handoff, KnowledgeEntry, Project
from app.schemas import (
    DepositCounts,
    DepositProjectInfo,
    DepositRequest,
    DepositResponse,
    EventIn,
    KnowledgeAckItem,
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


# --- knowledge[] (contracts-v1.md §3) ---

VALID_KNOWLEDGE_NAMESPACES = frozenset({"lessons", "howto", "reference"})  # exactly three shelves, per §3
MAX_BODY_BYTES = 1024 * 1024  # 1 MB body cap

# Cheap FTS similarity threshold + caps for duplicate hints on arrival (§3).
# `search_vector @@ query` already filters to true matches; MIN_DUPLICATE_RANK
# is a small extra floor against noise from an incidental single shared word
# out of a large OR-query (see `_duplicate_hints`).
MIN_DUPLICATE_RANK = 0.05
MAX_DUPLICATE_HINTS = 5
BODY_KEYWORD_CAP = 12  # top-N most frequent body lexemes folded into the OR query

_RECOVERY_INVALID_KNOWLEDGE_ITEM = "fix the listed field(s), resend"
_RECOVERY_BAD_SUPERSEDES = "fix or drop the supersedes reference, resend"
_RECOVERY_RETIRE_TARGET = "fix or drop the retire action, resend"
_RECOVERY_PROPOSAL_SUPERSEDES = "file the proposal without supersedes, or supersede only other proposals"


def _is_retire_item(item: Any) -> bool:
    return isinstance(item, dict) and "retire" in item


def _validate_knowledge_shape(items: list[Any]) -> None:
    """Whole-deposit, self-explaining structural validation for knowledge[]
    items -- shape only (namespace, non-empty title/body, size cap, field
    types). Existence checks against the database (supersedes references,
    retire targets) happen separately in `_validate_knowledge_references`,
    mirroring the two-pass style used for events[] above -- one resend can
    fix everything flagged by either pass.
    """
    failing: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            failing.append(
                {"index": i, "reason": "item is not an object", "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM}
            )
            continue

        if _is_retire_item(item):
            retire = item.get("retire")
            reason = item.get("reason")
            if not isinstance(retire, str) or not retire.strip():
                failing.append(
                    {
                        "index": i,
                        "reason": "`retire` must be a non-empty entry id",
                        "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                    }
                )
            if not isinstance(reason, str) or not reason.strip():
                failing.append(
                    {
                        "index": i,
                        "reason": "`reason` must be non-empty when retiring an entry",
                        "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                    }
                )
            # A retire action mutates an existing library entry in place --
            # `doctrine_proposal` (which only ever applies to a newly-created
            # entry) is a nonsensical combination with it.
            if item.get("doctrine_proposal"):
                failing.append(
                    {
                        "index": i,
                        "reason": "`doctrine_proposal` cannot be combined with a `retire` action",
                        "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                    }
                )
            continue

        title = item.get("title")
        namespace = item.get("namespace")
        body_text = item.get("body")
        tags = item.get("tags", [])
        project = item.get("project")
        supersedes = item.get("supersedes", [])

        if not isinstance(title, str) or not title.strip():
            failing.append(
                {"index": i, "reason": "`title` must be non-empty", "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM}
            )
        if namespace not in VALID_KNOWLEDGE_NAMESPACES:
            failing.append(
                {
                    "index": i,
                    "reason": f"`namespace` must be one of {sorted(VALID_KNOWLEDGE_NAMESPACES)}, got {namespace!r}",
                    "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                }
            )
        if not isinstance(body_text, str) or not body_text.strip():
            failing.append(
                {"index": i, "reason": "`body` must be non-empty", "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM}
            )
        elif len(body_text.encode("utf-8")) > MAX_BODY_BYTES:
            failing.append(
                {
                    "index": i,
                    "reason": f"`body` exceeds the {MAX_BODY_BYTES} byte cap",
                    "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                }
            )
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            failing.append(
                {"index": i, "reason": "`tags` must be a list of strings", "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM}
            )
        if project is not None and not isinstance(project, str):
            failing.append(
                {"index": i, "reason": "`project` must be a string", "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM}
            )
        if not isinstance(supersedes, list) or not all(isinstance(s, str) for s in supersedes):
            failing.append(
                {
                    "index": i,
                    "reason": "`supersedes` must be a list of entry id strings",
                    "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                }
            )
        # Doctrine proposal flag (contracts-v1.md §4): an ordinary knowledge[]
        # item, opted into proposal treatment (excluded from the bootstrap
        # digest and default search scope; visible via scope=proposals and
        # GET /v1/proposals) by this one boolean.
        doctrine_proposal = item.get("doctrine_proposal", False)
        if not isinstance(doctrine_proposal, bool):
            failing.append(
                {
                    "index": i,
                    "reason": "`doctrine_proposal` must be a boolean",
                    "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                }
            )

    if failing:
        raise ApiError(
            422,
            "invalid_knowledge_entry",
            f"{len(failing)} knowledge[] item(s) failed validation; the whole deposit was rejected. "
            "See `failing_items` for the scripted recovery.",
            extra={"failing_items": failing},
        )


async def _validate_knowledge_references(db: AsyncSession, items: list[dict[str, Any]]) -> None:
    """Second pass: existence/status checks that require the database.
    `supersedes[]` references must exist; retire targets must exist and be
    'active'. Every bad item is listed at once -- one resend fixes all of them.
    """
    referenced_ids: set[str] = set()
    for item in items:
        if _is_retire_item(item):
            referenced_ids.add(item["retire"])
        else:
            referenced_ids.update(item.get("supersedes", []))

    existing: dict[str, KnowledgeEntry] = {}
    if referenced_ids:
        rows = (await db.scalars(select(KnowledgeEntry).where(KnowledgeEntry.id.in_(referenced_ids)))).all()
        existing = {row.id: row for row in rows}

    bad_supersedes: list[dict[str, Any]] = []
    bad_retires: list[dict[str, Any]] = []
    # Proposals must stay inert against the live library (contracts-v1.md
    # §4): a proposal item may supersede another proposal (updating one's own
    # proposal -- both inert), but never a real, non-proposal library entry.
    bad_proposal_supersedes: list[dict[str, Any]] = []

    for i, item in enumerate(items):
        if _is_retire_item(item):
            target_id = item["retire"]
            target = existing.get(target_id)
            if target is None:
                bad_retires.append(
                    {
                        "index": i,
                        "retire": target_id,
                        "reason": "no such library entry",
                        "recovery": _RECOVERY_RETIRE_TARGET,
                    }
                )
            elif target.status != "active":
                bad_retires.append(
                    {
                        "index": i,
                        "retire": target_id,
                        "reason": f"entry is already '{target.status}', not 'active' -- retiring it again is meaningless",
                        "recovery": _RECOVERY_RETIRE_TARGET,
                    }
                )
        else:
            supersedes = item.get("supersedes", [])
            missing = [s for s in supersedes if s not in existing]
            if missing:
                bad_supersedes.append({"index": i, "missing": missing, "recovery": _RECOVERY_BAD_SUPERSEDES})
            if item.get("doctrine_proposal", False):
                non_proposal_targets = [s for s in supersedes if s in existing and not existing[s].is_doctrine_proposal]
                if non_proposal_targets:
                    bad_proposal_supersedes.append(
                        {"index": i, "supersedes": non_proposal_targets, "recovery": _RECOVERY_PROPOSAL_SUPERSEDES}
                    )

    if bad_supersedes:
        raise ApiError(
            422,
            "unknown_supersedes_reference",
            f"{len(bad_supersedes)} knowledge[] item(s) reference a `supersedes` id that does not exist; "
            "the whole deposit was rejected. See `failing_items` for the scripted recovery.",
            extra={"failing_items": bad_supersedes},
        )
    if bad_proposal_supersedes:
        all_ids = sorted({s for item in bad_proposal_supersedes for s in item["supersedes"]})
        raise ApiError(
            422,
            "proposal_cannot_supersede_library",
            f"{len(bad_proposal_supersedes)} doctrine-proposal item(s) name non-proposal library entry "
            f"id(s) {all_ids} in `supersedes`; proposals must stay inert against the live library. "
            f"Recovery: {_RECOVERY_PROPOSAL_SUPERSEDES}.",
            extra={"failing_items": bad_proposal_supersedes},
        )
    if bad_retires:
        raise ApiError(
            422,
            "invalid_retire_target",
            f"{len(bad_retires)} retire action(s) target an entry that cannot be retired; "
            "the whole deposit was rejected. See `failing_items` for the scripted recovery.",
            extra={"failing_items": bad_retires},
        )


# Builds the duplicate-hint query entirely in SQL: every distinct lexeme of
# the new entry's title, plus its top `body_keyword_cap` highest-frequency
# body lexemes (frequency = number of positions Postgres recorded for that
# lexeme), OR'd together and matched against other active entries' title+body
# `search_vector` in the same namespace. Title-only, AND-only matching (the
# prior implementation) missed near-duplicates that share body content under
# an unrelated title, or titles differing by only one word.
#
# The OR-query is assembled via `quote_literal(lexeme)` + `::tsquery` --
# NOT `to_tsquery('english', string_agg(lexeme, ' | '))` -- because lexemes
# coming out of `to_tsvector` are already stemmed, and re-running an
# already-stemmed lexeme back through the english dictionary is not always
# idempotent (verified empirically: 'compose' -> to_tsvector -> 'compos' ->
# to_tsquery('english', ...) -> 'compo', a *different* lexeme that no longer
# matches 'compos' in any other row's search_vector). Casting a string of
# already-quoted lexemes straight to `::tsquery` uses them verbatim, exactly
# like a tsquery's own text representation round-trips.
_DUPLICATE_HINTS_SQL = text(
    """
    WITH target AS (
        SELECT to_tsvector('english', :title) AS title_tsv,
               to_tsvector('english', :body) AS body_tsv
    ),
    title_terms AS (
        SELECT DISTINCT lexeme
        FROM target, unnest(title_tsv) AS u(lexeme, positions, weights)
    ),
    body_terms AS (
        SELECT lexeme
        FROM (
            SELECT lexeme, cardinality(positions) AS freq
            FROM target, unnest(body_tsv) AS u(lexeme, positions, weights)
        ) ranked
        ORDER BY freq DESC, lexeme
        LIMIT :body_keyword_cap
    ),
    terms AS (
        SELECT lexeme FROM title_terms
        UNION
        SELECT lexeme FROM body_terms
    ),
    q AS (
        SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery AS query
        FROM terms
    )
    SELECT ke.id AS id, ke.title AS title, ts_rank(ke.search_vector, q.query) AS rank
    FROM knowledge_entries ke, q
    WHERE q.query IS NOT NULL
      AND ke.namespace = :namespace
      AND ke.status = 'active'
      AND ke.id != :entry_id
      -- Doctrine proposals are excluded from the candidate pool entirely
      -- (contracts-v1.md §4: proposals stay inert against the live
      -- library) -- a proposal never surfaces as a "possible duplicate" of
      -- an ordinary entry. The other direction (a proposal not receiving
      -- hints) is enforced by the caller skipping this query for proposal
      -- entries -- see `_apply_knowledge`.
      AND ke.is_doctrine_proposal = false
      AND ke.search_vector @@ q.query
      AND ts_rank(ke.search_vector, q.query) > :min_rank
    ORDER BY rank DESC
    LIMIT :hint_limit
    """
)


async def _duplicate_hints(db: AsyncSession, entry: KnowledgeEntry) -> list[tuple[str, str, float]]:
    """Cheap FTS similarity check run on arrival (§3): same namespace,
    active entries only, ranked by `ts_rank`, top 5. Never blocks acceptance
    -- callers only ever attach hints. See `_DUPLICATE_HINTS_SQL` for the
    query construction rationale.
    """
    result = await db.execute(
        _DUPLICATE_HINTS_SQL,
        {
            "title": entry.title,
            "body": entry.body,
            "body_keyword_cap": BODY_KEYWORD_CAP,
            "namespace": entry.namespace,
            "entry_id": entry.id,
            "min_rank": MIN_DUPLICATE_RANK,
            "hint_limit": MAX_DUPLICATE_HINTS,
        },
    )
    return [(row.id, row.title, float(row.rank)) for row in result.all()]


async def _apply_knowledge(
    db: AsyncSession, items: list[dict[str, Any]], deposit: Deposit, principal: Principal
) -> list[KnowledgeAckItem]:
    """Applies the knowledge[] compartment in list order, within the same
    uncommitted transaction as the rest of the deposit. Both validation
    passes above have already run against pre-deposit database state; the
    defensive re-check on retire targets below guards the (untested,
    contract-silent) same-deposit ordering case where an earlier item in
    this same batch already changed a later item's target status.
    """
    ack: list[KnowledgeAckItem] = []

    for i, item in enumerate(items):
        if _is_retire_item(item):
            target = await db.get(KnowledgeEntry, item["retire"])
            if target is None or target.status != "active":
                raise ApiError(
                    422,
                    "invalid_retire_target",
                    f"retire target '{item['retire']}' is no longer 'active' due to an earlier action in this "
                    "same deposit; the whole deposit was rejected. Recovery: reorder or drop the conflicting "
                    "action, resend.",
                )
            target.status = "retired"
            target.retire_reason = item["reason"]
            ack.append(KnowledgeAckItem(index=i, action="retired", id=target.id, title=target.title))
            continue

        entry = KnowledgeEntry(
            id=str(ULID()),
            title=item["title"],
            namespace=item["namespace"],
            project=item.get("project"),
            tags=item.get("tags", []),
            status="active",
            supersedes=item.get("supersedes", []),
            body=item["body"],
            machine_id=principal.machine.id,
            tool=deposit.tool,
            session=deposit.session,
            deposit_id=deposit.deposit_id,
            created_at=deposit.received_at,
            is_doctrine_proposal=bool(item.get("doctrine_proposal", False)),
        )
        db.add(entry)
        await db.flush()  # id must exist before the fork/duplicate queries below reference it

        # Dedupe before the status/fork loop, preserving first-seen order:
        # `supersedes: [X, X]` must produce exactly one fork flag and one
        # clean status transition for X, not one per (redundant) occurrence.
        # `entry.supersedes` itself is left as submitted -- this only affects
        # loop iteration below.
        unique_parent_ids = list(dict.fromkeys(entry.supersedes))

        for parent_id in unique_parent_ids:
            parent = await db.get(KnowledgeEntry, parent_id)

            # Fork detection (§3): other entries that already name this same
            # parent in their own supersedes[] are existing siblings -- a
            # second child for the same parent is a fork. Accepted, both
            # left active, flagged for the librarian.
            siblings = (
                await db.scalars(
                    select(KnowledgeEntry).where(
                        KnowledgeEntry.id != entry.id,
                        KnowledgeEntry.supersedes.any(parent_id),
                    )
                )
            ).all()
            for sibling in siblings:
                db.add(
                    Flag(
                        id=str(ULID()),
                        type="fork",
                        entry_id=entry.id,
                        related_entry_id=sibling.id,
                        detail={"parent_id": parent_id},
                        created_at=deposit.received_at,
                    )
                )

            # Supersession rule (§3): an 'active' parent transitions to
            # 'superseded'. A 'retired' parent stays 'retired' -- the
            # lineage is recorded via `supersedes[]`, but a terminal,
            # explicitly-reasoned retirement is never silently reopened.
            # (An already-'superseded' parent, e.g. a prior fork sibling,
            # likewise stays as-is -- this branch only fires for 'active'.)
            if parent.status == "active":
                parent.status = "superseded"

        # Proposals stay inert against the live library in both directions
        # (contracts-v1.md §4): the SQL above already excludes proposal rows
        # from the candidate pool, and a proposal entry itself never gets
        # hints pointing at library entries -- skip the check entirely.
        if not entry.is_doctrine_proposal:
            for other_id, other_title, rank in await _duplicate_hints(db, entry):
                db.add(
                    Flag(
                        id=str(ULID()),
                        type="duplicate",
                        entry_id=entry.id,
                        related_entry_id=other_id,
                        detail={"rank": rank, "title": other_title},
                        created_at=deposit.received_at,
                    )
                )

        ack.append(KnowledgeAckItem(index=i, action="created", id=entry.id, title=entry.title))

    return ack


async def _events_count(db: AsyncSession, deposit_id: str) -> int:
    return await db.scalar(select(func.count()).select_from(Event).where(Event.deposit_id == deposit_id))


async def _handoff_stored(db: AsyncSession, deposit_id: str) -> bool:
    handoff_id = await db.scalar(select(Handoff.id).where(Handoff.deposit_id == deposit_id))
    return handoff_id is not None


async def _build_ack(db: AsyncSession, deposit: Deposit, *, replayed: bool) -> DepositResponse:
    events_count = await _events_count(db, deposit.deposit_id)
    handoff_stored = await _handoff_stored(db, deposit.deposit_id)
    knowledge_ack = [KnowledgeAckItem(**item) for item in (deposit.knowledge_ack or [])]
    return DepositResponse(
        deposit_id=deposit.deposit_id,
        received_at=deposit.received_at,
        replayed=replayed,
        counts=DepositCounts(events=events_count, handoff=handoff_stored, knowledge=len(knowledge_ack)),
        project=DepositProjectInfo(name=deposit.project, stub_created=deposit.stub_created),
        knowledge=knowledge_ack,
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

    knowledge_ack = await _apply_knowledge(db, body.knowledge, deposit, principal)
    # Stored verbatim on the deposit row -- see `Deposit.knowledge_ack`'s
    # docstring for why idempotent replay can't re-derive this from DB state.
    deposit.knowledge_ack = [item.model_dump() for item in knowledge_ack]

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

    _validate_knowledge_shape(body.knowledge)
    await _validate_knowledge_references(db, body.knowledge)
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
