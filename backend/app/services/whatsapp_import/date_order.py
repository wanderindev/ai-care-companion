"""Re-date an import read in the wrong day/month order (#117, operator CLI
``python -m scripts.whatsapp_imports fix-date-order``).

A short export whose every date reads both ways (10/9 - 10/10) could be read
as day/month when it was month/day: its messages were stored a month early,
mostly as *new* copies of messages already stored on the right day. For
every message **first stored by that import** whose date changes when day
and month are swapped:

- its ``dedupe_hash`` is recomputed on the swapped date (same sender, text,
  has-media flag and occurrence number, found by matching the stored hash);
- a hash some other message already has → the row is a duplicate: deleted;
- otherwise ``sent_at`` (same wall-clock time) and ``dedupe_hash`` are
  updated.

Messages with a file, a transcript, a classification, or a lab report / lab
link pointing at them are refused (listed by id): the operator decides.
Messages whose stored hash does not match a recomputation, or whose new hash
belongs to another message being changed, are skipped and counted. The
swapped and original days are removed from ``stats.analysis.days_done`` so
the analysis can look at them again. Every change is audited. Counts and ids
only: never message text, senders or file names.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.lab import LabLink, LabReport
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import audit
from app.services.whatsapp import PANAMA_TZ, dedupe_hash

SWAPPABLE = ("dmy", "mdy")
MAX_OCCURRENCE = 50


class FixError(Exception):
    """A refusal to run at all (unknown import, unsupported orders)."""


@dataclass
class FixReport:
    candidates: int = 0  # messages of the import
    unchanged: int = 0  # same date in both orders (day == month, or day > 12)
    redated: int = 0
    deleted_duplicates: int = 0
    refused_ids: list[int] = field(default_factory=list)  # file/transcript/...
    hash_mismatch: int = 0
    conflicts: int = 0
    days_cleared: int = 0
    refused_reasons: Counter = field(default_factory=Counter)


def swap_day_month(day: date) -> date | None:
    """``day`` read in the other order (day and month swapped); None when that
    is impossible or the same date."""
    if day.day > 12 or day.day == day.month:
        return None
    try:
        return date(day.year, day.day, day.month)
    except ValueError:
        return None


def _has_media(row: WhatsappMessage) -> bool:
    return row.media_kind is not None or row.media_filename is not None


def _occurrence(row: WhatsappMessage) -> int | None:
    """The occurrence number that reproduces the stored hash, if any."""
    for occurrence in range(1, MAX_OCCURRENCE + 1):
        if (
            dedupe_hash(row.sent_at, row.sender, row.text, _has_media(row), occurrence)
            == row.dedupe_hash
        ):
            return occurrence
    return None


def _refusals(db: DbSession, rows: list[WhatsappMessage]) -> dict[int, str]:
    ids = [row.id for row in rows]
    reasons: dict[int, str] = {}
    for row in rows:
        if row.attachment_id is not None or row.media_import_id is not None or row.wa_key:
            reasons[row.id] = "file"
        elif row.transcript is not None:
            reasons[row.id] = "transcript"
        elif row.classification is not None:
            reasons[row.id] = "classification"
    for (message_id,) in db.execute(
        select(LabReport.source_message_id).where(LabReport.source_message_id.in_(ids))
    ):
        reasons.setdefault(message_id, "lab_report")
    for (message_id,) in db.execute(select(LabLink.message_id).where(LabLink.message_id.in_(ids))):
        reasons.setdefault(message_id, "lab_link")
    return reasons


def fix_date_order(
    db: DbSession,
    import_id: int,
    *,
    from_order: str,
    to_order: str,
    dry_run: bool,
    user_id: int | None = None,
) -> FixReport:
    """Re-date import ``import_id``'s messages from ``from_order`` to
    ``to_order`` (``dmy`` <-> ``mdy``). Commits unless ``dry_run`` (then
    rolls back)."""
    if from_order not in SWAPPABLE or to_order not in SWAPPABLE or from_order == to_order:
        raise FixError("Only dmy -> mdy or mdy -> dmy is supported.")
    imp = db.get(WhatsappImport, import_id, with_for_update=True)
    if imp is None:
        raise FixError(f"Import {import_id} not found.")
    # Imports since #117 record the order they were read in; a fixed import
    # records the new one, so a second run cannot swap the dates back.
    recorded = (imp.stats or {}).get("date_order")
    if recorded == to_order:
        raise FixError(f"Import {import_id} is already in {to_order} order.")
    if recorded is not None and recorded != from_order:
        raise FixError(f"Import {import_id} was read as {recorded}, not {from_order}.")
    report = FixReport()
    rows = list(
        db.scalars(
            select(WhatsappMessage)
            .where(WhatsappMessage.import_id == import_id)
            .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
            .with_for_update()
        )
    )
    report.candidates = len(rows)

    # (row, new sent_at, new hash) for every row whose date changes.
    plans: list[tuple[WhatsappMessage, datetime, str]] = []
    changing = [row for row in rows if swap_day_month(row.sent_at.astimezone(PANAMA_TZ).date())]
    report.unchanged = len(rows) - len(changing)
    refused = _refusals(db, changing) if changing else {}
    for row in changing:
        if row.id in refused:
            report.refused_ids.append(row.id)
            report.refused_reasons[refused[row.id]] += 1
            continue
        occurrence = _occurrence(row)
        if occurrence is None:
            report.hash_mismatch += 1
            continue
        local = row.sent_at.astimezone(PANAMA_TZ)
        new_day = swap_day_month(local.date())
        new_sent_at = local.replace(year=new_day.year, month=new_day.month, day=new_day.day)
        new_hash = dedupe_hash(new_sent_at, row.sender, row.text, _has_media(row), occurrence)
        plans.append((row, new_sent_at, new_hash))

    changing_ids = {row.id for row, _, _ in plans}
    owners = (
        dict(
            db.execute(
                select(WhatsappMessage.dedupe_hash, WhatsappMessage.id).where(
                    WhatsappMessage.dedupe_hash.in_([h for _, _, h in plans])
                )
            ).all()
        )
        if plans
        else {}
    )

    days: set[str] = set()
    for row, new_sent_at, new_hash in plans:
        owner = owners.get(new_hash)
        if owner is not None and owner in changing_ids:
            report.conflicts += 1
            continue
        days.add(row.sent_at.astimezone(PANAMA_TZ).date().isoformat())
        days.add(new_sent_at.date().isoformat())
        if owner is not None:
            audit.record(
                db,
                user_id=user_id,
                action=audit.ACTION_DELETE,
                entity="whatsapp_message",
                entity_id=row.id,
                before=audit.snapshot(row, bookkeeping=True),
            )
            db.delete(row)
            report.deleted_duplicates += 1
        else:
            before = audit.snapshot(row)
            row.sent_at = new_sent_at
            row.dedupe_hash = new_hash
            db.flush()
            audit.record(
                db,
                user_id=user_id,
                action=audit.ACTION_UPDATE,
                entity="whatsapp_message",
                entity_id=row.id,
                before=before,
                after=audit.snapshot(row),
            )
            report.redated += 1
    db.flush()

    before = audit.snapshot(imp)
    stats = dict(imp.stats or {})
    analysis = stats.get("analysis")
    done = analysis.get("days_done") if isinstance(analysis, dict) else None
    if isinstance(done, list) and days & set(done):
        report.days_cleared = len(days & set(done))
        stats["analysis"] = {**analysis, "days_done": [d for d in done if d not in days]}
    stats["date_order"] = to_order
    stats["date_order_fixed_from"] = from_order
    imp.stats = stats
    flag_modified(imp, "stats")
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity="whatsapp_import",
        entity_id=imp.id,
        before=before,
        after=audit.snapshot(imp),
    )

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return report
