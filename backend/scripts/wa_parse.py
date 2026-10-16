"""Check how a WhatsApp export zip parses, without showing its content.

Run from ``backend/``:

    python -m scripts.wa_parse <export.zip> --stats [--reference 2026-10-09T15:00]

Prints only counts: detected format, messages (with/without author), media
by kind, media files present/missing in the zip, the date range and the
number of unparsed lines (their line numbers, never their text). Nothing is
written and no database is touched, so the operator can validate a real
export locally and share the output. Never add message text, sender names or
file names to this output (PHI).

A file whose dates all read both ways (a short sync export of 10/8-10/9)
gets its day/month order from a reference time, as the import does with the
import's creation time: now by default, or ``--reference`` (ISO date/time,
naive = Panamá) to reproduce an import made earlier.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from app.services.whatsapp import MEDIA_KINDS, PANAMA_TZ, WaExportError, parse_export

MAX_LINE_NUMBERS = 20


def _reference(value: str) -> datetime:
    """``--reference``: an ISO date or date/time; naive = Panamá wall clock."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected an ISO date/time, e.g. 2026-10-09T15:00"
        ) from exc
    return moment.replace(tzinfo=PANAMA_TZ) if moment.tzinfo is None else moment


def _stats(path: str, reference: datetime | None = None) -> int:
    if not Path(path).is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 1
    reference = reference or datetime.now(PANAMA_TZ)
    try:
        report = parse_export(path, reference=reference)
    except WaExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    chat = report.chat
    messages = chat.messages
    system = sum(m.is_system for m in messages)
    print(
        f"format: {chat.style or 'unknown'}, date order {chat.date_order or '-'}, "
        f"clock {chat.clock or '-'}"
    )
    print(f"lines: {chat.line_count}")
    print(
        f"messages: {len(messages)} ({len(messages) - system} from members, {system} system), "
        f"{len({m.sender for m in messages if m.sender and not m.is_system})} distinct senders"
    )
    kinds = chat.kind_counts()
    attached = sum(1 for m in messages if m.media_kind and not m.media_omitted)
    omitted = sum(1 for m in messages if m.media_omitted)
    named = sum(1 for m in messages if m.media_omitted and m.media_filename)
    by_kind = ", ".join(f"{kind} {kinds.get(kind, 0)}" for kind in MEDIA_KINDS)
    print(
        f"media: {attached} attached, {omitted} omitted ({named} with a file name); "
        f"by kind: {by_kind}"
    )
    print(
        f"zip: {report.member_count} members, {report.media_files} media files, "
        f"{report.missing_media} referenced but missing, {report.unreferenced_media} "
        f"unreferenced, {report.unsafe_members} unsafe names ignored"
    )
    if messages:
        first = min(m.sent_at for m in messages)
        last = max(m.sent_at for m in messages)
        print(f"date range: {first:%Y-%m-%d} .. {last:%Y-%m-%d}")
    else:
        print("date range: -")
    unparsed = chat.unparsed_lines
    shown = ", ".join(map(str, unparsed[:MAX_LINE_NUMBERS]))
    more = " …" if len(unparsed) > MAX_LINE_NUMBERS else ""
    before = sum(1 for n in unparsed if not messages or n < messages[0].line_no)
    detail = f" ({before} before the first message; line numbers: {shown}{more})" if shown else ""
    print(f"unparsed lines: {len(unparsed)}{detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.wa_parse",
        description="Parse a WhatsApp export zip and print counts only (no message content).",
    )
    parser.add_argument("zip", help="WhatsApp export (.zip with the chat .txt and media)")
    parser.add_argument(
        "--stats", action="store_true", help="print counts by kind, date range, unparsed lines"
    )
    parser.add_argument(
        "--reference",
        type=_reference,
        help="time that breaks a day/month tie, as the import's creation time does "
        "(ISO, naive = Panamá; default: now)",
    )
    args = parser.parse_args(argv)
    if not args.stats:
        parser.error("nothing to do: pass --stats")
    return _stats(args.zip, args.reference)


if __name__ == "__main__":
    sys.exit(main())
