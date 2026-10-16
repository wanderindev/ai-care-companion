"""WhatsApp chat export parsing (DESIGN §5.6, §6.5, §6.7).

A pure library, no database: ``parse_export(zip)`` turns a WhatsApp export
zip (Android, iOS or WhatsApp Web; any locale order of day/month, 12h/24h)
into ``WaMessage`` objects plus media references, and ``parse_chat_text``
does the same for the chat ``.txt`` alone.

- ``chat``: the line parser (format detection, multi-line messages, system
  messages, media markers, ``dedupe_hash``).
- ``export``: the zip reader. It only reads the chat ``.txt`` (size-capped)
  and lists member names; media bytes are never loaded, so callers stream
  them with ``zipfile`` using ``WaMessage.media_member``.

The CLI is ``python -m scripts.wa_parse <zip> --stats``. Everything here
handles PHI: never log message text, sender names or file names.
"""

from app.services.whatsapp.chat import (
    MEDIA_KINDS,
    PANAMA_TZ,
    ChatParse,
    WaMessage,
    dedupe_hash,
    media_kind,
    parse_chat_text,
)
from app.services.whatsapp.export import ExportParse, WaExportError, parse_export

__all__ = [
    "MEDIA_KINDS",
    "PANAMA_TZ",
    "ChatParse",
    "ExportParse",
    "WaExportError",
    "WaMessage",
    "dedupe_hash",
    "media_kind",
    "parse_chat_text",
    "parse_export",
]
