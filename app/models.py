"""SQLAlchemy ORM models.

Phase 1: owner_token, machines.
Phase 2: deposits, events, handoffs, projects (see docs/spec/contracts-v1.md §2).
Phase 3: knowledge_entries, flags, and FTS `search_vector` columns on
knowledge_entries/handoffs/events (see docs/spec/contracts-v1.md §3, §6, §7).
"""

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, Computed, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# SQL text for the generated `search_vector` columns (phase 3 FTS). Kept as
# module-level constants so the ORM model (used by tests via
# `Base.metadata.create_all`) and alembic/versions/0003_library.py (used by
# `alembic upgrade head` in the real stack) stay in sync -- see docs/dev.md
# for why the two paths exist.
_EVENT_SEARCH_VECTOR_SQL = "to_tsvector('english', coalesce(summary, ''))"

_HANDOFF_SEARCH_VECTOR_SQL = (
    "to_tsvector('english', "
    "coalesce(stands, '') || ' ' || coalesce(in_flight, '') || ' ' || "
    "coalesce(blocked, '') || ' ' || coalesce(next_steps, '') || ' ' || coalesce(notes, ''))"
)

_KNOWLEDGE_ENTRY_SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(body, '')), 'B')"
)


class OwnerToken(Base):
    """The single root credential. The fixed 'singleton' primary key makes a
    second row a primary-key violation, enforcing "one row" at the schema level.
    """

    __tablename__ = "owner_token"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="singleton")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Machine(Base):
    """A registered machine allowed to authenticate with a per-machine bearer token."""

    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class Project(Base):
    """Thin registry stub (full registry is phase 5). Auto-created on first
    mention of an unknown project name by a deposit.
    """

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Deposit(Base):
    """One atomic checkpoint deposit envelope. `deposit_id` is client-supplied
    (a ULID) and doubles as the idempotency key -- retries with the same id
    return the original acknowledgment and store nothing new.
    """

    __tablename__ = "deposits"

    deposit_id: Mapped[str] = mapped_column(String(26), primary_key=True)  # client ULID
    machine_id: Mapped[str] = mapped_column(String(26), ForeignKey("machines.id"), nullable=False, index=True)
    tool: Mapped[str] = mapped_column(String(255), nullable=False)
    session: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str] = mapped_column(String(255), ForeignKey("projects.name"), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    client_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    doctrine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    no_handoff: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Whether this deposit was the one that auto-created its project stub.
    # Stored directly so idempotent replay can return the original
    # acknowledgment verbatim without re-deriving it.
    stub_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Per-item knowledge[] acknowledgment detail (index/action/id/title),
    # stored verbatim for the same reason as `stub_created` above: a retire
    # action mutates a pre-existing entry that carries its *own* (earlier)
    # deposit_id, so the ack detail cannot be re-derived from DB state alone
    # on idempotent replay -- it must be captured at acceptance time.
    knowledge_ack: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)


class Event(Base):
    """One journal entry within a deposit's `events[]` compartment.

    `project` is denormalized from the parent deposit (rather than requiring a
    join through `deposits` for every project-scoped journal query) -- a
    deliberate query-efficiency call flagged in the phase 2 brief as the
    implementer's choice.
    """

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    deposit_id: Mapped[str] = mapped_column(String(26), ForeignKey("deposits.deposit_id"), nullable=False, index=True)
    project: Mapped[str] = mapped_column(String(255), ForeignKey("projects.name"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # Postgres-generated column backing FTS (journal scope of GET /v1/search);
    # never written by the app -- Postgres derives it from `summary` on every
    # insert/update.
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_EVENT_SEARCH_VECTOR_SQL, persisted=True), nullable=True
    )

    __table_args__ = (Index("ix_events_search_vector", "search_vector", postgresql_using="gin"),)


class Handoff(Base):
    """The structured handoff note carried by a deposit, if any. At most one
    per deposit (enforced by the unique index on `deposit_id`).
    """

    __tablename__ = "handoffs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    deposit_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("deposits.deposit_id"), nullable=False, unique=True, index=True
    )
    project: Mapped[str] = mapped_column(String(255), ForeignKey("projects.name"), nullable=False, index=True)
    stands: Mapped[str] = mapped_column(Text, nullable=False)
    in_flight: Mapped[str] = mapped_column(Text, nullable=False)
    blocked: Mapped[str] = mapped_column(Text, nullable=False)
    next_steps: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_HANDOFF_SEARCH_VECTOR_SQL, persisted=True), nullable=True
    )

    __table_args__ = (Index("ix_handoffs_search_vector", "search_vector", postgresql_using="gin"),)


# --- Phase 3: library (contracts-v1.md §3) ---


class KnowledgeEntry(Base):
    """A library entry: markdown body + frontmatter (contracts-v1.md §3).

    `search_vector` is a Postgres-generated column (title weighted above
    body) backing both GET /v1/search and the duplicate-hint FTS query run
    on arrival; never written by the app.
    """

    __tablename__ = "knowledge_entries"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # 'lessons' | 'howto' | 'reference' -- exactly three shelves (§3).
    namespace: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    project: Mapped[str | None] = mapped_column(String(255), ForeignKey("projects.name"), nullable=True, index=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # 'active' | 'superseded' (server-set) | 'retired' (explicit, reasoned).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    retire_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Array of parent entry ids this entry supersedes -- merges have one
    # child, many parents (§3). Any session may supersede any entry.
    supersedes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Source (§3, "mixed"): `machine` from the authenticated token; `tool`
    # and `session` denormalized from the parent deposit envelope, same
    # rationale as `Event.project` above.
    machine_id: Mapped[str] = mapped_column(String(26), ForeignKey("machines.id"), nullable=False, index=True)
    tool: Mapped[str] = mapped_column(String(255), nullable=False)
    session: Mapped[str] = mapped_column(String(255), nullable=False)
    deposit_id: Mapped[str] = mapped_column(String(26), ForeignKey("deposits.deposit_id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_KNOWLEDGE_ENTRY_SEARCH_VECTOR_SQL, persisted=True), nullable=True
    )

    __table_args__ = (Index("ix_knowledge_entries_search_vector", "search_vector", postgresql_using="gin"),)


class Flag(Base):
    """The librarian's future inbox (§3): fork and duplicate signals raised
    while processing a deposit's knowledge[] compartment. Purely informative
    -- never blocks acceptance of the deposit that raised them.
    """

    __tablename__ = "flags"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # 'fork' | 'duplicate'
    entry_id: Mapped[str] = mapped_column(String(26), ForeignKey("knowledge_entries.id"), nullable=False, index=True)
    related_entry_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("knowledge_entries.id"), nullable=True, index=True
    )
    # 'fork': {"parent_id": ...}. 'duplicate': {"rank": ..., "title": ...}.
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
