"""Levodopa Equivalent Daily Dose (LEDD) of a medication list (ticket #133,
DESIGN §5.4, §6.4).

Pure functions over snapshot-shaped items (``app.services.meds_diff``
documents the shape): no database, no I/O. The app **never acts** on the
number: it is a reference for talking to the neurologist, and the UI says it
is an approximation (a research convention, not a dose to give).

**LEDD** = Σ (daily dose of each dopaminergic drug × its conversion factor),
in mg/day of immediate-release levodopa.

**Reference table** (:data:`CATALOGUE`, versioned by :data:`TABLE_VERSION`;
bump it whenever a factor, name or rule changes, so a changed number is
traceable). Each entry cites its source:

- Tomlinson CL et al., "Systematic review of levodopa dose equivalency
  reporting in Parkinson's disease", *Mov Disord* 2010;25:2649-2653 — the
  most cited table and the convention the family already uses by hand, so it
  is the base wherever it has the drug.
- Jost ST et al., "Levodopa Dose Equivalency in Parkinson's Disease: Updated
  Systematic Review and Proposals", *Mov Disord* 2023;38:1236-1252 — used for
  drugs Tomlinson lacks (opicapone, safinamide). Where Jost 2023 differs from
  Tomlinson it is noted on the entry (``note``): rotigotine × 30.3 (we keep
  × 30), safinamide 150 (Schade 2020: 100).
- Schade S et al., *Mov Disord Clin Pract* 2020;7:343-345 (opicapone,
  safinamide; superseded by Jost 2023 for safinamide).

Entries marked "Jost 2023 no verificado" could not be checked against the
2023 paper's table; Tomlinson's value is kept.

**Matching** (:func:`match_substance`): the item's name (plus strength and
dose note for the CR / sublingual markers) is normalized like
``meds_checks.normalize`` and searched for each entry's names as whole
words, most specific entry first (Stalevo before levodopa, CR before IR).

**Daily dose of an item**: active mg per unit (:func:`active_per_unit`: the
strength, or the levodopa content of a combination — Prolopa 250 is 200 mg
levodopa + 50 mg benserazide) × quantity (½, ¼, "2 cápsulas", "1 parche") ×
days per week / 7 (``days``). Each item is one intake (the list has one item
per time of day), so four Prolopa items add up.

**Buckets** (:func:`compute`):

- ``contributions`` — counted, grouped by substance;
- ``as_needed`` — ``segun_necesidad`` items and conditional ones ("si…",
  "cuando despierte", "en caso de"…): never counted, shown as
  "+ hasta N mg si se da" (family decision, 2026-10-11);
- ``not_calculated`` — a dopaminergic item whose dose cannot be read without
  guessing (no or unreadable strength / quantity, an ambiguous combination
  strength, a frequency written as text): listed with a reason, never
  guessed;
- ``pending`` — a name outside the catalogue that the AI classification
  (``app.services.ledd_substances``) proposed as dopaminergic (or was unsure
  about): "pendiente de confirmar" until an admin confirms it; never counted.

A substance confirmed by an admin (``learned``) is counted like a catalogue
entry with its confirmed factor. Names neither in the catalogue nor learned
add 0 silently (most of a list is not dopaminergic).

**COMT inhibitors** (entacapone, tolcapone, opicapone) do not have a dose
factor: their contribution is the levodopa LED (IR + CR×0.75) × ratio,
whatever their own dose, once per substance (Tomlinson 2010; opicapone per
Jost 2023 / Schade 2020). Stalevo carries its own entacapone: its levodopa
counts × 1 and its row adds × 0.33, and it is left out of the base of a
separate COMT inhibitor. **Safinamide** is a fixed 150 mg whatever its dose
(Jost 2023).
"""

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

TABLE_VERSION = "2026-10-11.1"

TOMLINSON = "Tomlinson 2010"
JOST = "Jost 2023"
TOMLINSON_UNVERIFIED = "Tomlinson 2010 (Jost 2023 no verificado)"

AS_NEEDED_SLOT = "segun_necesidad"

# kind: "dose" = daily mg × factor; "comt" = levodopa LED × factor; "fixed" =
# factor mg/day whenever the drug is taken.
DOSE, COMT, FIXED = "dose", "comt", "fixed"


@dataclass(frozen=True)
class Substance:
    key: str
    label: str  # Spanish display name of the substance
    factor: float
    source: str
    names: tuple[str, ...]  # normalized on load
    kind: str = DOSE
    # How the unit's active amount is read from the strength (see active_per_unit).
    content: str = "single"
    # Words (in name, strength or dose note) required for this entry to match.
    markers: tuple[str, ...] = ()
    note: str | None = None
    is_levodopa: bool = False  # part of the base of a COMT inhibitor


CR_MARKERS = ("hbs", "cr", "lp", "retard", "liberacion prolongada", "liberacion controlada")
_LEVODOPA_NAMES = (
    "levodopa",
    "prolopa",
    "madopar",
    "sinemet",
    "benserazida",
    "carbidopa",
)

CATALOGUE: tuple[Substance, ...] = (
    Substance(
        key="levodopa_entacapona",
        label="Levodopa + entacapona (Stalevo)",
        factor=1.33,
        source=TOMLINSON,
        names=("stalevo", "levodopa carbidopa entacapona", "carbidopa levodopa entacapona"),
        content="first",
        note="Levodopa × 1 más entacapona × 0.33 de esa levodopa.",
        is_levodopa=False,
    ),
    Substance(
        key="levodopa_cr",
        label="Levodopa de liberación prolongada",
        factor=0.75,
        source=TOMLINSON,
        names=_LEVODOPA_NAMES,
        markers=CR_MARKERS,
        content="levodopa",
        note="Jost 2023 mantiene × 0.75 (liberación controlada).",
        is_levodopa=True,
    ),
    Substance(
        key="levodopa",
        label="Levodopa",
        factor=1.0,
        source=TOMLINSON,
        names=_LEVODOPA_NAMES,
        content="levodopa",
        note="Cuenta la levodopa que contiene, no el total de la tableta.",
        is_levodopa=True,
    ),
    Substance(
        key="entacapona",
        label="Entacapona",
        factor=0.33,
        source=TOMLINSON,
        names=("entacapona", "entacapone", "comtan"),
        kind=COMT,
    ),
    Substance(
        key="tolcapona",
        label="Tolcapona",
        factor=0.5,
        source=TOMLINSON,
        names=("tolcapona", "tolcapone", "tasmar"),
        kind=COMT,
    ),
    Substance(
        key="opicapona",
        label="Opicapona",
        factor=0.5,
        source=JOST,
        names=("opicapona", "opicapone", "ongentys"),
        kind=COMT,
        note="Schade 2020 propuso el mismo × 0.5.",
    ),
    Substance(
        key="pramipexol",
        label="Pramipexol",
        factor=100.0,
        source=TOMLINSON,
        names=("pramipexol", "pramipexole", "mirapex", "mirapexin", "sifrol", "parmital"),
        content="pramipexole",
        note="× 100 por mg de sal (Jost 2023: × 142.86 por mg de base).",
    ),
    Substance(
        key="ropinirol",
        label="Ropinirol",
        factor=20.0,
        source=TOMLINSON,
        names=("ropinirol", "ropinirole", "requip", "adartrel"),
        note="Jost 2023 mantiene × 20.",
    ),
    Substance(
        key="rotigotina",
        label="Rotigotina (parche)",
        factor=30.0,
        source=TOMLINSON,
        names=("rotigotina", "rotigotine", "neupro"),
        note="Parche en mg/24 h. Jost 2023 propone × 30.3.",
    ),
    Substance(
        key="apomorfina",
        label="Apomorfina",
        factor=10.0,
        source=TOMLINSON_UNVERIFIED,
        names=("apomorfina", "apomorphine", "apokyn", "apo go"),
    ),
    Substance(
        key="piribedil",
        label="Piribedil",
        factor=1.0,
        source=TOMLINSON_UNVERIFIED,
        names=("piribedil", "trivastal"),
    ),
    Substance(
        key="bromocriptina",
        label="Bromocriptina",
        factor=10.0,
        source=TOMLINSON_UNVERIFIED,
        names=("bromocriptina", "bromocriptine", "parlodel"),
    ),
    Substance(
        key="cabergolina",
        label="Cabergolina",
        factor=80.0,
        source=TOMLINSON_UNVERIFIED,
        names=("cabergolina", "cabergoline", "dostinex", "cabaser"),
    ),
    Substance(
        key="selegilina_sublingual",
        label="Selegilina sublingual",
        factor=80.0,
        source=TOMLINSON,
        names=("selegilina", "selegiline", "zelapar"),
        markers=("sublingual", "zelapar"),
        note="Jost 2023 mantiene × 80.",
    ),
    Substance(
        key="selegilina",
        label="Selegilina (oral)",
        factor=10.0,
        source=TOMLINSON_UNVERIFIED,
        names=("selegilina", "selegiline", "eldepryl", "jumex"),
    ),
    Substance(
        key="rasagilina",
        label="Rasagilina",
        factor=100.0,
        source=TOMLINSON,
        names=("rasagilina", "rasagiline", "azilect"),
        note="Jost 2023 mantiene × 100.",
    ),
    Substance(
        key="safinamida",
        label="Safinamida",
        factor=150.0,
        source=JOST,
        names=("safinamida", "safinamide", "xadago"),
        kind=FIXED,
        note="150 mg fijos con 50 o 100 mg/día (Schade 2020 proponía 100).",
    ),
    Substance(
        key="amantadina",
        label="Amantadina",
        factor=1.0,
        source=TOMLINSON_UNVERIFIED,
        names=("amantadina", "amantadine", "symmetrel", "pk merz"),
    ),
)

# Pramipexole strengths that only exist as mg of *base* (EU labelling): 0.088
# base = 0.125 salt, 0.18 = 0.25, 0.35 = 0.5, 0.7 = 1.0; ER 0.26/0.52/1.05/2.1.
PRAMIPEXOLE_BASE_STRENGTHS = frozenset({0.088, 0.18, 0.35, 0.7, 0.26, 0.52, 1.05, 2.1})
PRAMIPEXOLE_BASE_TO_SALT = 1.4286  # Jost 2023: × 142.86 per mg base vs × 100 per mg salt

# Levodopa/benserazide tablets written by their total strength (4:1).
BENSERAZIDE_TOTALS = {62.5: 50.0, 125.0: 100.0, 250.0: 200.0}
# Levodopa/carbidopa written as one number: only the levodopa amounts sold.
CARBIDOPA_LEVODOPA = frozenset({100.0, 200.0, 250.0})

REASONS = {
    "no_strength": "Falta la concentración (mg).",
    "bad_strength": "No se pudo leer la concentración.",
    "ambiguous_strength": "No se sabe cuánta levodopa contiene cada tableta.",
    "no_quantity": "Falta la cantidad.",
    "bad_quantity": "No se pudo leer la cantidad.",
    "text_schedule": "La frecuencia está escrita como texto.",
}

# ── Normalization ────────────────────────────────────────────────────────────

_NON_WORD = re.compile(r"[^\w]+")
_MARKS = re.compile(r"[™℠®©]")


def normalize(text: str | None) -> str:
    """Casefolded, accent-free, punctuation as single spaces (like
    ``meds_checks.normalize``, kept local so this module stays dependency-free)."""
    if not text:
        return ""
    text = _MARKS.sub(" ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(_NON_WORD.sub(" ", text.casefold()).replace("_", " ").split())


_DOSE_UNITS = frozenset({"mg", "mcg", "g", "ml", "ui", "mg ml"})


def name_key(name: str | None) -> str:
    """The key of a medication name for learned substances (normalized name,
    without numbers: «Losartán 50 mg» and «Losartan» share it)."""
    words = [
        w
        for w in normalize(name).split()
        if not any(ch.isdigit() for ch in w) and w not in _DOSE_UNITS
    ]
    return " ".join(words)[:200]


def _has_phrase(haystack: str, phrase: str) -> bool:
    return f" {phrase} " in f" {haystack} "


def match_substance(item: dict[str, Any]) -> Substance | None:
    """The catalogue entry for an item, or None."""
    name = normalize(item.get("name"))
    context = " ".join(
        normalize(item.get(field)) for field in ("name", "strength", "dose_note")
    ).strip()
    for entry in CATALOGUE:
        if not any(_has_phrase(name, n) for n in entry.names):
            continue
        if entry.markers and not any(_has_phrase(context, m) for m in entry.markers):
            continue
        return entry
    return None


# ── Parsing ──────────────────────────────────────────────────────────────────

_VULGAR = {"½": Fraction(1, 2), "¼": Fraction(1, 4), "¾": Fraction(3, 4), "⅓": Fraction(1, 3)}
_WORDS = {
    "un": 1,
    "una": 1,
    "uno": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "medio": Fraction(1, 2),
    "media": Fraction(1, 2),
}
UNIT_WORDS = (
    "tableta",
    "tabletas",
    "tab",
    "tabs",
    "comprimido",
    "comprimidos",
    "pastilla",
    "pastillas",
    "capsula",
    "capsulas",
    "cap",
    "caps",
    "parche",
    "parches",
    "unidad",
    "unidades",
)
_NUMBER = r"\d+(?:[.,]\d+)?"
_QTY = re.compile(
    rf"^(?:(?P<int>\d+)\s*)?(?P<frac>[½¼¾⅓])$|^(?P<a>\d+)\s*/\s*(?P<b>\d+)$|^(?P<num>{_NUMBER})$"
)


def _to_float(text: str) -> float:
    return float(text.replace(",", "."))


def normalize_keep_numbers(text: str) -> str:
    """Casefolded and accent-free (canonical decomposition, so «½» survives),
    keeping digits, ``.``, ``,`` and ``/`` (for quantities and strengths)."""
    out = unicodedata.normalize("NFD", text)
    out = "".join(c for c in out if not unicodedata.combining(c))
    return " ".join(out.casefold().split())


_UNIT_SUFFIX = re.compile(r"\s*\b(?:" + "|".join(UNIT_WORDS) + r")\b\.?$")


def parse_quantity(text: str | None) -> float | None:
    """Units per intake: "½" 0.5, "1 ½" 1.5, "1/4", "2", "1,5", "dos",
    "1 CAPSULA", "3 Capsulas", "1 parche". Anything else (ranges, grams,
    drops, ml) → None."""
    if text is None:
        return None
    s = _UNIT_SUFFIX.sub("", normalize_keep_numbers(text)).strip()
    if s in _WORDS:
        return float(_WORDS[s])
    m = _QTY.match(s)
    if not m:
        return None
    if m.group("frac"):
        whole = int(m.group("int")) if m.group("int") else 0
        return float(whole + _VULGAR[m.group("frac")])
    if m.group("a"):
        b = int(m.group("b"))
        return int(m.group("a")) / b if b else None
    value = _to_float(m.group("num"))
    return value if value > 0 else None


_STRENGTH = re.compile(
    rf"^(?P<nums>{_NUMBER}(?:\s*/\s*{_NUMBER})*)\s*(?P<unit>mg|mcg|µg|ug|g)?(?:\s*/\s*24\s*h(?:oras)?)?$"
)


def parse_strength(text: str | None) -> list[float] | None:
    """The strength's numbers in mg: "250 mg" [250], "0.25 mg", "2.5 MG",
    "20mg", "100 mcg" [0.1], "25/250 mg" [25, 250], "4 mg/24 h" [4]. A
    bare number is taken as mg. Percentages, ml and anything else → None."""
    if text is None:
        return None
    raw = normalize_keep_numbers(text).replace("μ", "µ")
    m = _STRENGTH.match(raw)
    if not m:
        return None
    unit = m.group("unit") or "mg"
    scale = {"mg": 1.0, "g": 1000.0}.get(unit, 0.001)
    return [_to_float(n) * scale for n in re.split(r"\s*/\s*", m.group("nums"))]


def active_per_unit(entry: Substance, item: dict[str, Any]) -> tuple[float | None, str | None]:
    """(active mg per unit, None) or (None, reason code)."""
    if not (item.get("strength") or "").strip():
        return None, "no_strength"
    numbers = parse_strength(item.get("strength"))
    if not numbers:
        return None, "bad_strength"
    name = normalize(item.get("name"))
    if entry.content == "first":  # Stalevo: levodopa/carbidopa/entacapone
        return numbers[0], None
    if entry.content == "levodopa":
        if len(numbers) > 1:  # "25/250", "200/50": levodopa is the larger number
            return max(numbers), None
        value = numbers[0]
        if any(_has_phrase(name, n) for n in ("prolopa", "madopar", "benserazida")):
            levodopa = BENSERAZIDE_TOTALS.get(value)
            return (levodopa, None) if levodopa else (None, "ambiguous_strength")
        if any(_has_phrase(name, n) for n in ("sinemet", "carbidopa")):
            return (value, None) if value in CARBIDOPA_LEVODOPA else (None, "ambiguous_strength")
        return value, None
    if len(numbers) != 1:
        return None, "ambiguous_strength"
    value = numbers[0]
    if entry.content == "pramipexole" and round(value, 3) in PRAMIPEXOLE_BASE_STRENGTHS:
        return value * PRAMIPEXOLE_BASE_TO_SALT, None
    return value, None


_CONDITIONAL = re.compile(
    r"\b(si|cuando|en caso|segun necesidad|si necesita|rescate|prn|de ser necesario)\b"
)
_FREQUENCY = re.compile(r"\b(cada|veces)\b|\bc/")


def is_conditional(item: dict[str, Any]) -> bool:
    if item.get("slot") == AS_NEEDED_SLOT:
        return True
    text = " ".join(normalize(item.get(f)) for f in ("schedule_note", "instructions", "dose_note"))
    return bool(_CONDITIONAL.search(text))


def _text_frequency(item: dict[str, Any]) -> bool:
    text = normalize_keep_numbers(item.get("schedule_note") or "")
    return bool(_FREQUENCY.search(text))


def days_fraction(days: list[int] | None) -> float:
    return len(set(days)) / 7 if days else 1.0


def _round(value: float) -> float:
    return round(value, 2)


# ── Computation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Learned:
    """A name outside the catalogue with an AI proposal or an admin decision
    (``ledd_substances`` row, passed in by the caller)."""

    id: int
    status: str  # queued | proposed | confirmed | rejected | failed
    is_dopaminergic: bool | None
    factor: float | None
    mechanism: str | None
    confidence: str | None
    label: str | None = None
    source: str | None = None


def needs_review(row: Learned) -> bool:
    """A proposal shown as «pendiente de confirmar»: dopaminergic, or unsure."""
    return row.status == "proposed" and (
        row.is_dopaminergic is not False or row.confidence != "high"
    )


def _learned_substance(row: Learned, name: str) -> Substance:
    kind = COMT if row.mechanism == "comt_inhibitor" else DOSE
    return Substance(
        key=f"learned:{row.id}",
        label=row.label or name,
        factor=float(row.factor or 0),
        source=f"Confirmado por un administrador ({row.source})"
        if row.source
        else "Confirmado por un administrador",
        names=(),
        kind=kind,
    )


def _item_ref(item: dict[str, Any]) -> dict[str, Any]:
    return {"item_id": item.get("id"), "name": item.get("name"), "strength": item.get("strength")}


def compute(
    items: list[dict[str, Any]], learned: dict[str, Learned] | None = None
) -> dict[str, Any]:
    """The LEDD of a list (module docstring has the rules). Returns::

    {"table_version", "total_mg" (None when nothing dopaminergic is
     counted or pending), "has_dopaminergic",
     "contributions": [{"substance", "label", "kind", "factor", "source",
                        "note", "item_ids", "names", "daily_dose_mg", "mg"}],
     "as_needed": [{"item_id", "name", "strength", "substance", "label",
                    "factor", "source", "max_mg"}],
     "not_calculated": [{"item_id", "name", "strength", "label", "reason",
                         "reason_text"}],
     "pending": [{"item_id", "name", "strength", "substance_id",
                  "proposed_factor", "mechanism", "confidence"}]}
    """
    learned = learned or {}
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    as_needed: list[dict[str, Any]] = []
    not_calculated: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    comt: dict[str, tuple[Substance, list[dict[str, Any]]]] = {}
    fixed: dict[str, tuple[Substance, list[dict[str, Any]]]] = {}

    for item in items:
        entry = match_substance(item)
        if entry is None:
            row = learned.get(name_key(item.get("name")))
            if row is None:
                continue
            if row.status == "confirmed" and row.is_dopaminergic and row.factor:
                entry = _learned_substance(row, item.get("name") or "")
            else:
                if needs_review(row):
                    pending.append(
                        {
                            **_item_ref(item),
                            "substance_id": row.id,
                            "proposed_factor": row.factor,
                            "mechanism": row.mechanism,
                            "confidence": row.confidence,
                        }
                    )
                continue

        conditional = is_conditional(item)
        if entry.kind in (COMT, FIXED):
            if conditional:
                as_needed.append(
                    {
                        **_item_ref(item),
                        "substance": entry.key,
                        "label": entry.label,
                        "factor": entry.factor,
                        "source": entry.source,
                        "max_mg": None,
                    }
                )
                continue
            bucket = comt if entry.kind == COMT else fixed
            bucket.setdefault(entry.key, (entry, []))[1].append(item)
            continue

        per_unit, reason = active_per_unit(entry, item)
        quantity = parse_quantity(item.get("quantity"))
        if reason is None and quantity is None:
            reason = "no_quantity" if not (item.get("quantity") or "").strip() else "bad_quantity"
        if reason is None and not conditional and _text_frequency(item):
            reason = "text_schedule"
        if reason is not None:
            not_calculated.append(
                {
                    **_item_ref(item),
                    "label": entry.label,
                    "reason": reason,
                    "reason_text": REASONS[reason],
                }
            )
            continue
        dose = per_unit * quantity
        if conditional:
            as_needed.append(
                {
                    **_item_ref(item),
                    "substance": entry.key,
                    "label": entry.label,
                    "factor": entry.factor,
                    "source": entry.source,
                    "max_mg": _round(dose * entry.factor),
                }
            )
            continue
        daily = dose * days_fraction(item.get("days"))
        group = groups.get(entry.key)
        if group is None:
            group = groups[entry.key] = {
                "entry": entry,
                "item_ids": [],
                "names": [],
                "daily_dose_mg": 0.0,
            }
            order.append(entry.key)
        group["item_ids"].append(item.get("id"))
        if item.get("name") not in group["names"]:
            group["names"].append(item.get("name"))
        group["daily_dose_mg"] += daily

    contributions = []
    for key in order:
        group = groups[key]
        entry: Substance = group["entry"]
        contributions.append(
            _contribution(
                entry,
                group["item_ids"],
                group["names"],
                group["daily_dose_mg"],
                group["daily_dose_mg"] * entry.factor,
            )
        )

    levodopa_base = sum(c["mg"] for c in contributions if _is_levodopa(c["substance"]))
    for entry, rows in comt.values():
        dose = _sum_doses(entry, rows)
        contributions.append(
            _contribution(
                entry,
                [r.get("id") for r in rows],
                _names(rows),
                dose,
                levodopa_base * entry.factor,
                base_mg=levodopa_base,
            )
        )
    for entry, rows in fixed.values():
        dose = _sum_doses(entry, rows)
        contributions.append(
            _contribution(entry, [r.get("id") for r in rows], _names(rows), dose, entry.factor)
        )

    has = bool(contributions or as_needed or not_calculated or pending)
    total = _round(sum(c["mg"] for c in contributions)) if contributions else None
    return {
        "table_version": TABLE_VERSION,
        "total_mg": total if has else None,
        "has_dopaminergic": has,
        "contributions": contributions,
        "as_needed": as_needed,
        "not_calculated": not_calculated,
        "pending": pending,
    }


_LEVODOPA_KEYS = frozenset(e.key for e in CATALOGUE if e.is_levodopa)


def _is_levodopa(key: str) -> bool:
    return key in _LEVODOPA_KEYS


def _names(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for r in rows:
        if r.get("name") not in out:
            out.append(r.get("name"))
    return out


def _sum_doses(entry: Substance, rows: list[dict[str, Any]]) -> float | None:
    """The COMT / fixed drug's own daily mg, for display only (None if unreadable)."""
    total = 0.0
    for r in rows:
        per_unit, reason = active_per_unit(entry, r)
        quantity = parse_quantity(r.get("quantity"))
        if reason or quantity is None:
            return None
        total += per_unit * quantity * days_fraction(r.get("days"))
    return round(total, 3)


def _contribution(
    entry: Substance,
    item_ids: list[Any],
    names: list[str],
    daily: float | None,
    mg: float,
    *,
    base_mg: float | None = None,
) -> dict[str, Any]:
    out = {
        "substance": entry.key,
        "label": entry.label,
        "kind": entry.kind,
        "factor": entry.factor,
        "source": entry.source,
        "note": entry.note,
        "item_ids": item_ids,
        "names": names,
        "daily_dose_mg": round(daily, 3) if daily is not None else None,
        "mg": _round(mg),
    }
    if entry.kind == COMT:
        out["base_mg"] = _round(base_mg or 0)
    return out


def change(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    """«LEDD: 920 → 1040 mg/día (+13 %)» data, or None when it did not change
    (or neither side has a number). ``pct`` is None when ``before`` is 0/None."""
    old = (before or {}).get("total_mg")
    new = (after or {}).get("total_mg")
    if old is None and new is None:
        return None
    if old is not None and new is not None and round(old, 1) == round(new, 1):
        return None
    pct = None
    if old:
        pct = round((float(new or 0) - old) / old * 100)
    return {"before_mg": old, "after_mg": new, "pct": pct}


def format_mg(value: float) -> str:
    """At most one decimal, period as separator like Panamá (and labs.js): 412.5, 800."""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text


def catalogue_rows() -> list[dict[str, Any]]:
    """The reference table for display (the breakdown's «Fuentes»)."""
    return [
        {
            "substance": e.key,
            "label": e.label,
            "kind": e.kind,
            "factor": e.factor,
            "source": e.source,
            "note": e.note,
        }
        for e in CATALOGUE
    ]
