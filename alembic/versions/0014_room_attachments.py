"""Room file attachments (ADR-0012 storage core): content-addressed blob
storage plus per-room attachment references, reference-counted deletion,
and the Brain-document link.

`attachment_blobs` is the content-addressed store (decision 2): one row per
unique sha256, byte size only -- no filename, no room reference. Path on
disk (the `attachment_data` named volume, docker-compose.yml) is derived
from `sha256` alone (app/attachments.py's `blob_path`), so path traversal
via a supplied filename is impossible by construction.

`room_attachments` is a room's reference to a blob, carrying the DISPLAY
filename (decision 2: the name lives on the reference, not the blob, so one
blob can appear under different names in different rooms with zero
duplication). `room_id` is `ondelete="CASCADE"` -- see app/models.py's
`RoomAttachment` docstring for why this is a deliberate, narrow exception to
this codebase's usual explicit-application-cascade convention (app/rooms.py's
`delete_room` doesn't know about this table; without DB-level cascade,
deleting a room with attachments would 500 on an FK violation).

`attachment_storage_stats` is the single-row running total of bytes across
all blobs, locked under `SELECT ... FOR UPDATE` before admitting a new
(non-deduplicated) blob -- the global-ceiling check (decision 6), same
denormalized-counter discipline `rooms.message_count` uses for the
per-room message cap.

`mirrored_documents.blob_sha256` is the optional Brain-document link
(decisions 3/4): set only when a document was born from "saving" a room
attachment, so the reference-counted deletion sweep can see a Brain
document still references a blob and keep it alive forever, independent of
what happens to the room(s) that originally held the file.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "attachment_blobs",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("byte_size", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("byte_size > 0", name="ck_attachment_blobs_byte_size_positive"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_attachment_blobs_sha256_hex64"),
    )

    op.create_table(
        "room_attachments",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("room_id", sa.String(length=26), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("blob_sha256", sa.String(length=64), sa.ForeignKey("attachment_blobs.sha256"), nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("uploaded_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_room_attachments_room_id", "room_attachments", ["room_id"])
    op.create_index("ix_room_attachments_blob_sha256", "room_attachments", ["blob_sha256"])

    op.create_table(
        "attachment_storage_stats",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("total_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.CheckConstraint("total_bytes >= 0", name="ck_attachment_storage_stats_total_bytes_nonneg"),
    )
    # Seed the single singleton row here rather than having app code create
    # it lazily on first use -- app/attachments.py's `_get_or_create_blob`
    # always finds it present and fails loudly if it doesn't, rather than
    # needing its own race-prone "insert if missing" path for a row that
    # should only ever be created once, at migration time.
    op.execute("INSERT INTO attachment_storage_stats (id, total_bytes) VALUES ('singleton', 0)")

    op.add_column(
        "mirrored_documents",
        sa.Column("blob_sha256", sa.String(length=64), sa.ForeignKey("attachment_blobs.sha256"), nullable=True),
    )
    op.create_index("ix_mirrored_documents_blob_sha256", "mirrored_documents", ["blob_sha256"])


def downgrade() -> None:
    op.drop_index("ix_mirrored_documents_blob_sha256", table_name="mirrored_documents")
    op.drop_column("mirrored_documents", "blob_sha256")

    op.drop_table("attachment_storage_stats")

    op.drop_index("ix_room_attachments_blob_sha256", table_name="room_attachments")
    op.drop_index("ix_room_attachments_room_id", table_name="room_attachments")
    op.drop_table("room_attachments")

    op.drop_table("attachment_blobs")
