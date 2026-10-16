from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ImportMode = Literal["labs_only", "backfill", "daily"]


class WhatsappImportCreate(BaseModel):
    """Start a chunked upload: the zip's name and exact size in bytes."""

    mode: ImportMode
    patient_focus_id: int | None = None
    filename: str = Field(min_length=1, max_length=500)
    size: int = Field(gt=0)
    # Sync extension runs: files will be uploaded for the stored messages
    # after the import (``POST …/{id}/messages/{message_id}/media``).
    expects_media: bool = False
    # The day/month order the uploader saw (the sync extension reads it from
    # WhatsApp Web); used only when the file's dates read both ways.
    date_order: Literal["dmy", "mdy", "ymd"] | None = None


class WhatsappImportOut(BaseModel):
    id: int
    mode: str
    # uploading / queued / processing / done / failed / cancelled. "failed"
    # also when the job gave up after its retries.
    status: str
    patient_focus_id: int | None
    filename: str
    size: int
    chunk_size: int
    chunk_count: int
    job_id: int | None
    range_from: datetime | None
    range_to: datetime | None
    stats: dict[str, Any]
    uploaded_by_name: str | None
    created_at: datetime
    finished_at: datetime | None
    awaiting_media: bool = False
    media_completed_at: datetime | None = None
    # Only on GET /imports/whatsapp/{id} while uploading: chunks stored so far
    # (0-based), so an interrupted upload resumes with the missing ones.
    received_chunks: list[int] | None = None


class ChunkOut(BaseModel):
    index: int
    size: int


class LastMessageOut(BaseModel):
    """``GET /imports/whatsapp/last``: where the next export should start."""

    # Newest stored message, on the Panamá clock (-05:00); null when none.
    last_message_at: datetime | None
    # The newest finished import (any mode), for display.
    last_import_id: int | None
    last_import_finished_at: datetime | None


class MissingMediaItem(BaseModel):
    """A stored message whose file is not stored yet. PHI: never log it."""

    id: int
    import_id: int
    sent_at: datetime  # Panamá clock (-05:00), minute precision as exported
    sender: str | None
    media_kind: str  # audio / image / pdf / video / other
    media_filename: str | None
    caption: str  # the message text, at most 200 characters
    forwarded: bool
    # Stored media messages with the same sent_at + sender + text: the
    # extension can only tell them apart by their order in the chat.
    twin_index: int
    twin_count: int
    # WhatsApp Web message key (data-id) recorded with an earlier upload.
    wa_key: str | None = None


class MissingMediaOut(BaseModel):
    items: list[MissingMediaItem]
    has_more: bool


class MediaUploadOut(BaseModel):
    message_id: int
    attachment_id: int
    media_kind: str
    # False when the message already had exactly these bytes (a retry).
    created: bool
