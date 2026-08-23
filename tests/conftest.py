"""Shared test fixtures. Tests run against a real Postgres database -- the
DATABASE_URL environment variable must already point at it (and at a
dedicated test database; see docs/dev.md).
"""

import os
from urllib.parse import SplitResult, urlsplit, urlunsplit

import asyncpg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import AsyncSessionLocal, engine
from app.main import app
from app.models import (
    Base,
    BootstrapFetch,
    Deposit,
    DoctrineVersion,
    Event,
    Flag,
    Handoff,
    KnowledgeEntry,
    LlmConfig,
    Machine,
    MirroredDocument,
    NotificationConfig,
    OwnerToken,
    Project,
    Room,
    RoomMember,
    RoomMessage,
)


def _asyncpg_dsn(database_url: str, dbname: str) -> str:
    """Convert the app's SQLAlchemy asyncpg URL into a plain asyncpg DSN
    pointed at a different database name (used to reach the maintenance db).
    """
    plain = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(plain)
    return urlunsplit(SplitResult(parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _prepare_database():
    """Create the target database if missing, then create all tables."""
    database_url = os.environ["DATABASE_URL"]
    target_db = database_url.rsplit("/", 1)[-1]

    conn = await asyncpg.connect(_asyncpg_dsn(database_url, "postgres"))
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target_db)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        await conn.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield

    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Every test starts from an empty database. Deletion order respects
    foreign keys: flags before the entries they reference, entries/mirrored
    documents/journal/handoff rows before their deposit, deposits before
    machines/projects, doctrine_versions/bootstrap_fetches before the
    machines/projects they reference.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(RoomMessage.__table__.delete())
        await session.execute(RoomMember.__table__.delete())
        await session.execute(Room.__table__.delete())
        await session.execute(Flag.__table__.delete())
        await session.execute(KnowledgeEntry.__table__.delete())
        await session.execute(MirroredDocument.__table__.delete())
        await session.execute(Event.__table__.delete())
        await session.execute(Handoff.__table__.delete())
        await session.execute(Deposit.__table__.delete())
        await session.execute(BootstrapFetch.__table__.delete())
        await session.execute(DoctrineVersion.__table__.delete())
        await session.execute(NotificationConfig.__table__.delete())
        await session.execute(LlmConfig.__table__.delete())
        await session.execute(Project.__table__.delete())
        await session.execute(Machine.__table__.delete())
        await session.execute(OwnerToken.__table__.delete())
        await session.commit()
    yield


@pytest_asyncio.fixture
async def db_session():
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
