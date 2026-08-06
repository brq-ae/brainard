"""Shared full-text search query (contracts-v1.md §6 note, §7).

Used by both GET /v1/search (app/routers/search.py) and the UI search page
(app/routers/ui_search.py) -- kept here once so the two surfaces never
drift on scope semantics or ranking.
"""

import base64
from typing import Literal, NamedTuple

from sqlalchemy import Integer, Text, cast, func, literal, null, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import latest_mirrored_document_ids
from app.errors import ApiError
from app.models import Event, Handoff, KnowledgeEntry, MirroredDocument

SNIPPET_TRUNCATE_CHARS = 200

SearchScope = Literal["default", "journal", "all", "proposals", "decisions"]


class SearchResult(NamedTuple):
    type: str
    id: str
    snippet: str
    project: str | None
    rank: float
    path: str | None
    version: int | None


def encode_cursor(rank: float, id_: str) -> str:
    raw = f"{rank!r}|{id_}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        rank_s, id_ = raw.split("|", 1)
        return float(rank_s), id_
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            422,
            "invalid_cursor",
            "The `cursor` parameter is not a valid cursor for this endpoint. Recovery: omit `cursor` to start "
            "from the first page, or reuse a `next_cursor` value returned by a previous search call.",
        ) from exc


async def run_search(
    db: AsyncSession,
    *,
    q: str,
    scope: SearchScope = "default",
    include_history: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[SearchResult], str | None]:
    query_expr = func.websearch_to_tsquery("english", q)

    # Every branch below carries the same six columns (type, id, snippet,
    # project, rank, path, version) so they can be `union_all`'d; branches
    # with no notion of path/version (library, handoff, event) fill those
    # with a typed NULL.
    no_path = cast(null(), Text).label("path")
    no_version = cast(null(), Integer).label("version")

    lib_stmt = select(
        literal("library").label("type"),
        KnowledgeEntry.id.label("id"),
        KnowledgeEntry.title.label("snippet"),
        KnowledgeEntry.project.label("project"),
        func.ts_rank(KnowledgeEntry.search_vector, query_expr).label("rank"),
        no_path,
        no_version,
    ).where(KnowledgeEntry.search_vector.op("@@")(query_expr))
    if not include_history:
        # Readers see `active` content by default; history on request
        # (contracts-v1.md Principles).
        lib_stmt = lib_stmt.where(KnowledgeEntry.status == "active")
    # Doctrine proposals (§4) are inert library entries: never served by
    # default/journal/all/decisions, only surfaced via the explicit
    # 'proposals' scope.
    if scope == "proposals":
        lib_stmt = lib_stmt.where(KnowledgeEntry.is_doctrine_proposal.is_(True))
    else:
        lib_stmt = lib_stmt.where(KnowledgeEntry.is_doctrine_proposal.is_(False))

    handoff_stmt = select(
        literal("handoff").label("type"),
        Handoff.id.label("id"),
        func.left(Handoff.stands, SNIPPET_TRUNCATE_CHARS).label("snippet"),
        Handoff.project.label("project"),
        func.ts_rank(Handoff.search_vector, query_expr).label("rank"),
        no_path,
        no_version,
    ).where(Handoff.search_vector.op("@@")(query_expr))

    event_stmt = select(
        literal("event").label("type"),
        Event.id.label("id"),
        Event.summary.label("snippet"),
        Event.project.label("project"),
        func.ts_rank(Event.search_vector, query_expr).label("rank"),
        no_path,
        no_version,
    ).where(Event.search_vector.op("@@")(query_expr))

    # Mirrored decisions/docs (contracts-v1.md §5, §7): only the latest
    # version per (project, path) is ever searchable -- superseded versions
    # stay in the table (supersede-never-erase) but drop out of search.
    latest_doc_ids = latest_mirrored_document_ids()
    decision_stmt = select(
        literal("decision").label("type"),
        MirroredDocument.id.label("id"),
        MirroredDocument.title.label("snippet"),
        MirroredDocument.project.label("project"),
        func.ts_rank(MirroredDocument.search_vector, query_expr).label("rank"),
        MirroredDocument.path.label("path"),
        MirroredDocument.version.label("version"),
    ).where(
        MirroredDocument.search_vector.op("@@")(query_expr),
        MirroredDocument.kind == "adr",
        MirroredDocument.id.in_(latest_doc_ids),
    )
    document_stmt = select(
        literal("document").label("type"),
        MirroredDocument.id.label("id"),
        MirroredDocument.title.label("snippet"),
        MirroredDocument.project.label("project"),
        func.ts_rank(MirroredDocument.search_vector, query_expr).label("rank"),
        MirroredDocument.path.label("path"),
        MirroredDocument.version.label("version"),
    ).where(
        MirroredDocument.search_vector.op("@@")(query_expr),
        MirroredDocument.kind == "doc",
        MirroredDocument.id.in_(latest_doc_ids),
    )

    # Scopes (contracts-v1.md §7): 'default' = library + decisions (latest
    # ADR mirrors) + handoffs. 'journal' adds the events journal on top of
    # default. 'all' is everything: default + journal + doc-kind mirrors.
    # 'proposals' and 'decisions' are their own isolated lanes -- never mixed
    # with the others.
    if scope == "proposals":
        statements = [lib_stmt]
    elif scope == "decisions":
        statements = [decision_stmt]
    else:
        statements = [lib_stmt, handoff_stmt, decision_stmt]
        if scope in ("journal", "all"):
            statements.append(event_stmt)
        if scope == "all":
            statements.append(document_stmt)

    subq = union_all(*statements).subquery("search_results")
    outer = select(subq.c.type, subq.c.id, subq.c.snippet, subq.c.project, subq.c.rank, subq.c.path, subq.c.version)

    if cursor is not None:
        cursor_rank, cursor_id = decode_cursor(cursor)
        # Keyset pagination: rows strictly after the cursor in (rank DESC, id
        # DESC) order -- id (a ULID) is a stable, globally unique tiebreaker
        # even across the different source tables unioned above.
        outer = outer.where(tuple_(subq.c.rank, subq.c.id) < tuple_(cursor_rank, cursor_id))

    outer = outer.order_by(subq.c.rank.desc(), subq.c.id.desc()).limit(limit + 1)

    rows = (await db.execute(outer)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    results = [
        SearchResult(type=r.type, id=r.id, snippet=r.snippet, project=r.project, rank=float(r.rank), path=r.path, version=r.version)
        for r in page_rows
    ]

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(float(last.rank), last.id)

    return results, next_cursor
