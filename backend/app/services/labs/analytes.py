"""Analyte review (#15): list, confirm/edit, merge (admins).

The extraction creates an analyte flagged ``needs_review`` for every name it
does not know (``matching.AnalyteMatcher``). An admin either **confirms** it
(clears the flag, optionally fixing the canonical name, unit, category or
specimen) or **merges** it into an existing analyte.

``update_analyte``: a renamed analyte keeps its old canonical name as an
alias, so the same printed name keeps matching it. The new name may not be
another analyte's canonical name (unique) nor a name or alias of another
analyte of the same specimen (that is a merge, not a rename).

``merge_analytes`` mirrors ``app.services.doctors.merge_doctors``: in the
caller's transaction both rows are locked (``FOR UPDATE``, in id order, so
crossing merges cannot deadlock and a second merge of the same duplicate
waits, then finds it gone); every row with a FK to ``analytes.id`` (today
``lab_results.analyte_id``, found in ``Base.metadata``) is re-pointed to the
target; the source's canonical name and aliases become target aliases
(deduplicated by ``normalize_key``, never equal to the target's name); the
target's empty unit/category are filled from the source; the source is
deleted. Different specimens (blood vs. urine glucose) are refused unless the
caller passes ``allow_specimen_mismatch``. Audited: ``merge`` on the target
(both before, target after, moved result ids) and ``delete`` on the source.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Column, func, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.database import Base
from app.models.lab import Analyte, LabResult
from app.services import audit
from app.services.labs.values import normalize_key

ENTITY = "analyte"
EDITABLE_FIELDS = frozenset(
    {"canonical_name", "default_unit", "category", "specimen", "needs_review"}
)
EXAMPLES_PER_ANALYTE = 3


class AnalyteError(Exception):
    """A refused change; ``message`` is Spanish and safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class AnalyteStats:
    result_count: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)


def analyte_references() -> list[Column]:
    """Every column with a foreign key to ``analytes.id``."""
    return [
        column
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if any(fk.column.table.name == "analytes" for fk in column.foreign_keys)
    ]


def stats(db: DbSession, analyte_ids: Iterable[int]) -> dict[int, AnalyteStats]:
    """Result count and a few example results (raw name, value, unit) per analyte."""
    ids = list(analyte_ids)
    out = {i: AnalyteStats() for i in ids}
    if not ids:
        return out
    counts = db.execute(
        select(LabResult.analyte_id, func.count())
        .where(LabResult.analyte_id.in_(ids))
        .group_by(LabResult.analyte_id)
    )
    for analyte_id, count in counts:
        out[analyte_id].result_count = count
    ranked = (
        select(
            LabResult.analyte_id,
            LabResult.raw_name,
            LabResult.value_text,
            LabResult.unit,
            func.row_number()
            .over(partition_by=LabResult.analyte_id, order_by=LabResult.id.desc())
            .label("rn"),
        )
        .where(LabResult.analyte_id.in_(ids))
        .subquery()
    )
    rows = db.execute(
        select(ranked.c.analyte_id, ranked.c.raw_name, ranked.c.value_text, ranked.c.unit)
        .where(ranked.c.rn <= EXAMPLES_PER_ANALYTE)
        .order_by(ranked.c.analyte_id, ranked.c.rn)
    )
    for analyte_id, raw_name, value_text, unit in rows:
        out[analyte_id].examples.append(
            {"raw_name": raw_name, "value_text": value_text, "unit": unit}
        )
    return out


def list_analytes(db: DbSession, *, needs_review: bool | None = None) -> list[Analyte]:
    stmt = select(Analyte).order_by(func.lower(Analyte.canonical_name), Analyte.id)
    if needs_review is not None:
        stmt = stmt.where(Analyte.needs_review.is_(needs_review))
    return list(db.scalars(stmt))


def clean_aliases(canonical_name: str, names: Iterable[str]) -> list[str]:
    """Aliases deduplicated by normalized key (first spelling kept), without
    blanks or the canonical name itself."""
    seen = {normalize_key(canonical_name)}
    out: list[str] = []
    for name in names:
        name = " ".join((name or "").split())
        key = normalize_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _name_taken(db: DbSession, analyte: Analyte, name: str, specimen: str) -> Analyte | None:
    """Another analyte whose canonical name equals ``name``, or that matches
    it (name or alias) within ``specimen``."""
    exact = db.scalar(
        select(Analyte).where(Analyte.canonical_name == name, Analyte.id != analyte.id)
    )
    if exact is not None:
        return exact
    key = normalize_key(name)
    for other in db.scalars(
        select(Analyte).where(Analyte.specimen == specimen, Analyte.id != analyte.id)
    ):
        if key in {normalize_key(n) for n in [other.canonical_name, *(other.aliases or [])]}:
            return other
    return None


def update_analyte(
    db: DbSession, analyte: Analyte, changes: dict[str, Any], *, user_id: int | None
) -> Analyte:
    """Apply ``changes`` (``EDITABLE_FIELDS``), validate and audit (caller
    commits; raises ``AnalyteError``)."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise AnalyteError(422, f"Campos no permitidos: {', '.join(sorted(unknown))}.")
    before = audit.snapshot(analyte)
    old_name = analyte.canonical_name
    name = changes.get("canonical_name", old_name)
    specimen = changes.get("specimen", analyte.specimen)
    if name != old_name or specimen != analyte.specimen:
        other = _name_taken(db, analyte, name, specimen)
        if other is not None:
            raise AnalyteError(
                409,
                f"Ya existe el examen «{other.canonical_name}» con ese nombre. "
                "Usa «Combinar con…» si son el mismo examen.",
            )
    for key, value in changes.items():
        setattr(analyte, key, value)
    if name != old_name:
        analyte.aliases = clean_aliases(name, [*(analyte.aliases or []), old_name])
    after = audit.snapshot(analyte)
    if after != before:
        analyte.updated_by = user_id
        analyte.updated_at = func.now()
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_UPDATE,
            entity=ENTITY,
            entity_id=analyte.id,
            before=before,
            after=after,
        )
    db.flush()
    return analyte


def merge_analytes(
    db: DbSession,
    *,
    target_id: int,
    source_id: int,
    user_id: int | None,
    allow_specimen_mismatch: bool = False,
) -> Analyte:
    """Fold the analyte ``source`` into ``target`` (caller commits; raises
    ``AnalyteError``). See the module docstring."""
    if target_id == source_id:
        raise AnalyteError(422, "No se puede combinar un examen consigo mismo.")
    rows = {
        a.id: a
        for a in db.scalars(
            select(Analyte)
            .where(Analyte.id.in_([target_id, source_id]))
            .order_by(Analyte.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    target, source = rows.get(target_id), rows.get(source_id)
    if target is None or source is None:
        raise AnalyteError(404, "Examen no encontrado.")
    if target.specimen != source.specimen and not allow_specimen_mismatch:
        raise AnalyteError(
            409,
            f"Las muestras son distintas ({source.specimen} y {target.specimen}). "
            "Confirma si de verdad son el mismo examen.",
        )

    target_before = audit.snapshot(target)
    source_before = audit.snapshot(source, bookkeeping=True)

    moved: dict[str, list] = {}
    for column in analyte_references():
        table = column.table
        pk = next(iter(table.primary_key.columns))
        values: dict[str, Any] = {column.name: target.id}
        if "updated_by" in table.c and user_id is not None:
            values["updated_by"] = user_id
        ids = db.scalars(
            update(table).where(column == source.id).values(values).returning(pk)
        ).all()
        if ids:
            moved[f"{table.name}.{column.name}"] = sorted(ids)

    target.aliases = clean_aliases(
        target.canonical_name,
        [*(target.aliases or []), source.canonical_name, *(source.aliases or [])],
    )
    if not target.default_unit and source.default_unit:
        target.default_unit = source.default_unit
    if not target.category and source.category:
        target.category = source.category
    target.updated_by = user_id
    target.updated_at = func.now()

    db.delete(source)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_MERGE,
        entity=ENTITY,
        entity_id=target.id,
        before={"target": target_before, "source": source_before},
        after={"target": audit.snapshot(target), "source_id": source_id, "moved": moved},
    )
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_DELETE,
        entity=ENTITY,
        entity_id=source_id,
        before=source_before,
        after={"merged_into": target.id},
    )
    return target
