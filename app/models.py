"""SQLAlchemy ORM models for Phase 1: owner_token, machines."""

from datetime import datetime

from sqlalchemy import DateTime, String, func
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
