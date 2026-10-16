"""Doctor name normalization and alias matching.

Reused by importers (the DOCX bitácora importer, WhatsApp ingestion) to map
free text such as "Dra. Ana Pérez", "ana perez" or an alias like "Anita" to
one ``doctors`` row. Matching is exact on the *normalized* form:

- accents removed (Unicode NFKD, combining marks dropped: "Núñez" → "nunez"),
- lowercased (casefold),
- leading titles stripped (Dr., Dra., Doctor, Doctora, Lic., Licda., Licdo.),
- punctuation turned into spaces and runs of whitespace collapsed.

The directory is small (tens of rows), so matching loads the doctors and
compares in Python; no database function is needed.
"""

import re
import unicodedata

from sqlalchemy import Column, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.database import Base
from app.models.doctor import Doctor
from app.services import audit

_TITLES = frozenset(
    {"dr", "dra", "doctor", "doctora", "lic", "licda", "licdo", "licenciada", "licenciado"}
)
_NON_WORD = re.compile(r"[^\w]+")


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(value: str | None) -> str:
    """Canonical comparison key for a person's name ('' for blank input)."""
    if not value:
        return ""
    words = _NON_WORD.sub(" ", strip_accents(value).casefold()).split()
    while words and words[0] in _TITLES:
        words.pop(0)
    return " ".join(words)


def doctor_keys(doctor: Doctor) -> set[str]:
    """Normalized forms of a doctor's name and every alias."""
    keys = {normalize_name(doctor.name)}
    keys.update(normalize_name(alias) for alias in doctor.aliases or [])
    keys.discard("")
    return keys


def match_doctors(
    db: DbSession, text: str, *, include_inactive: bool = True, exclude_id: int | None = None
) -> list[Doctor]:
    """Doctors whose name or any alias normalizes to the same key as ``text``.

    Usually zero or one; more than one means the directory has an ambiguous
    alias and the caller should ask a human (or flag the row for review).
    """
    key = normalize_name(text)
    if not key:
        return []
    stmt = select(Doctor).order_by(Doctor.id)
    if not include_inactive:
        stmt = stmt.where(Doctor.is_active.is_(True))
    return [
        doctor
        for doctor in db.scalars(stmt)
        if doctor.id != exclude_id and key in doctor_keys(doctor)
    ]


def match_doctor(db: DbSession, text: str) -> Doctor | None:
    """The single doctor matching ``text``, or None when there is none or several."""
    matches = match_doctors(db, text)
    return matches[0] if len(matches) == 1 else None


def clean_aliases(name: str, aliases: list[str]) -> list[str]:
    """Trim aliases, drop blanks and those equivalent to the name or each other."""
    seen = {normalize_name(name)}
    result: list[str] = []
    for alias in aliases:
        alias = alias.strip()
        key = normalize_name(alias)
        if key and key not in seen:
            seen.add(key)
            result.append(alias)
    return result


def find_conflict(db: DbSession, doctor: Doctor, exclude_id: int | None = None) -> Doctor | None:
    """Another doctor already identified by ``doctor``'s name or one of its aliases."""
    for text in [doctor.name, *doctor.aliases]:
        matches = match_doctors(db, text, exclude_id=exclude_id)
        if matches:
            return matches[0]
    return None


class DoctorConflict(ValueError):
    """The new doctor's name or an alias already identifies ``existing``."""

    def __init__(self, existing: Doctor):
        super().__init__(f"Ya existe un médico con ese nombre o alias: {existing.name}.")
        self.existing = existing


def create_doctor(db: DbSession, data: dict, *, user_id: int | None) -> Doctor:
    """Insert and audit a doctor (``POST /doctors`` and approved suggestions).

    ``data`` holds ``DoctorCreate`` fields; aliases are cleaned. Raises
    :class:`DoctorConflict` when the name or an alias is already taken. The
    caller commits.
    """
    values = dict(data)
    values["aliases"] = clean_aliases(values["name"], values.get("aliases") or [])
    doctor = Doctor(**values, created_by=user_id, updated_by=user_id)
    existing = find_conflict(db, doctor)
    if existing is not None:
        raise DoctorConflict(existing)
    db.add(doctor)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity="doctor",
        entity_id=doctor.id,
        after=audit.snapshot(doctor),
    )
    return doctor


# ── Merging duplicates ───────────────────────────────────────────────────────

# Contact fields copied from the duplicate only when the target's is empty.
_FILL_FIELDS = ("specialty", "phone", "email", "clinic", "address")


class MergeError(Exception):
    """A merge that cannot run; ``status`` is the HTTP status the API returns."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def doctor_references() -> list[Column]:
    """Every column in the schema with a foreign key to ``doctors.id``.

    Discovered from ``Base.metadata`` instead of listed by hand, so the
    appointments / medications tables of later epics (``doctor_id``,
    ``prescriber_id``) are repointed by :func:`merge_doctors` as soon as their
    models are registered in ``app.models``.
    """
    import app.models  # noqa: F401  -- every model registered on Base.metadata

    refs = [
        fk.parent
        for table in Base.metadata.sorted_tables
        for fk in table.foreign_keys
        if fk.column.table.name == Doctor.__tablename__ and fk.column.name == "id"
    ]
    return sorted(refs, key=lambda col: (col.table.name, col.name))


def merge_doctors(db: DbSession, *, target_id: int, source_id: int, user_id: int | None) -> Doctor:
    """Fold the duplicate ``source`` into ``target`` (the caller commits).

    In the caller's transaction: both rows are locked (``FOR UPDATE``, in id
    order, so two crossing merges cannot deadlock and a concurrent merge of
    the same duplicate waits, then finds it gone); every row referencing the
    source (any FK to ``doctors.id``) is repointed to the target; the source's
    name and aliases become target aliases (deduplicated by normalized form,
    never equal to the target's name); empty contact fields of the target are
    filled from the source (non-empty ones are kept) and the source's notes
    are appended to the target's. Then the source is **deleted**: nothing
    references it any more, and keeping it (even inactive) would leave its
    name colliding with the alias just added to the target. The ``merge``
    audit row on the target keeps both doctors before/after and the moved row
    ids; a ``delete`` row on the source keeps its full snapshot.
    """
    if target_id == source_id:
        raise MergeError(422, "No se puede combinar un médico consigo mismo.")
    rows = {
        doctor.id: doctor
        for doctor in db.scalars(
            select(Doctor)
            .where(Doctor.id.in_([target_id, source_id]))
            .order_by(Doctor.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    target, source = rows.get(target_id), rows.get(source_id)
    if target is None or source is None:
        raise MergeError(404, "Médico no encontrado.")
    if not target.is_active:
        raise MergeError(409, "El médico destino está inactivo. Reactívalo antes de combinar.")

    target_before = audit.snapshot(target)
    source_before = audit.snapshot(source, bookkeeping=True)

    moved: dict[str, list] = {}
    for column in doctor_references():
        table = column.table
        pk = next(iter(table.primary_key.columns))
        values = {column.name: target.id}
        if "updated_by" in table.c and user_id is not None:
            values["updated_by"] = user_id
        ids = db.scalars(
            update(table).where(column == source.id).values(values).returning(pk)
        ).all()
        if ids:
            moved[f"{table.name}.{column.name}"] = sorted(ids)

    target.aliases = clean_aliases(
        target.name, [*target.aliases, source.name, *(source.aliases or [])]
    )
    for field in _FILL_FIELDS:
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
    if source.notes and source.notes != target.notes:
        target.notes = f"{target.notes}\n\n{source.notes}" if target.notes else source.notes
    target.updated_by = user_id

    db.delete(source)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_MERGE,
        entity="doctor",
        entity_id=target.id,
        before={"target": target_before, "source": source_before},
        after={"target": audit.snapshot(target), "source_id": source_id, "moved": moved},
    )
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_DELETE,
        entity="doctor",
        entity_id=source_id,
        before=source_before,
        after={"merged_into": target.id},
    )
    return target
