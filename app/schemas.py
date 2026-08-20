"""Pydantic request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MachineCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Commander/Builder division of labor (doctrine rule G10; app/roles.py).
    # `Literal` (not a plain str) so a bad value is FastAPI's ordinary 422 --
    # same precedent as DepositRequest.reason above.
    role: Literal["solo", "commander", "builder"] = "solo"
    # Hint only, used to pre-fill generated onboarding prompts -- never
    # enforced (see app/models.py Machine.default_project docstring).
    default_project: str | None = Field(default=None, min_length=1, max_length=255)


class MachineCreateResponse(BaseModel):
    id: str
    name: str
    role: str
    default_project: str | None
    token: str  # plaintext -- shown exactly once, never retrievable again


class MachineListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime
    last_seen: datetime | None
    status: str
    role: str
    default_project: str | None


class MachineRevokeResponse(BaseModel):
    id: str
    status: str


class HealthResponse(BaseModel):
    ok: bool
    database: str


# --- Deposits (contracts-v1.md §2) ---


class MetricsIn(BaseModel):
    """Fully optional; any subset is valid, absence is never a violation."""

    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_estimate: float | None = None
    duration: float | None = None


class EventIn(BaseModel):
    seq: int
    ts: datetime
    # `kind` is validated against the fixed nine-kind vocabulary in the route
    # handler (not via a pydantic Literal) so that a violation can produce the
    # contract's self-explaining, per-event rejection instead of a generic
    # 422 validation error.
    kind: str
    summary: str = Field(min_length=1)
    payload: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)


class HandoffIn(BaseModel):
    """Structured handoff note: where the project stands / in flight /
    blocked / next steps, plus optional free notes.
    """

    stands: str
    in_flight: str
    blocked: str
    next_steps: str
    notes: str | None = None


class DepositRequest(BaseModel):
    deposit_id: str  # client-supplied ULID; also the idempotency key
    tool: str = Field(min_length=1)
    session: str = Field(min_length=1)
    project: str = Field(min_length=1)
    reason: Literal["session_end", "daily", "manual"]
    client_ts: datetime
    doctrine_version: str | None = None
    metrics: MetricsIn | None = None
    events: list[EventIn] = Field(default_factory=list)
    handoff: HandoffIn | None = None
    no_handoff: str | None = Field(default=None, min_length=1)
    # Library entries (contracts-v1.md §3): either a new entry
    # {title, namespace, body, tags?, project?, supersedes?} or a retire
    # action {retire: "<id>", reason: "<non-empty>"}. Kept as untyped dicts
    # (not a pydantic discriminated union) so the route handler can produce
    # the contract's self-explaining, per-item rejection instead of a
    # generic 422 validation error -- same reasoning as `EventIn.kind` above.
    knowledge: list[dict[str, Any]] = Field(default_factory=list)
    # Mirrored ADRs/docs (contracts-v1.md §5): {path, kind, title, content}.
    # Kept as untyped dicts, same reasoning as `knowledge` above.
    documents: list[dict[str, Any]] = Field(default_factory=list)
    # Optional project registry write, applied atomically with the deposit
    # (contracts-v1.md §5). Kept as an untyped dict (not a pydantic model)
    # so unknown keys / bad status can produce the contract's self-explaining
    # rejection instead of a generic 422 -- same reasoning as `knowledge`.
    project_update: dict[str, Any] | None = None


class DepositCounts(BaseModel):
    events: int
    handoff: bool
    knowledge: int
    documents: int


class DepositProjectInfo(BaseModel):
    name: str
    stub_created: bool


class KnowledgeAckItem(BaseModel):
    """Per-item knowledge[] acknowledgment detail. `id` is server-generated
    for `created` entries and echoes the target for `retired` actions.
    """

    index: int
    action: Literal["created", "retired"]
    id: str
    title: str


class DocumentAckItem(BaseModel):
    """Per-item documents[] acknowledgment detail (contracts-v1.md §5),
    consistent with the `knowledge_ack` replay pattern.
    """

    path: str
    version: int
    id: str


class DepositResponse(BaseModel):
    deposit_id: str
    received_at: datetime
    replayed: bool = False
    counts: DepositCounts
    project: DepositProjectInfo
    knowledge: list[KnowledgeAckItem] = Field(default_factory=list)
    documents: list[DocumentAckItem] = Field(default_factory=list)


# --- Library (contracts-v1.md §3, §7) ---


class LibraryEntryRef(BaseModel):
    """A minimal reference to another library entry, used in the
    supersession chain (parents/children).
    """

    id: str
    title: str
    status: str


class LibraryDuplicateHint(BaseModel):
    entry_id: str
    title: str
    rank: float


class LibrarySource(BaseModel):
    machine_id: str
    tool: str
    session: str


class LibraryEntryResponse(BaseModel):
    id: str
    title: str
    namespace: str
    project: str | None
    tags: list[str]
    status: str
    retire_reason: str | None
    supersedes: list[str]
    body: str
    source: LibrarySource
    created_at: datetime
    deposit_id: str
    parents: list[LibraryEntryRef]
    children: list[LibraryEntryRef]
    duplicate_hints: list[LibraryDuplicateHint]


# --- Search (contracts-v1.md §6 note, §7) ---


class SearchResultItem(BaseModel):
    type: Literal["library", "handoff", "event", "decision", "document"]
    id: str
    snippet: str
    project: str | None
    rank: float
    # Set only for type 'decision'/'document' (mirrored ADR/doc results).
    path: str | None = None
    version: int | None = None


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    next_cursor: str | None


# --- Doctrine (contracts-v1.md §4, §7) ---


class DoctrineRuleIn(BaseModel):
    """One global rule. `tier` is kept as a plain str (not a pydantic
    Literal) so an invalid tier can produce the contract's self-explaining,
    named rejection in the route handler instead of a generic 422 -- same
    reasoning as `EventIn.kind` in the deposits schemas above.
    """

    id: str = Field(min_length=1)
    tier: str
    text: str = Field(min_length=1)


class DoctrineGlobalRequest(BaseModel):
    content: str = Field(min_length=1)
    rules: list[DoctrineRuleIn] = Field(default_factory=list)


class DoctrineOverrideIn(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class DoctrineAdditionIn(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class DoctrineOverlayRequest(BaseModel):
    content: str = Field(min_length=1)
    overrides: list[DoctrineOverrideIn] = Field(default_factory=list)
    additions: list[DoctrineAdditionIn] = Field(default_factory=list)


class DoctrineRuleOut(BaseModel):
    id: str
    tier: str
    text: str


class DoctrineGlobalResponse(BaseModel):
    version: int
    content: str
    rules: list[DoctrineRuleOut]
    created_at: datetime


class DoctrineOverlayRuleRefOut(BaseModel):
    id: str
    text: str


class DoctrineOverlayResponse(BaseModel):
    project: str
    version: int
    content: str
    overrides: list[DoctrineOverlayRuleRefOut]
    additions: list[DoctrineOverlayRuleRefOut]
    created_at: datetime


class DoctrineGetResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Aliased to "global" in the wire format -- `global` is a Python keyword,
    # so the attribute is named `doctrine_global` and serialized by alias
    # (FastAPI's default: response_model_by_alias=True).
    doctrine_global: DoctrineGlobalResponse | None = Field(default=None, alias="global")
    overlays: list[DoctrineOverlayResponse] = Field(default_factory=list)


# --- Doctrine proposals (contracts-v1.md §4) ---


class ProposalListItem(BaseModel):
    id: str
    title: str
    namespace: str
    project: str | None
    tags: list[str]
    body: str
    status: str
    proposal_decision: str | None
    proposal_decided_at: datetime | None
    created_at: datetime
    source: LibrarySource


class ProposalDecisionResponse(BaseModel):
    id: str
    decision: str
    decided_at: datetime


# --- Projects (contracts-v1.md §5, §7) ---


class ProjectMachineInfo(BaseModel):
    """A machine that has deposited on a project -- server-derived, never
    written directly (contracts-v1.md §5: "`machines` (server-derived from
    deposits)").
    """

    id: str
    name: str
    last_deposit_at: datetime


class ProjectHandoffOut(BaseModel):
    id: str
    stands: str
    in_flight: str
    blocked: str
    next_steps: str
    notes: str | None
    received_at: datetime
    deposit_id: str


class ProjectDocumentCounts(BaseModel):
    adr: int
    doc: int


class ProjectCounts(BaseModel):
    active_library_entries: int
    mirrored_documents: ProjectDocumentCounts
    total_deposits: int


class ProjectDetailResponse(BaseModel):
    name: str
    description: str | None
    status: str
    created_at: datetime
    machines: list[ProjectMachineInfo]
    overlay_version: int | None
    latest_handoff: ProjectHandoffOut | None
    counts: ProjectCounts


class ProjectListItem(BaseModel):
    name: str
    status: str
    description: str | None
    created_at: datetime
    # Server-derived "activity" signal used to order the list (contracts-v1.md
    # §7 lists GET /v1/projects/{name} and .../handoffs; this list endpoint
    # itself is a surface addition beyond the literal spec -- see
    # app/routers/projects.py module docstring).
    latest_deposit_at: datetime | None


class ProjectListResponse(BaseModel):
    results: list[ProjectListItem]
    next_cursor: str | None


class ProjectPatchResponse(BaseModel):
    name: str
    description: str | None
    status: str


class HandoffListResponse(BaseModel):
    results: list[ProjectHandoffOut]
    next_cursor: str | None


# --- Notification channel config (owner-managed ntfy channel) ---


class NotificationConfigCreateRequest(BaseModel):
    ntfy_url: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    note: str | None = None


class NotificationConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    ntfy_url: str
    topic: str
    note: str | None
    created_at: datetime


class NotificationConfigGetResponse(BaseModel):
    current: NotificationConfigResponse | None
    history: list[NotificationConfigResponse] = Field(default_factory=list)


# --- Flags (contracts-v1.md §3, ADR-0004 librarian inbox) ---


class FlagListItem(BaseModel):
    id: str
    type: str
    entry_id: str
    related_entry_id: str | None
    detail: dict[str, Any] | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


class FlagListResponse(BaseModel):
    results: list[FlagListItem]
    next_cursor: str | None


class FlagResolveResponse(BaseModel):
    id: str
    resolved_at: datetime
    resolved_by: str
    already_resolved: bool = False


# --- Events (contracts-v1.md §2, §7; phase 8 librarian support) ---


class EventListItem(BaseModel):
    id: str
    deposit_id: str
    project: str
    seq: int
    ts: datetime
    kind: str
    summary: str
    tags: list[str]
    # Omitted (null) unless `include_payload=true` was requested.
    payload: dict[str, Any] | None = None


class EventListResponse(BaseModel):
    results: list[EventListItem]
    next_cursor: str | None


# --- Agent chat rooms (ADR-0006, phase A) ---


class RoomCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    # Exactly 2 distinct non-empty agent-name strings -- validated in
    # app/rooms.py (self-explaining ApiError, not a bare pydantic 422), same
    # reasoning as EventIn.kind above.
    members: list[str] = Field(default_factory=list)
    max_messages: int | None = None
    # --- ADR-0007: room modes and time limits ---
    # Plain str (not a pydantic Literal) validated in app/rooms.py via
    # app/room_modes.py's validate_mode -- same self-explaining-ApiError
    # reasoning as `kind` above.
    mode: str = "freeform"
    # Required (non-empty) whenever mode != 'freeform'; ignored for
    # freeform -- enforced in app/rooms.py, not here.
    topic: str | None = None
    # Required for asymmetric modes (debate, critique): {agent_name: side},
    # covering both `members` with both of the mode's distinct side values.
    # Ignored for symmetric modes (freeform, collaborate, brainstorm).
    sides: dict[str, str] | None = None
    # At most one of `duration_seconds`/`expires_at` -- app/rooms.py
    # computes the room's `expires_at` from whichever is given (or leaves
    # it null: no deadline).
    duration_seconds: int | None = None
    expires_at: datetime | None = None
    # ADR-0008: optional free-form group label (-> Room.group_name; 'group'
    # is a SQL reserved word, hence the different storage name). Trimmed,
    # blank/empty treated as "no group", validated in app/rooms.py's
    # `_validate_group` -- same self-explaining-ApiError reasoning as `mode`
    # above, not a pydantic length constraint here.
    group: str | None = None


class RoomCreateResponse(BaseModel):
    id: str
    name: str
    status: str
    members: list[str]
    max_messages: int
    mode: str
    topic: str | None
    expires_at: datetime | None
    # {agent_name: side} -- side is null for symmetric/freeform members.
    sides: dict[str, str | None]
    group: str | None


class RoomListItem(BaseModel):
    id: str
    name: str
    status: str
    members: list[str]
    message_count: int
    max_messages: int
    created_at: datetime
    close_reason: str | None
    mode: str
    topic: str | None
    expires_at: datetime | None
    sides: dict[str, str | None]
    group: str | None


class RoomListResponse(BaseModel):
    results: list[RoomListItem]
    next_cursor: str | None


class RoomMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    seq: int
    sender: str
    text: str
    kind: str
    created_at: datetime


class RoomDetailResponse(BaseModel):
    id: str
    name: str
    status: str
    members: list[str]
    max_messages: int
    message_count: int
    notify_on_close: bool
    created_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    mode: str
    topic: str | None
    expires_at: datetime | None
    sides: dict[str, str | None]
    group: str | None
    # Most recent N messages, oldest first (chat reading order).
    messages: list[RoomMessageOut]


class RoomPostMessageRequest(BaseModel):
    sender: str = Field(min_length=1)
    text: str = Field(min_length=1)
    # Plain str (not a pydantic Literal), validated in app/rooms.py -- same
    # reasoning as EventIn.kind above: a self-explaining ApiError instead of
    # a generic 422 validation error.
    kind: str = "message"


class RoomPostMessageResponse(BaseModel):
    id: str
    seq: int
    room_status: str
    close_reason: str | None


class RoomCloseRequest(BaseModel):
    reason: str | None = None


class RoomCloseResponse(BaseModel):
    id: str
    status: str
    close_reason: str | None
    closed_at: datetime | None


class RoomMessagesPollResponse(BaseModel):
    room_status: str
    messages: list[RoomMessageOut]


# --- Room delete + free-form groups (ADR-0008) ---


class RoomDeleteResponse(BaseModel):
    id: str
    deleted: bool = True
    deleted_messages: int
    deleted_members: int


class RoomGroupAssignRequest(BaseModel):
    room_ids: list[str] = Field(default_factory=list)
    # null/blank clears the group on the selected rooms; validated in
    # app/rooms.py's `_validate_group` (same rules as RoomCreateRequest.group).
    group: str | None = None


class RoomGroupAssignResponse(BaseModel):
    updated: int
    # The group actually applied, after trim/blank-to-None -- echoes what
    # `_validate_group` resolved `group` to, not necessarily the raw input.
    group: str | None
