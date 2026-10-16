"""WhatsApp chat ``.txt`` → messages (deterministic, no AI, no database).

Supported shapes (after the invisible characters are normalized)::

    12/9/23, 14:05 - Ana: texto                     ← Android
    12/9/2023, 2:05 p. m. - Ana: texto              ← 12 h, Spanish
    9/12/23, 2:05 PM - Ana: text                    ← 12 h, US order
    12/9/23, 14:05 - Ana creó el grupo "X"          ← system (no "sender: ")
    [12/9/23, 14:05:33] Ana: texto                  ← iOS (bracketed, seconds)
    [12/9/23, 14:05:33] Grupo: <U+200E>Ana añadió a Beto  ← iOS system
    [6/17/25, 3:45:12 PM] Ana: <image omitted> caption    ← WhatsApp Web

Media markers: ``IMG-…jpg (archivo adjunto)`` / ``(file attached)``
(Android), ``<adjunto: 00000012-PHOTO-….jpg>`` / ``<attached: …>`` (iOS),
``<Multimedia omitido>`` / ``<Media omitted>`` (Android without media), iOS
``imagen omitida`` / ``image omitted``, and WhatsApp Web's ``<image omitted>``,
``<voice message omitted>``, ``<document omitted> name.pdf`` (a document keeps
its name) with an optional ``[forwarded]`` prefix. A caption is the rest of
the text. The Web shapes come from the operator's real (English-UI) export;
the Spanish spellings of the ``<… omitted>`` / ``[forwarded]`` markers are
best guesses.

The style (bracketed or dash) is detected from the first header lines and
then used for the whole file. Day/month order is resolved across the whole
file: an order is impossible if any date would have a month > 12; when both
remain possible, the one whose timestamps go backwards less often wins. A tie
(a short sync export such as 10/9 - 10/10, every date readable both ways)
goes, in turn, to the uploader's ``date_order`` hint, to the order that puts
no message weeks before the chat's newest stored message (``floor``), to the
one that puts no message in the future and ends closest to the ``reference``
time (the import's), to the shorter span, and last to day/month (the
Spanish locale); see ``_choose_order``. A 4-digit first part means
year/month/day. Two-digit years are 20xx. Times are wall-clock times in
America/Panama (UTC-5, no DST).

Lines that do not start a message continue the previous one (multi-line
messages). *Unparsed* lines are the ones before the first message, header
lines with an impossible date/time, and lines shaped like a timestamp that
do not match the detected style; the last two are still kept as text.

Never log message text, senders or file names (PHI).
"""

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    PANAMA_TZ = ZoneInfo("America/Panama")
except ZoneInfoNotFoundError:  # slim image without tzdata: Panama is a fixed UTC-5
    PANAMA_TZ = timezone(timedelta(hours=-5), "America/Panama")

# Directional marks, embeddings and the BOM WhatsApp sprinkles into exports.
_INVISIBLE = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"
_STRIP_INVISIBLE = str.maketrans("", "", _INVISIBLE)
# Narrow no-break space (before "p. m."), no-break space, thin space.
_SPACES = str.maketrans({"\u202f": " ", "\u00a0": " ", "\u2009": " "})

_DATE = r"(?P<d1>\d{1,4})(?P<sep>[/.\-])(?P<d2>\d{1,2})(?P=sep)(?P<d3>\d{1,4})"
_TIME = (
    r"(?P<h>\d{1,2})[:.](?P<mi>\d{2})(?:[:.](?P<s>\d{2}))?"
    r"(?:\s*(?P<ampm>[aApP])\.?\s?[mM]\.?)?"
)
_HEADERS = {
    "bracketed": re.compile(rf"^\[{_DATE},?\s+{_TIME}\]\s?(?P<rest>.*)$"),
    "dash": re.compile(rf"^{_DATE},?\s+{_TIME}\s+[-–]\s(?P<rest>.*)$"),
}
# Anything shaped like "[date, time" at a line start: a header that failed.
_TIMESTAMP_LIKE = re.compile(r"^\[?\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4},?\s+\d{1,2}[:.]\d{2}")
DETECT_LINES = 50

_SENDER = re.compile(r"^(?P<sender>[^:\n]{1,80}?):(?:\s(?P<text>.*))?$", re.DOTALL)

_ATTACHED_SUFFIX = re.compile(
    r"^(?P<file>.+?)\s+\((?:file attached|archivo adjunto|arquivo anexado|fichier joint)\)$",
    re.IGNORECASE,
)
# "<attached: 00000013-Informe.pdf>" (iOS), with the document descriptor or a
# caption before it, or (assumed for desktop exports) after it.
_ATTACHED_TAG = re.compile(
    r"^(?P<before>.*?)<(?:attached|adjunto|anexo|anexado|pièce jointe): (?P<file>[^<>]+)>"
    r"\s*(?P<after>.*)$",
    re.IGNORECASE,
)
# iOS / desktop document descriptor: "Informe.pdf • 2 páginas", "Notas.docx".
_DOC_DESCRIPTOR = re.compile(
    r"^(?P<name>[^<>•\n]*\S\.[A-Za-z0-9]{1,5})"
    r"(?P<pages>\s*•\s*\d+\s+(?:páginas?|paginas?|pages?|págs?\.?))?$",
    re.IGNORECASE,
)
# iOS per-export sequence prefix of attached files: "00000013-Informe.pdf".
_SEQUENCE_PREFIX = re.compile(r"^\d{8}-")
# "<Media omitted>" (Android), "<image omitted>", "<voice message omitted>",
# "<document omitted> name.pdf" (WhatsApp Web: a document keeps its name after
# the tag, an image/video its caption).
_OMITTED_TAG = re.compile(
    r"^<(?P<what>[^<>]+?)\s+(?:omitted|omitido|omitida|omitidos|oculta|omis)>\s*(?P<after>.*)$",
    re.IGNORECASE,
)
# iOS without media: "<U+200E>image omitted", "<U+200E>imagen omitida".
_OMITTED_WORD = re.compile(
    r"^(?:(?P<before>.*)\s)?(?P<what>image|imagen|audio|video|vídeo|document|documento|sticker|gif)"
    r"\s+(?:omitted|omitido|omitida)$",
    re.IGNORECASE,
)
_OMITTED_KIND = (
    ("image", ("image", "imagen", "foto", "photo", "sticker")),
    ("audio", ("audio", "voice", "voz")),
    ("video", ("video", "vídeo", "gif")),
)
_DOCUMENT_WORDS = ("document", "documento")
# WhatsApp Web prefixes forwarded messages: "[forwarded] <image omitted>".
_FORWARDED = re.compile(r"^\[(?:forwarded|reenviado|reenviada)\]\s*", re.IGNORECASE)

MEDIA_KINDS = ("audio", "image", "pdf", "video", "other")
_EXTENSIONS = {
    "audio": {"opus", "ogg", "m4a", "mp3", "aac", "amr", "wav", "oga", "weba"},
    "image": {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp"},
    "pdf": {"pdf"},
    "video": {"mp4", "mov", "3gp", "avi", "mkv", "webm", "m4v"},
}


def media_kind(filename: str) -> str:
    """``audio``/``image``/``pdf``/``video``/``other`` from the file extension."""
    suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
    for kind, extensions in _EXTENSIONS.items():
        if suffix in extensions:
            return kind
    return "other"


def clean(value: str) -> str:
    """Remove WhatsApp's invisible marks and unify odd spaces (not newlines)."""
    return value.translate(_STRIP_INVISIBLE).translate(_SPACES)


def normalize_text(value: str) -> str:
    """Comparison form of a message text: NFC, whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFC", clean(value)).split())


def dedupe_hash(
    sent_at: datetime, sender: str | None, text: str, has_media: bool, occurrence=1
) -> str:
    """SHA-256 of sent_at (Panama wall clock, **truncated to the minute**) +
    sender + normalized text + a "has media" flag. ``occurrence`` > 1 marks
    the n-th identical message in one export (same minute, sender, text and
    media flag), so it is not collapsed into the first.

    Minute precision: phone exports (the stored baseline) carry minutes only,
    WhatsApp Web exports add seconds; truncating makes both hash a message
    equally (identical messages within one minute are then told apart by
    ``occurrence``, in chat order, in both). ``sent_at`` itself is stored
    with whatever seconds the export had.

    Neither the media file name nor its kind is hashed: the same message must
    hash the same in an export *without* media (``<image omitted>``, Android's
    generic ``<Media omitted>``, a document's bare name) and one *with* media
    (``00000012-PHOTO-….jpg``, whose sequence prefix also changes between
    exports), so the importer can attach the file to the message it already
    stored. Two media messages sent in the same minute without captions are
    told apart by ``occurrence``, which is stable as long as both exports list
    them in the same order (they do: the chat order is the send order).
    """
    parts = [
        sent_at.astimezone(PANAMA_TZ).replace(second=0, microsecond=0, tzinfo=None).isoformat(),
        normalize_text(sender or ""),
        normalize_text(text),
        "media" if has_media else "",
    ]
    if occurrence > 1:
        parts.append(f"#{occurrence}")
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class WaMessage:
    sent_at: datetime  # aware, America/Panama
    sender: str | None  # None for system messages without an author
    text: str  # caption for media; "" when there is none
    media_filename: str | None  # base name as written in the chat
    media_kind: str | None  # one of MEDIA_KINDS; None when no media
    media_omitted: bool  # exported "without media" (a document may still have its name)
    forwarded: bool  # WhatsApp Web "[forwarded]" prefix (removed from text)
    is_system: bool
    line_no: int  # 1-based line where the message starts
    dedupe_hash: str
    media_member: str | None = None  # zip member holding the file (set by export)


@dataclass
class ChatParse:
    messages: list[WaMessage] = field(default_factory=list)
    style: str | None = None  # "bracketed" (iOS / WhatsApp Web) or "dash" (Android)
    date_order: str | None = None  # "dmy", "mdy" or "ymd"
    clock: str | None = None  # "12h" or "24h"
    line_count: int = 0
    unparsed_lines: list[int] = field(default_factory=list)  # 1-based numbers only

    def kind_counts(self) -> Counter:
        return Counter(m.media_kind for m in self.messages if m.media_kind)


@dataclass
class _Header:
    index: int
    parts: tuple[int, int, int]
    widths: tuple[int, int, int]
    hour: int
    minute: int
    second: int
    ampm: str | None
    rest: str


def _detect_style(lines: list[str]) -> str | None:
    def tally(chunk):
        counts = Counter()
        for line in chunk:
            for style, pattern in _HEADERS.items():
                if pattern.match(line):
                    counts[style] += 1
        return counts

    first = [line for line in lines if line.strip()][:DETECT_LINES]
    counts = tally(first) or tally(lines)
    return counts.most_common(1)[0][0] if counts else None


def _to_date(header: _Header, order: str) -> tuple[int, int, int] | None:
    a, b, c = header.parts
    if order == "ymd":
        year, month, day = a, b, c
        year_width = header.widths[0]
    elif order == "dmy":
        day, month, year = a, b, c
        year_width = header.widths[2]
    else:
        month, day, year = a, b, c
        year_width = header.widths[2]
    if year_width == 2:
        year += 2000
    elif year_width != 4:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def _to_datetime(header: _Header, order: str) -> datetime | None:
    ymd = _to_date(header, order)
    if ymd is None:
        return None
    hour = header.hour
    if header.ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if header.ampm == "p" else 0)
    try:
        return datetime(*ymd, hour, header.minute, header.second, tzinfo=PANAMA_TZ)
    except ValueError:
        return None


DATE_ORDERS = ("dmy", "mdy", "ymd")
# A reading that puts messages more than this before the chat's last stored
# message (``floor``) is suspect: a sync export overlaps what is stored by
# a little, not by weeks. The floor only counts when it is recent.
FLOOR_SLACK = timedelta(days=2)
FLOOR_RECENT = timedelta(days=45)


def _choose_order(
    headers: list[_Header],
    reference: datetime | None = None,
    *,
    hint: str | None = None,
    floor: datetime | None = None,
) -> str:
    """Day/month order of the whole file. Lowest key wins:

    1. ``invalid``: dates impossible in that order (month > 12, …);
    2. ``backwards``: timestamps that go backwards;
    then, only for a file both orders read equally well (a short sync
    export such as 10/9 - 10/10, where every date reads both ways):
    3. ``hint``: the order the uploader saw (the sync extension's
       ``date_order``), when given;
    4. ``early``: messages more than ``FLOOR_SLACK`` before ``floor`` (the
       newest stored message of the chat, when within ``FLOOR_RECENT`` of
       ``reference``): a daily sync does not reach weeks back;
    5. ``future``: messages over a day after ``reference``;
    6. ``distance``: how far the last message is from ``reference``;
    7. ``span``: the shorter range (a one-day sync is not a 30-day jump:
       10/9 - 10/10 is 9-10 Oct, not 10 Sep - 10 Oct);
    8. day/month (the Spanish locale).
    """
    if headers and all(h.widths[0] == 4 for h in headers):
        return "ymd"
    if floor is not None and reference is not None and reference - floor > FLOOR_RECENT:
        floor = None
    scores = []
    for rank, order in enumerate(("dmy", "mdy")):
        stamps = [_to_datetime(h, order) for h in headers]
        invalid = sum(s is None for s in stamps)
        valid = [s for s in stamps if s is not None]
        backwards = sum(1 for x, y in zip(valid, valid[1:], strict=False) if y < x)
        mismatch = int(hint in DATE_ORDERS and hint != order)
        early = 0
        if floor is not None:
            early = sum(1 for s in valid if s < floor - FLOOR_SLACK)
        future = distance = 0.0
        if reference is not None and valid:
            limit = reference + timedelta(days=1)
            future = sum(1 for s in valid if s > limit)
            distance = abs((reference - max(valid)).total_seconds())
        span = (max(valid) - min(valid)).total_seconds() if valid else 0.0
        key = (invalid, backwards, mismatch, early, future, distance, span, rank)
        scores.append((key, order))
    return min(scores)[1]


def _omitted_kind(what: str) -> str:
    words = what.casefold()
    for kind, needles in _OMITTED_KIND:
        if any(needle in words for needle in needles):
            return kind
    return "other"


def _basename(name: str) -> str:
    return PurePosixPath(name.strip().replace("\\", "/")).name


@dataclass
class _Media:
    filename: str | None = None
    kind: str | None = None
    omitted: bool = False
    forwarded: bool = False


def _strip_descriptor(text: str, filename: str) -> str:
    """Drop a document descriptor ("Informe.pdf • 2 páginas") that only repeats
    the attached file, so the text matches the export without media."""
    descriptor = _DOC_DESCRIPTOR.match(text)
    if descriptor is None:
        return text
    name = _basename(descriptor["name"]).casefold()
    if descriptor["pages"] or name == _SEQUENCE_PREFIX.sub("", filename).casefold():
        return ""
    return text


def _split_media(text: str) -> tuple[_Media, str]:
    """Media reference and caption from a message's text."""
    first, newline, tail = text.partition("\n")
    first_clean = clean(first).strip()
    media = _Media()
    forwarded = _FORWARDED.match(first_clean)
    if forwarded:
        media.forwarded = True
        first_clean = first_clean[forwarded.end() :]
        text = first_clean + newline + tail

    if match := _ATTACHED_SUFFIX.match(first_clean):
        media.filename = _basename(match["file"])
        media.kind = media_kind(media.filename)
        return media, tail
    if match := _ATTACHED_TAG.match(first_clean):
        media.filename = _basename(match["file"])
        media.kind = media_kind(media.filename)
        parts = [
            _strip_descriptor(match["before"].strip(), media.filename),
            _strip_descriptor(match["after"].strip(), media.filename),
            tail,
        ]
        return media, "\n".join(part for part in parts if part)
    if match := _OMITTED_TAG.match(first_clean):
        media.omitted = True
        what, after = match["what"], match["after"].strip()
        if any(word in what.casefold() for word in _DOCUMENT_WORDS) and (
            descriptor := _DOC_DESCRIPTOR.match(after)
        ):
            media.filename = _basename(descriptor["name"])
            media.kind = media_kind(media.filename)
            return media, tail
        media.kind = _omitted_kind(what)
        return media, "\n".join(part for part in (after, tail) if part)
    match = _OMITTED_WORD.match(first_clean)
    if match and (first.startswith("\u200e") or first_clean.casefold().endswith(" omitted")):
        media.omitted = True
        before = (match["before"] or "").strip()
        descriptor = _DOC_DESCRIPTOR.match(before)
        if any(word in match["what"].casefold() for word in _DOCUMENT_WORDS) and descriptor:
            # iOS: "Informe.pdf • 2 páginas documento omitido".
            media.filename = _basename(descriptor["name"])
            media.kind = media_kind(media.filename)
        else:
            media.kind = _omitted_kind(match["what"])
        return media, tail
    return media, text


def _build(header: _Header, continuation: list[str], sent_at: datetime) -> dict:
    rest = header.rest
    sender: str | None = None
    is_system = False
    body = rest
    match = _SENDER.match(rest)
    if match and '"' not in match["sender"]:
        sender = clean(match["sender"]).strip() or None
        body = match["text"] or ""
        # iOS marks system lines (and only those, among non-media) with U+200E.
        if body.startswith("\u200e"):
            is_system = True
    else:
        is_system = True
    body = "\n".join([body, *continuation]) if continuation else body
    media, caption = _split_media(body)
    if media.filename or media.omitted:
        is_system = False
    text = "\n".join(clean(line).rstrip() for line in caption.split("\n")).strip()
    return {
        "sent_at": sent_at,
        "sender": sender,
        "text": text,
        "media_filename": media.filename,
        "media_kind": media.kind,
        "media_omitted": media.omitted,
        "forwarded": media.forwarded,
        "is_system": is_system,
        "line_no": header.index + 1,
    }


def parse_chat_text(
    content: str,
    *,
    reference: datetime | None = None,
    date_order: str | None = None,
    floor: datetime | None = None,
) -> ChatParse:
    """Parse a whole chat ``.txt`` (already decoded) into messages.

    ``reference`` (timezone-aware, normally the import's creation time),
    ``date_order`` (the uploader's order, ``dmy``/``mdy``/``ymd``) and
    ``floor`` (the newest stored message's time) only break a day/month tie
    the file itself cannot decide; see ``_choose_order``. The caller passes
    them in: the parser never reads the database.
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    result = ChatParse(line_count=len(lines))
    stripped = [clean(line.lstrip(_INVISIBLE)) for line in lines]
    result.style = _detect_style(stripped)
    if result.style is None:
        result.unparsed_lines = [i + 1 for i, line in enumerate(lines) if line.strip()]
        return result
    pattern = _HEADERS[result.style]

    headers: list[_Header] = []
    header_at: dict[int, _Header] = {}
    for index, line in enumerate(stripped):
        match = pattern.match(line)
        if not match:
            continue
        # Keep the original rest (with U+200E) for iOS system detection.
        raw = lines[index].lstrip(_INVISIBLE).translate(_SPACES)
        raw_match = pattern.match(raw)
        rest = raw_match["rest"] if raw_match else match["rest"]
        header = _Header(
            index=index,
            parts=(int(match["d1"]), int(match["d2"]), int(match["d3"])),
            widths=(len(match["d1"]), len(match["d2"]), len(match["d3"])),
            hour=int(match["h"]),
            minute=int(match["mi"]),
            second=int(match["s"] or 0),
            ampm=match["ampm"].casefold() if match["ampm"] else None,
            rest=rest,
        )
        headers.append(header)
        header_at[index] = header
    result.date_order = _choose_order(headers, reference, hint=date_order, floor=floor)
    result.clock = "12h" if any(h.ampm for h in headers) else "24h"

    pending: tuple[_Header, datetime] | None = None
    continuation: list[str] = []
    built: list[dict] = []

    def flush():
        if pending is not None:
            built.append(_build(pending[0], continuation, pending[1]))

    for index, line in enumerate(lines):
        header = header_at.get(index)
        sent_at = _to_datetime(header, result.date_order) if header else None
        if header is not None and sent_at is not None:
            flush()
            pending, continuation = (header, sent_at), []
            continue
        if pending is None:
            if line.strip():
                result.unparsed_lines.append(index + 1)
            continue
        if header is not None or _TIMESTAMP_LIKE.match(stripped[index]):
            result.unparsed_lines.append(index + 1)
        continuation.append(line)
    flush()

    seen: Counter = Counter()
    for item in built:
        has_media = bool(item["media_kind"] or item["media_filename"] or item["media_omitted"])
        key = dedupe_hash(item["sent_at"], item["sender"], item["text"], has_media)
        seen[key] += 1
        item["dedupe_hash"] = (
            key
            if seen[key] == 1
            else dedupe_hash(item["sent_at"], item["sender"], item["text"], has_media, seen[key])
        )
        result.messages.append(WaMessage(**item))
    return result
