"""WhatsApp export zip → ``ChatParse`` with media resolved to zip members.

Safe on user uploads: only the chat ``.txt`` is decompressed, through a
streaming read capped at ``MAX_CHAT_BYTES`` (a zip bomb cannot inflate past
it, whatever its header claims); media members are only *listed*, never read.
Member names that are absolute, contain ``..`` or a drive letter are never
used (path traversal), and nothing is ever extracted to disk. Callers that
need a file stream it with ``zipfile.ZipFile.open(message.media_member)``.
"""

import re
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import IO

from app.services.whatsapp.chat import ChatParse, parse_chat_text

MAX_CHAT_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 100_000

_CHAT_NAME = re.compile(
    r"^(?:_chat|whatsapp chat\b.*|chat de whatsapp\b.*|conversa do whatsapp\b.*)\.txt$",
    re.IGNORECASE,
)


class WaExportError(ValueError):
    """The upload is not a usable WhatsApp export (message is safe to show)."""


@dataclass
class ExportParse:
    chat: ChatParse
    chat_member: str
    member_count: int = 0
    media_files: int = 0  # members other than the chat .txt
    unsafe_members: int = 0  # ignored names (absolute, "..", drive letters)
    missing_media: int = 0  # attachments (not "omitted") whose file is not in the zip
    unreferenced_media: int = 0  # files in the zip no message names

    @property
    def messages(self):
        return self.chat.messages


def _safe(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name or "\x00" in name:
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    return ".." not in PurePosixPath(name).parts


def _find_chat(names: list[str]) -> str:
    texts = [
        n
        for n in names
        if n.lower().endswith(".txt") and not n.startswith("__MACOSX/") and not n.endswith("/")
    ]
    preferred = [n for n in texts if _CHAT_NAME.match(PurePosixPath(n).name)]
    # Prefer the shallowest match (the chat sits at the root of the export).
    for candidates in (preferred, texts if len(texts) == 1 else []):
        if candidates:
            return min(candidates, key=lambda n: (n.count("/"), n))
    if not texts:
        raise WaExportError("el archivo no contiene el chat (.txt) de WhatsApp")
    raise WaExportError("el archivo contiene varios .txt y ninguno parece el chat de WhatsApp")


def _read_capped(archive: zipfile.ZipFile, name: str) -> str:
    with archive.open(name) as stream:
        data = stream.read(MAX_CHAT_BYTES + 1)
    if len(data) > MAX_CHAT_BYTES:
        raise WaExportError("el chat de WhatsApp es demasiado grande")
    return data.decode("utf-8-sig", errors="replace")


def parse_export(
    source: str | IO[bytes],
    *,
    reference: datetime | None = None,
    date_order: str | None = None,
    floor: datetime | None = None,
) -> ExportParse:
    """Parse a WhatsApp export zip (a path or a seekable binary file).

    ``reference``, ``date_order`` and ``floor`` break a day/month tie
    (``parse_chat_text``)."""
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise WaExportError("el archivo no es un zip válido") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise WaExportError("el zip tiene demasiados archivos")
        safe = [i.filename for i in infos if _safe(i.filename) and not i.is_dir()]
        unsafe = sum(1 for i in infos if not _safe(i.filename))
        chat_member = _find_chat(safe)
        try:
            content = _read_capped(archive, chat_member)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as exc:
            # Corrupt data, unsupported compression or an encrypted member.
            raise WaExportError("no se pudo leer el chat dentro del zip") from exc

    chat = parse_chat_text(content, reference=reference, date_order=date_order, floor=floor)
    media = [n for n in safe if n != chat_member and not n.startswith("__MACOSX/")]
    by_name: dict[str, str] = {}
    for name in sorted(media, key=lambda n: (n.count("/"), n)):
        by_name.setdefault(PurePosixPath(name).name, name)

    report = ExportParse(
        chat=chat,
        chat_member=chat_member,
        member_count=len(infos),
        media_files=len(media),
        unsafe_members=unsafe,
    )
    referenced: set[str] = set()
    messages = []
    for message in chat.messages:
        if message.media_filename and not message.media_omitted:
            member = by_name.get(message.media_filename)
            if member is None:
                report.missing_media += 1
            else:
                referenced.add(member)
                message = replace(message, media_member=member)
        messages.append(message)
    chat.messages = messages
    report.unreferenced_media = len(set(media) - referenced)
    return report
