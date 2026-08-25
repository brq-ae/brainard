"""Room file attachments -- storage core (ADR-0012). Content-addressed blob
storage on disk (the `attachment_data` named volume, docker-compose.yml),
per-room attachment reference rows, and reference-counted deletion. No HTTP
surface lives here -- consumed by app/routers/room_attachments.py (agent +
owner v1 API) and app/routers/ui_rooms.py (owner UI), and usable standalone
by tests and by app/room_sweeper.py-style background code.

Stage 2 additions on top of the stage 1 storage core above: listing/lookup
helpers, attaching an already-saved Brain document into a room (decision 8 --
bypasses the agent-upload switch, since no new bytes are written), deleting
a single attachment reference (with eager reclaim of a now-unreferenced
blob), promoting an attachment to a Brain `documents` deposit (decision 3 --
"Save to Brain"), and `release_blobs_for_deleted_room` (the
`app.rooms.delete_room` blob-reclaim fix, ADR-0012 stage 2 item 3).

Three ADR-0012 decisions shape almost everything below:

  - Decision 2 (content-addressed storage): the blob is stored once, keyed
    by sha256 of its bytes; a room never owns a blob, only an attachment
    reference row carrying the DISPLAY filename. The storage PATH is
    derived from the validated hex hash alone (`blob_path`) -- never from
    anything caller-supplied -- so path traversal is impossible by
    construction, and identical uploads dedupe automatically.

  - Decision 4 (reference-counted lifetime) + decision 6 (limits, all
    env-configurable, all enforced under a row lock): every counter this
    module checks -- a room's attachment count against its cap, the global
    byte ceiling -- is read and updated under the identical
    `SELECT ... FOR UPDATE` + `execution_options(populate_existing=True)`
    row-lock discipline app/rooms.py established for `message_count`
    (`post_message`, app/rooms.py:487-507) and documents at length there:
    without the lock, concurrent writers can each read the same
    "current" count and both squeeze past a cap that was supposed to be
    hard. Attachment counts get the identical treatment, not a new pattern.

  - Decision 16 (content, not name, determines type): only the actual
    leading bytes are trusted (`PDF_MAGIC`). The filename and any
    client-supplied Content-Type are cosmetic -- validated bytes decide
    acceptance, sanitized text (via app/room_export.py's
    `safe_filename_component`) decides what gets displayed, and neither
    ever influences the storage path.
"""

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ulid import ULID

from app.auth import Principal
from app.config import Settings, get_settings
from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.models import (
    AttachmentBlob,
    AttachmentStorageStats,
    Machine,
    MirroredDocument,
    Room,
    RoomAttachment,
    RoomMember,
)
from app.reserved_machines import ensure_reserved_machine
from app.room_export import safe_filename_component
from app.routers.deposits import create_deposit as apply_deposit
from app.schemas import DepositRequest

# Only these leading bytes are ever accepted (ADR-0012 decision 16) --
# never the filename extension, never the client's Content-Type header.
PDF_MAGIC = b"%PDF-"

# A sha256 hex digest is always exactly 64 lowercase hex characters.
# `blob_path` refuses anything else, which is what makes "the path comes
# from the hash alone" also mean "path traversal is impossible by
# construction": nothing that isn't already a validated hash ever reaches
# `Path(...) / ...`.
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Fixed PK, same "singleton row" trick app/models.py's OwnerToken uses to
# enforce "exactly one row" at the schema level. Seeded by the 0014
# migration -- always present, never created lazily at request time (which
# would itself need its own race handling for zero benefit).
STATS_SINGLETON_ID = "singleton"

# Bounded retry for the one genuine race this module can hit: two
# concurrent uploads of *different* new content, or a concurrent sweep
# deletion, landing on the same lock window. Mirrors app/rooms.py's
# MAX_INSERT_ATTEMPTS / the deposits.py version-conflict retry -- same
# "rollback and redo the whole locked attempt" shape, not a new pattern.
MAX_ATTACHMENT_ATTEMPTS = 3

_ATTACHMENT_FILENAME_FALLBACK = "attachment.pdf"

# --- Fix 2 (independent review): the free-disk floor made atomic across
# concurrent uploads ---
#
# `shutil.disk_usage()` only ever answers "how much is free RIGHT NOW" --
# it has no idea how many other uploads are mid-stream and might still
# write more bytes before they're done. Without coordination, N concurrent
# uploads can each call it at the same instant, each see enough headroom
# for THEIR next chunk, and collectively land the real free space below
# the configured floor -- undermining even this floor's more modest,
# accurate billing (app/config.py's `attachment_free_disk_floor_bytes`
# docstring): a SECONDARY, LIVE guard against real disk pressure the
# (PRIMARY, hard, row-locked) global ceiling has no way to see.
#
# Fix: a process-wide pledge counter, guarded by this lock (single ASGI
# worker process -- same scope the in-process `AsyncSessionLocal` engine
# pool already assumes; a multi-process deployment would need a cross-
# process mechanism, out of scope here). Each upload, once its magic bytes
# are validated and it commits to actually streaming, pledges its full
# per-file cap (`attachment_max_file_bytes`) -- the worst case of how much
# MORE it might still write -- into `_reserved_bytes`. As real bytes land
# on disk (and therefore start showing up in `shutil.disk_usage()` on
# their own), the SAME amount is released from the pledge, so the pledge
# always represents "bytes not yet written that this upload might still
# write", never double-counting bytes already real. Every floor check then
# subtracts every OTHER in-flight upload's outstanding pledge, not just
# its own next chunk, from the free-space figure -- turning "enough
# headroom for my next 8KB" into "enough headroom for my next 8KB even in
# the worst case of what everyone else still intends to write".
#
# Reserve-and-release, not a global mutex around the whole transfer:
# concurrent uploads still stream in parallel, they just can no longer
# collectively lie their way past the floor.
_reservation_lock = asyncio.Lock()
_reserved_bytes = 0


async def _reserve_upload_budget(amount: int) -> None:
    """Pledges `amount` bytes of possible future writes on behalf of one
    upload. Always paired with `_release_upload_budget` for that same
    amount -- called unconditionally, from a `finally`/`except BaseException`
    block, so the pledge is released on success, on any rejection
    (size cap, disk floor, invalid type), and on cancellation (a client
    disconnect surfacing as `asyncio.CancelledError`, which subclasses
    `BaseException` and is therefore already caught by that handler) alike.
    """
    global _reserved_bytes
    async with _reservation_lock:
        _reserved_bytes += amount


async def _release_upload_budget(amount: int) -> None:
    """Releases `amount` bytes of outstanding pledge -- either incrementally
    as chunks land (they're now real, on-disk bytes that `shutil.disk_usage`
    already accounts for on its own, so continuing to also pledge them
    would only make the floor needlessly conservative) or, at the very end
    of an upload (success, rejection, or cancellation), whatever pledge is
    still outstanding. Clamped at zero: a negative reservation would
    silently under-protect every OTHER in-flight upload, which is a far
    worse failure mode than this clamp ever masking a real bug (an
    over-release here can only make the check MORE conservative, never
    less).
    """
    global _reserved_bytes
    async with _reservation_lock:
        _reserved_bytes = max(0, _reserved_bytes - amount)


def blob_path(storage_dir: str | Path, sha256_hex: str) -> Path:
    """The on-disk path for a blob -- derived from the validated hex hash
    ONLY. Never accepts, and never even looks at, a filename. Raises
    ValueError (a programming-error signal, not a user-facing rejection --
    every caller in this module only ever passes a hash it just computed or
    read back from the DB) if given anything that isn't exactly 64 lowercase
    hex characters.
    """
    if not _HEX64_RE.match(sha256_hex):
        raise ValueError(f"not a valid sha256 hex digest: {sha256_hex!r}")
    return Path(storage_dir) / f"{sha256_hex}.pdf"


async def _assert_disk_headroom(storage_dir: Path, about_to_write: int, floor_bytes: int, *, own_reserved: int) -> None:
    """ADR-0012 decision 6's free-disk floor -- a live check of the actual
    filesystem (see app/config.py's `attachment_free_disk_floor_bytes`
    docstring for how this and the global byte ceiling divide the "hard
    guarantee" vs. "live check" responsibilities). Checked immediately
    before every single write, not once up front: called once before the
    temp file is opened and again before every subsequent chunk, so a disk
    that fills up mid-stream (from this upload or anything else on the
    host) is caught the moment it would breach the floor, not after the
    fact.

    `own_reserved` is THIS upload's own currently-outstanding pledge (see
    the `_reserve_upload_budget`/`_release_upload_budget` module docstring
    above) -- subtracted back out of the global total so the check below
    sees "how much could every OTHER in-flight upload still write", not a
    number that (incorrectly) includes this call's own future writes twice.
    """
    free = shutil.disk_usage(storage_dir).free
    async with _reservation_lock:
        reserved_by_others = _reserved_bytes - own_reserved
    if free - about_to_write - reserved_by_others < floor_bytes:
        raise ApiError(
            507,
            "attachment_disk_floor_exceeded",
            f"Only {free} bytes free on the attachment storage volume (with {reserved_by_others} more bytes "
            "pledged by other in-flight uploads); writing "
            f"{about_to_write} more would leave less than the configured {floor_bytes}-byte free-disk floor. "
            "Recovery: free disk space, wait for other uploads to finish, or lower "
            "ATTACHMENT_FREE_DISK_FLOOR_BYTES if the deployment genuinely has less headroom to spare than the "
            "default assumes.",
        )


async def _write_chunk(
    f, hasher, data: bytes, total: int, *, max_bytes: int, storage_dir: Path, floor_bytes: int, reserved: int
) -> tuple[int, int]:
    """One streamed write: size-cap check, then free-disk check (accounting
    for every other in-flight upload's outstanding pledge, Fix 2), then the
    actual write -- all three run BEFORE the byte touches disk, and all
    three run per-chunk rather than once at the end, which is what makes
    the per-file size cap and the free-disk floor real *streaming*
    protections (ADR-0012's requirement) rather than after-the-fact ones: a
    2 GB upload is rejected mid-read, never buffered whole in memory or
    written whole to disk first.

    Returns `(new_total, new_reserved)` -- `new_reserved` is `reserved`
    (this upload's own outstanding pledge, passed in) minus `len(data)`:
    once these bytes are actually written, they're real and already
    reflected in `shutil.disk_usage()` on their own, so they're released
    from the pledge in the same step that writes them (never left
    double-counted, and never released a moment before they're genuinely
    on disk).
    """
    new_total = total + len(data)
    if new_total > max_bytes:
        raise ApiError(
            413,
            "attachment_too_large",
            f"Upload exceeds the {max_bytes}-byte per-file cap (ATTACHMENT_MAX_FILE_BYTES). "
            "Recovery: shrink the file, or raise the cap.",
        )
    await _assert_disk_headroom(storage_dir, len(data), floor_bytes, own_reserved=reserved)
    hasher.update(data)
    f.write(data)
    await _release_upload_budget(len(data))
    return new_total, reserved - len(data)


async def receive_pdf_upload(stream: AsyncIterable[bytes], *, settings: Settings | None = None) -> tuple[Path, str, int]:
    """Streams `stream` (an async iterable of byte chunks -- e.g. a future
    router's `request.stream()`) into a temp file inside the configured
    storage directory. Returns `(temp_path, sha256_hex, byte_size)` on
    success.

    Ordering, all per ADR-0012 decision 16 and the streaming requirement:
      1. Buffer only the first `len(PDF_MAGIC)` bytes, in memory, and check
         them against `PDF_MAGIC` BEFORE opening any file -- an upload that
         fails this check never has a single byte written to disk. The
         filename and Content-Type are never consulted; only what's
         actually asked for is what's actually checked.
      2. Once validated, open a temp file (`tempfile.mkstemp`, same
         directory as the final destination so the later rename is on the
         same filesystem and therefore atomic) and stream every chunk
         through `_write_chunk`, which enforces the size cap and the
         free-disk floor before each write.
      3. On ANY failure (invalid magic bytes, size cap, disk floor, a
         mid-stream client disconnect surfacing as an exception from
         `stream`), the temp file -- if one was even opened -- is deleted
         before the exception propagates. Nothing at a trusted hash path is
         ever created from a partial or rejected upload.

    Does not touch the database -- purely I/O, so a caller can run this
    entirely before opening any DB transaction (and therefore before
    holding any row lock).

    Fix 2 (independent review, concurrent free-disk floor): as soon as the
    magic bytes validate and this function commits to actually streaming,
    it pledges its full `max_bytes` budget via `_reserve_upload_budget` --
    the module-level docstring above `_reserve_upload_budget` explains why.
    That pledge is released incrementally as real bytes land (inside
    `_write_chunk`) and, on ANY exit -- success, rejection, or cancellation
    (`except BaseException`, matching this function's own existing
    temp-file cleanup discipline, since `asyncio.CancelledError` is a
    `BaseException`) -- whatever is still outstanding is released in the
    `finally` block below, so a pledge is never leaked regardless of how
    this function exits.
    """
    settings = settings or get_settings()
    storage_dir = Path(settings.attachment_storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.attachment_max_file_bytes
    floor_bytes = settings.attachment_free_disk_floor_bytes

    hasher = hashlib.sha256()
    total = 0
    prefix = bytearray()
    validated = False
    f = None
    tmp_path: Path | None = None
    reserved = 0  # this upload's own outstanding pledge; grows to max_bytes once validated
    try:
        async for chunk in stream:
            if not chunk:
                continue
            if not validated:
                prefix.extend(chunk)
                if len(prefix) < len(PDF_MAGIC):
                    continue
                if bytes(prefix[: len(PDF_MAGIC)]) != PDF_MAGIC:
                    raise ApiError(
                        415,
                        "attachment_invalid_type",
                        "Only PDF files are accepted. The actual leading bytes of the upload are checked "
                        "('%PDF-'), never the filename extension or the Content-Type header.",
                    )
                validated = True
                fd, tmp_name = tempfile.mkstemp(dir=storage_dir, prefix=".upload-", suffix=".part")
                tmp_path = Path(tmp_name)
                f = os.fdopen(fd, "wb")
                await _reserve_upload_budget(max_bytes)
                reserved = max_bytes
                total, reserved = await _write_chunk(
                    f,
                    hasher,
                    bytes(prefix),
                    0,
                    max_bytes=max_bytes,
                    storage_dir=storage_dir,
                    floor_bytes=floor_bytes,
                    reserved=reserved,
                )
                continue

            total, reserved = await _write_chunk(
                f,
                hasher,
                chunk,
                total,
                max_bytes=max_bytes,
                storage_dir=storage_dir,
                floor_bytes=floor_bytes,
                reserved=reserved,
            )

        if not validated:
            raise ApiError(
                415,
                "attachment_invalid_type",
                "Upload was empty or too short to contain a PDF magic-byte header.",
            )
    except BaseException:
        if f is not None:
            f.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    else:
        f.close()
        return tmp_path, hasher.hexdigest(), total
    finally:
        # Release whatever pledge is still outstanding, no matter which of
        # the two branches above ran -- success (reserved is normally 0 by
        # now, since every actually-written byte released its own share
        # already, but the total content length can be smaller than
        # max_bytes, leaving the unused remainder still pledged) or a raise
        # partway through (reserved holds everything not yet written).
        if reserved:
            await _release_upload_budget(reserved)


async def _get_or_create_blob(
    db: AsyncSession, *, sha256_hex: str, byte_size: int, tmp_path: Path, settings: Settings
) -> AttachmentBlob:
    """Dedup-or-create for one content hash, called with the caller's outer
    transaction already open (no commit here -- the caller commits once,
    after also inserting its `RoomAttachment` row, so the whole operation
    lands atomically or not at all).

    Locking (ADR-0012 decision 4/6): the existing-blob lookup takes
    `SELECT ... FOR UPDATE` on that row so a concurrent `sweep_expired_blobs`
    can't delete it out from under us between this check and the caller's
    attachment insert -- whichever transaction gets the lock first, the
    other blocks until it commits, so "blob exists and is about to gain a
    new reference" and "blob has zero references and is about to be
    deleted" can never interleave. A genuinely new hash instead locks the
    single `AttachmentStorageStats` row (the global-ceiling counter) so two
    concurrent uploads of *different* new content can't both slip past the
    ceiling by reading the same stale total.
    """
    existing = await db.scalar(
        select(AttachmentBlob)
        .where(AttachmentBlob.sha256 == sha256_hex)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        # Dedup (decision 2): identical content already stored under this
        # hash. Discard the just-streamed temp file -- or no-op via
        # missing_ok if an earlier attempt in this same retry loop already
        # consumed it (see add_room_attachment's docstring) -- and reuse
        # the existing row.
        tmp_path.unlink(missing_ok=True)
        return existing

    stats = await db.scalar(
        select(AttachmentStorageStats)
        .where(AttachmentStorageStats.id == STATS_SINGLETON_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if stats is None:
        # The real deployment path always finds this row (the 0014
        # migration seeds it at `alembic upgrade`), but a test database
        # built via `Base.metadata.create_all` (tests/conftest.py -- see
        # docs/dev.md for why that path exists alongside migrations) runs
        # no migration data seeds at all, so this module has to be able to
        # create it lazily too. Race-safe the same way the blob-hash race
        # below already is: if two concurrent transactions both find it
        # missing, one's INSERT wins and the other's flush raises
        # IntegrityError, which propagates out to add_room_attachment's
        # own outer retry loop -- same recovery as any other conflict this
        # module hits, not a new failure mode.
        stats = AttachmentStorageStats(id=STATS_SINGLETON_ID, total_bytes=0)
        db.add(stats)
        await db.flush()

    if stats.total_bytes + byte_size > settings.attachment_global_ceiling_bytes:
        tmp_path.unlink(missing_ok=True)
        raise ApiError(
            413,
            "attachment_global_ceiling_exceeded",
            f"Storing this file would bring total attachment storage to {stats.total_bytes + byte_size} bytes, "
            f"exceeding the {settings.attachment_global_ceiling_bytes}-byte global ceiling "
            "(ATTACHMENT_GLOBAL_CEILING_BYTES). Recovery: delete an existing attachment, or raise the ceiling.",
        )

    final_path = blob_path(settings.attachment_storage_dir, sha256_hex)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        # Content-addressed: a file already at this exact hash path is, by
        # definition, byte-identical to what we just streamed. Reached when
        # a previous attempt in this same retry loop already renamed the
        # temp file here (see add_room_attachment) or a fully-independent
        # concurrent writer finished first.
        tmp_path.unlink(missing_ok=True)
    else:
        if not tmp_path.exists():
            raise ApiError(
                500,
                "attachment_storage_inconsistent",
                "Upload temp file is missing and no blob exists at the target hash path; cannot store.",
            )
        # Atomic write (ADR-0012): rename, never copy-then-delete, and both
        # paths are on the same filesystem (same storage_dir) so this is a
        # single atomic filesystem operation -- a crash before this line
        # leaves only a temp file (never trusted, never referenced by any
        # DB row); a crash after leaves a complete file at the hash path.
        # There is no window where a partial file sits at a path the rest
        # of this module would ever treat as a complete blob.
        os.replace(tmp_path, final_path)

    blob = AttachmentBlob(sha256=sha256_hex, byte_size=byte_size, created_at=datetime.now(UTC))
    db.add(blob)
    stats.total_bytes += byte_size
    await db.flush()
    return blob


def _clean_sender(sender: str, principal: Principal) -> str:
    """Format validation for `sender` PLUS the identity-binding check the
    independent review flagged (fix for the `sender=owner` bypass): `sender`
    is a client-supplied string with no identity of its own -- a bearer
    machine token is a global credential, not scoped to any one room or
    agent name (app/models.py's `Machine` has no per-room binding at all),
    so nothing about the token proves who is actually asking. Without this
    check, ANY valid machine token could pass `sender=owner` and inherit
    everything the literal 'owner' sender is trusted with elsewhere in this
    module (the agent-upload-switch bypass at decision 7/9, and an
    `uploaded_by: "owner"` record that misattributes the upload) -- exactly
    the impersonation app.rooms.post_message had the identical bug for
    (fixed alongside this one, same rule, same reasoning).

    `principal` is the AUTHENTICATED identity FastAPI's `Depends` resolved
    from the request's bearer token (see app/auth.py) -- never anything the
    request body/query string can influence. `sender == "owner"` is only
    ever accepted when `principal.kind == "owner"`, i.e. the token hashed to
    a real `OwnerToken` row. A machine principal claiming any OTHER sender
    string (a room member's `agent_name`) is unaffected by this check --
    machine tokens are deliberately not bound to a single agent identity in
    this system (any valid machine token may act as any of a room's
    members, by design: there is no per-agent credential to check against),
    so `_sender_is_member` below remains the only -- and sufficient --
    guard for that case.

    Raises ApiError with no side effects (callers that hold a temp file to
    clean up on rejection do that themselves around this call).
    """
    if not isinstance(sender, str) or not sender.strip():
        raise ApiError(422, "invalid_sender", "`sender` must be a non-empty string.")
    cleaned = sender.strip()
    if cleaned == "owner" and principal.kind != "owner":
        raise ApiError(
            403,
            "owner_sender_requires_owner_token",
            "`sender=owner` can only be claimed by a request authenticated with the owner token. Recovery: "
            "authenticate as the owner, or pass this room's own member `agent_name` as `sender` instead.",
        )
    return cleaned


async def _sender_is_member(db: AsyncSession, room_id: str, sender: str) -> bool:
    """True if `sender` (already format-cleaned, never 'owner' -- callers
    only call this for the non-owner branch) is one of `room_id`'s members.
    A plain, direct `room_members` query rather than importing
    app.rooms.get_members: app.rooms imports THIS module (for the
    `delete_room` blob-reclaim fix), so the reverse import would be
    circular -- this module talks to `RoomMember` directly instead, same
    table, no behavior difference.
    """
    members = (await db.scalars(select(RoomMember.agent_name).where(RoomMember.room_id == room_id))).all()
    return sender in members


async def add_room_attachment(
    db: AsyncSession,
    *,
    room_id: str,
    sender: str,
    principal: Principal,
    display_filename: str,
    tmp_path: Path,
    sha256_hex: str,
    byte_size: int,
    settings: Settings | None = None,
) -> RoomAttachment:
    """The DB-side half of adding an attachment: locks the room, enforces
    the per-room file-count cap, resolves (dedups or creates) the blob, and
    inserts the attachment reference row -- one transaction, one commit,
    same shape as app/rooms.py's `post_message`.

    `tmp_path` must already hold the fully-streamed, hash-validated
    content (see `receive_pdf_upload`) -- this function owns it from here:
    every exit path either consumes it (renamed into place) or deletes it,
    so a caller never needs its own cleanup.

    Retries the WHOLE locked attempt (room lock, count check, blob
    resolution, insert) up to `MAX_ATTACHMENT_ATTEMPTS` times on
    IntegrityError, mirroring `post_message`'s own retry loop exactly: an
    IntegrityError here means a concurrent transaction won a race on the
    blob's sha256 primary key (two uploads of the same brand-new content
    landing in the same lock window), and rolling back releases every lock
    this transaction held, including the room's -- so the only correct
    recovery is to redo the entire attempt from a fresh lock acquisition,
    not to patch up part of it. `_get_or_create_blob`'s exists-checks make
    this idempotent: a retry after a partial rename finds the file already
    in place and simply reuses it.
    """
    settings = settings or get_settings()
    filename = safe_filename_component(display_filename, fallback=_ATTACHMENT_FILENAME_FALLBACK)

    # Unlocked pre-check: existence/open-status never need the row lock to
    # be correct here either (mirrors post_message's own reasoning) -- it
    # just avoids paying for a lock acquisition on an obviously-wrong id.
    room = await db.get(Room, room_id)
    if room is None:
        tmp_path.unlink(missing_ok=True)
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")

    for attempt in range(1, MAX_ATTACHMENT_ATTEMPTS + 1):
        room = await db.scalar(
            select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
        )
        if room is None:
            tmp_path.unlink(missing_ok=True)
            raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
        if room.status != "open":
            tmp_path.unlink(missing_ok=True)
            raise ApiError(
                409,
                "room_closed",
                f"Room '{room.name}' is closed; attachments can only be added to an open room.",
            )

        # ADR-0012 decisions 7/9 (the agent-upload switch) + the same
        # sender-identity rule app.rooms.post_message enforces: `sender`
        # must be the literal 'owner' or one of this room's two members.
        # Re-checked every attempt, under the lock, so a concurrent
        # mid-room toggle (app.rooms.set_agent_uploads_allowed) is always
        # observed fresh -- never a stale "was allowed when I started
        # streaming" read. The owner is never blocked by this switch
        # (decision 7: it disables AGENT upload only).
        try:
            sender = _clean_sender(sender, principal)
        except ApiError:
            tmp_path.unlink(missing_ok=True)
            raise
        if sender != "owner" and not await _sender_is_member(db, room_id, sender):
            tmp_path.unlink(missing_ok=True)
            raise ApiError(
                403,
                "sender_not_room_member",
                f"'{sender}' is not a member of room '{room.name}' and is not the literal 'owner'. Recovery: "
                "upload as one of the room's members, or as 'owner'.",
            )
        if sender != "owner" and not room.agent_uploads_allowed:
            tmp_path.unlink(missing_ok=True)
            raise ApiError(
                403,
                "agent_uploads_disabled",
                "Agent uploads are disabled for this room by the owner. Do not generate or upload a file -- "
                "put the content directly in a room message instead, or ask the owner to enable uploads. You "
                "may still attach a document already saved in the Brain (Attach from Brain) even while this "
                "is off.",
            )

        existing_count = await db.scalar(
            select(func.count()).select_from(RoomAttachment).where(RoomAttachment.room_id == room_id)
        )
        if existing_count >= settings.attachment_max_files_per_room:
            tmp_path.unlink(missing_ok=True)
            raise ApiError(
                409,
                "room_attachment_cap_exceeded",
                f"Room '{room.name}' already has {existing_count} attachment(s), the max of "
                f"{settings.attachment_max_files_per_room} (ATTACHMENT_MAX_FILES_PER_ROOM). "
                "Recovery: remove an existing attachment, or attach to a different room.",
            )

        try:
            blob = await _get_or_create_blob(
                db, sha256_hex=sha256_hex, byte_size=byte_size, tmp_path=tmp_path, settings=settings
            )
            attachment = RoomAttachment(
                id=str(ULID()),
                room_id=room_id,
                blob_sha256=blob.sha256,
                filename=filename,
                uploaded_by=sender,
                created_at=datetime.now(UTC),
            )
            db.add(attachment)
            await db.commit()
        except IntegrityError:
            # Rollback releases every lock this transaction held, including
            # the room's -- the next loop iteration re-fetches `room` fresh
            # (with a fresh FOR UPDATE) rather than trying to salvage
            # anything from this attempt, same as post_message's own retry.
            await db.rollback()
            if attempt < MAX_ATTACHMENT_ATTEMPTS:
                continue
            raise ApiError(
                503,
                "attachment_conflict_retry",
                "A concurrent upload of the same new content collided with this one repeatedly and "
                "in-server retries did not resolve it. Recovery: resend the upload.",
            ) from None
        else:
            return attachment

    raise AssertionError("unreachable: loop above always returns or raises")


async def upload_pdf_attachment(
    db: AsyncSession,
    stream: AsyncIterable[bytes],
    *,
    room_id: str,
    sender: str,
    principal: Principal,
    display_filename: str,
    settings: Settings | None = None,
) -> RoomAttachment:
    """Convenience wrapper combining `receive_pdf_upload` (streaming
    validation, no DB) and `add_room_attachment` (locked DB bookkeeping) --
    the full upload path a future router would call. Kept separate above
    so tests (and any caller with its own reasons to control the two
    phases, e.g. wanting to open the DB transaction only after I/O is done)
    can drive them independently.

    `principal` is required and threaded straight through to
    `add_room_attachment` -- see `_clean_sender` for why: it's the only
    thing that makes the `sender=owner` claim trustworthy.
    """
    settings = settings or get_settings()
    tmp_path, sha256_hex, byte_size = await receive_pdf_upload(stream, settings=settings)
    return await add_room_attachment(
        db,
        room_id=room_id,
        sender=sender,
        principal=principal,
        display_filename=display_filename,
        tmp_path=tmp_path,
        sha256_hex=sha256_hex,
        byte_size=byte_size,
        settings=settings,
    )


async def _blob_is_referenced(db: AsyncSession, sha256_hex: str, *, protected_cutoff: datetime) -> bool:
    """True if `sha256_hex` must NOT be deleted (ADR-0012 decision 4): a
    Brain document references it (permanent -- MirroredDocument rows are
    never deleted, supersede-never-erase), OR at least one attachment
    reference belongs to a room that is still open, or closed but not yet
    past its grace period (`protected_cutoff` is `now - grace_period`, so
    "closed_at > protected_cutoff" means "closed more recently than the
    grace period allows" -- still protected).
    """
    doc_ref = await db.scalar(
        select(func.count()).select_from(MirroredDocument).where(MirroredDocument.blob_sha256 == sha256_hex)
    )
    if doc_ref:
        return True

    room_ref = await db.scalar(
        select(func.count())
        .select_from(RoomAttachment)
        .join(Room, RoomAttachment.room_id == Room.id)
        .where(RoomAttachment.blob_sha256 == sha256_hex)
        .where(or_(Room.status == "open", Room.closed_at.is_(None), Room.closed_at > protected_cutoff))
    )
    return bool(room_ref)


async def _reclaim_blob_if_unreferenced(
    db: AsyncSession, sha256_hex: str, *, protected_cutoff: datetime, settings: Settings
) -> bool:
    """One candidate blob's lock-check-delete, factored out of
    `sweep_expired_blobs` so `release_blobs_for_deleted_room` (ADR-0012
    stage 2, `app.rooms.delete_room`'s blob-reclaim fix) can reuse the
    IDENTICAL reference-counted check and deletion mechanics on a narrower
    candidate list, rather than a second, drifted copy. Re-checks under its
    own `SELECT ... FOR UPDATE` lock immediately before deletion -- this is
    what actually prevents the race against a concurrent `add_room_attachment`
    dedup-referencing this exact blob: one of the two transactions blocks
    until the other commits, so a blob can never be deleted in the same
    instant it gains a new reference. Returns True iff this call actually
    deleted the blob (row, stats, and file).
    """
    blob = await db.scalar(
        select(AttachmentBlob)
        .where(AttachmentBlob.sha256 == sha256_hex)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if blob is None:
        return False  # a concurrent sweep/reclaim already removed it

    if await _blob_is_referenced(db, sha256_hex, protected_cutoff=protected_cutoff):
        return False

    # Every remaining room_attachments row for this hash (if any) is, by the
    # check just above, guaranteed to belong to a room that is closed and
    # past its grace period -- i.e. exactly the rows the ADR says are no
    # longer entitled to the file. Clear them out before the blob itself,
    # both because `AttachmentBlob.sha256` is a plain (non-cascading) FK
    # target -- deleting a still-referenced blob row would otherwise fail
    # outright -- and because a reference row pointing at a blob that no
    # longer exists on disk would be a dangling record, not a useful
    # history: nothing in this ADR asks for "the file used to exist" to
    # survive the file itself.
    await db.execute(delete(RoomAttachment).where(RoomAttachment.blob_sha256 == sha256_hex))

    stats = await db.scalar(
        select(AttachmentStorageStats)
        .where(AttachmentStorageStats.id == STATS_SINGLETON_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if stats is not None:
        stats.total_bytes -= blob.byte_size
    await db.delete(blob)
    await db.commit()

    # File removal happens AFTER the DB commit, deliberately: if the
    # process crashed between them, the worst outcome is an orphaned file
    # nothing points at (wasted disk, cleaned up by re-running the sweep's
    # own dangling-file check -- none implemented yet, noted as follow-up)
    # -- never the other way around, a DB row whose file is already gone,
    # which would 404/error the next time anything tried to serve it.
    blob_path(settings.attachment_storage_dir, sha256_hex).unlink(missing_ok=True)
    return True


async def sweep_expired_blobs(db: AsyncSession, *, settings: Settings | None = None) -> list[str]:
    """Deletes every blob that is no longer referenced by anything (ADR-0012
    decision 4): erased only when no `MirroredDocument` references it AND
    every room that ever attached it is closed and past the configured
    grace period. Two rooms sharing a deduped blob can never delete each
    other's file -- `_blob_is_referenced` checks ALL referencing rooms, not
    just one, before a blob is considered deletable.

    Delegates the actual lock-check-delete of each candidate to
    `_reclaim_blob_if_unreferenced` (one commit per blob, never batching the
    whole sweep into one giant transaction, so a failure partway through
    loses at most the one blob being processed, and each blob's lock is
    held for the shortest time possible).

    Returns the list of deleted sha256 hashes (empty if nothing was
    eligible). Intended to be called periodically by a background task
    (not wired up by this ADR-0012 storage-core task -- that's
    app/room_sweeper.py-shaped follow-up work) or directly by tests/ops
    tooling.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    protected_cutoff = now - timedelta(days=settings.attachment_grace_period_days)

    candidate_hashes = (await db.scalars(select(AttachmentBlob.sha256))).all()
    deleted: list[str] = []
    for sha256_hex in candidate_hashes:
        if await _reclaim_blob_if_unreferenced(db, sha256_hex, protected_cutoff=protected_cutoff, settings=settings):
            deleted.append(sha256_hex)

    return deleted


async def release_blobs_for_deleted_room(
    db: AsyncSession, candidate_hashes: list[str], *, settings: Settings | None = None
) -> list[str]:
    """ADR-0012 stage 2: closes the gap `RoomAttachment`'s docstring flags --
    `app.rooms.delete_room` hard-deletes a room's `room_attachments` rows
    (explicitly now, with `ondelete="CASCADE"` as a backstop either way) but
    on its own never ran the reference-counted blob sweep, orphaning any
    blob only that room referenced.

    Called by `delete_room` AFTER it has committed the room's hard delete,
    with the sha256 hashes that room's (now-gone) `room_attachments` rows
    used to point at. Reuses the EXACT same reference-counted check + delete
    mechanics `sweep_expired_blobs` runs (`_reclaim_blob_if_unreferenced`)
    on just this narrower candidate list, called eagerly instead of waiting
    for the next scheduled sweep pass -- correct because a hard delete
    (unlike a room merely closing) is a deliberate, stronger owner action
    that forfeits the grace-period cushion on ITS OWN reference (see
    `RoomAttachment`'s docstring), while a blob still referenced by ANOTHER
    room or a Brain `MirroredDocument` remains exactly as protected as
    `sweep_expired_blobs` would protect it -- that other reference's own
    open/grace-period/permanent status is unaffected by this room's delete.

    Returns the list of sha256 hashes actually reclaimed (a subset of
    `candidate_hashes`, possibly empty).
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    protected_cutoff = now - timedelta(days=settings.attachment_grace_period_days)

    reclaimed: list[str] = []
    for sha256_hex in candidate_hashes:
        if await _reclaim_blob_if_unreferenced(db, sha256_hex, protected_cutoff=protected_cutoff, settings=settings):
            reclaimed.append(sha256_hex)
    return reclaimed


# --- Listing + lookup (stage 2: backs the list/download/delete/save HTTP
# endpoints) ---


@dataclass(frozen=True)
class RoomAttachmentView:
    """A `RoomAttachment` paired with its blob's `byte_size` -- the size
    lives on `AttachmentBlob`, not the reference row (decision 2: the same
    blob can be referenced by many rows), so listing needs the join every
    time. Kept as a thin pairing rather than denormalizing size onto
    `RoomAttachment` -- one fewer place for a byte count to go stale.
    """

    attachment: RoomAttachment
    byte_size: int


async def list_room_attachments(db: AsyncSession, room_id: str) -> list[RoomAttachmentView]:
    """A room's current attachments, oldest-first (same reading order
    app.rooms.get_all_messages uses for a room's transcript) -- backs the
    List endpoint and the files panel.
    """
    rows = (
        await db.execute(
            select(RoomAttachment, AttachmentBlob.byte_size)
            .join(AttachmentBlob, RoomAttachment.blob_sha256 == AttachmentBlob.sha256)
            .where(RoomAttachment.room_id == room_id)
            .order_by(RoomAttachment.created_at)
        )
    ).all()
    return [RoomAttachmentView(attachment=a, byte_size=b) for a, b in rows]


async def _get_attachment_or_404(db: AsyncSession, room_id: str, attachment_id: str) -> RoomAttachment:
    attachment = await db.get(RoomAttachment, attachment_id)
    if attachment is None or attachment.room_id != room_id:
        raise ApiError(404, "attachment_not_found", f"No attachment with id '{attachment_id}' in room '{room_id}'.")
    return attachment


async def get_room_attachment_for_download(
    db: AsyncSession, *, room_id: str, attachment_id: str
) -> tuple[RoomAttachment, AttachmentBlob]:
    """Looks up one attachment plus its blob row, for the download endpoint
    (which additionally needs `blob.sha256` to resolve `blob_path` and
    doesn't want a second query for that).
    """
    attachment = await _get_attachment_or_404(db, room_id, attachment_id)
    blob = await db.get(AttachmentBlob, attachment.blob_sha256)
    if blob is None:
        raise ApiError(
            500, "attachment_storage_inconsistent", "The attachment's blob record is missing; cannot serve it."
        )
    return attachment, blob


# --- Attach from Brain (ADR-0012 decisions 8/10) ---


async def add_attachment_from_brain_document(
    db: AsyncSession,
    *,
    room_id: str,
    sender: str,
    principal: Principal,
    document_id: str,
    settings: Settings | None = None,
) -> RoomAttachment:
    """Attaches a document already saved in the Brain into a room, by
    reference to the SAME blob (`document.blob_sha256`) -- no new bytes are
    written, so no dedup/global-ceiling/free-disk check applies (nothing
    new is being stored). Per decision 8, this deliberately does NOT check
    `Room.agent_uploads_allowed`: the switch blocks CREATING files, not
    linking a document the owner already approved into the Brain -- so this
    is allowed for agents even while uploads are disabled.

    Still takes the room's row lock and enforces the identical per-room
    file-count cap `add_room_attachment` enforces (this room gains one more
    attachment reference either way) and the identical sender/membership
    validation -- the only two guardrails that still apply.

    Raises `document_not_found` for an unknown document id, and
    `document_not_attachable` for a real Brain document that has no
    associated blob (an ordinary mirrored ADR/doc, never created by saving
    a room attachment) -- there is nothing to attach in that case.
    """
    settings = settings or get_settings()

    room = await db.get(Room, room_id)
    if room is None:
        raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")

    document = await db.get(MirroredDocument, document_id)
    if document is None:
        raise ApiError(404, "document_not_found", f"No Brain document with id '{document_id}'.")
    if document.blob_sha256 is None:
        raise ApiError(
            422,
            "document_not_attachable",
            f"Brain document '{document.title}' (id '{document_id}') has no associated file -- it was not "
            "created by saving a room attachment, so there is nothing to attach. Recovery: pick a different "
            "document, or upload a new PDF directly to this room.",
        )

    filename = safe_filename_component(document.title, fallback=_ATTACHMENT_FILENAME_FALLBACK)
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    for attempt in range(1, MAX_ATTACHMENT_ATTEMPTS + 1):
        room = await db.scalar(
            select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True)
        )
        if room is None:
            raise ApiError(404, "room_not_found", f"No room with id '{room_id}'.")
        if room.status != "open":
            raise ApiError(
                409,
                "room_closed",
                f"Room '{room.name}' is closed; attachments can only be added to an open room.",
            )

        cleaned_sender = _clean_sender(sender, principal)
        if cleaned_sender != "owner" and not await _sender_is_member(db, room_id, cleaned_sender):
            raise ApiError(
                403,
                "sender_not_room_member",
                f"'{cleaned_sender}' is not a member of room '{room.name}' and is not the literal 'owner'. "
                "Recovery: attach as one of the room's members, or as 'owner'.",
            )

        existing_count = await db.scalar(
            select(func.count()).select_from(RoomAttachment).where(RoomAttachment.room_id == room_id)
        )
        if existing_count >= settings.attachment_max_files_per_room:
            raise ApiError(
                409,
                "room_attachment_cap_exceeded",
                f"Room '{room.name}' already has {existing_count} attachment(s), the max of "
                f"{settings.attachment_max_files_per_room} (ATTACHMENT_MAX_FILES_PER_ROOM). "
                "Recovery: remove an existing attachment, or attach to a different room.",
            )

        attachment = RoomAttachment(
            id=str(ULID()),
            room_id=room_id,
            blob_sha256=document.blob_sha256,
            filename=filename,
            uploaded_by=cleaned_sender,
            created_at=datetime.now(UTC),
        )
        db.add(attachment)
        try:
            await db.commit()
        except IntegrityError:
            # Same "redo the whole locked attempt" recovery add_room_attachment
            # uses -- rollback releases every lock this transaction held.
            await db.rollback()
            if attempt < MAX_ATTACHMENT_ATTEMPTS:
                continue
            raise ApiError(
                503,
                "attachment_conflict_retry",
                "A concurrent write collided with this one repeatedly and in-server retries did not resolve "
                "it. Recovery: retry the attach.",
            ) from None
        else:
            return attachment

    raise AssertionError("unreachable: loop above always returns or raises")


# --- Delete a single attachment reference (owner-only; eager reclaim) ---


async def delete_room_attachment(
    db: AsyncSession, *, room_id: str, attachment_id: str, settings: Settings | None = None
) -> str:
    """Owner-only delete of one attachment reference. Deletes the
    `RoomAttachment` row, commits, then eagerly attempts to reclaim the
    blob it referenced (`_reclaim_blob_if_unreferenced`) -- the blob
    survives if anything else (another room's reference, open or within its
    own grace period, or a `MirroredDocument`) still references it,
    identical to `sweep_expired_blobs`'s own check. No grace period applies
    to THIS reference: an explicit owner delete is immediate, same posture
    `app.rooms.delete_room`'s own blob-reclaim fix takes for a whole-room
    hard delete.

    Returns the deleted attachment's blob sha256 (its removal from disk is
    not guaranteed -- only that this reference is gone and reclaim was
    attempted).
    """
    settings = settings or get_settings()
    attachment = await _get_attachment_or_404(db, room_id, attachment_id)
    sha256_hex = attachment.blob_sha256

    await db.delete(attachment)
    await db.commit()

    now = datetime.now(UTC)
    protected_cutoff = now - timedelta(days=settings.attachment_grace_period_days)
    await _reclaim_blob_if_unreferenced(db, sha256_hex, protected_cutoff=protected_cutoff, settings=settings)
    return sha256_hex


# --- Save to Brain (ADR-0012 decision 3) ---
#
# Reuses the existing documents[] deposit path (`app.routers.deposits
# .create_deposit`) directly -- not by hand-writing a `MirroredDocument` row
# -- so every guardrail that path already enforces (shape validation, size
# cap, project auto-stub) holds exactly as it would for a real machine
# deposit. Same "own dedicated reserved machine identity" pattern
# app/room_ai.py's `deposit_result` uses, for the identical two reasons that
# module documents: (1) `MirroredDocument.machine_id` would otherwise show
# an unrelated identity as the source of a document whose `tool` says
# something else, and (2) a dedicated identity gives this feature its own
# independent kill switch (Admin -> Machines revoke), not entangled with any
# other feature's.

ROOM_ATTACHMENTS_MACHINE_ID = "brainard-room-attachments"
ROOM_ATTACHMENTS_MACHINE_NAME = "brainard-room-attachments"
ROOM_ATTACHMENTS_TOOL = "brainard-room-attachments"


async def _ensure_room_attachments_machine(session_factory: async_sessionmaker[AsyncSession]) -> Machine:
    return await ensure_reserved_machine(session_factory, ROOM_ATTACHMENTS_MACHINE_ID, ROOM_ATTACHMENTS_MACHINE_NAME)


def _attachments_principal(machine_id: str) -> Principal:
    # A transient, never-persisted Machine instance -- only `.id` is ever
    # read by the deposit domain path (app/routers/deposits.py only touches
    # `principal.machine.id`), same technique app/room_ai.py's own
    # `_principal_for` uses.
    return Principal(kind="machine", machine=Machine(id=machine_id))


@dataclass(frozen=True)
class SavedAttachment:
    document_id: str
    path: str
    version: int
    project: str


async def save_attachment_to_brain(
    db: AsyncSession,
    *,
    room_id: str,
    attachment_id: str,
    project: str,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> SavedAttachment:
    """Promotes a room attachment to a Brain `documents` deposit (decision
    3), linked back to the SAME blob via `MirroredDocument.blob_sha256` --
    the reference-counted sweep (`_blob_is_referenced`) treats that link as
    permanent (a `MirroredDocument` row is never deleted, supersede-never-
    erase), so the blob now survives forever regardless of what happens to
    the room(s) that originally held the file.

    The room copy is UNTOUCHED: nothing moves, nothing is deleted -- only a
    new `MirroredDocument` row is created. `RoomAttachment`/download from
    this room keep working exactly as before (decision 3: "a saved file
    remains readable in its original room; nothing moves, only the
    guarantee of its lifetime changes").

    No PDF content is extracted (decision 1, v1 scope) -- the deposit's
    `content` field is a fixed, honest placeholder describing the file,
    never fabricated PDF text.

    `blob_sha256` isn't part of the shared documents[] deposit shape (only
    path/kind/title/content are -- app/routers/deposits.py's
    `_validate_documents_shape` rejects unknown keys), so it's set with a
    follow-up UPDATE, on the SAME already-created row, right after the
    shared deposit path returns -- still one logical "create this saved
    document" step, just two statements because the shared path can't be
    handed a field it doesn't know about.
    """
    attachment = await _get_attachment_or_404(db, room_id, attachment_id)

    if not isinstance(project, str) or not project.strip():
        raise ApiError(422, "invalid_project", "`project` must be a non-empty string.")
    cleaned_project = project.strip()

    blob = await db.get(AttachmentBlob, attachment.blob_sha256)
    if blob is None:
        raise ApiError(
            500, "attachment_storage_inconsistent", "The attachment's blob record is missing; cannot save."
        )

    machine = await _ensure_room_attachments_machine(session_factory)
    if machine.status == "revoked":
        raise ApiError(
            503,
            "room_attachments_identity_revoked",
            "The reserved 'brainard-room-attachments' machine identity is revoked -- Save to Brain is disabled "
            "until it's reactivated in Admin -> Machines.",
        )

    content = (
        f"(PDF attachment saved from a room -- ADR-0012 v1 serves bytes only, no text is extracted. Original "
        f"filename: {attachment.filename}; {blob.byte_size} bytes; sha256 {blob.sha256}. Download the "
        "original bytes via the room or this Brain document.)"
    )
    deposit_body = DepositRequest(
        deposit_id=str(ULID()),
        tool=ROOM_ATTACHMENTS_TOOL,
        session=room_id,
        project=cleaned_project,
        reason="manual",
        client_ts=datetime.now(UTC),
        documents=[
            {
                "path": f"rooms/{room_id}/{attachment.filename}",
                "kind": "doc",
                "title": attachment.filename,
                "content": content,
            }
        ],
    )
    response = await apply_deposit(body=deposit_body, principal=_attachments_principal(machine.id), db=db)
    doc_ack = response.documents[0]

    doc = await db.get(MirroredDocument, doc_ack.id)
    assert doc is not None  # just created by apply_deposit, on this same session
    doc.blob_sha256 = blob.sha256
    await db.commit()

    return SavedAttachment(document_id=doc_ack.id, path=doc_ack.path, version=doc_ack.version, project=cleaned_project)
