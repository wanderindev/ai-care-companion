"""Medication checks (DESIGN §6.4, ticket #29). Warnings only: nothing here
ever refuses a save.

**EVITAR check** (:func:`avoid_warnings`): an item of the list matches an
EVITAR row when one of the row's *terms* appears as a whole word or phrase
in the item's name + strength, after :func:`normalize` (trademark signs
as spaces, no accents, casefolded, punctuation as spaces, single spaces).
Whole words only, so «AINE» never matches «Gabapentina» or «Prednisona»;
the term's last word may take a plural «s» / «es» («Aspirinas»). A row's
terms are:

- the whole substance, and its parts split on ``/ , ; ( ) +`` (so
  «Ibuprofeno (Advil)» or «Ibuprofeno / Naproxeno» check each name);
- for each of those, its leading words before the first word with a digit
  («Aspirina 100 mg» → «aspirina», so «Aspirina 81 mg» matches too);
- terms shorter than :data:`MIN_TERM` letters are dropped;
- for each term, every synonym in its :data:`SYNONYMS` group.

A false negative is the costly mistake here, so the rules lean towards
warning.

:data:`SYNONYMS` is a deliberately small built-in table of brand ↔ generic
names common in Panamá. It is a basic check by name, not a drug-interaction
database: it does not know drug classes («AINEs» does not match
«Ibuprofeno» unless both names are on the list) and the UI says so
(«revisión básica por nombre; no reemplaza al médico»). The frontend mirrors
the same rules in ``frontend/src/lib/meds.js`` (``itemWarnings``): keep both
in sync.

**Out-of-date check** (:func:`outdated_entries`): bitácora entries of the
patient dated **after** the current version's «Desde» (``effective_date``,
else the Panamá day of ``changed_at``) that carry medication events, minus
the ones the family dismissed («Ya está reflejado», ``med_check_dismissals``).
«Desde» rather than ``changed_at``: the imported version 1 is saved long
after the sheet's date, and the changes recorded between the sheet's date and
the import are exactly the ones that may be missing. An entry dated the same
day as an explicit ``effective_date`` is taken as reflected; with the
``changed_at`` fallback an entry of the save's day still counts (the save
says nothing about that day's visit). A dismissal is permanent per entry:
events linked to it later do not bring it back. An entry's events are its
``med_events`` rows (any review status) plus, for imported entries, the
``origin_ref.medication_events`` not yet backfilled into rows (and not
discarded in the app). Manual entries carry no extracted events, so they are
never reported (a Claude check of new entries is left to the daily ingestion
of a later epic). No version yet → nothing to compare against → no entries.
"""

import re
import unicodedata
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from app.models.log_entry import LogEntry
from app.models.medication import MedCheckDismissal, MedEvent
from app.services import audit, meds
from app.services.med_events_backfill import KIND_MAP, ORIGIN, discarded_sources
from app.services.whatsapp.chat import PANAMA_TZ

MIN_TERM = 3
_NON_WORD = re.compile(r"[^\w]+")
_SPLIT = re.compile(r"[/,;()+]")
_MARKS = re.compile(r"[™℠®©]")
PLURALS = ("", "s", "es")

# Small, documented brand ↔ generic groups (normalized on load). Every name
# of a group matches every other. Never a claim of clinical completeness.
SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("ibuprofeno", "advil", "motrin"),
    ("acido acetilsalicilico", "aspirina"),
    ("acetaminofen", "paracetamol", "tylenol", "panadol"),
    ("naproxeno", "aleve"),
    ("diclofenaco", "voltaren"),
    ("metamizol", "dipirona"),
)


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(value: str | None) -> str:
    """Comparison key: no accents, casefolded, punctuation as spaces, single
    spaces ('' for blank input). Trademark signs become spaces first: NFKD
    would turn «™» into "TM" and glue it to the name («Aspirina™»)."""
    if not value:
        return ""
    value = _MARKS.sub(" ", value)
    return " ".join(_NON_WORD.sub(" ", strip_accents(value).casefold().replace("_", " ")).split())


def _leading_words(term: str) -> str:
    """The words of a normalized term before the first one with a digit
    ("aspirina 100 mg" → "aspirina")."""
    words = []
    for word in term.split():
        if any(ch.isdigit() for ch in word):
            break
        words.append(word)
    return " ".join(words)


_SYNONYM_INDEX: dict[str, frozenset[str]] = {}
for _group in SYNONYMS:
    _names = frozenset(normalize(n) for n in _group)
    for _name in _names:
        _SYNONYM_INDEX[_name] = _names


def avoid_terms(substance: str | None) -> set[str]:
    """Normalized names that count as this EVITAR substance."""
    parts = [substance or "", *_SPLIT.split(substance or "")]
    terms = {normalize(p) for p in parts}
    terms |= {_leading_words(t) for t in terms}
    terms = {t for t in terms if len(t.replace(" ", "")) >= MIN_TERM}
    for term in list(terms):
        terms |= _SYNONYM_INDEX.get(term, frozenset())
    return terms


def matches(text: str | None, terms: set[str]) -> bool:
    """True when one of ``terms`` is a whole word / phrase of ``text``, with
    an optional plural ending ("s" / "es") on its last word."""
    haystack = f" {normalize(text)} "
    return any(f" {term}{end} " in haystack for term in terms for end in PLURALS)


def avoid_warnings(items: list[dict[str, Any]], avoid: list[dict[str, Any]]) -> list[dict]:
    """One warning per (item, EVITAR row) that match: ``item_index`` (position
    in ``items``), ``item_id`` (null for a new item), ``name``,
    ``substance``, ``reason``."""
    rows = [(r, avoid_terms(r.get("substance"))) for r in avoid]
    warnings = []
    for index, item in enumerate(items):
        text = " ".join(filter(None, (item.get("name"), item.get("strength"))))
        for row, terms in rows:
            if terms and matches(text, terms):
                warnings.append(
                    {
                        "item_index": index,
                        "item_id": item.get("id"),
                        "name": item.get("name"),
                        "substance": row.get("substance"),
                        "reason": row.get("reason"),
                    }
                )
    return warnings


# ── Out-of-date check ────────────────────────────────────────────────────────

CHECKS_LIMIT = 50
ENTRY_NOT_FOUND = "La entrada de bitácora no existe o es de otro paciente."


def reference_date(db: DbSession, patient_id: int) -> tuple[date | None, bool]:
    """``(«Desde», same_day_counts)`` of the current version; ``(None, False)``
    when the patient has no version. An explicit ``effective_date`` is the
    sheet's date: an entry of that day is taken as reflected. The fallback
    (the Panamá day of ``changed_at``) only says when it was saved: an entry
    of that day may not be in it, so it counts."""
    latest = meds.latest_version(db, patient_id)
    if latest is None:
        return None, False
    if latest.effective_date:
        return latest.effective_date, False
    return latest.changed_at.astimezone(PANAMA_TZ).date(), True


def _pending_origin_events(entry: LogEntry, done: set[int]) -> list[dict[str, Any]]:
    if entry.origin != ORIGIN:
        return []
    raw = (entry.origin_ref or {}).get("medication_events")
    if not isinstance(raw, list):
        return []
    out = []
    for index, event in enumerate(raw):
        if index in done or not isinstance(event, dict):
            continue
        kind = KIND_MAP.get(event.get("kind"))
        medication = event.get("medication")
        if kind is None or not isinstance(medication, str) or not medication.strip():
            continue
        detail = event.get("detail")
        out.append(
            {
                "kind": kind,
                "medication": medication.strip(),
                "detail": detail.strip() or None if isinstance(detail, str) else None,
            }
        )
    return out


def outdated_entries(db: DbSession, patient_id: int) -> dict[str, Any]:
    """``{reference_date, total, entries}``: the entries the banner lists
    (module docstring), oldest first, at most :data:`CHECKS_LIMIT`; ``total``
    counts them all. Each entry: ``id``, ``entry_date``, ``title``,
    ``events`` (``kind``, ``medication``, ``detail``)."""
    since, inclusive = reference_date(db, patient_id)
    if since is None:
        return {"reference_date": None, "total": 0, "entries": []}
    dismissed = select(MedCheckDismissal.entry_id).where(MedCheckDismissal.patient_id == patient_id)
    entries = db.scalars(
        select(LogEntry)
        .where(
            LogEntry.patient_id == patient_id,
            LogEntry.entry_date >= since if inclusive else LogEntry.entry_date > since,
            LogEntry.id.not_in(dismissed),
        )
        .order_by(LogEntry.entry_date, LogEntry.id)
    ).all()
    if not entries:
        return {"reference_date": since, "total": 0, "entries": []}

    ids = [e.id for e in entries]
    rows: dict[int, list[MedEvent]] = {}
    for row in db.scalars(
        select(MedEvent)
        .where(MedEvent.source_entry_id.in_(ids))
        .order_by(MedEvent.source_index.nulls_last(), MedEvent.id)
    ):
        rows.setdefault(row.source_entry_id, []).append(row)
    discarded = (
        discarded_sources(db)
        if any(
            e.origin == ORIGIN and (e.origin_ref or {}).get("medication_events") for e in entries
        )
        else set()
    )

    found = []
    for entry in entries:
        linked = rows.get(entry.id, [])
        done = {r.source_index for r in linked if r.source_index is not None}
        done |= {index for entry_id, index in discarded if entry_id == entry.id}
        events = [
            {"kind": r.kind, "medication": r.medication, "detail": r.detail} for r in linked
        ] + _pending_origin_events(entry, done)
        if events:
            found.append(
                {
                    "id": entry.id,
                    "entry_date": entry.entry_date,
                    "title": entry.title,
                    "events": events,
                }
            )
    return {"reference_date": since, "total": len(found), "entries": found[:CHECKS_LIMIT]}


def dismiss(db: DbSession, patient_id: int, entry_id: int, *, user_id: int | None) -> None:
    """«Ya está reflejado»: stop reporting ``entry_id`` (idempotent, audited).
    The caller commits. Raises :class:`meds.MedsError` (404) when the entry
    is missing or another patient's."""
    meds.get_patient(db, patient_id)
    entry = db.get(LogEntry, entry_id)
    if entry is None or entry.patient_id != patient_id:
        raise meds.MedsError(ENTRY_NOT_FOUND, status=404)
    # ON CONFLICT: two siblings tapping at once must not turn into a 500.
    row_id = db.scalar(
        pg_insert(MedCheckDismissal)
        .values(patient_id=patient_id, entry_id=entry_id, created_by=user_id)
        .on_conflict_do_nothing(index_elements=["entry_id"])
        .returning(MedCheckDismissal.id)
    )
    if row_id is None:
        return
    row = db.get(MedCheckDismissal, row_id)
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_CREATE,
        entity="med_check_dismissal",
        entity_id=row.id,
        after=audit.snapshot(row),
    )
