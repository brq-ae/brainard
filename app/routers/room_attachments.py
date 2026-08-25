"""Room file attachments -- v1 API (ADR-0012 stage 2). Kept in its OWN
router file, the same precedent app/routers/rooms_ai.py sets for ADR-0011:
this is a new feature adding new paths, not touching app/routers/rooms.py's
existing contract.

Auth split, per the ADR: upload/list/download/attach-from-Brain are owner
OR machine (agents genuinely need to upload, list, download, and attach);
delete/save-to-Brain/the agent-upload-switch toggle are owner-only (decision
7's checkbox is an owner control; deleting and promoting to the Brain are
owner curation decisions, same trust posture as app/room_ai.py's
`deposit_result` and app/routers/rooms.py's `close_room_endpoint`).

Upload streams the request body directly (`request.stream()`) into
app/attachments.py's `receive_pdf_upload` -- never FastAPI's `UploadFile`/
multipart form parsing, which would buffer each part into Starlette's own
spooled temp file before this router even sees a byte. `filename`/`sender`
travel as query parameters instead of multipart fields, precisely so the
body can stay a single raw byte stream with nothing to demultiplex.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse

from app.attachments import (
    add_attachment_from_brain_document,
    delete_room_attachment,
    get_room_attachment_for_download,
    list_room_attachments,
    save_attachment_to_brain,
    upload_pdf_attachment,
)
from app.attachments import blob_path as attachment_blob_path
from app.auth import Principal, require_machine_or_owner, require_owner
from app.config import get_settings
from app.db import get_db
from app.models import AttachmentBlob, RoomAttachment
from app.rooms import post_attachment_added_message, post_attachment_removed_message
from app.rooms import set_agent_uploads_allowed as set_agent_uploads_allowed_op
from app.schemas import (
    RoomAgentUploadsRequest,
    RoomAgentUploadsResponse,
    RoomAttachFromBrainRequest,
    RoomAttachmentListResponse,
    RoomAttachmentOut,
    RoomAttachmentSaveRequest,
    RoomAttachmentSaveResponse,
)

router = APIRouter(prefix="/v1/rooms", tags=["room-attachments"])


def _download_headers(filename: str) -> dict[str, str]:
    """ADR-0012 decision 14: the response-header discipline for serving an
    uploaded file back. `filename` is already sanitized (app/room_export.py's
    `safe_filename_component`, applied at upload/attach time -- see
    app/attachments.py) -- safe to interpolate straight into the quoted
    header value, same precedent app/routers/ui_rooms.py's transcript-export
    endpoints already establish (`Content-Disposition`, there at
    ui_rooms.py:702,720). `X-Content-Type-Options: nosniff` and a
    restrictive CSP/sandbox are new headers this app doesn't set anywhere
    else -- defense in depth on TOP of decision 16's magic-byte gate at
    upload time, not a substitute for it.
    """
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }


@router.post("/{room_id}/attachments", response_model=RoomAttachmentOut, status_code=201)
async def upload_room_attachment_endpoint(
    room_id: str,
    request: Request,
    filename: str = Query(..., min_length=1),
    sender: str = Query(..., min_length=1),
    principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomAttachmentOut:
    """Owner and agents (ADR-0012 decision 7). `request.stream()` is passed
    straight through to `upload_pdf_attachment` -- the request body is
    never buffered whole, in memory or otherwise, before the magic-byte
    check and the per-chunk size/disk-floor checks run (app/attachments.py's
    `receive_pdf_upload`).

    `principal` (the authenticated identity FastAPI resolved from the
    bearer token) is threaded straight through to `upload_pdf_attachment` --
    it's what lets `_clean_sender` reject a machine token trying to claim
    `sender=owner` (fix for the independent-review finding: `sender` alone
    carries no identity, so it can never be trusted on its own).
    """
    attachment = await upload_pdf_attachment(
        db, request.stream(), room_id=room_id, sender=sender, principal=principal, display_filename=filename
    )
    blob = await db.get(AttachmentBlob, attachment.blob_sha256)
    await post_attachment_added_message(
        db, room_id, filename=attachment.filename, byte_size=blob.byte_size, uploaded_by=attachment.uploaded_by
    )
    return RoomAttachmentOut(
        id=attachment.id,
        room_id=attachment.room_id,
        filename=attachment.filename,
        byte_size=blob.byte_size,
        uploaded_by=attachment.uploaded_by,
        created_at=attachment.created_at,
    )


@router.get("/{room_id}/attachments", response_model=RoomAttachmentListResponse)
async def list_room_attachments_endpoint(
    room_id: str,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomAttachmentListResponse:
    views = await list_room_attachments(db, room_id)
    return RoomAttachmentListResponse(
        results=[
            RoomAttachmentOut(
                id=v.attachment.id,
                room_id=v.attachment.room_id,
                filename=v.attachment.filename,
                byte_size=v.byte_size,
                uploaded_by=v.attachment.uploaded_by,
                created_at=v.attachment.created_at,
            )
            for v in views
        ]
    )


@router.get("/{room_id}/attachments/{attachment_id}/download")
async def download_room_attachment_endpoint(
    room_id: str,
    attachment_id: str,
    _principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    attachment, blob = await get_room_attachment_for_download(db, room_id=room_id, attachment_id=attachment_id)
    settings = get_settings()
    path = attachment_blob_path(settings.attachment_storage_dir, blob.sha256)
    return FileResponse(path=path, media_type="application/pdf", headers=_download_headers(attachment.filename))


@router.delete("/{room_id}/attachments/{attachment_id}")
async def delete_room_attachment_endpoint(
    room_id: str,
    attachment_id: str,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Snapshot the filename BEFORE deleting -- the row (and its filename)
    # is gone once delete_room_attachment returns, and the system message
    # needs it. A mismatched/missing snapshot (already-deleted id) just
    # means delete_room_attachment's own 404 fires below and no message is
    # ever posted -- same outcome as today, no new failure mode.
    snapshot = await db.get(RoomAttachment, attachment_id)
    filename = snapshot.filename if snapshot is not None and snapshot.room_id == room_id else None

    sha256_hex = await delete_room_attachment(db, room_id=room_id, attachment_id=attachment_id)
    if filename is not None:
        await post_attachment_removed_message(db, room_id, filename=filename, removed_by="owner")
    return {"id": attachment_id, "deleted": True, "blob_sha256": sha256_hex}


@router.post("/{room_id}/attachments/{attachment_id}/save", response_model=RoomAttachmentSaveResponse)
async def save_room_attachment_endpoint(
    room_id: str,
    attachment_id: str,
    body: RoomAttachmentSaveRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomAttachmentSaveResponse:
    """Owner-only (ADR-0012 decision 3). The room copy is untouched --
    nothing moves, nothing is deleted; see app/attachments.py's
    `save_attachment_to_brain`.
    """
    saved = await save_attachment_to_brain(db, room_id=room_id, attachment_id=attachment_id, project=body.project)
    return RoomAttachmentSaveResponse(
        document_id=saved.document_id, path=saved.path, version=saved.version, project=saved.project
    )


@router.post("/{room_id}/attach-from-brain", response_model=RoomAttachmentOut, status_code=201)
async def attach_from_brain_endpoint(
    room_id: str,
    body: RoomAttachFromBrainRequest,
    principal: Principal = Depends(require_machine_or_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomAttachmentOut:
    """Owner via a search picker, agents by document id (ADR-0012 decision
    10) -- allowed even when the agent-upload switch is off (decision 8):
    this creates no new file.

    `principal` is threaded through to `add_attachment_from_brain_document`
    for the identical `sender=owner`-claim identity binding `upload_room_attachment_endpoint`
    documents above.
    """
    attachment = await add_attachment_from_brain_document(
        db, room_id=room_id, sender=body.sender, principal=principal, document_id=body.document_id
    )
    blob = await db.get(AttachmentBlob, attachment.blob_sha256)
    await post_attachment_added_message(
        db, room_id, filename=attachment.filename, byte_size=blob.byte_size, uploaded_by=attachment.uploaded_by
    )
    return RoomAttachmentOut(
        id=attachment.id,
        room_id=attachment.room_id,
        filename=attachment.filename,
        byte_size=blob.byte_size,
        uploaded_by=attachment.uploaded_by,
        created_at=attachment.created_at,
    )


@router.post("/{room_id}/agent-uploads", response_model=RoomAgentUploadsResponse)
async def set_agent_uploads_allowed_endpoint(
    room_id: str,
    body: RoomAgentUploadsRequest,
    _owner: Principal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> RoomAgentUploadsResponse:
    """Owner-only mid-session toggle (ADR-0012 decisions 7/9) -- see
    app/rooms.py's `set_agent_uploads_allowed` for the row lock + system
    announcement.
    """
    room, announcement = await set_agent_uploads_allowed_op(db, room_id, body.allowed)
    return RoomAgentUploadsResponse(id=room.id, agent_uploads_allowed=room.agent_uploads_allowed, announcement=announcement)
