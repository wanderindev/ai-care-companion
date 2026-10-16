"""Bitácora entries: create/update/delete with audit, filters and search.

``create_entry`` is the one write path for new entries: the API calls it for
manual entries and the DOCX importer (#7) calls it with
``origin="import_docx"``, ``review_status="pending_review"`` and the source
data in ``origin_ref``. The API never lets a client choose ``origin`` or
``origin_ref``.

Search (``search_condition``): the query text is split into words, each word
becomes a prefix term (``neumo`` finds "Neumonía") and all must match. Both
sides go through ``immutable_unaccent`` and the ``spanish`` config, so accents
and case do not matter. Besides the entry's own text (title, source label,
indications, family notes) a word also matches the entry's doctor by name or
alias.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, literal_column, or_, select
from sqlalchemy.orm import Session as DbSession

from app.models.attachment import Attachment
from app.models.doctor import Doctor
from app.models.log_entry import ORIGIN_MANUAL, ORIGINS, REVIEW_OK, REVIEW_PENDING, LogEntry
from app.models.patient import Patient
from app.services import appointments, audit
from app.services.attachments import link_attachments, unlink_attachments

OWNER_TYPE = "log_entry"
ENTITY = "log_entry"

# Entries are past events; one day of slack covers time zones (Panamá is UTC-5,
# the server UTC) and a visit written down late at night.
FUTURE_SKEW = timedelta(days=1)
MIN_DATE = date(1900, 1, 1)
MAX_QUERY_WORDS = 10

# Fields a caller may set; everything else (origin, bookkeeping) is managed here.
EDITABLE_FIELDS = frozenset(
    {
        "patient_id",
        "entry_date",
        "entry_time",
        "doctor_id",
        "source_label",
        "entry_type",
        "channel",
        "title",
        "indications",
        "family_notes",
        "review_status",
    }
)

_WORD = re.compile(r"[^\W_]+")


class LogEntryError(ValueError):
    """Invalid entry data; the message is Spanish and safe to show."""


HEADER_REQUIRED = "Indica el médico, la fuente o un título."


def validate(db: DbSession, entry: LogEntry) -> None:
    """Rules for every write path (API and importers). Raises ``LogEntryError``."""
    if entry.origin not in ORIGINS:
        raise LogEntryError("Origen no válido.")
    if entry.patient_id is None or db.get(Patient, entry.patient_id) is None:
        raise LogEntryError("El paciente no existe.")
    if entry.doctor_id is not None and db.get(Doctor, entry.doctor_id) is None:
        raise LogEntryError("El médico no existe.")
    if entry.entry_date < MIN_DATE or entry.entry_date > date.today() + FUTURE_SKEW:
        raise LogEntryError("La fecha no es válida: la bitácora registra hechos pasados.")
    # A card needs a header: the doctor, the source as written, or a title.
    if entry.doctor_id is None and not entry.source_label and not entry.title:
        raise LogEntryError(HEADER_REQUIRED)


def create_entry(
    db: DbSession,
    data: dict[str, Any],
    *,
    user_id: int | None,
    origin: str = ORIGIN_MANUAL,
    origin_ref: dict[str, Any] | None = None,
    attachment_ids: Sequence[int] = (),
    next_appointment: dict[str, Any] | None = None,
) -> LogEntry:
    """Validate, insert, link attachments and audit. The caller commits.

    ``data`` holds model fields from ``EDITABLE_FIELDS`` (others raise
    ``LogEntryError``); ``review_status`` defaults to ``ok``.
    ``next_appointment`` ("Próxima cita") creates the entry's appointment (see
    ``appointments.sync_next_appointment``). Raises ``LogEntryError``,
    ``AppointmentError`` or ``AttachmentLinkError``; the caller rolls back.
    """
    unknown = set(data) - EDITABLE_FIELDS
    if unknown:
        raise LogEntryError(f"Campos no permitidos: {', '.join(sorted(unknown))}.")
    entry = LogEntry(
        **{"review_status": REVIEW_OK, **data},
        origin=origin,
        origin_ref=origin_ref,
        created_by=user_id,
        updated_by=user_id,
    )
    validate(db, entry)
    db.add(entry)
    db.flush()
    if attachment_ids:
        link_attachments(
            db, list(attachment_ids), owner_type=OWNER_TYPE, owner_id=entry.id, user_id=user_id
        )
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity=ENTITY,
        entity_id=entry.id,
        after=audit.snapshot(entry),
    )
    if next_appointment is not None:
        appointments.sync_next_appointment(db, entry, next_appointment, user_id=user_id)
    return entry


# ``update_entry(next_appointment=UNCHANGED)``: leave the entry's appointment alone.
UNCHANGED: Any = object()


def update_entry(
    db: DbSession,
    entry: LogEntry,
    changes: dict[str, Any],
    *,
    user_id: int | None,
    attachment_ids: Sequence[int] | None = None,
    next_appointment: dict[str, Any] | None = UNCHANGED,
) -> LogEntry:
    """Apply ``changes``; ``attachment_ids`` (when not None) becomes the full set
    of linked files. Audited only when something changed. The caller commits.

    ``next_appointment``: a dict creates/updates the entry's appointment,
    ``None`` cancels it, ``UNCHANGED`` (default) leaves it, except that a
    ``programada`` one follows the entry to another patient."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise LogEntryError(f"Campos no permitidos: {', '.join(sorted(unknown))}.")
    before = audit.snapshot(entry)
    for field, value in changes.items():
        setattr(entry, field, value)
    validate(db, entry)
    touched = False
    if attachment_ids is not None:
        keep = list(dict.fromkeys(attachment_ids))
        removed = unlink_attachments(
            db, owner_type=OWNER_TYPE, owner_id=entry.id, keep_ids=keep, user_id=user_id
        )
        added = 0
        if keep:
            already = set(
                db.scalars(
                    select(Attachment.id).where(
                        Attachment.owner_type == OWNER_TYPE, Attachment.owner_id == entry.id
                    )
                )
            )
            added = len(set(keep) - already)
            link_attachments(db, keep, owner_type=OWNER_TYPE, owner_id=entry.id, user_id=user_id)
        touched = bool(removed) or added > 0
    after = audit.snapshot(entry)
    if after != before:
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_UPDATE,
            entity=ENTITY,
            entity_id=entry.id,
            before=before,
            after=after,
        )
        touched = True
    if touched:
        entry.updated_by = user_id
        entry.updated_at = func.now()
    db.flush()
    if next_appointment is UNCHANGED:
        if "patient_id" in changes:
            appointments.follow_entry_patient(db, entry, user_id=user_id)
    else:
        appointments.sync_next_appointment(db, entry, next_appointment, user_id=user_id)
    return entry


def delete_entry(db: DbSession, entry: LogEntry, *, user_id: int | None) -> None:
    """Hard delete. The audit row keeps the full entry (incl. ``origin_ref`` and
    who/when created and last edited it) and the ids of its files; the files
    are unlinked, not deleted. The caller commits."""
    before = audit.snapshot(entry, bookkeeping=True)
    removed = unlink_attachments(db, owner_type=OWNER_TYPE, owner_id=entry.id, user_id=user_id)
    before["attachment_ids"] = [row.id for row in removed]
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_DELETE,
        entity=ENTITY,
        entity_id=entry.id,
        before=before,
    )
    db.delete(entry)
    db.flush()


def mark_reviewed(
    db: DbSession, conds: Sequence[ColumnElement[bool]], *, user_id: int | None
) -> list[int]:
    """Bulk «Marcar como revisadas» (#58): every ``pending_review`` entry matching
    ``conds`` (``filter_conditions`` or an id list) becomes ``ok``. The rows are
    locked first, then each goes through ``update_entry``, so every change gets
    its own audit row. Returns the ids changed. The caller commits; on a
    ``LogEntryError`` (an entry that no longer validates) it rolls back."""
    stmt = (
        select(LogEntry)
        .where(LogEntry.review_status == REVIEW_PENDING, *conds)
        .order_by(LogEntry.id)
        .with_for_update(of=LogEntry)
    )
    changed: list[int] = []
    for entry in db.scalars(stmt).all():
        try:
            update_entry(db, entry, {"review_status": REVIEW_OK}, user_id=user_id)
        except LogEntryError as exc:
            raise LogEntryError(f"Entrada del {entry.entry_date:%d/%m/%Y}: {exc}") from None
        changed.append(entry.id)
    return changed


def attachments_by_entry(db: DbSession, entry_ids: Iterable[int]) -> dict[int, list[Attachment]]:
    ids = list(entry_ids)
    result: dict[int, list[Attachment]] = {i: [] for i in ids}
    if not ids:
        return result
    rows = db.scalars(
        select(Attachment)
        .where(Attachment.owner_type == OWNER_TYPE, Attachment.owner_id.in_(ids))
        .order_by(Attachment.id)
    )
    for row in rows:
        result[row.owner_id].append(row)
    return result


# ── Listing ──────────────────────────────────────────────────────────────────


def tsquery_text(q: str | None) -> str | None:
    """``to_tsquery`` input for *already unaccented* text: every word as a
    prefix term, ANDed.

    Only letters and digits survive, so nothing the user types can be tsquery
    syntax. Unaccent first (``search_condition`` does): it can expand a
    character into syntax (``⑴`` → ``(1)``, ``ŉ`` → ``'n``). None when there
    is no word to search for.
    """
    words = _WORD.findall(q or "")[:MAX_QUERY_WORDS]
    if not words:
        return None
    return " & ".join(f"{word}:*" for word in words)


def search_condition(db: DbSession, q: str) -> ColumnElement[bool] | None:
    # Unaccent in the database (the same function the vector uses), then split
    # into words, so the tsquery text never contains syntax characters.
    text = tsquery_text(db.scalar(select(func.immutable_unaccent(q))))
    if text is None:
        return None
    config = literal_column("'spanish'::regconfig")
    tsq = func.to_tsquery(config, text)
    # The doctor's words join the entry's own, so "perez neumonia" matches an
    # entry titled "Neumonía" written by Dra. Pérez. Computed per row: the
    # bitácora is a few thousand rows at most, so a scan is fine.
    doctor_text = Doctor.name + " " + func.array_to_string(Doctor.aliases, " ")
    doctor_vector = (
        select(func.to_tsvector(config, func.immutable_unaccent(doctor_text)))
        .where(Doctor.id == LogEntry.doctor_id)
        # Explicit: with sort=doctor the outer query also joins doctors, and
        # auto-correlation would then leave this subquery with no FROM.
        .correlate(LogEntry)
        .scalar_subquery()
    )
    combined = LogEntry.search_vector.op("||")(
        func.coalesce(doctor_vector, literal_column("''::tsvector"))
    )
    return or_(
        LogEntry.search_vector.op("@@")(tsq),
        and_(LogEntry.doctor_id.is_not(None), combined.op("@@")(tsq)),
    )


def filter_conditions(
    db: DbSession,
    *,
    patient_ids: Sequence[int] | None = None,
    doctor_ids: Sequence[int] | None = None,
    entry_types: Sequence[str] | None = None,
    channels: Sequence[str] | None = None,
    review_status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if patient_ids:
        conds.append(LogEntry.patient_id.in_(patient_ids))
    if doctor_ids:
        conds.append(LogEntry.doctor_id.in_(doctor_ids))
    if entry_types:
        conds.append(LogEntry.entry_type.in_(entry_types))
    if channels:
        conds.append(LogEntry.channel.in_(channels))
    if review_status:
        conds.append(LogEntry.review_status == review_status)
    if date_from:
        conds.append(LogEntry.entry_date >= date_from)
    if date_to:
        conds.append(LogEntry.entry_date <= date_to)
    if q:
        search = search_condition(db, q)
        if search is not None:
            conds.append(search)
    return conds


_TITLE_PREFIX = r"^\s*(dra?|doctora?|lic|licda|licdo|licenciad[ao])\.?\s+"


def _sort_key(column):
    """Name for sorting: leading title stripped, unaccented, lowercased."""
    stripped = func.regexp_replace(func.immutable_unaccent(column), _TITLE_PREFIX, "", "i")
    return func.lower(stripped)


def _ordered(stmt: Select, sort: str, order: str) -> Select:
    desc = order == "desc"

    def direction(col):
        return col.desc() if desc else col.asc()

    # Same-day entries: by time (untimed last), then by when they were written.
    by_date = [
        direction(LogEntry.entry_date),
        direction(LogEntry.entry_time).nulls_last(),
        direction(LogEntry.created_at),
        direction(LogEntry.id),
    ]
    newest_first = [
        LogEntry.entry_date.desc(),
        LogEntry.entry_time.desc().nulls_last(),
        LogEntry.created_at.desc(),
        LogEntry.id.desc(),
    ]
    if sort == "doctor":
        # By name without its title ("Dr. Bruno" after "Dra. Ana"), accents and
        # case ignored; entries without a doctor follow, by source label.
        stmt = stmt.outerjoin(Doctor, Doctor.id == LogEntry.doctor_id)
        return stmt.order_by(
            direction(_sort_key(Doctor.name)).nulls_last(),
            direction(_sort_key(LogEntry.source_label)).nulls_last(),
            *newest_first,
        )
    if sort == "type":
        return stmt.order_by(direction(LogEntry.entry_type), *newest_first)
    return stmt.order_by(*by_date)


def list_entries(
    db: DbSession,
    conds: list[ColumnElement[bool]],
    *,
    sort: str = "date",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[LogEntry], int]:
    where = and_(*conds) if conds else None
    count_stmt = select(func.count()).select_from(LogEntry)
    stmt = select(LogEntry)
    if where is not None:
        count_stmt = count_stmt.where(where)
        stmt = stmt.where(where)
    total = db.scalar(count_stmt) or 0
    stmt = _ordered(stmt, sort, order).limit(limit).offset(offset)
    return list(db.scalars(stmt)), total
