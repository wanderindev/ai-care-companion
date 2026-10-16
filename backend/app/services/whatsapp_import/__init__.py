"""WhatsApp export import (DESIGN §5.6, §6.5, §6.7, §6.8): upload, storage, dedupe.

- ``upload``: chunked upload of the export zip into an S3 multipart upload
  (``POST /imports/whatsapp`` → ``PUT …/chunks/{i}`` → ``POST …/complete``).
- ``process``: the worker job — parse (``app.services.whatsapp``), store new
  messages (dedupe on ``dedupe_hash``) and their media as attachments.
- ``media_upload``: the sync extension's files for stored messages that have
  none (WhatsApp Web exports text only), attached through an import created
  with ``expects_media``.
- ``jobs``: the ``whatsapp_import`` and ``whatsapp_media`` job handlers.

Downstream features hook in with ``after_import`` (runs when an import is
done, in its transaction) and find the PDFs an import stored with
``pdf_messages(db, import_id)``. Hooks that only look at an import's files
(``media_import_id``) also register with ``after_media``, so files the
extension adds later get the same processing.
"""

from app.services.whatsapp_import.process import (
    AFTER_IMPORT_HOOKS,
    AFTER_MEDIA_HOOKS,
    after_import,
    after_media,
    pdf_messages,
    process_import,
)

__all__ = [
    "AFTER_IMPORT_HOOKS",
    "AFTER_MEDIA_HOOKS",
    "after_import",
    "after_media",
    "pdf_messages",
    "process_import",
]
