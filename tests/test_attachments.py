"""app/attachments.py -- ADR-0012 storage core: magic-byte validation,
streaming size cap, free-disk floor, the global ceiling, per-room count cap
(including a genuine-concurrency proof), content-addressed dedup,
reference-counted deletion (incl. the two-rooms-share-one-blob case and the
Brain-document permanence case), grace period, and atomic writes.

No HTTP surface exists yet for this feature (storage-core task only) --
every test below drives app/attachments.py directly against ORM rows built
by hand, the same "domain module, no client" style tests/test_documents.py
and friends use for logic that predates their own router.
"""

import asyncio
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from ulid import ULID

import app.attachments as attachments_module
from app.attachments import (
    PDF_MAGIC,
    add_room_attachment,
    blob_path,
    receive_pdf_upload,
    sweep_expired_blobs,
    upload_pdf_attachment,
)
from app.auth import Principal
from app.db import AsyncSessionLocal
from app.errors import ApiError
from app.models import (
    AttachmentBlob,
    AttachmentStorageStats,
    Deposit,
    Machine,
    MirroredDocument,
    Project,
    Room,
    RoomAttachment,
)
from app.security import generate_machine_token, hash_token


# --- local fixtures -- these new tables aren't known to tests/conftest.py's
# own `_clean_tables` (a storage-core task boundary: conftest.py is shared,
# existing, and out of scope here), so this file cleans up after itself. ---


@pytest_asyncio.fixture(autouse=True)
async def _clean_attachment_state():
    async with AsyncSessionLocal() as session:
        await session.execute(RoomAttachment.__table__.delete())
        await session.execute(MirroredDocument.__table__.delete())
        await session.execute(AttachmentBlob.__table__.delete())
        await session.execute(AttachmentStorageStats.__table__.delete())
        await session.commit()
    yield


@dataclass
class _FakeSettings:
    attachment_storage_dir: str
    attachment_max_file_bytes: int = 10 * 1024 * 1024
    attachment_max_files_per_room: int = 10
    attachment_global_ceiling_bytes: int = 500 * 1024 * 1024
    attachment_free_disk_floor_bytes: int = 100  # low: real disk usage must never trip tests by default
    attachment_grace_period_days: int = 7


@pytest.fixture
def fake_settings(tmp_path):
    def _make(**overrides) -> _FakeSettings:
        return _FakeSettings(attachment_storage_dir=str(tmp_path), **overrides)

    return _make


_OWNER = Principal(kind="owner")


def _pdf_bytes(body: bytes = b"hello world") -> bytes:
    return b"%PDF-1.4\n" + body + b"\n%%EOF"


async def _agen(chunks: list[bytes]):
    for c in chunks:
        yield c


def _chunked(data: bytes, size: int = 8) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


async def _make_room(
    db_session, *, status: str = "open", closed_at: datetime | None = None, name: str = "room"
) -> Room:
    room = Room(
        id=str(ULID()),
        name=name,
        status=status,
        max_messages=100,
        message_count=0,
        created_at=datetime.now(UTC),
        closed_at=closed_at,
        close_reason="owner" if status == "closed" else None,
    )
    db_session.add(room)
    await db_session.commit()
    return room


async def _close_room(db_session, room: Room, *, closed_at: datetime) -> None:
    """Attachments can only be added to an OPEN room (add_room_attachment's
    own guardrail) -- so every sweep test below uploads first, then closes
    the room afterward via this helper, rather than creating an
    already-closed room and trying to upload into it.
    """
    row = await db_session.get(Room, room.id)
    row.status = "closed"
    row.closed_at = closed_at
    row.close_reason = "owner"
    await db_session.commit()


# --- magic-byte validation ---


async def test_valid_pdf_accepted(fake_settings):
    settings = fake_settings()
    tmp_path, sha256_hex, size = await receive_pdf_upload(_agen(_chunked(_pdf_bytes())), settings=settings)
    assert tmp_path.exists()
    assert size == len(_pdf_bytes())
    assert len(sha256_hex) == 64
    tmp_path.unlink()


async def test_html_renamed_to_pdf_rejected(fake_settings):
    """The classic spoof: an HTML file whose *name* ends in .pdf. This
    module never even looks at a filename to decide type -- only content
    reaches receive_pdf_upload -- so the rejection is inherent, not a name
    check that could be bypassed.
    """
    settings = fake_settings()
    html = b"<html><body><script>alert(1)</script></body></html>"

    with pytest.raises(ApiError) as exc_info:
        await receive_pdf_upload(_agen(_chunked(html)), settings=settings)

    assert exc_info.value.code == "attachment_invalid_type"
    assert list(Path(settings.attachment_storage_dir).iterdir()) == []  # nothing written to disk


async def test_lying_content_type_is_irrelevant(fake_settings):
    """A client can send `Content-Type: application/pdf` alongside HTML
    bytes -- this function has no content_type parameter at all, so there
    is nothing for a lying header to influence. Only the actual bytes
    decide (ADR-0012 decision 16).
    """
    settings = fake_settings()
    html = b"<html>not a pdf</html>"
    claimed_content_type = "application/pdf"  # never passed anywhere below

    with pytest.raises(ApiError) as exc_info:
        await receive_pdf_upload(_agen(_chunked(html)), settings=settings)

    assert exc_info.value.code == "attachment_invalid_type"
    assert claimed_content_type == "application/pdf"  # the claim existed; it just had no effect


async def test_empty_upload_rejected(fake_settings):
    settings = fake_settings()
    with pytest.raises(ApiError) as exc_info:
        await receive_pdf_upload(_agen([]), settings=settings)
    assert exc_info.value.code == "attachment_invalid_type"


async def test_too_short_to_contain_magic_bytes_rejected(fake_settings):
    settings = fake_settings()
    with pytest.raises(ApiError) as exc_info:
        await receive_pdf_upload(_agen([b"%PD"]), settings=settings)
    assert exc_info.value.code == "attachment_invalid_type"


# --- streaming size cap ---


async def test_streaming_size_cap_rejects_mid_stream_without_buffering_everything(fake_settings):
    settings = fake_settings(attachment_max_file_bytes=32)
    payload = _pdf_bytes(b"x" * 500)  # comfortably over the 32-byte cap
    chunks = _chunked(payload, size=8)
    yielded = []

    async def _tracking_gen():
        for c in chunks:
            yielded.append(c)
            yield c

    with pytest.raises(ApiError) as exc_info:
        await receive_pdf_upload(_tracking_gen(), settings=settings)

    assert exc_info.value.code == "attachment_too_large"
    # Aborted partway through -- never asked the generator for every chunk,
    # proving this is a genuine mid-stream rejection, not read-then-check.
    assert len(yielded) < len(chunks)
    assert list(Path(settings.attachment_storage_dir).iterdir()) == []  # no partial file left behind


async def test_upload_within_cap_accepted(fake_settings):
    settings = fake_settings(attachment_max_file_bytes=1024)
    payload = _pdf_bytes(b"x" * 100)
    tmp_path, _sha, size = await receive_pdf_upload(_agen(_chunked(payload)), settings=settings)
    assert size == len(payload)
    tmp_path.unlink()


# --- free-disk floor ---


async def test_free_disk_floor_refuses_upload(fake_settings, monkeypatch):
    floor_bytes = 2 * 1024 * 1024 * 1024  # 2 GiB floor
    settings = fake_settings(attachment_free_disk_floor_bytes=floor_bytes)

    class _FakeUsage:
        # Only 5 bytes of headroom above the floor -- the very first write
        # (the buffered magic-byte prefix, >= 5 bytes) already breaches it.
        free = floor_bytes + 5

    monkeypatch.setattr(attachments_module.shutil, "disk_usage", lambda _path: _FakeUsage())

    with pytest.raises(ApiError) as exc_info:
        await receive_pdf_upload(_agen(_chunked(_pdf_bytes(b"y" * 1000))), settings=settings)

    assert exc_info.value.code == "attachment_disk_floor_exceeded"
    assert list(Path(settings.attachment_storage_dir).iterdir()) == []


async def test_free_disk_floor_allows_upload_with_headroom(fake_settings, monkeypatch):
    settings = fake_settings(attachment_free_disk_floor_bytes=100)

    class _FakeUsage:
        free = 10_000_000_000  # plenty of headroom

    monkeypatch.setattr(attachments_module.shutil, "disk_usage", lambda _path: _FakeUsage())

    tmp_path, _sha, _size = await receive_pdf_upload(_agen(_chunked(_pdf_bytes())), settings=settings)
    assert tmp_path.exists()
    tmp_path.unlink()


# --- Fix 2 (independent review): the free-disk floor made atomic across
# concurrent uploads. Before this fix, each in-flight upload independently
# called shutil.disk_usage() with no in-process coordination at all, so N
# concurrent uploads could each observe "enough headroom" at the same
# instant and collectively push real free space below the floor -- the
# floor was a soft, per-upload guarantee despite app/config.py documenting
# it as the PRIMARY protection. These tests pin shutil.disk_usage() to a
# FIXED value (isolating the reservation counter as the only thing that
# can possibly cause a rejection -- real bytes landing in a tmp_path could
# never move a multi-GB free-space figure anyway) and use genuinely
# concurrent tasks (asyncio.gather, synchronized on an asyncio.Barrier so
# neither task's reservation can race ahead of the other's check being
# evaluated -- removing any dependency on incidental event-loop scheduling
# order) to prove the floor now holds where, under independent
# shutil.disk_usage() checks alone, it would not have. ---


async def test_disk_floor_reservation_blocks_when_others_pledge_exceeds_headroom(fake_settings, monkeypatch):
    """Primitive-level proof: `_assert_disk_headroom` must see every OTHER
    in-flight upload's outstanding pledge, not just its own next write.
    """
    settings = fake_settings()
    storage_dir = Path(settings.attachment_storage_dir)
    # reserved_by_others (as seen by EITHER task) is the OTHER task's own
    # pledge alone (1_500), never the combined total -- each task's own
    # pledge is accounted for separately via `about_to_write`, not via
    # `reserved_by_others`. So the floor has to sit above
    # free_bytes - about_to_write - 1_500 (== 1_490 below) for the fix to
    # visibly matter, not above free_bytes - 2 * 1_500 (a bug in an
    # earlier draft of this test, caught by actually running it).
    floor_bytes = 1_500
    free_bytes = 3_000

    class _FakeUsage:
        free = free_bytes

    monkeypatch.setattr(attachments_module.shutil, "disk_usage", lambda _path: _FakeUsage())

    # Two barriers, not one: an asyncio.Barrier only guarantees release
    # happens after every party has ARRIVED -- it does NOT guarantee they
    # resume running in lockstep afterward. In practice the last arriver
    # continues immediately (no extra scheduling round-trip) while earlier
    # arrivers are woken via the event loop's callback queue, so a single
    # barrier between "reserve" and "check" would let the last arriver
    # check-AND-release before an earlier arriver even resumes -- silently
    # collapsing back to "sequential", not "concurrent" (caught by actually
    # running an earlier draft of this test). A second barrier, between
    # "check" and "release", closes that gap: neither task's release can
    # run before BOTH checks have been evaluated.
    reserved_barrier = asyncio.Barrier(2)
    checked_barrier = asyncio.Barrier(2)

    async def _reserve_then_check(pledge: int, about_to_write: int) -> str:
        await attachments_module._reserve_upload_budget(pledge)
        await reserved_barrier.wait()  # both pledges are live before either one checks
        try:
            await attachments_module._assert_disk_headroom(storage_dir, about_to_write, floor_bytes, own_reserved=pledge)
            result = "ok"
        except ApiError as exc:
            result = exc.code
        await checked_barrier.wait()  # both checks are done before either one releases
        await attachments_module._release_upload_budget(pledge)
        return result

    # Two uploads each pledging 1_500 bytes. Under the OLD code (no
    # reservation at all) BOTH would independently see free_bytes=3_000 and
    # both would pass (3_000 - 10 >= 1_500). With the reservation counter,
    # each one's check ALSO sees the OTHER's 1_500-byte pledge:
    # 3_000 - 10 - 1_500 == 1_490 < 1_500 -- both must be refused.
    results = await asyncio.gather(_reserve_then_check(1_500, 10), _reserve_then_check(1_500, 10))
    assert results == ["attachment_disk_floor_exceeded", "attachment_disk_floor_exceeded"]

    # Both pledges were released (the `finally` above, exception-safe by
    # construction) -- the identical check now succeeds on its own,
    # proving the rejection above was genuinely about the OTHER upload's
    # outstanding pledge, not some unrelated miscalculation, and that
    # release is not leaked.
    await attachments_module._assert_disk_headroom(storage_dir, 10, floor_bytes, own_reserved=0)
    assert attachments_module._reserved_bytes == 0


async def test_disk_floor_reservation_prevents_concurrent_receive_pdf_uploads_from_both_succeeding(
    fake_settings, monkeypatch
):
    """End-to-end version of the primitive-level proof above, through the
    real public entry point: two concurrent `receive_pdf_upload` calls,
    started together (a shared asyncio.Barrier) and kept interleaved
    (`asyncio.sleep(0)` between chunks) rather than one running to
    completion before the other begins.
    """
    # Same arithmetic as the primitive-level test above: reserved_by_others
    # is the OTHER upload's pledge alone (max_bytes), not the combined
    # total, and shrinks as that other upload's own chunks land -- so the
    # margin has to clear free_bytes - <a few chunks written> - max_bytes,
    # not just free_bytes - 2 * max_bytes. Generous margin chosen (and
    # confirmed by actually running this test) rather than tuned to an
    # exact boundary, since the precise interleaving order is an
    # event-loop scheduling detail, not something this test should pin.
    floor_bytes = 2_200
    free_bytes = 3_500
    max_bytes = 1_500
    settings = fake_settings(attachment_max_file_bytes=max_bytes, attachment_free_disk_floor_bytes=floor_bytes)

    class _FakeUsage:
        free = free_bytes

    monkeypatch.setattr(attachments_module.shutil, "disk_usage", lambda _path: _FakeUsage())

    payload = _pdf_bytes(b"x" * 200)
    barrier = asyncio.Barrier(2)

    async def _gated_stream():
        first = True
        for c in _chunked(payload, size=16):
            if first:
                await barrier.wait()  # both streams start consuming together
                first = False
            else:
                await asyncio.sleep(0)  # keep yielding control between chunks
            yield c

    async def _one():
        try:
            tmp_path, _sha, _size = await receive_pdf_upload(_gated_stream(), settings=settings)
            tmp_path.unlink()
            return "ok"
        except ApiError as exc:
            return exc.code

    results = await asyncio.gather(_one(), _one())

    # Both pledge the full 1_500-byte per-file cap; free_bytes - <first
    # chunk> - max_bytes < floor_bytes for either one's view of the OTHER's
    # pledge, so at least one must be refused -- proving the floor
    # genuinely held where, under independent shutil.disk_usage() checks
    # alone (the pre-fix
    # code), both would have observed the same 3_000-byte free figure and
    # both would have been admitted.
    assert "attachment_disk_floor_exceeded" in results
    assert all(r in ("ok", "attachment_disk_floor_exceeded") for r in results)
    # No leaked pledge either way -- the process-wide counter this test
    # started with (0, per _clean_attachment_state-adjacent isolation) is
    # back to 0 once both tasks have fully exited.
    assert attachments_module._reserved_bytes == 0


# --- atomic writes: no partial file survives a mid-stream failure ---


async def test_atomic_write_leaves_no_partial_file_on_mid_stream_failure(fake_settings):
    settings = fake_settings()

    async def _exploding_gen():
        yield b"%PDF-1.4\n"
        yield b"some bytes "
        raise RuntimeError("simulated client disconnect")

    with pytest.raises(RuntimeError):
        await receive_pdf_upload(_exploding_gen(), settings=settings)

    # No temp file, and definitely nothing at any would-be final hash path.
    assert list(Path(settings.attachment_storage_dir).iterdir()) == []


async def test_atomic_write_via_add_room_attachment_leaves_no_partial_blob_on_db_failure(fake_settings, db_session):
    """Even if the DB half fails outright (bad room id here), the blob file
    is never left half-written or orphaned at a trusted hash path: the temp
    file simply gets deleted by add_room_attachment's own cleanup.
    """
    settings = fake_settings()
    tmp_path, sha256_hex, size = await receive_pdf_upload(_agen(_chunked(_pdf_bytes())), settings=settings)

    with pytest.raises(ApiError) as exc_info:
        await add_room_attachment(
            db_session,
            room_id="nonexistent-room",
            sender="owner",
            principal=_OWNER,
            display_filename="doc.pdf",
            tmp_path=tmp_path,
            sha256_hex=sha256_hex,
            byte_size=size,
            settings=settings,
        )

    assert exc_info.value.code == "room_not_found"
    assert not tmp_path.exists()
    assert not blob_path(settings.attachment_storage_dir, sha256_hex).exists()


# --- global ceiling ---


async def test_global_ceiling_refuses_new_blob_over_budget(fake_settings, db_session):
    settings = fake_settings(attachment_global_ceiling_bytes=20)
    room = await _make_room(db_session)
    payload = _pdf_bytes(b"x" * 200)  # well over the 20-byte ceiling

    with pytest.raises(ApiError) as exc_info:
        await upload_pdf_attachment(
            db_session,
            _agen(_chunked(payload)),
            room_id=room.id,
            sender="owner",
            principal=_OWNER,
            display_filename="big.pdf",
            settings=settings,
        )

    assert exc_info.value.code == "attachment_global_ceiling_exceeded"
    # The ceiling was never actually claimed by this rejected upload -- the
    # (lazily-created, since this test DB has no migration-seeded row)
    # stats singleton stays at 0, and no blob row/file exists at all.
    stats = await db_session.get(AttachmentStorageStats, attachments_module.STATS_SINGLETON_ID)
    if stats is not None:
        assert stats.total_bytes == 0
    blobs = (await db_session.execute(AttachmentBlob.__table__.select())).all()
    assert blobs == []
    assert list(Path(settings.attachment_storage_dir).glob("*.pdf")) == []


async def test_global_ceiling_allows_upload_within_budget(fake_settings, db_session):
    settings = fake_settings(attachment_global_ceiling_bytes=10_000)
    room = await _make_room(db_session)

    attachment = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(_pdf_bytes())),
        room_id=room.id,
        sender="owner",
        principal=_OWNER,
        display_filename="small.pdf",
        settings=settings,
    )

    stats = await db_session.get(AttachmentStorageStats, attachments_module.STATS_SINGLETON_ID)
    assert stats.total_bytes == len(_pdf_bytes())
    assert attachment.filename == "small.pdf"


async def test_global_ceiling_survives_concurrent_uploads_of_different_new_content(fake_settings, db_session):
    """Fix 4(a) (independent review): the genuine race this module's own
    docstrings call out (MAX_ATTACHMENT_ATTEMPTS / `_get_or_create_blob`) --
    concurrent uploads of DIFFERENT brand-new content landing in the same
    `AttachmentStorageStats` lock window. Proven the same way
    `test_per_room_cap_survives_concurrent_writers` above already proves
    the per-room cap: real, separate `AsyncSessionLocal()` sessions (real
    asyncpg I/O forces genuine interleaving, no artificial delay needed)
    fanning far past the ceiling. The ceiling must hold exactly -- no lost
    update, no silent overrun -- even though every upload here is a
    distinct hash, unlike the dedup cases elsewhere in this file.
    """
    blob_size = len(_pdf_bytes(b"x" * 50))  # every upload below has this exact size, distinct content
    ceiling_slots = 5
    settings = fake_settings(
        attachment_global_ceiling_bytes=blob_size * ceiling_slots,
        attachment_max_files_per_room=100,  # high enough that only the ceiling can ever reject here
    )
    room = await _make_room(db_session)
    room_id = room.id
    fan_out = 14

    async def _one_upload(i: int):
        payload = _pdf_bytes(f"{i:04d}".encode().ljust(50, b"x"))  # distinct content, identical size
        async with AsyncSessionLocal() as session:
            try:
                await upload_pdf_attachment(
                    session,
                    _agen(_chunked(payload)),
                    room_id=room_id,
                    sender="owner",
                    principal=_OWNER,
                    display_filename=f"ceiling-{i}.pdf",
                    settings=settings,
                )
                return "ok"
            except ApiError as exc:
                return exc.code

    results = await asyncio.gather(*(_one_upload(i) for i in range(fan_out)))

    ok_count = results.count("ok")
    rejected_count = results.count("attachment_global_ceiling_exceeded")
    assert ok_count == ceiling_slots  # exactly the ceiling's worth, never more
    assert ok_count + rejected_count == fan_out  # every writer got a definitive answer, nothing silently lost
    assert not any(code not in ("ok", "attachment_global_ceiling_exceeded") for code in results)

    async with AsyncSessionLocal() as check_session:
        stats = await check_session.get(AttachmentStorageStats, attachments_module.STATS_SINGLETON_ID)
        assert stats.total_bytes == blob_size * ceiling_slots  # the DB itself agrees -- no lost update, no overrun
        blobs = (await check_session.execute(AttachmentBlob.__table__.select())).all()
        assert len(blobs) == ceiling_slots


# --- per-room file count cap ---


async def test_per_room_cap_rejects_beyond_limit(fake_settings, db_session):
    settings = fake_settings(attachment_max_files_per_room=2)
    room = await _make_room(db_session)

    for i in range(2):
        await upload_pdf_attachment(
            db_session,
            _agen(_chunked(_pdf_bytes(f"file-{i}".encode()))),
            room_id=room.id,
            sender="owner",
            principal=_OWNER,
            display_filename=f"f{i}.pdf",
            settings=settings,
        )

    with pytest.raises(ApiError) as exc_info:
        await upload_pdf_attachment(
            db_session,
            _agen(_chunked(_pdf_bytes(b"file-2"))),
            room_id=room.id,
            sender="owner",
            principal=_OWNER,
            display_filename="f2.pdf",
            settings=settings,
        )

    assert exc_info.value.code == "room_attachment_cap_exceeded"
    rows = (
        await db_session.execute(RoomAttachment.__table__.select().where(RoomAttachment.room_id == room.id))
    ).all()
    assert len(rows) == 2


async def test_per_room_cap_survives_concurrent_writers(fake_settings, db_session):
    """The concrete precedent this guards against (ADR-0012 decision 4,
    citing app/rooms.py's message_count bug): an unlocked read-check-write
    counter lets concurrent writers each observe "under the cap" and all
    proceed, overrunning a cap that was supposed to be hard. Fires
    `fan_out` genuinely concurrent uploads (real, separate DB sessions, no
    artificial delay needed -- Postgres's own FOR UPDATE row lock on the
    room serializes them) against a room with a small cap, and asserts the
    final count never exceeds it -- no lost update, no overrun.
    """
    settings = fake_settings(attachment_max_files_per_room=5)
    room = await _make_room(db_session)
    room_id = room.id
    fan_out = 15

    async def _one_upload(i: int):
        async with AsyncSessionLocal() as session:
            try:
                await upload_pdf_attachment(
                    session,
                    _agen(_chunked(_pdf_bytes(f"concurrent-{i}".encode()))),
                    room_id=room_id,
                    sender="owner",
                    principal=_OWNER,
                    display_filename=f"c{i}.pdf",
                    settings=settings,
                )
                return "ok"
            except ApiError as exc:
                return exc.code

    results = await asyncio.gather(*(_one_upload(i) for i in range(fan_out)))

    ok_count = results.count("ok")
    rejected_count = results.count("room_attachment_cap_exceeded")
    assert ok_count == 5  # exactly the cap, never more
    assert ok_count + rejected_count == fan_out  # every writer got a definitive answer, nothing silently lost
    assert not any(code not in ("ok", "room_attachment_cap_exceeded") for code in results)

    async with AsyncSessionLocal() as check_session:
        rows = (
            await check_session.execute(RoomAttachment.__table__.select().where(RoomAttachment.room_id == room_id))
        ).all()
        assert len(rows) == 5  # the DB itself agrees -- no lost update, no overrun


# --- dedup ---


async def test_identical_content_dedupes_to_one_blob_different_filenames(fake_settings, db_session):
    settings = fake_settings()
    room_a = await _make_room(db_session, name="room-a")
    room_b = await _make_room(db_session, name="room-b")
    payload = _pdf_bytes(b"shared content")

    att_a = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(payload)),
        room_id=room_a.id,
        sender="owner",
        principal=_OWNER,
        display_filename="as-seen-in-a.pdf",
        settings=settings,
    )
    att_b = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(payload)),
        room_id=room_b.id,
        sender="owner",
        principal=_OWNER,
        display_filename="totally different name.pdf",
        settings=settings,
    )

    assert att_a.blob_sha256 == att_b.blob_sha256
    assert att_a.filename != att_b.filename

    blobs = (await db_session.execute(AttachmentBlob.__table__.select())).all()
    assert len(blobs) == 1  # stored once

    stats = await db_session.get(AttachmentStorageStats, attachments_module.STATS_SINGLETON_ID)
    assert stats.total_bytes == len(payload)  # counted once, not twice

    on_disk = list(Path(settings.attachment_storage_dir).glob("*.pdf"))
    assert len(on_disk) == 1


# --- reference-counted deletion ---


async def test_sweep_deletes_unreferenced_blob(fake_settings, db_session):
    settings = fake_settings(attachment_grace_period_days=0)
    room = await _make_room(db_session)

    attachment = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(_pdf_bytes())),
        room_id=room.id,
        sender="owner",
        principal=_OWNER,
        display_filename="doc.pdf",
        settings=settings,
    )
    await _close_room(db_session, room, closed_at=datetime.now(UTC) - timedelta(days=1))
    sha256_hex = attachment.blob_sha256
    on_disk_path = blob_path(settings.attachment_storage_dir, sha256_hex)
    assert on_disk_path.exists()

    deleted = await sweep_expired_blobs(db_session, settings=settings)

    assert deleted == [sha256_hex]
    assert not on_disk_path.exists()
    assert (await db_session.get(AttachmentBlob, sha256_hex)) is None
    stats = await db_session.get(AttachmentStorageStats, attachments_module.STATS_SINGLETON_ID)
    assert stats.total_bytes == 0


async def test_sweep_respects_grace_period(fake_settings, db_session):
    settings = fake_settings(attachment_grace_period_days=7)
    room = await _make_room(db_session)

    attachment = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(_pdf_bytes())),
        room_id=room.id,
        sender="owner",
        principal=_OWNER,
        display_filename="doc.pdf",
        settings=settings,
    )
    await _close_room(db_session, room, closed_at=datetime.now(UTC))  # just closed
    sha256_hex = attachment.blob_sha256

    deleted = await sweep_expired_blobs(db_session, settings=settings)

    assert deleted == []
    assert blob_path(settings.attachment_storage_dir, sha256_hex).exists()
    assert (await db_session.get(AttachmentBlob, sha256_hex)) is not None


async def test_sweep_deletes_once_grace_period_has_passed(fake_settings, db_session):
    settings = fake_settings(attachment_grace_period_days=7)
    room = await _make_room(db_session)

    attachment = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(_pdf_bytes())),
        room_id=room.id,
        sender="owner",
        principal=_OWNER,
        display_filename="doc.pdf",
        settings=settings,
    )
    # Closed 8 days ago -- grace is 7, so this is past it.
    await _close_room(db_session, room, closed_at=datetime.now(UTC) - timedelta(days=8))
    sha256_hex = attachment.blob_sha256

    deleted = await sweep_expired_blobs(db_session, settings=settings)

    assert deleted == [sha256_hex]
    assert not blob_path(settings.attachment_storage_dir, sha256_hex).exists()


async def test_sweep_never_deletes_while_room_still_open(fake_settings, db_session):
    settings = fake_settings(attachment_grace_period_days=0)
    room = await _make_room(db_session, status="open")

    attachment = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(_pdf_bytes())),
        room_id=room.id,
        sender="owner",
        principal=_OWNER,
        display_filename="doc.pdf",
        settings=settings,
    )
    sha256_hex = attachment.blob_sha256

    deleted = await sweep_expired_blobs(db_session, settings=settings)

    assert deleted == []
    assert blob_path(settings.attachment_storage_dir, sha256_hex).exists()


async def test_sweep_two_rooms_sharing_a_blob_neither_deletes_the_others_copy(fake_settings, db_session):
    """The concrete case ADR-0012 decision 4 calls out by name: two rooms
    dedup to the same blob; closing (and passing grace on) just ONE of them
    must never delete the file the other room still needs.
    """
    settings = fake_settings(attachment_grace_period_days=0)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    room_a = await _make_room(db_session, name="room-a")
    room_b = await _make_room(db_session, name="room-b")
    payload = _pdf_bytes(b"shared across two rooms")

    att_a = await upload_pdf_attachment(
        db_session, _agen(_chunked(payload)), room_id=room_a.id, sender="owner", principal=_OWNER, display_filename="a.pdf", settings=settings
    )
    await upload_pdf_attachment(
        db_session, _agen(_chunked(payload)), room_id=room_b.id, sender="owner", principal=_OWNER, display_filename="b.pdf", settings=settings
    )
    sha256_hex = att_a.blob_sha256
    await _close_room(db_session, room_a, closed_at=long_ago)  # room_b stays open

    # room_a is closed and past its (zero-day) grace period, but room_b is
    # still open and shares the same blob -- must survive.
    deleted = await sweep_expired_blobs(db_session, settings=settings)
    assert deleted == []
    assert blob_path(settings.attachment_storage_dir, sha256_hex).exists()

    # Now close room_b too, past grace -- only now is the blob deletable.
    room_b_row = await db_session.get(Room, room_b.id)
    room_b_row.status = "closed"
    room_b_row.closed_at = long_ago
    room_b_row.close_reason = "owner"
    await db_session.commit()

    deleted = await sweep_expired_blobs(db_session, settings=settings)
    assert deleted == [sha256_hex]
    assert not blob_path(settings.attachment_storage_dir, sha256_hex).exists()


async def test_sweep_never_deletes_blob_referenced_by_a_brain_document(fake_settings, db_session):
    """Decision 3/4: once a room attachment is "saved" (a MirroredDocument
    referencing the same blob exists), the blob must survive forever --
    supersede-never-erase means that document row is never deleted, so
    nothing in this module may ever delete the blob it points at, no
    matter what happens to the room(s) that originally held the file.
    """
    settings = fake_settings(attachment_grace_period_days=0)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    room = await _make_room(db_session)

    attachment = await upload_pdf_attachment(
        db_session,
        _agen(_chunked(_pdf_bytes())),
        room_id=room.id,
        sender="owner",
        principal=_OWNER,
        display_filename="keep-me.pdf",
        settings=settings,
    )
    await _close_room(db_session, room, closed_at=long_ago)
    sha256_hex = attachment.blob_sha256

    machine = Machine(id=str(ULID()), name="m", token_hash=hash_token(generate_machine_token()), status="active")
    project = Project(name="brain", status="active", created_at=datetime.now(UTC))
    db_session.add_all([machine, project])
    await db_session.flush()
    deposit = Deposit(
        deposit_id=str(ULID()),
        machine_id=machine.id,
        tool="t",
        session="s",
        project=project.name,
        reason="daily",
        client_ts=datetime.now(UTC),
        received_at=datetime.now(UTC),
        stub_created=False,
    )
    db_session.add(deposit)
    await db_session.flush()
    doc = MirroredDocument(
        id=str(ULID()),
        project=project.name,
        path="rooms/keep-me.pdf",
        kind="doc",
        title="keep-me.pdf",
        content="(binary PDF content saved from a room attachment)",
        version=1,
        deposit_id=deposit.deposit_id,
        machine_id=machine.id,
        created_at=datetime.now(UTC),
        blob_sha256=sha256_hex,
    )
    db_session.add(doc)
    await db_session.commit()

    deleted = await sweep_expired_blobs(db_session, settings=settings)

    assert deleted == []
    assert blob_path(settings.attachment_storage_dir, sha256_hex).exists()


async def test_concurrent_dedup_upload_races_sweep_reclaim_new_reference_never_lost(fake_settings, db_session):
    """Fix 4(b) (independent review): a blob that gains a brand-new
    reference (a second room deduping to the same content) at the exact
    moment `sweep_expired_blobs` is trying to reclaim it as unreferenced
    must never be lost. `_reclaim_blob_if_unreferenced`'s `SELECT ... FOR
    UPDATE` on the blob row is what's supposed to prevent this (see its
    docstring): whichever transaction -- the sweep's delete, or the new
    upload's dedup lookup in `_get_or_create_blob` -- gets the lock first,
    the other blocks until it commits, so "blob has zero references and is
    about to be deleted" and "blob is about to gain a new reference" can
    never interleave.

    Proven with real, separate `AsyncSessionLocal()` sessions racing via
    `asyncio.gather` (genuine asyncpg I/O, no artificial delay). Either
    valid outcome is acceptable -- the new upload's lock wins and the blob
    survives untouched, OR the sweep's delete commits first and the new
    upload (finding no existing blob at that hash any more) simply
    recreates it -- but the one thing that must NEVER happen is the
    invariant this asserts: once the new room's attachment reference is
    committed, its blob row and on-disk file both genuinely exist too. A
    blob must never be left referenced-but-gone.
    """
    settings = fake_settings(attachment_grace_period_days=0)
    payload = _pdf_bytes(b"raced-dedup-content")

    room_a = await _make_room(db_session, name="race-room-a")
    room_b = await _make_room(db_session, name="race-room-b")

    # room_a holds the only reference so far, and is already closed past
    # its (zero-day) grace period -- exactly what makes this blob a sweep
    # candidate the instant nothing else references it.
    await upload_pdf_attachment(
        db_session,
        _agen(_chunked(payload)),
        room_id=room_a.id,
        sender="owner",
        principal=_OWNER,
        display_filename="a.pdf",
        settings=settings,
    )
    await _close_room(db_session, room_a, closed_at=datetime.now(UTC) - timedelta(days=1))

    async def _sweep():
        async with AsyncSessionLocal() as session:
            return await sweep_expired_blobs(session, settings=settings)

    async def _dedup_upload_into_room_b():
        async with AsyncSessionLocal() as session:
            try:
                attachment = await upload_pdf_attachment(
                    session,
                    _agen(_chunked(payload)),  # identical content -- room_b dedups to the same hash
                    room_id=room_b.id,
                    sender="owner",
                    principal=_OWNER,
                    display_filename="b.pdf",
                    settings=settings,
                )
                return ("ok", attachment.blob_sha256)
            except ApiError as exc:
                return ("error", exc.code)

    _sweep_result, upload_result = await asyncio.gather(_sweep(), _dedup_upload_into_room_b())

    assert upload_result[0] == "ok", upload_result
    sha256_hex = upload_result[1]

    async with AsyncSessionLocal() as check_session:
        att_row = (
            await check_session.execute(
                RoomAttachment.__table__.select().where(RoomAttachment.__table__.c.room_id == room_b.id)
            )
        ).first()
        assert att_row is not None, "room_b's attachment reference itself went missing"
        blob_row = await check_session.get(AttachmentBlob, sha256_hex)
        assert blob_row is not None, "room_b's attachment references a blob row that no longer exists"
    assert blob_path(
        settings.attachment_storage_dir, sha256_hex
    ).exists(), "room_b's attachment references a blob file that was reclaimed out from under it"


# --- blob_path: path traversal impossible by construction ---


@pytest.mark.parametrize(
    "bad_hash",
    [
        "../../etc/passwd",
        "not-hex-at-all",
        "a" * 63,  # one short
        "a" * 65,  # one long
        "A" * 64,  # uppercase not accepted
        "",
    ],
)
def test_blob_path_rejects_anything_that_is_not_a_clean_hex64_hash(bad_hash, tmp_path):
    with pytest.raises(ValueError):
        blob_path(tmp_path, bad_hash)


def test_blob_path_accepts_a_genuine_hash(tmp_path):
    good = "a" * 64
    p = blob_path(tmp_path, good)
    assert p.parent == tmp_path
    assert p.name == f"{good}.pdf"


def test_pdf_magic_constant():
    assert PDF_MAGIC == b"%PDF-"
