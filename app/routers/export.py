"""Bulk export -- GET /v1/export (contracts-v1.md §7: "GET /v1/export (bulk
NDJSON)"). Owner-token only.

Streams every table's rows -- except `owner_token` (the root credential's
hash; deliberately excluded, see EXPORT_TABLES below) -- as newline-delimited
JSON, one line per row: `{"table": "<name>", "row": {...}}`. Streamed via an
async generator and keyset-paginated batches per table, with the ORM
session's identity map expunged after each batch (`db.expunge_all()`) so it
doesn't accumulate every exported row for the lifetime of the request --
never a single `SELECT *`, and never an unbounded identity map, either --
so an export stays cheap regardless of table size.

Deliberately includes `machines.token_hash` in the `machines` rows. This is
safe to export: it is a SHA-256 digest of a 32+ byte, cryptographically
random token (app/security.py's `hash_token`/`generate_machine_token`), not
the token itself -- recovering the plaintext from the hash is infeasible
given the token's entropy, the same reasoning that lets a plain (unsalted)
SHA-256 digest serve as the lookup key in app/auth.py in the first place.
The endpoint is owner-only regardless, matching every other admin-only
surface in contracts-v1.md §7.
"""

import json
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_owner
from app.db import get_db
from app.models import (
    BootstrapFetch,
    Deposit,
    DoctrineVersion,
    Event,
    Flag,
    Handoff,
    KnowledgeEntry,
    Machine,
    MirroredDocument,
    Project,
)

router = APIRouter(prefix="/v1/export", tags=["export"])

# Every table in the schema *except* `owner_token`, in a roughly
# parent-before-child order (purely for readability of a manual scan through
# the stream -- NDJSON lines are self-contained and order carries no
# functional meaning). `owner_token` is deliberately left out: it is a
# schema-enforced singleton holding the root credential's hash, a fresh one
# is minted at first boot on any new deployment regardless (docs/ops.md
# "Fresh deploy"), and there is no legitimate reason to carry a hash of the
# *old* deployment's root credential into a migrated/restored one -- the
# export's purpose is moving the knowledge (machines, projects, journal,
# library, doctrine, mirrored docs), not root-credential material. Keep this
# in sync with app/models.py: a new table is exported by adding it here.
EXPORT_TABLES: list[tuple[str, type]] = [
    ("machines", Machine),
    ("projects", Project),
    ("deposits", Deposit),
    ("events", Event),
    ("handoffs", Handoff),
    ("knowledge_entries", KnowledgeEntry),
    ("flags", Flag),
    ("doctrine_versions", DoctrineVersion),
    ("bootstrap_fetches", BootstrapFetch),
    ("mirrored_documents", MirroredDocument),
]

BATCH_SIZE = 500


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def _row_to_dict(model: type, instance) -> dict:
    return {col.name: getattr(instance, col.name) for col in model.__table__.columns}


async def _stream_table(db: AsyncSession, table_name: str, model: type) -> AsyncIterator[bytes]:
    """Keyset-paginated batch loop over one table's primary key, so the
    whole table is never buffered in memory at once -- the actual substance
    of "streaming" beyond just wrapping a single big query in a generator.

    Each batch's rows are also expunged from the session's identity map
    (`db.expunge_all()`) once serialized: left unexpunged, `AsyncSession`
    keeps every ORM object it has ever loaded resident for the life of the
    session (that's what the identity map is for), which would silently
    defeat the whole point of batching -- memory would still grow with the
    full export size, just spread out over more, smaller queries instead of
    one big one. Safe to call here: every column value needed for this
    batch's JSON line and for computing the next page's cursor has already
    been read out into plain Python values before the expunge.
    """
    pk_cols = list(model.__table__.primary_key.columns)
    last_pk: tuple | None = None

    while True:
        stmt = select(model).order_by(*pk_cols).limit(BATCH_SIZE)
        if last_pk is not None:
            stmt = stmt.where(tuple_(*pk_cols) > tuple_(*last_pk))
        rows = (await db.scalars(stmt)).all()
        if not rows:
            break

        for row in rows:
            line = {"table": table_name, "row": _row_to_dict(model, row)}
            yield (json.dumps(line, default=_json_default, ensure_ascii=False) + "\n").encode("utf-8")

        last_pk = tuple(getattr(rows[-1], c.name) for c in pk_cols)
        has_more = len(rows) >= BATCH_SIZE
        db.expunge_all()
        if not has_more:
            break


async def _export_stream(db: AsyncSession) -> AsyncIterator[bytes]:
    for table_name, model in EXPORT_TABLES:
        async for chunk in _stream_table(db, table_name, model):
            yield chunk


@router.get("")
async def export(
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return StreamingResponse(_export_stream(db), media_type="application/x-ndjson")
