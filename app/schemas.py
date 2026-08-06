"""Pydantic request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MachineCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class MachineCreateResponse(BaseModel):
    id: str
    name: str
    token: str  # plaintext -- shown exactly once, never retrievable again


class MachineListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime
    last_seen: datetime | None
    status: str


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
    # Library entries -- not implemented until phase 3. Accepted here only so
    # a non-empty submission can be rejected with a self-explaining error
    # rather than silently dropped or bounced by generic schema validation.
    knowledge: list[dict[str, Any]] = Field(default_factory=list)


class DepositCounts(BaseModel):
    events: int
    handoff: bool


class DepositProjectInfo(BaseModel):
    name: str
    stub_created: bool


class DepositResponse(BaseModel):
    deposit_id: str
    received_at: datetime
    replayed: bool = False
    counts: DepositCounts
    project: DepositProjectInfo
