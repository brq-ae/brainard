"""SQLAlchemy ORM models.

Phase 1: owner_token, machines.
Phase 2: deposits, events, handoffs, projects (see docs/spec/contracts-v1.md §2).
"""

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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
