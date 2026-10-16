"""Medication list diff and Spanish change summary (DESIGN §5.4, §6.4).

Pure functions over two version snapshots: no database, no I/O.

**Snapshot shape** (stored in ``med_list_versions.snapshot`` and built by
``app.services.meds.build_snapshot``)::

    {
      "items": [            # printed-sheet order: slot (SLOTS order), sort_order
        {"id": 7, "name": "Prolopa", "strength": "250 mg", "quantity": "½",
         "dose_note": "(125 mg total)", "slot": "desayuno", "time": "06:00",
         "days": [1, 3, 5], "schedule_note": None, "instructions": "con agua",
         "prescriber_id": 3, "prescriber_name": "Dra. Ana Pérez", "reason": None,
         "start_date": "2026-10-01", "end_date": None, "sort_order": 0,
         "source_entry_id": None},
        ...
      ],
      "avoid": [{"id": 2, "substance": "Ibuprofeno", "reason": "…",
                 "source_entry_id": None}, ...],
      "list_note": "| Hora | … |",       # since migration 0013
      "effective_date": "2026-09-30"     # since migration 0013
    }

**Diff** (:func:`diff_snapshots`): rows are matched by ``id`` (a rename keeps
the id, so it is a change, not a removal plus an addition)::

    {"added": [item], "removed": [item],
     "changed": [{"id", "name", "before": item, "after": item,
                  "fields": [{"field", "label", "before", "after",
                              "before_text", "after_text"}]}],
     "reordered_slots": ["desayuno"],
     "avoid_added": [row], "avoid_removed": [row],
     "avoid_changed": [{"id", "substance", "before", "after", "fields": [...]}],
     "list_changed": [{"field", "label", "before", "after",
                       "before_text", "after_text"}]}

``list_changed`` holds the list-level fields (#135): ``effective_date``
(«Desde») and ``list_note`` (the regimen table). A missing key reads as
``None`` (snapshots older than migration 0013). The note's ``*_text`` is not
the note itself (it can be a long table) but «con nota» / «—»; the summary
says only that it was added, modified or removed.

``sort_order`` is never a field change: a slot is reported in
``reordered_slots`` only when the *relative order* of the items it had in both
snapshots changed (renumbering, additions and removals are not a reorder).
A reorder is a real change to the printed sheet, so it does create a version
("Se reordenó Desayuno"). ``prescriber_name`` is display data only: the
prescriber is compared by ``prescriber_id`` (a doctor merge that repoints it
is a visible change and is reported). ``source_entry_id`` changes stay in the
structured diff but are left out of the summary text (``SUMMARY_SKIP``).
"""

from datetime import date
from typing import Any

from app.models.medication import SLOTS

SLOT_LABELS = {
    "al_despertar": "Al despertar",
    "desayuno": "Desayuno",
    "manana": "Mañana",
    "almuerzo": "Almuerzo",
    "tarde": "Tarde",
    "cena": "Cena",
    "noche": "Noche",
    "madrugada": "Madrugada",
    "segun_necesidad": "Según necesidad",
    "otros": "Otros medicamentos",
}

WEEKDAYS = {1: "lun", 2: "mar", 3: "mié", 4: "jue", 5: "vie", 6: "sáb", 7: "dom"}

# Compared item fields, in the order a change is described, with Spanish labels.
ITEM_FIELDS = {
    "name": "nombre",
    "strength": "concentración",
    "quantity": "cantidad",
    "dose_note": "nota de dosis",
    "slot": "horario",
    "time": "hora",
    "days": "días",
    "schedule_note": "pauta",
    "instructions": "instrucciones",
    "prescriber_id": "médico",
    "reason": "motivo",
    "start_date": "fecha de inicio",
    "end_date": "fecha de fin",
    "source_entry_id": "entrada de bitácora",
}
AVOID_FIELDS = {
    "substance": "sustancia",
    "reason": "motivo",
    "source_entry_id": "entrada de bitácora",
}

# List-level fields (#135), in the order a change is described.
LIST_FIELDS = {
    "effective_date": "«Desde»",
    "list_note": "nota de la lista",
}
NOTE_TEXT = "con nota"

# In the structured diff but not in the human summary: a bitácora hard delete
# nulls ``source_entry_id`` outside any save, and the next unrelated save would
# otherwise say "entrada de bitácora de entrada #N a —".
SUMMARY_SKIP = frozenset({"source_entry_id"})
LINKS_ONLY = "Se actualizaron los vínculos con la bitácora."

EMPTY_TEXT = "—"
NO_CHANGES = "Sin cambios."

_SLOT_INDEX = {slot: i for i, slot in enumerate(SLOTS)}


def empty_snapshot() -> dict[str, list]:
    return {"items": [], "avoid": []}


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Printed-sheet order: slot, then ``sort_order``, then ``id``."""
    return sorted(
        items,
        key=lambda it: (
            _SLOT_INDEX.get(it.get("slot"), len(SLOTS)),
            it.get("sort_order") or 0,
            it.get("id") or 0,
        ),
    )


# ── Display text ─────────────────────────────────────────────────────────────


def format_days(days: list[int] | None) -> str:
    if not days:
        return "todos los días"
    return ", ".join(WEEKDAYS.get(d, str(d)) for d in days)


def _format_date(value: str | None) -> str:
    if not value:
        return EMPTY_TEXT
    try:
        return date.fromisoformat(value).strftime("%d/%m/%Y")
    except ValueError:
        return value


def value_text(field: str, value: Any, row: dict[str, Any] | None = None) -> str:
    """Spanish display text of a snapshot value (``row`` gives the prescriber name)."""
    if field == "days":
        return format_days(value)
    if value is None or value == "":
        return EMPTY_TEXT
    if field == "slot":
        return SLOT_LABELS.get(value, value)
    if field == "list_note":
        return NOTE_TEXT
    if field in ("start_date", "end_date", "effective_date"):
        return _format_date(value)
    if field == "prescriber_id":
        name = (row or {}).get("prescriber_name")
        return name or f"médico #{value}"
    if field == "source_entry_id":
        return f"entrada #{value}"
    return str(value)


def item_label(item: dict[str, Any]) -> str:
    """ "Prolopa 250 mg": name plus strength when there is one."""
    parts = [item.get("name") or "", item.get("strength") or ""]
    return " ".join(p.strip() for p in parts if p and p.strip())


# ── Diff ─────────────────────────────────────────────────────────────────────


def _norm(value: Any) -> Any:
    """Blank strings and empty lists compare equal to ``None``."""
    if isinstance(value, str):
        value = value.strip()
    return value if value not in ("", [], None) else None


def _field_changes(
    before: dict[str, Any], after: dict[str, Any], fields: dict[str, str]
) -> list[dict[str, Any]]:
    changes = []
    for field, label in fields.items():
        if _norm(before.get(field)) == _norm(after.get(field)):
            continue
        changes.append(
            {
                "field": field,
                "label": label,
                "before": before.get(field),
                "after": after.get(field),
                "before_text": value_text(field, before.get(field), before),
                "after_text": value_text(field, after.get(field), after),
            }
        )
    return changes


def _reordered_slots(before: list[dict], after: list[dict]) -> list[str]:
    before_slot = {it["id"]: it["slot"] for it in before}
    after_slot = {it["id"]: it["slot"] for it in after}
    stayed = {i for i, s in after_slot.items() if before_slot.get(i) == s}
    slots = []
    for slot in SLOTS:
        old = [it["id"] for it in before if it["slot"] == slot and it["id"] in stayed]
        new = [it["id"] for it in after if it["slot"] == slot and it["id"] in stayed]
        if old != new:
            slots.append(slot)
    return slots


def diff_snapshots(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, list[Any]]:
    """Structured diff from ``before`` (``None`` = no previous version) to ``after``."""
    before = before or empty_snapshot()
    old_items = sort_items(before.get("items") or [])
    new_items = sort_items(after.get("items") or [])
    old_by_id = {it["id"]: it for it in old_items}
    new_by_id = {it["id"]: it for it in new_items}

    changed = []
    for item in new_items:
        prev = old_by_id.get(item["id"])
        if prev is None:
            continue
        fields = _field_changes(prev, item, ITEM_FIELDS)
        if fields:
            changed.append(
                {
                    "id": item["id"],
                    "name": item.get("name"),
                    "before": prev,
                    "after": item,
                    "fields": fields,
                }
            )

    old_avoid = before.get("avoid") or []
    new_avoid = after.get("avoid") or []
    old_avoid_by_id = {row["id"]: row for row in old_avoid}
    new_avoid_ids = {row["id"] for row in new_avoid}
    avoid_changed = []
    for row in new_avoid:
        prev = old_avoid_by_id.get(row["id"])
        if prev is None:
            continue
        fields = _field_changes(prev, row, AVOID_FIELDS)
        if fields:
            avoid_changed.append(
                {
                    "id": row["id"],
                    "substance": row.get("substance"),
                    "before": prev,
                    "after": row,
                    "fields": fields,
                }
            )

    return {
        "added": [it for it in new_items if it["id"] not in old_by_id],
        "removed": [it for it in old_items if it["id"] not in new_by_id],
        "changed": changed,
        "reordered_slots": _reordered_slots(old_items, new_items),
        "avoid_added": [row for row in new_avoid if row["id"] not in old_avoid_by_id],
        "avoid_removed": [row for row in old_avoid if row["id"] not in new_avoid_ids],
        "avoid_changed": avoid_changed,
        "list_changed": _field_changes(before, after, LIST_FIELDS),
    }


def is_empty(diff: dict[str, list[Any]]) -> bool:
    return not any(diff.values())


# ── Summary ──────────────────────────────────────────────────────────────────


def _change_clause(change: dict[str, Any]) -> list[str]:
    fields = change["fields"]
    clauses = []
    name = change["after"].get("name") or ""
    rest = []
    for f in fields:
        if f["field"] in SUMMARY_SKIP:
            continue
        if f["field"] == "name":
            clauses.append(f"se renombró {f['before_text']} a {f['after_text']}")
        else:
            rest.append(f)
    if not rest:
        return clauses
    if len(rest) == 1 and rest[0]["field"] == "quantity":
        f = rest[0]
        clauses.append(f"{name} cambió de {f['before_text']} a {f['after_text']}")
    elif len(rest) == 1 and rest[0]["field"] == "slot":
        f = rest[0]
        clauses.append(f"{name} pasó de {f['before_text']} a {f['after_text']}")
    else:
        parts = ", ".join(f"{f['label']} de {f['before_text']} a {f['after_text']}" for f in rest)
        clauses.append(f"{name}: {parts}")
    return clauses


def _list_clause(f: dict[str, Any]) -> str:
    if f["field"] == "list_note":
        if _norm(f["before"]) is None:
            return "se agregó la nota de la lista"
        if _norm(f["after"]) is None:
            return "se quitó la nota de la lista"
        return "se modificó la nota de la lista"
    if f["before"] is None:
        return f"{f['label']}: {f['after_text']}"
    if f["after"] is None:
        return f"se quitó la fecha {f['label']}"
    return f"{f['label']} pasó de {f['before_text']} a {f['after_text']}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def initial_summary(snapshot: dict[str, Any]) -> str:
    items = len(snapshot.get("items") or [])
    avoid = len(snapshot.get("avoid") or [])
    text = "Lista inicial: " + _plural(items, "medicamento", "medicamentos")
    if avoid:
        text += " y " + _plural(avoid, "sustancia", "sustancias") + " en EVITAR"
    return text + "."


def summarize(diff: dict[str, list[Any]]) -> str:
    """One Spanish sentence per change, joined with "; " ("Se agregó X; Y
    cambió de 1 a ½; se suspendió Z.")."""
    clauses: list[str] = []
    for item in diff["added"]:
        details = [d for d in (item.get("quantity"), SLOT_LABELS.get(item.get("slot"))) if d]
        suffix = f" ({', '.join(details)})" if details else ""
        clauses.append(f"se agregó {item_label(item)}{suffix}")
    for item in diff["removed"]:
        clauses.append(f"se suspendió {item_label(item)}")
    for change in diff["changed"]:
        clauses.extend(_change_clause(change))
    for slot in diff["reordered_slots"]:
        clauses.append(f"se reordenó {SLOT_LABELS.get(slot, slot)}")
    for row in diff["avoid_added"]:
        clauses.append(f"se agregó {row.get('substance')} a EVITAR")
    for row in diff["avoid_removed"]:
        clauses.append(f"se quitó {row.get('substance')} de EVITAR")
    for change in diff["avoid_changed"]:
        shown = [f for f in change["fields"] if f["field"] not in SUMMARY_SKIP]
        if not shown:
            continue
        parts = ", ".join(f"{f['label']} de {f['before_text']} a {f['after_text']}" for f in shown)
        clauses.append(f"se modificó {change['before'].get('substance')} en EVITAR ({parts})")
    for f in diff.get("list_changed", []):
        clauses.append(_list_clause(f))
    if not clauses:
        return NO_CHANGES if is_empty(diff) else LINKS_ONLY
    text = "; ".join(clauses)
    return text[0].upper() + text[1:] + "."


def summary_for(before: dict[str, Any] | None, after: dict[str, Any]) -> str:
    """The auto-generated summary of a save: "Lista inicial…" for the first version."""
    if before is None:
        return initial_summary(after)
    return summarize(diff_snapshots(before, after))
