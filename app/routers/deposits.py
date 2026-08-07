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
from app.models import Deposit, Event, Flag, Handoff, KnowledgeEntry, MirroredDocument, Project
from app.projects import apply_project_update, validate_project_update
from app.schemas import (
    DepositCounts,
    DepositProjectInfo,
    DepositRequest,
    DepositResponse,
    DocumentAckItem,
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
_RECOVERY_LIBRARY_SUPERSEDES_PROPOSAL = "proposals are closed via the owner's approve/reject, not by supersession"
_RECOVERY_UNKNOWN_ENTRY_PROJECT = (
    "use an existing project name, omit the key to file under this deposit's project, or send null for "
    "universal knowledge"
)


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
        if project is not None and (not isinstance(project, str) or not project.strip()):
            failing.append(
                {
                    "index": i,
                    "reason": "`project` must be a non-empty string or null",
                    "recovery": _RECOVERY_INVALID_KNOWLEDGE_ITEM,
                }
            )
        # Shape-only here: `project is None` covers both an absent `project`
        # key and an explicit `"project": null` -- this check only needs to
        # reject a wrong *type*, not distinguish the two. The distinction
        # (absent -> inherits the deposit's project; explicit null -> stays
        # universal) is a key-presence check (`"project" in item`, not
        # `item.get("project")`), and only matters once we know the item is
        # a well-shaped dict -- see the project cascade rule applied in
        # `_apply_knowledge` below.
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


async def _validate_knowledge_references(db: AsyncSession, items: list[dict[str, Any]], envelope_project: str) -> None:
    """Second pass: existence/status checks that require the database.
    `supersedes[]` references must exist; retire targets must exist and be
    'active'; an explicit entry-level `project` (naming a project other than
    the deposit's own) must already be registered. Every bad item is listed
    at once -- one resend fixes all of them.
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

    # Entry-level `project` existence check (added 2026-08-07, fixing the
    # FK-trap where an unknown explicit project name died as an
    # IntegrityError deep in `_apply_knowledge`, mis-surfaced as a 503 with
    # permanently-wrong "resend unchanged" advice -- resending unchanged
    # would hit the exact same FK violation forever). Unlike the deposit's
    # own envelope `project` (which auto-stubs -- contracts-v1.md §5/§6),
    # an entry naming a *different* project must name one that already
    # exists: naming a project is owner authority, and a knowledge item
    # must never be able to silently mint one. The envelope's own project
    # always counts as "existing" here even when it's brand-new to this
    # deposit -- `_insert_deposit` guarantees that stub exists (or already
    # existed) before `_apply_knowledge` ever runs.
    bad_entry_projects: list[dict[str, Any]] = []
    named_other_projects = {
        item["project"]
        for item in items
        if not _is_retire_item(item)
        and "project" in item
        and item["project"] is not None
        and item["project"] != envelope_project
    }
    if named_other_projects:
        existing_project_names = set(
            (await db.scalars(select(Project.name).where(Project.name.in_(named_other_projects)))).all()
        )
        for i, item in enumerate(items):
            if _is_retire_item(item):
                continue
            name = item.get("project")
            if "project" in item and name is not None and name != envelope_project and name not in existing_project_names:
                bad_entry_projects.append({"index": i, "project": name, "recovery": _RECOVERY_UNKNOWN_ENTRY_PROJECT})

    if bad_entry_projects:
        offending = sorted({item["project"] for item in bad_entry_projects})
        raise ApiError(
            422,
            "unknown_entry_project",
            f"{len(bad_entry_projects)} knowledge[] item(s) name a `project` {offending} that is not a "
            "registered project; the whole deposit was rejected. See `failing_items` for the scripted "
            "recovery.",
            extra={"failing_items": bad_entry_projects},
        )

    bad_supersedes: list[dict[str, Any]] = []
    bad_retires: list[dict[str, Any]] = []
    # Proposals must stay inert against the live library, in *both*
    # directions (contracts-v1.md §4): a proposal item may supersede another
    # proposal (updating one's own proposal -- both inert), but never a real,
    # non-proposal library entry (`bad_proposal_supersedes`). The carried-over
    # phase 4 delta review also requires the mirror rejection: an ordinary,
    # non-proposal item may never name a proposal in its own `supersedes[]`
    # either (`bad_library_supersedes_proposal`) -- proposals are closed by
    # the owner's approve/reject, never by another session's supersession.
    bad_proposal_supersedes: list[dict[str, Any]] = []
    bad_library_supersedes_proposal: list[dict[str, Any]] = []

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
            else:
                proposal_targets = [s for s in supersedes if s in existing and existing[s].is_doctrine_proposal]
                if proposal_targets:
                    bad_library_supersedes_proposal.append(
                        {
                            "index": i,
                            "supersedes": proposal_targets,
                            "recovery": _RECOVERY_LIBRARY_SUPERSEDES_PROPOSAL,
                        }
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
    if bad_library_supersedes_proposal:
        all_ids = sorted({s for item in bad_library_supersedes_proposal for s in item["supersedes"]})
        raise ApiError(
            422,
            "library_cannot_supersede_proposal",
            f"{len(bad_library_supersedes_proposal)} non-proposal knowledge[] item(s) name doctrine-proposal "
            f"id(s) {all_ids} in `supersedes`; supersession never crosses the proposal boundary in either "
            f"direction. Recovery: {_RECOVERY_LIBRARY_SUPERSEDES_PROPOSAL}.",
            extra={"failing_items": bad_library_supersedes_proposal},
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

        # Project cascade rule (ratified 2026-08-07, contracts-v1.md §3
        # "Absent = universal knowledge" amended): a knowledge[] item whose
        # `project` KEY IS ABSENT inherits the deposit envelope's own
        # `project` -- filed under this deposit's project by default, the
        # common case for a session working on one project. An item with an
        # EXPLICIT `"project": null` stays universal (stored as project
        # NULL) -- an opt-in, not a fallback. An explicit other project name
        # is honored as before. `"project" in item` (key presence), not
        # `item.get("project")` (which can't tell absent from explicit
        # None), is what makes this distinction real.
        entry_project = item["project"] if "project" in item else deposit.project

        entry = KnowledgeEntry(
            id=str(ULID()),
            title=item["title"],
            namespace=item["namespace"],
            project=entry_project,
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
            # Duplicate-hint suppression vs supersession: an entry that
            # explicitly names a parent in its own `supersedes[]` is a
            # deliberate, corrective replacement of that parent -- not an
            # accidental near-duplicate of it. Flagging "possibly duplicates
            # entry X" against the very entry X it was just filed to
            # supersede would be noise the librarian doesn't need. Unrelated
            # similar entries are still flagged normally.
            superseded_ids = set(unique_parent_ids)
            for other_id, other_title, rank in await _duplicate_hints(db, entry):
                if other_id in superseded_ids:
                    continue
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


# --- documents[] -- mirrored ADRs/docs (contracts-v1.md §5) ---

VALID_DOCUMENT_KINDS = frozenset({"adr", "doc"})
MAX_DOCUMENT_CONTENT_BYTES = 1024 * 1024  # 1 MB content cap, same as knowledge[] body

_RECOVERY_INVALID_DOCUMENT_ITEM = "fix the listed field(s), resend"


def _validate_documents_shape(items: list[Any]) -> None:
    """Whole-deposit, self-explaining structural validation for documents[]
    items -- no database-dependent checks are needed (unlike knowledge[]'s
    supersedes/retire references): every mirrored document is a fresh,
    self-contained version, never a reference to another one.
    """
    failing: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            failing.append(
                {"index": i, "reason": "item is not an object", "recovery": _RECOVERY_INVALID_DOCUMENT_ITEM}
            )
            continue

        path = item.get("path")
        kind = item.get("kind")
        title = item.get("title")
        content = item.get("content")

        if not isinstance(path, str) or not path.strip():
            failing.append(
                {"index": i, "reason": "`path` must be a non-empty repo-relative string", "recovery": _RECOVERY_INVALID_DOCUMENT_ITEM}
            )
        if kind not in VALID_DOCUMENT_KINDS:
            failing.append(
                {
                    "index": i,
                    "reason": f"`kind` must be one of {sorted(VALID_DOCUMENT_KINDS)}, got {kind!r}",
                    "recovery": _RECOVERY_INVALID_DOCUMENT_ITEM,
                }
            )
        if not isinstance(title, str) or not title.strip():
            failing.append(
                {"index": i, "reason": "`title` must be non-empty", "recovery": _RECOVERY_INVALID_DOCUMENT_ITEM}
            )
        if not isinstance(content, str) or not content.strip():
            failing.append(
                {"index": i, "reason": "`content` must be non-empty", "recovery": _RECOVERY_INVALID_DOCUMENT_ITEM}
            )
        elif len(content.encode("utf-8")) > MAX_DOCUMENT_CONTENT_BYTES:
            failing.append(
                {
                    "index": i,
                    "reason": f"`content` exceeds the {MAX_DOCUMENT_CONTENT_BYTES} byte cap",
                    "recovery": _RECOVERY_INVALID_DOCUMENT_ITEM,
                }
            )
        # Unknown keys are rejected too -- strict validation, matching the
        # contract's "no direct write endpoints, checkpoints only" posture:
        # a typo'd field should never silently no-op.
        unknown_keys = sorted(set(item) - {"path", "kind", "title", "content"})
        if unknown_keys:
            failing.append(
                {
                    "index": i,
                    "reason": f"unknown field(s) {unknown_keys}; only path/kind/title/content are recognized",
                    "recovery": _RECOVERY_INVALID_DOCUMENT_ITEM,
                }
            )

    if failing:
        raise ApiError(
            422,
            "invalid_document_entry",
            f"{len(failing)} documents[] item(s) failed validation; the whole deposit was rejected. "
            "See `failing_items` for the scripted recovery.",
            extra={"failing_items": failing},
        )


async def _apply_documents(
    db: AsyncSession, items: list[dict[str, Any]], deposit: Deposit, principal: Principal
) -> list[DocumentAckItem]:
    """Applies the documents[] compartment in list order, within the same
    uncommitted transaction as the rest of the deposit. `version` is a
    per-(project, path) sequence starting at 1: supersede-never-erase means a
    redeposit of the same path is never an overwrite, it's the next version.
    Flushing after each insert makes same-deposit duplicate paths resolve
    deterministically and sequentially -- the max() query below sees the
    prior item's flushed-but-uncommitted row within this same transaction.
    """
    ack: list[DocumentAckItem] = []

    for item in items:
        path = item["path"]
        max_version = await db.scalar(
            select(func.max(MirroredDocument.version)).where(
                MirroredDocument.project == deposit.project, MirroredDocument.path == path
            )
        )
        version = (max_version or 0) + 1

        doc = MirroredDocument(
            id=str(ULID()),
            project=deposit.project,
            path=path,
            kind=item["kind"],
            title=item["title"],
            content=item["content"],
            version=version,
            deposit_id=deposit.deposit_id,
            machine_id=principal.machine.id,
            created_at=deposit.received_at,
        )
        db.add(doc)
        await db.flush()  # so the next same-path item in this deposit sees this version

        ack.append(DocumentAckItem(path=doc.path, version=doc.version, id=doc.id))

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
    documents_ack = [DocumentAckItem(**item) for item in (deposit.documents_ack or [])]
    return DepositResponse(
        deposit_id=deposit.deposit_id,
        received_at=deposit.received_at,
        replayed=replayed,
        counts=DepositCounts(
            events=events_count, handoff=handoff_stored, knowledge=len(knowledge_ack), documents=len(documents_ack)
        ),
        project=DepositProjectInfo(name=deposit.project, stub_created=deposit.stub_created),
        knowledge=knowledge_ack,
        documents=documents_ack,
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

    # Optional project registry write (contracts-v1.md §5), applied to the
    # same row -- new stub or pre-existing -- atomically with the rest of
    # this deposit. Already validated (shape + values) before `_insert_deposit`
    # was called.
    if body.project_update is not None:
        apply_project_update(project, body.project_update)

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

    documents_ack = await _apply_documents(db, body.documents, deposit, principal)
    deposit.documents_ack = [item.model_dump() for item in documents_ack]

    await db.commit()
    return deposit


# Two distinct races surface as IntegrityError on the insert attempt below,
# disambiguated by whether *this* deposit_id exists afterward:
#   1. A concurrent retry of the *same* deposit_id committed first -- the
#      pre-existing idempotent-replay path (unchanged): return its ack.
#   2. A concurrent, *different* deposit collided on something else -- e.g.
#      two deposits computing the same "next version" for the same mirrored
#      document (project, path) via ix_mirrored_documents_project_path_version
#      (see app/models.py's MirroredDocument and app/documents.py). This
#      deposit_id was never written, so replay doesn't apply: bounded
#      in-server retry instead, recomputing version numbers fresh each time.
MAX_INSERT_ATTEMPTS = 3

_RECOVERY_DEPOSIT_CONFLICT = "resend the same deposit_id unchanged; it will be accepted"


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
    await _validate_knowledge_references(db, body.knowledge, body.project)
    _validate_documents_shape(body.documents)
    _validate_events(body.events)
    _validate_handoff_or_waiver(body)
    if body.project_update is not None:
        validate_project_update(body.project_update)

    for attempt in range(1, MAX_INSERT_ATTEMPTS + 1):
        try:
            deposit = await _insert_deposit(db, body, principal)
        except IntegrityError:
            await db.rollback()
            existing = await db.get(Deposit, body.deposit_id)
            if existing is not None:
                # Race #1 above: lost to a concurrent identical retry that
                # committed first. Same outcome as a normal replay.
                return await _build_ack(db, existing, replayed=True)
            if attempt < MAX_INSERT_ATTEMPTS:
                # Race #2 above: retry the whole insert, version numbers
                # recomputed fresh against the post-rollback state.
                # `rollback()` expires every object tracked by this session,
                # including `principal.machine` (loaded earlier, on this
                # same session, by the `require_machine` dependency) --
                # refresh it explicitly so the retried `_insert_deposit`
                # doesn't trigger an implicit lazy load, which AsyncSession
                # doesn't support outside an already-awaited context.
                await db.refresh(principal.machine)
                continue
            # Pathological contention: every attempt collided. Fail loudly
            # but through the contract's envelope, never a raw 500 -- every
            # rejection is self-explaining with a scripted recovery
            # (Principles: "never lose knowledge at deposit time").
            raise ApiError(
                503,
                "deposit_conflict_retry",
                "A concurrent write collided with this deposit repeatedly and in-server retries did not "
                f"resolve it; nothing was stored. Recovery: {_RECOVERY_DEPOSIT_CONFLICT}.",
            ) from None
        else:
            return await _build_ack(db, deposit, replayed=False)

    # Unreachable: the loop above always returns or raises.
    raise AssertionError("unreachable")
