"""Medication lists (DESIGN §5.4, §6.4): the current list, full-list saves
that create a version, and version history.

Rules:

- **Current list** = items whose ``end_date`` is null or today or later
  (Panamá). Items with a future ``start_date`` are listed (the printed sheet
  is the plan); ``start_date`` tells the reader when they begin.
- **Save** (:func:`save_list`) is a full-list save in one transaction: the
  patient row is locked (``SELECT … FOR UPDATE``) so two siblings' saves are
  serialized, and ``expected_version`` must equal the current
  ``version_no`` (else :class:`MedsError` with status 409). Items with an
  ``id`` are updated, items without one are created, and current items
  missing from the request are **removed**: ``end_date`` = yesterday
  (Panamá), so they are off today's list but stay in the table. EVITAR rows
  missing from the request are deleted. ``sort_order`` is the item's
  position among the request's items of its slot.
- The new snapshot is diffed against the **previous version's snapshot**
  (not against the rows), so an item whose ``end_date`` passed since the last
  save shows up as suspended in the next version. A save whose diff is empty
  creates no version and changes nothing (the transaction is rolled back).
- Every row change and every version is audited in the same transaction.
- List-level fields (migration 0013, editable since #135): ``list_note``
  (e.g. a regimen table) and ``effective_date`` (the sheet's "Desde"; null
  means the date of ``changed_at``) are carried forward from the previous
  version unless the caller passes them (``KEEP`` is the default; an explicit
  ``None`` clears). Both are written to the snapshot (``list_note`` /
  ``effective_date`` keys) and are part of the diff, so changing only them
  creates a version. The previous side of the diff takes them from the
  version's **columns** (:func:`snapshot_with_list_fields`), which are
  authoritative over a snapshot key.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.orm import Session as DbSession

from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.models.medication import (
    REVIEW_OK,
    REVIEW_PENDING,
    SLOTS,
    MedAvoid,
    MedEvent,
    MedItem,
    MedListVersion,
)
from app.models.patient import Patient
from app.models.user import User
from app.services import audit, meds_diff
from app.services.appointments import today_local

ITEM_FIELDS = (
    "name",
    "strength",
    "quantity",
    "dose_note",
    "slot",
    "time",
    "days",
    "schedule_note",
    "instructions",
    "prescriber_id",
    "reason",
    "start_date",
    "end_date",
    "source_entry_id",
)
AVOID_FIELDS = ("substance", "reason", "source_entry_id")

PATIENT_NOT_FOUND = "Paciente no encontrado."
VERSION_NOT_FOUND = "Versión no encontrada."
CONFLICT = (
    "Alguien guardó la lista de medicamentos mientras la editabas. "
    "Recarga la lista y vuelve a aplicar tus cambios."
)


# save_list(list_note=KEEP, effective_date=KEEP): carry the previous version's value
KEEP: Any = object()


class MedsError(ValueError):
    """A save or read that cannot proceed; the message is Spanish and safe to
    show, ``status`` is the HTTP status the API returns."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class SaveOutcome:
    saved: bool
    auto_summary: str
    version: MedListVersion | None
    diff: dict[str, list[Any]] = field(default_factory=dict)
    # The two sides of the diff (plain dicts, usable after a rollback): the
    # LEDD before → after of the save (ticket #133).
    previous: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None


# ── Reads ────────────────────────────────────────────────────────────────────


def _slot_order():
    return case({slot: i for i, slot in enumerate(SLOTS)}, value=MedItem.slot)


def current_items(db: DbSession, patient_id: int, today: date | None = None) -> list[MedItem]:
    """The patient's current items in printed-sheet order (slot, sort_order)."""
    today = today or today_local()
    return list(
        db.scalars(
            select(MedItem)
            .where(
                MedItem.patient_id == patient_id,
                or_(MedItem.end_date.is_(None), MedItem.end_date >= today),
            )
            .order_by(_slot_order(), MedItem.sort_order, MedItem.id)
        )
    )


def current_avoid(db: DbSession, patient_id: int) -> list[MedAvoid]:
    return list(
        db.scalars(select(MedAvoid).where(MedAvoid.patient_id == patient_id).order_by(MedAvoid.id))
    )


def latest_version(db: DbSession, patient_id: int) -> MedListVersion | None:
    return db.scalars(
        select(MedListVersion)
        .where(MedListVersion.patient_id == patient_id)
        .order_by(MedListVersion.version_no.desc())
        .limit(1)
    ).first()


def get_patient(db: DbSession, patient_id: int, *, lock: bool = False) -> Patient:
    patient = db.get(Patient, patient_id, with_for_update=lock)
    if patient is None:
        raise MedsError(PATIENT_NOT_FOUND, status=404)
    return patient


# ── Snapshots ────────────────────────────────────────────────────────────────


def item_snapshot(item: MedItem, prescriber_name: str | None) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "strength": item.strength,
        "quantity": item.quantity,
        "dose_note": item.dose_note,
        "slot": item.slot,
        "time": item.time.strftime("%H:%M") if item.time else None,
        "days": list(item.days) if item.days else None,
        "schedule_note": item.schedule_note,
        "instructions": item.instructions,
        "prescriber_id": item.prescriber_id,
        "prescriber_name": prescriber_name,
        "reason": item.reason,
        "start_date": item.start_date.isoformat() if item.start_date else None,
        "end_date": item.end_date.isoformat() if item.end_date else None,
        "sort_order": item.sort_order,
        "source_entry_id": item.source_entry_id,
    }


def avoid_snapshot(row: MedAvoid) -> dict[str, Any]:
    return {
        "id": row.id,
        "substance": row.substance,
        "reason": row.reason,
        "source_entry_id": row.source_entry_id,
    }


def _doctor_names(db: DbSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return dict(db.execute(select(Doctor.id, Doctor.name).where(Doctor.id.in_(ids))).all())


def build_snapshot(db: DbSession, items: list[MedItem], avoid: list[MedAvoid]) -> dict[str, Any]:
    """The version snapshot (shape in :mod:`app.services.meds_diff`)."""
    names = _doctor_names(db, {i.prescriber_id for i in items if i.prescriber_id})
    return {
        "items": meds_diff.sort_items(
            [item_snapshot(i, names.get(i.prescriber_id)) for i in items]
        ),
        "avoid": [avoid_snapshot(r) for r in avoid],
    }


# ── Save ─────────────────────────────────────────────────────────────────────


def _check_refs(db: DbSession, patient_id: int, items: list[dict], avoid: list[dict]) -> None:
    doctor_ids = {i["prescriber_id"] for i in items if i.get("prescriber_id")}
    if doctor_ids - set(_doctor_names(db, doctor_ids)):
        raise MedsError("El médico no existe.")
    entry_ids = {r["source_entry_id"] for r in [*items, *avoid] if r.get("source_entry_id")}
    if entry_ids:
        found = dict(
            db.execute(
                select(LogEntry.id, LogEntry.patient_id).where(LogEntry.id.in_(entry_ids))
            ).all()
        )
        if set(found) != entry_ids or any(p != patient_id for p in found.values()):
            raise MedsError("La entrada de bitácora no existe o es de otro paciente.")


def _check_ids(rows: list[dict], existing: dict[int, Any], what: str) -> None:
    ids = [r["id"] for r in rows if r.get("id") is not None]
    if len(ids) != len(set(ids)):
        raise MedsError(f"Hay {what} repetidos en la lista.")
    if set(ids) - set(existing):
        raise MedsError(f"Uno de los {what} ya no está en la lista; recárgala.")


def _apply(
    db: DbSession,
    row: Any,
    values: dict[str, Any],
    fields: tuple[str, ...],
    *,
    entity: str,
    user_id: int | None,
) -> None:
    """Create (``row.id`` is None) or update ``row`` with ``values``, audited."""
    is_new = row.id is None
    before = None if is_new else audit.snapshot(row)
    for name in fields:
        if name in values:
            setattr(row, name, values[name])
    if is_new:
        row.created_by = user_id
        db.add(row)
    db.flush()
    after = audit.snapshot(row)
    if before == after:
        return
    if not is_new:
        row.updated_by = user_id
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE if is_new else audit.ACTION_UPDATE,
        entity=entity,
        entity_id=row.id,
        before=before,
        after=after,
    )


def snapshot_with_list_fields(
    snapshot: dict[str, Any], effective_date: date | None, list_note: Any = KEEP
) -> dict[str, Any]:
    """A version's snapshot with its list-level fields taken from the version's
    columns (a snapshot key can lag behind a column fixed by hand or by
    ``scripts.meds_versions``). ``list_note=KEEP`` leaves the snapshot's note."""
    out = dict(snapshot)
    out["effective_date"] = effective_date.isoformat() if effective_date else None
    if list_note is not KEEP:
        out["list_note"] = list_note
    return out


def save_list(
    db: DbSession,
    patient_id: int,
    *,
    items: list[dict[str, Any]],
    avoid: list[dict[str, Any]],
    expected_version: int,
    change_summary: str | None = None,
    dry_run: bool = False,
    user_id: int | None,
    list_note: Any = KEEP,
    effective_date: Any = KEEP,
    today: date | None = None,
) -> SaveOutcome:
    """Save the patient's complete list (module docstring has the rules).

    The caller commits when ``outcome.saved``; otherwise this function has
    already rolled the transaction back. Raises :class:`MedsError`.
    """
    today = today or today_local()
    get_patient(db, patient_id, lock=True)
    latest = latest_version(db, patient_id)
    current_no = latest.version_no if latest else 0
    if expected_version != current_no:
        db.rollback()
        raise MedsError(CONFLICT, status=409)

    existing = {i.id: i for i in current_items(db, patient_id, today)}
    existing_avoid = {r.id: r for r in current_avoid(db, patient_id)}
    try:
        _check_ids(items, existing, "medicamentos")
        _check_ids(avoid, existing_avoid, "elementos de EVITAR")
        _check_refs(db, patient_id, items, avoid)
    except MedsError:
        db.rollback()
        raise

    positions: dict[str, int] = {}
    kept: set[int] = set()
    for data in items:
        row = existing.get(data.get("id")) if data.get("id") else None
        if row is None:
            row = MedItem(patient_id=patient_id)
        else:
            kept.add(row.id)
        values = {k: data.get(k) for k in ITEM_FIELDS if row.id is None or k in data}
        values["sort_order"] = positions.get(data["slot"], 0)
        positions[data["slot"]] = values["sort_order"] + 1
        _apply(db, row, values, (*ITEM_FIELDS, "sort_order"), entity="med_item", user_id=user_id)

    yesterday = today - timedelta(days=1)
    for item_id, row in existing.items():
        if item_id not in kept:
            _apply(
                db, row, {"end_date": yesterday}, ("end_date",), entity="med_item", user_id=user_id
            )

    kept_avoid: set[int] = set()
    for data in avoid:
        row = existing_avoid.get(data.get("id")) if data.get("id") else None
        if row is None:
            row = MedAvoid(patient_id=patient_id)
        else:
            kept_avoid.add(row.id)
        values = {k: data.get(k) for k in AVOID_FIELDS if row.id is None or k in data}
        _apply(db, row, values, AVOID_FIELDS, entity="med_avoid", user_id=user_id)
    for row_id, row in existing_avoid.items():
        if row_id not in kept_avoid:
            audit.record(
                db,
                user_id=user_id,
                action=audit.ACTION_DELETE,
                entity="med_avoid",
                entity_id=row_id,
                before=audit.snapshot(row, bookkeeping=True),
            )
            db.delete(row)
    db.flush()

    if list_note is KEEP:
        list_note = latest.list_note if latest else None
    list_note = (list_note or "").strip() or None
    if effective_date is KEEP:
        effective_date = latest.effective_date if latest else None
    snapshot = build_snapshot(
        db, current_items(db, patient_id, today), current_avoid(db, patient_id)
    )
    snapshot["list_note"] = list_note
    snapshot["effective_date"] = effective_date.isoformat() if effective_date else None
    previous = (
        snapshot_with_list_fields(latest.snapshot, latest.effective_date, latest.list_note)
        if latest
        else None
    )
    diff = meds_diff.diff_snapshots(previous, snapshot)
    auto = meds_diff.summary_for(previous, snapshot)
    if dry_run or meds_diff.is_empty(diff):
        db.rollback()
        return SaveOutcome(
            saved=False,
            auto_summary=auto,
            version=None,
            diff=diff,
            previous=previous,
            snapshot=snapshot,
        )

    version = MedListVersion(
        patient_id=patient_id,
        version_no=current_no + 1,
        snapshot=snapshot,
        change_summary=change_summary or auto,
        changed_by=user_id,
        list_note=list_note,
        effective_date=effective_date,
    )
    db.add(version)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity="med_list_version",
        entity_id=version.id,
        after={
            "patient_id": patient_id,
            "version_no": version.version_no,
            "change_summary": version.change_summary,
        },
    )
    return SaveOutcome(
        saved=True,
        auto_summary=auto,
        version=version,
        diff=diff,
        previous=previous,
        snapshot=snapshot,
    )


# ── Versions ─────────────────────────────────────────────────────────────────


def _version_query():
    return select(
        MedListVersion.version_no,
        MedListVersion.change_summary,
        MedListVersion.changed_by,
        User.display_name.label("changed_by_name"),
        MedListVersion.changed_at,
        MedListVersion.effective_date,
        func.jsonb_array_length(MedListVersion.snapshot["items"]).label("item_count"),
        func.jsonb_array_length(MedListVersion.snapshot["avoid"]).label("avoid_count"),
    ).outerjoin(User, User.id == MedListVersion.changed_by)


def list_versions(db: DbSession, patient_id: int) -> list[dict[str, Any]]:
    """Every version of the patient's list, newest first, without snapshots."""
    rows = db.execute(
        _version_query()
        .where(MedListVersion.patient_id == patient_id)
        .order_by(MedListVersion.version_no.desc())
    ).mappings()
    return [dict(r) for r in rows]


def version_items(db: DbSession, patient_id: int) -> dict[int, list[dict[str, Any]]]:
    """Every version's snapshot items by ``version_no`` (the Historial's LEDD)."""
    rows = db.execute(
        select(MedListVersion.version_no, MedListVersion.snapshot["items"]).where(
            MedListVersion.patient_id == patient_id
        )
    ).all()
    return {number: items or [] for number, items in rows}


def get_version(db: DbSession, patient_id: int, version_no: int) -> dict[str, Any]:
    """One version with its snapshot and the diff against the previous one."""
    row = (
        db.execute(
            _version_query()
            .add_columns(MedListVersion.snapshot)
            .where(
                MedListVersion.patient_id == patient_id,
                MedListVersion.version_no == version_no,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise MedsError(VERSION_NOT_FOUND, status=404)
    previous = db.scalars(
        select(MedListVersion)
        .where(
            MedListVersion.patient_id == patient_id,
            MedListVersion.version_no < version_no,
        )
        .order_by(MedListVersion.version_no.desc())
        .limit(1)
    ).first()
    following = db.scalar(
        select(func.min(MedListVersion.version_no)).where(
            MedListVersion.patient_id == patient_id,
            MedListVersion.version_no > version_no,
        )
    )
    detail = dict(row)
    detail["previous_items"] = (previous.snapshot.get("items") or []) if previous else None
    detail["previous_version_no"] = previous.version_no if previous else None
    detail["next_version_no"] = following
    detail["diff"] = meds_diff.diff_snapshots(
        snapshot_with_list_fields(previous.snapshot, previous.effective_date, previous.list_note)
        if previous
        else None,
        snapshot_with_list_fields(row["snapshot"], row["effective_date"]),
    )
    return detail


# ── Derived history (med_events) ─────────────────────────────────────────────

# The Historial reads the events a page at a time (ticket #82): a page is at
# most ``EVENTS_PAGE_MAX`` rows, ``EVENTS_PAGE`` by default.
EVENTS_PAGE = 100
EVENTS_PAGE_MAX = 500
BAD_CURSOR = "Cursor no válido."


def encode_cursor(event_date: date, event_id: int) -> str:
    """The keyset cursor of an event: ``"<YYYY-MM-DD>_<id>"``. Clients treat
    it as opaque and pass back the ``next_cursor`` they were given."""
    return f"{event_date.isoformat()}_{event_id}"


def decode_cursor(cursor: str) -> tuple[date, int]:
    """Inverse of :func:`encode_cursor`; a malformed cursor → ``MedsError`` 422."""
    day, sep, event_id = cursor.partition("_")
    try:
        number = int(event_id)
        if not sep or not 0 < number < 2**63:  # a BIGINT id
            raise ValueError
        return date.fromisoformat(day), number
    except ValueError:
        raise MedsError(BAD_CURSOR, status=422) from None


def list_events(
    db: DbSession,
    patient_id: int,
    *,
    limit: int | None = EVENTS_PAGE,
    before: tuple[date, int] | None = None,
    pending_only: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """The patient's ``med_events`` (history reconstructed from the bitácora,
    ticket #28), newest first by ``(event_date, id)``, with the date of the
    source entry when it still exists.

    Keyset pagination (ticket #82): at most ``limit`` rows (``None`` = all)
    strictly older than ``before`` (an ``(event_date, id)`` pair, see
    :func:`decode_cursor`); returns ``(rows, next_cursor)``, where
    ``next_cursor`` is the cursor of the last row when more rows exist, else
    ``None``. Ties on ``event_date`` are broken by ``id``, so paging never
    skips or repeats a row, and rows written meanwhile only shift pages that
    were not read yet.

    ``pending_only`` keeps only ``pending_review`` events (the «Por revisar»
    box): the caller passes ``limit=None`` so pending events are never hidden
    by paging; they are bounded by review."""
    query = (
        select(
            MedEvent.id,
            MedEvent.event_date,
            MedEvent.kind,
            MedEvent.medication,
            MedEvent.detail,
            MedEvent.source_entry_id,
            LogEntry.entry_date.label("source_entry_date"),
            MedEvent.review_status,
        )
        .outerjoin(LogEntry, LogEntry.id == MedEvent.source_entry_id)
        .where(MedEvent.patient_id == patient_id)
        .order_by(MedEvent.event_date.desc(), MedEvent.id.desc())
    )
    if pending_only:
        query = query.where(MedEvent.review_status == REVIEW_PENDING)
    if before is not None:
        query = query.where(tuple_(MedEvent.event_date, MedEvent.id) < tuple_(*before))
    if limit is not None:
        query = query.limit(limit + 1)
    rows = [dict(r) for r in db.execute(query).mappings()]
    if limit is None or len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    return rows, encode_cursor(rows[-1]["event_date"], rows[-1]["id"])


def event_out(db: DbSession, row: MedEvent) -> dict[str, Any]:
    """One event in the shape of :func:`list_events`."""
    entry = db.get(LogEntry, row.source_entry_id) if row.source_entry_id else None
    return {
        "id": row.id,
        "event_date": row.event_date,
        "kind": row.kind,
        "medication": row.medication,
        "detail": row.detail,
        "source_entry_id": row.source_entry_id,
        "source_entry_date": entry.entry_date if entry else None,
        "review_status": row.review_status,
    }


# ── Review of derived events (ticket #28) ────────────────────────────────────
#
# Any signed-in member reviews: ``update_event`` edits and/or confirms
# (``review_status = ok``), ``delete_event`` discards (hard delete; the audit
# row keeps the full event and stops the backfill from recreating it), and
# ``confirm_events`` confirms several pending events at once.

EVENT_ENTITY = "med_event"
EVENT_FIELDS = ("event_date", "kind", "medication", "detail", "review_status")
EVENT_NOT_FOUND = "Evento no encontrado."
# One «Confirmar todos» request; keep in sync with ``MedEventsConfirm.ids``.
# The pending list is not capped, so the frontend sends larger sets in chunks.
CONFIRM_MAX = 500


def create_event(db: DbSession, values: dict[str, Any], *, user_id: int | None) -> MedEvent:
    """Insert one ``med_events`` row (audited); the caller commits."""
    row = MedEvent()
    _apply(
        db,
        row,
        values,
        (*EVENT_FIELDS, "patient_id", "source_entry_id", "source_index"),
        entity=EVENT_ENTITY,
        user_id=user_id,
    )
    return row


def get_event(db: DbSession, patient_id: int, event_id: int, *, lock: bool = False) -> MedEvent:
    """The patient's event (404 when missing or another patient's)."""
    row = db.get(MedEvent, event_id, with_for_update=lock)
    if row is None or row.patient_id != patient_id:
        raise MedsError(EVENT_NOT_FOUND, status=404)
    return row


def update_event(
    db: DbSession, patient_id: int, event_id: int, values: dict[str, Any], *, user_id: int | None
) -> MedEvent:
    """Apply ``values`` (any of :data:`EVENT_FIELDS`), audited; the caller commits."""
    get_patient(db, patient_id)
    row = get_event(db, patient_id, event_id, lock=True)
    _apply(db, row, values, EVENT_FIELDS, entity=EVENT_ENTITY, user_id=user_id)
    return row


def delete_event(db: DbSession, patient_id: int, event_id: int, *, user_id: int | None) -> None:
    """Discard an event: hard delete with a self-contained audit row."""
    get_patient(db, patient_id)
    row = get_event(db, patient_id, event_id, lock=True)
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_DELETE,
        entity=EVENT_ENTITY,
        entity_id=row.id,
        before=audit.snapshot(row, bookkeeping=True),
    )
    db.delete(row)
    db.flush()


def confirm_events(
    db: DbSession, patient_id: int, event_ids: list[int], *, user_id: int | None
) -> int:
    """Set ``review_status = ok`` on the listed events that are this patient's
    and still pending (the others are ignored: confirmed or discarded by
    someone else meanwhile, or not this patient's). Returns how many changed."""
    get_patient(db, patient_id)
    rows = db.scalars(
        select(MedEvent)
        .where(
            MedEvent.patient_id == patient_id,
            MedEvent.id.in_(set(event_ids)),
            MedEvent.review_status == REVIEW_PENDING,
        )
        .order_by(MedEvent.id)
        .with_for_update()
    ).all()
    for row in rows:
        _apply(
            db,
            row,
            {"review_status": REVIEW_OK},
            ("review_status",),
            entity=EVENT_ENTITY,
            user_id=user_id,
        )
    return len(rows)
