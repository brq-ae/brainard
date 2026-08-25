"""SQLAlchemy ORM models.

Phase 1: owner_token, machines.
Phase 2: deposits, events, handoffs, projects (see docs/spec/contracts-v1.md §2).
Phase 3: knowledge_entries, flags, and FTS `search_vector` columns on
knowledge_entries/handoffs/events (see docs/spec/contracts-v1.md §3, §6, §7).
Phase 4: doctrine_versions, bootstrap_fetches, doctrine-proposal columns on
knowledge_entries, and `projects.description` (see docs/spec/contracts-v1.md
§4, §6).
Phase 5: mirrored_documents (ADR/doc mirror), `deposits.documents_ack`
(see docs/spec/contracts-v1.md §5, §7).
Notification channel config: notification_configs (owner-managed ntfy
channel injected into bootstrap's operating instructions).
"""

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
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

_MIRRORED_DOCUMENT_SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(content, '')), 'B')"
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
    # Commander/Builder division of labor (doctrine rule G10; see
    # app/roles.py for the single-source-of-truth role text). 'solo' is the
    # default -- no role text is injected into this machine's bootstrap, and
    # no role paragraph appears in a generated onboarding prompt for it.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="solo")
    # Hint only, used to pre-fill generated onboarding prompts -- NEVER
    # enforced: a token still bootstraps any project (contracts-v1.md §1
    # binds a token to a machine, not to a project).
    default_project: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (CheckConstraint("role IN ('solo', 'commander', 'builder')", name="ck_machines_role"),)


class Project(Base):
    """Thin registry stub (full registry is phase 5). Auto-created on first
    mention of an unknown project name by a deposit.
    """

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Registry fact served by bootstrap's "project context" section
    # (contracts-v1.md §5, §6). Absent for auto-stubbed projects until an
    # owner/session sets one -- no write endpoint exists yet (phase 5).
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    # Per-item documents[] acknowledgment detail (path/version/id), stored
    # verbatim for the same idempotent-replay reason as `knowledge_ack` above
    # -- a redeposit of the same path is versioned relative to whatever
    # existed at acceptance time, which can't be re-derived from current DB
    # state alone once later deposits have added still-newer versions.
    documents_ack: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)


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
    # Doctrine proposal flag (contracts-v1.md §4): a proposal is stored as an
    # ordinary library entry -- same table, same supersession/dedup rules --
    # but is excluded from the bootstrap lessons digest and from default
    # search scope (see app/routers/search.py, app/routers/bootstrap.py).
    is_doctrine_proposal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 'approved' | 'rejected', set once by POST /v1/proposals/{id}/approve|reject.
    # Recording the decision never mutates doctrine itself -- promotion is the
    # owner's separate, deliberate POST to the doctrine endpoints.
    proposal_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    proposal_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_KNOWLEDGE_ENTRY_SEARCH_VECTOR_SQL, persisted=True), nullable=True
    )

    __table_args__ = (Index("ix_knowledge_entries_search_vector", "search_vector", postgresql_using="gin"),)


class Flag(Base):
    """The librarian's inbox (§3): fork and duplicate signals raised while
    processing a deposit's knowledge[] compartment. Purely informative --
    never blocks acceptance of the deposit that raised them.

    Phase 8 (librarian support): `resolved_at`/`resolved_by` close a flag out
    -- set together, server-side, by `POST /v1/flags/{id}/resolve`
    (app/routers/flags.py). Both null means unresolved (the default listing
    filter). Resolution is a terminal action attributed to the resolving
    machine, same shape as every other "who/when" pair in this schema
    (`KnowledgeEntry.machine_id`/`created_at`, etc.) -- never re-cleared,
    consistent with supersede-never-erase: a flag is closed, not un-flagged.
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
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    resolved_by: Mapped[str | None] = mapped_column(String(26), ForeignKey("machines.id"), nullable=True, index=True)


# --- Phase 4: doctrine & bootstrap (contracts-v1.md §4, §6) ---


class DoctrineVersion(Base):
    """One immutable doctrine version -- either the global rulebook (`kind`
    'global', `project` null) or one project's overlay (`kind` 'overlay',
    `project` set). Owner-only writes; every write is a new version, never an
    edit -- supersede-never-erase applies to doctrine too (§4: "Versioned:
    every doctrine change bumps a version").
    """

    __tablename__ = "doctrine_versions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # 'global' | 'overlay'
    project: Mapped[str | None] = mapped_column(String(255), ForeignKey("projects.name"), nullable=True, index=True)
    # Per-(kind, project) sequence starting at 1. Computed and enforced in the
    # route handler (app/doctrine.py's `next_version`), not a DB constraint --
    # Postgres unique indexes treat every NULL `project` as distinct from every
    # other NULL, so a plain UNIQUE(kind, project, version) would never catch a
    # collision among 'global' rows (which are always project=NULL).
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # global: [{"id": "G1", "tier": "non_negotiable"|"default", "text": ...}, ...]
    # overlay: {"overrides": [{"id", "text"}, ...], "additions": [{"id", "text"}, ...]}
    rules: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_doctrine_versions_global_version",
            "kind",
            "version",
            unique=True,
            postgresql_where=text("project IS NULL"),
        ),
        Index(
            "ix_doctrine_versions_overlay_version",
            "project",
            "version",
            unique=True,
            postgresql_where=text("kind = 'overlay'"),
        ),
    )


# --- Phase 5: projects & mirrored documents (contracts-v1.md §5, §7) ---


class MirroredDocument(Base):
    """A mirrored copy of a project's own ADR or doc (contracts-v1.md §5):
    "the project's own git repo stays canonical ... the Brain stores it
    searchable under the project." Supersede-never-erase applies here too --
    a redeposit of the same `path` never overwrites, it creates the *next*
    `version` in a per-(project, path) sequence starting at 1. Latest-version
    lookups (search scope=decisions, project doc counts) join back against
    the max version per (project, path); see app/documents.py.
    """

    __tablename__ = "mirrored_documents"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    project: Mapped[str] = mapped_column(String(255), ForeignKey("projects.name"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # 'adr' | 'doc'
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    deposit_id: Mapped[str] = mapped_column(String(26), ForeignKey("deposits.deposit_id"), nullable=False, index=True)
    machine_id: Mapped[str] = mapped_column(String(26), ForeignKey("machines.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_MIRRORED_DOCUMENT_SEARCH_VECTOR_SQL, persisted=True), nullable=True
    )
    # ADR-0012 decision 3/4: set only when this document was born from
    # "saving" a room attachment to the Brain -- links this deposit's
    # `content` back to the exact blob it was extracted/copied from, so
    # app/attachments.py's reference-counted deletion sweep can see "a Brain
    # document still references this blob" and keep the blob alive forever
    # (supersede-never-erase: a MirroredDocument row is never deleted, so
    # once set this reference never goes away, regardless of what happens to
    # the room(s) that originally held the file). NULL for every ordinary
    # deposited document (the overwhelming majority) -- this column is
    # exclusively the room-attachment-to-Brain bridge, nothing else writes
    # it. No ondelete: `attachment_blobs` rows this points at are only ever
    # removed by the sweep once nothing (including this column) references
    # them, so the FK's default RESTRICT should never actually fire; if it
    # ever did, that would mean the sweep's own referenced-check was wrong,
    # and RESTRICT surfacing a loud DB error is the correct failure mode.
    blob_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("attachment_blobs.sha256"), nullable=True, index=True
    )

    __table_args__ = (
        Index("ix_mirrored_documents_project_path", "project", "path"),
        # Guards against two concurrent deposits computing the same "next
        # version" for the same (project, path) -- a genuine race the
        # application-level max()-then-insert logic in
        # app/routers/deposits.py cannot fully rule out on its own (same
        # limitation as DoctrineVersion.version; see that model's docstring).
        # A collision here raises IntegrityError, which app/routers/
        # deposits.py's create_deposit handles with a bounded in-server
        # retry (version numbers recomputed fresh each attempt); if every
        # attempt still collides, the client gets a proper enveloped 503
        # (`deposit_conflict_retry`) rather than a raw 500 -- never an
        # unexplained failure, per the contract's self-explaining principle.
        Index(
            "ix_mirrored_documents_project_path_version",
            "project",
            "path",
            "version",
            unique=True,
        ),
        Index("ix_mirrored_documents_search_vector", "search_vector", postgresql_using="gin"),
    )


class BootstrapFetch(Base):
    """Server-side log of every GET /v1/bootstrap call (§6: "Every fetch is
    logged server-side (machine, project, doctrine version, timestamp)").
    """

    __tablename__ = "bootstrap_fetches"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    machine_id: Mapped[str] = mapped_column(String(26), ForeignKey("machines.id"), nullable=False, index=True)
    project: Mapped[str] = mapped_column(String(255), ForeignKey("projects.name"), nullable=False, index=True)
    doctrine_global_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doctrine_overlay_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Notification channel config (owner-managed ntfy channel) ---


class NotificationConfig(Base):
    """One immutable version of the owner's ntfy notification channel
    config. Supersede-never-erase applies (Principles): every change --
    rotating the URL, changing the topic -- is a new version, never an edit.
    The CURRENT (highest-version) row is what app/routers/bootstrap.py
    interpolates into the "Notifications" subsection of the operating
    instructions, so it always reflects the live config.
    """

    __tablename__ = "notification_configs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    # Sequential from 1. Unlike DoctrineVersion, there is no (kind, project)
    # dimension to partition by here -- one channel, one global sequence --
    # so a plain table-wide unique index is the correct analog of doctrine's
    # partial-unique global-version index; see DoctrineVersion.version's
    # docstring for why *that* one needs a partial index (a shared table with
    # a second, differently-scoped 'overlay' kind) and why this table
    # doesn't share that concern. Computed in app/notifications.py's
    # `next_version`, not a DB sequence -- same pattern as
    # DoctrineVersion.version.
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    ntfy_url: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    # Owner's free-form comment on this version, e.g. "rotated after X".
    # Never shown to sessions via bootstrap -- owner-facing only.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- LLM provider config (ADR-0010 phase 1: pluggable librarian runtimes,
# built-in LLM client) ---


class LlmConfig(Base):
    """One immutable version of the owner's LLM provider config -- base
    URL, model, and an optional API key for the built-in librarian runtime
    (ADR-0010). Supersede-never-erase applies (Principles): every change is
    a new version, never an edit. The CURRENT (highest-version) row is what
    app/llm_config.py's `resolve_llm_config` falls back to when the
    `LLM_BASE_URL`/`LLM_MODEL` env vars are unset (ADR-0010 decision 3:
    "an environment variable takes precedence over the stored value").

    `api_key` is stored in plaintext, deliberately not encrypted at rest
    (ADR-0010 decision 3: an encryption key living in the same env file on
    the same host is security theatre) -- masked in every API/UI response
    (never served back in full; see app/llm_config.py's masking helpers)
    and never logged. Database backups will contain it.
    """

    __tablename__ = "llm_configs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    # Sequential from 1, single global scope -- same reasoning as
    # NotificationConfig.version (see that model's docstring); computed in
    # app/llm_config.py's `next_version`, not a DB sequence.
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL is a legitimate, common value: Ollama and other local
    # OpenAI-compatible endpoints need no key at all.
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Owner's free-form comment on this version, e.g. "switched to local
    # Ollama". Never served to sessions -- owner-facing only.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Built-in librarian run history (ADR-0010 phase 2: the built-in engine) ---


class LibrarianRun(Base):
    """One completed built-in-librarian run (app/librarian_engine.py's
    `run_librarian`). A single row is written once, at completion --
    there is no "in progress" row: a crash mid-run simply leaves no row for
    that attempt (same acceptable trade-off as any unlogged crash in a
    single-owner LAN deployment; the scheduled loop or the next owner-
    triggered run tries again). `status` is 'skipped' when no LLM provider
    was configured (no work attempted, no LLM call made), 'error' when the
    run aborted early (repeated provider failures) or hit an unexpected
    failure, and 'ok' otherwise. `counts` is a small JSON summary (flags
    seen/merged/left-distinct/stale per type, lessons seen/harvested/
    skipped, llm_calls/llm_failures, stale project names) -- the same
    figures also written into the run's own summary deposit note, so this
    table and the library/journal stay consistent without re-deriving one
    from the other.
    """

    __tablename__ = "librarian_runs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # 'ok' | 'error' | 'skipped'
    counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (CheckConstraint("status IN ('ok', 'error', 'skipped')", name="ck_librarian_runs_status"),)


# --- Agent chat rooms (ADR-0006, phase A: core rooms/messages/long-poll/
# guardrails/notify) ---


class Room(Base):
    """A live agent-to-agent chat room. v1 is two-agent only (enforced at
    create time by app/rooms.py, not by this table -- `room_members` models
    the general many-member concept per the ADR). `max_messages` is the hard
    backstop cap (guardrail 3 of 3, ADR-0006 decision 5); `message_count` is
    denormalized onto the room row so the cap check never needs a COUNT(*)
    over room_messages on every post. `close_reason` records which of the
    three guardrails (or an owner close) ended the room -- null while open.
    """

    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    max_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Owner notification on close (ADR-0006 decision 6) -- always true today
    # (no API surface sets it false in phase A), but modeled as a column so a
    # future per-room opt-out doesn't need a migration.
    notify_on_close: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ADR-0012 decision 7: per-room opt-out disabling AGENT (never owner)
    # file uploads -- same "always-on boolean the owner can flip off" shape
    # as `notify_on_close` just above. Default true (agent uploads allowed);
    # toggleable mid-room by the owner (app/rooms.py's
    # `set_agent_uploads_allowed`, same row-lock + system-announcement
    # pattern `switch_room_mode` uses). Decision 8: this only blocks
    # CREATING new files -- attaching a document already saved in the Brain
    # is unaffected by this flag either way.
    agent_uploads_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ADR-0007: room modes and time limits. `mode` shapes the join prompt's
    # injected role text (app/room_modes.py is the single source of what
    # each mode means); 'freeform' (default) carries no special stance.
    # `topic` is required by create_room whenever mode != 'freeform'.
    # `expires_at` is the optional wall-clock deadline the background
    # sweeper (app/room_sweeper.py) closes the room against (close_reason
    # 'time') -- an independent second backstop alongside the message cap.
    # `closing_warned_at` guards the sweeper's one-time "closing soon"
    # system-message nudge so it never double-posts.
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="freeform")
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closing_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ADR-0008: free-form room grouping label, set at creation or bulk-
    # assigned to selected rooms via the UI/API (app/rooms.py's
    # assign_group_to_rooms). Named `group_name` because 'group' is a SQL
    # reserved word; exposed as `group` in the API/UI (RoomCreateRequest.group,
    # etc.). NULL means ungrouped. Free-form text, not tied to Brain
    # projects (ADR-0008 decision 3) -- owner-supplied, so untrusted content
    # wherever it renders in the UI (must be autoescaped, never |safe).
    group_name: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="ck_rooms_status"),
        CheckConstraint(
            "close_reason IN ('done', 'owner', 'cap', 'stall', 'time') OR close_reason IS NULL",
            name="ck_rooms_close_reason",
        ),
        CheckConstraint(
            "mode IN ('freeform', 'debate', 'collaborate', 'brainstorm', 'critique')",
            name="ck_rooms_mode",
        ),
    )


class RoomMember(Base):
    """A room participant. v1 enforces exactly 2 rows per room at create
    time (app/rooms.py) -- this table itself models the general concept, per
    ADR-0006's "the table models the general concept" note.
    """

    __tablename__ = "room_members"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("rooms.id"), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # ADR-0007: this member's stance in an asymmetric mode -- 'for'/'against'
    # (debate) or 'proposer'/'critic' (critique), per app/room_modes.py's
    # ROOM_MODES[mode].sides. NULL for symmetric modes (collaborate,
    # brainstorm) and freeform.
    side: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_room_members_room_agent", "room_id", "agent_name", unique=True),)


class RoomMessage(Base):
    """One message in a room's transcript. `seq` is a monotonic, per-room
    sequence starting at 1 -- the cursor `GET .../messages?since=<seq>`
    long-polls against (app/rooms.py). `kind` 'done' is the agent-initiated
    close signal (guardrail 1); 'system' is reserved for server-authored
    messages (never accepted from POST .../messages -- see app/rooms.py's
    VALID_POST_KINDS).
    """

    __tablename__ = "room_messages"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    room_id: Mapped[str] = mapped_column(String(26), ForeignKey("rooms.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    sender: Mapped[str] = mapped_column(Text, nullable=False)  # a member's agent_name, or the literal 'owner'
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="message")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('message', 'done', 'system')", name="ck_room_messages_kind"),
        # Serves both the "index on (room_id, seq)" and "unique on
        # (room_id, seq)" requirements at once -- a unique index is also a
        # usable index for the ordinary range/cursor lookups.
        Index("ix_room_messages_room_seq", "room_id", "seq", unique=True),
    )


# --- Room file attachments (ADR-0012) ---


class AttachmentBlob(Base):
    """One piece of content-addressed blob storage (ADR-0012 decision 2):
    the bytes live once, on disk in the `attachment_data` named volume
    (docker-compose.yml), at a path derived *only* from `sha256` (see
    app/attachments.py's `blob_path` -- never from any filename, so path
    traversal via a supplied name is impossible by construction). The
    display filename is deliberately NOT stored here -- it lives on
    `RoomAttachment.filename` (and, once a file is saved, implicitly on the
    `MirroredDocument` row it becomes) precisely so the same blob can appear
    under different names in different rooms with zero duplication
    (decision 2: "identical uploads dedupe automatically").

    `sha256` is the primary key, not a separate server ULID -- content
    addressing means the hash already *is* the identity; a second surrogate
    id would only invite the two ever drifting apart.

    Lifetime is reference-counted (decision 4), never a status flag on this
    row: app/attachments.py's sweep computes "erase when no `MirroredDocument`
    references this hash AND every `RoomAttachment` referencing it belongs
    to a room that is closed and past its grace period" fresh from
    `RoomAttachment`/`MirroredDocument`/`Room` state each pass, rather than
    caching the answer here -- one less place for that answer to go stale.
    """

    __tablename__ = "attachment_blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("byte_size > 0", name="ck_attachment_blobs_byte_size_positive"),
        # A sha256 hex digest is always exactly 64 lowercase hex characters
        # -- app/attachments.py never writes anything else here, but the
        # constraint makes that invariant enforced at the DB level too, not
        # just trusted of the application layer (defense in depth for the
        # same "path traversal impossible by construction" property the
        # storage-path derivation relies on).
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_attachment_blobs_sha256_hex64"),
    )


class RoomAttachment(Base):
    """A room's reference to one blob (ADR-0012 decision 2): "a room does
    not own a blob -- it holds an attachment reference row." `filename` is
    the DISPLAY name (owner/agent-supplied, sanitized through
    app/room_export.py's `safe_filename_component` before it ever reaches
    this column -- see app/attachments.py) -- it lives here, not on
    `AttachmentBlob`, specifically so two rooms sharing a deduped blob can
    each call it something different.

    `room_id` uses `ondelete="CASCADE"`, a deliberate, narrow exception to
    this codebase's usual convention of explicit application-level cascade
    over FK-level cascade (see `Room.delete_room`'s docstring in
    app/rooms.py, which spells out that convention for `room_members`/
    `room_messages`). app/rooms.py's `delete_room` is out of scope for the
    task that introduced this table (ADR-0012's storage core landed while a
    parallel task owned the room UI/routers, `app/rooms.py` included) and
    does not know `room_attachments` exists, so without DB-level cascade,
    deleting a room that has any attachment would raise an unhandled FK
    violation. CASCADE here means an explicit room hard-delete also
    immediately drops that room's attachment references (and, transitively,
    any blob that leaves unreferenced becomes sweep-eligible next pass) --
    correct: a hard delete is a stronger, more deliberate owner action than
    a room merely closing, so losing the grace-period cushion on explicit
    delete is expected, not a bug. app/rooms.py's `delete_room` now DOES
    add `room_attachments` to its own explicit deletes (ADR-0012 stage 2,
    alongside `release_blobs_for_deleted_room`'s eager blob reclaim), for
    consistency with the rest of that function -- the CASCADE here is a
    harmless no-op backstop, not the only thing standing between a delete
    and a 500.
    """

    __tablename__ = "room_attachments"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # server ULID
    room_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blob_sha256: Mapped[str] = mapped_column(String(64), ForeignKey("attachment_blobs.sha256"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    # The room member's `agent_name` that uploaded it, or the literal
    # 'owner' -- same sender vocabulary as `RoomMessage.sender`.
    uploaded_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AttachmentStorageStats(Base):
    """Single-row (fixed 'singleton' PK, same enforcement trick as
    `OwnerToken`) running total of bytes committed across all
    `AttachmentBlob` rows -- the denormalized counter the global-ceiling
    check (ADR-0012 decision 6) locks and reads under `SELECT ... FOR
    UPDATE` before admitting a genuinely new (non-deduplicated) blob, same
    "lock the row, read the current total, decide, write" discipline
    `Room.message_count` uses for the per-room message cap (app/rooms.py).
    Kept as its own row rather than a `SUM(byte_size)` over
    `attachment_blobs` on every upload so the ceiling check is one cheap
    locked row read, not a full-table aggregate under lock (which would
    serialize *every* upload globally, not just genuinely-new-content ones).
    """

    __tablename__ = "attachment_storage_stats"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="singleton")
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (CheckConstraint("total_bytes >= 0", name="ck_attachment_storage_stats_total_bytes_nonneg"),)
