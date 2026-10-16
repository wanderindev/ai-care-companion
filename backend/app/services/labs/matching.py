"""Who a lab report belongs to, and which analyte each result is.

Patient (``match_patient``). The family prefers a review to a wrong
attribution, so a report is assigned only on strong evidence; anything else
gives ``patient_id`` NULL and ``needs_review``. Names are compared as sets of
normalized words (accents, case, punctuation and order ignored; particles
such as "de", "del", "la", "y" dropped).

1. **ID number (CIP)**: the extracted ID, normalized (``normalize_id_number``),
   equals exactly one patient's ``id_number`` → ``cip``, unless the report's
   name has a word that patient's ``full_name`` lacks (CSS forms for a
   dependent may print the insured relative's cédula: Mamá's lab with Papá's
   number, or a sibling's). A one-word name confirms the CIP only when no
   other patient's name has that word (a lone shared surname → review). A
   report without a name, or a patient without a ``full_name`` on file, is
   decided by the CIP alone. An ID that matches nobody is never overridden by
   the name.
2. **Name**, only when the report carries no ID or no patient has an ID on
   file: the report's words must **equal** the patient's ``full_name`` words
   (at least two). A subset (a lab that omits the married name) or extra
   words (a son named after his father, "Juan Ficticio Prueba Otro") → no
   match. Two patients with the same words → no match. A relative with
   exactly the patient's name and no ID cannot be told apart; that limit is
   accepted.

``identify_patient`` (#98) is what extraction and the re-match use: the
rules above, then two more steps, only when the ID number does not decide:

3. **Name variant** (``name_variant``): the report's words, put in "given
   names, surnames" order (a "SURNAME(S), GIVEN" form has the part before
   the comma moved to the end), appear **in the same order** in exactly one
   patient's ``full_name``, starting with its first given name, with at
   least two whole words; only words may be missing. Single letters are
   initials of the word in their place; accents and case never matter. And
   (operator's rule, #98) the name must carry a surname the couple's
   children never carry (``distinguishes``): the patient's own **2nd
   surname**, or for a married woman "**de** <husband's surname>" with the
   "de" printed. The children are "<given> <father's 1st surname> <mother's
   1st surname>" and may carry their parents' given names, so a name with
   only given names and 1st surnames ("Juan Carlos Pérez", "Ana María
   Pérez", "Ana López") never decides: Claude, then review. The surnames
   come from ``split_full_name`` (cédula order "<given…> <1st> <2nd> [de
   <husband>]"). Any extra word or a fit with two patients → no.
4. **Claude** (``llm``, ``patient_llm``): sees only the printed name and ID
   and the patients' names; only a ``high`` confidence answer whose name
   also passes ``distinguishes`` assigns (else ``llm_not_distinct``), and the
   report stays «Por revisar» (``process.confident``) until a person
   confirms it. The exact name (rule 2) needs ``distinguishes`` too inside
   ``identify_patient``.

"The ID number does not decide" means: none printed, none on file, or one
that matches nobody and does not look like a Panamanian cédula
(``looks_like_cedula``: e.g. a US lab's own patient number). An ID that
matches a patient is final (``cip``, or the insured-relative rule's no
match); a cédula that matches nobody is final too (``id_mismatch``). The
result says why a report stays unassigned (``Identification.reason``), for
the re-match counts; logs carry these codes only, never a name.

Patient hint (``apply_patient_hint``, manual upload #15). The uploader may
say whose report it is. The hint never overrides the document: a CIP/name
match to the **other** patient leaves the report unassigned (``needs_review``,
the family decides); a match to the hinted patient keeps its ``cip``/``name``
method. Without a match, the hint assigns the patient (``manual``) only when
the document does not point elsewhere: no ID number that could be compared
(none printed, or no patient has one on file), and a name that is absent or
whose words are all in the hinted patient's ``full_name`` (a lab that omits
the married name, or prints only the surname). Any other identifying text →
no patient, ``needs_review``. A report assigned by the hint alone still
stays ``needs_review`` (``process.save_report``) until someone confirms it.

Analytes (``AnalyteMatcher``): the normalized raw name (``normalize_key``)
against every analyte's canonical name and aliases **of the same specimen**
(a urinalysis "Glucosa" is not blood glucose; a missing or unknown specimen
is ``otro``, never blood). A matched percentage analyte ("Neutrófilos", unit
%) whose result comes in another unit is looked up again as the absolute
count ("<name> #"). Unknown names create a new analyte with
``needs_review = true`` (audited, no user), reused for the rest of the run.
"""

import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.lab import SPECIMENS, Analyte
from app.models.patient import Patient
from app.services import audit
from app.services.labs.values import normalize_id_number, normalize_key, strip_accents

PARTICLES = frozenset({"de", "del", "la", "las", "los", "y", "e", "van", "von", "da", "do"})
MIN_NAME_WORDS = 2
TITLES = frozenset(
    {"sr", "sra", "srta", "senor", "senora", "dr", "dra", "don", "dona", "mr", "mrs", "ms"}
)


class PatientMatch(NamedTuple):
    patient: Patient | None
    method: str | None  # "cip" | "name" | "name_variant" | "llm" | "manual" | None


def ordered_name_words(value: str | None) -> list[str]:
    """Normalized words of a name, in order, without particles or numbers."""
    if not value:
        return []
    words = normalize_key(strip_accents(value).replace("%", " ").replace("#", " ")).split()
    return [w for w in words if w not in PARTICLES and not w.isdigit()]


def name_words(value: str | None) -> set[str]:
    return set(ordered_name_words(value))


NO_MATCH = PatientMatch(None, None)


def match_patient(patients: list[Patient], name: str | None, id_number: str | None) -> PatientMatch:
    report_words = name_words(name)
    key = normalize_id_number(id_number)
    on_file = {p.id: normalize_id_number(p.id_number) for p in patients if p.id_number}
    if key and on_file:
        hits = [p for p in patients if on_file.get(p.id) == key]
        if len(hits) != 1:
            return NO_MATCH
        hit = hits[0]
        own = name_words(hit.full_name)
        # The CIP may be the insured relative's ("asegurado"): a name with any
        # word the patient's own name lacks is someone else's report.
        if report_words and own and not report_words <= own:
            return NO_MATCH
        # A one-word name confirms nothing when another patient's name has
        # that word too (the shared surname).
        if len(report_words) == 1 and own:
            others = set().union(*(name_words(p.full_name) for p in patients if p is not hit))
            if report_words & others:
                return NO_MATCH
        return PatientMatch(hit, "cip")

    if len(report_words) < MIN_NAME_WORDS:
        return NO_MATCH
    exact = [p for p in patients if name_words(p.full_name) == report_words]
    return PatientMatch(exact[0], "name") if len(exact) == 1 else NO_MATCH


# A Panamanian cédula: province (1-13, optionally AV/PI), PE, E or N, then
# two numeric groups with separators ("8-123-4567", "PE 12 345").
_CEDULA = re.compile(
    r"^\s*(?:\d{1,2}(?:\s*-?\s*(?:AV|PI))?|PE|E|N)\s*[-\s.]\s*\d{1,5}\s*[-\s.]\s*\d{1,6}\s*$",
    re.IGNORECASE,
)


def looks_like_cedula(value: str | None) -> bool:
    return bool(value) and bool(_CEDULA.match(strip_accents(value)))


def _tokens(value: str | None) -> list[str]:
    """Normalized words in order, particles kept ("de" matters for the married
    part), numbers dropped."""
    if not value:
        return []
    words = normalize_key(strip_accents(value).replace("%", " ").replace("#", " ")).split()
    return [w for w in words if not w.isdigit()]


def report_tokens(name: str | None) -> list[str]:
    """The report's name tokens in "given names, surnames" order: a
    "SURNAME(S), GIVEN NAME(S)" form has its part before the comma moved to
    the end. Particles are kept."""
    if not name:
        return []
    before, comma, after = name.partition(",")
    if comma and ordered_name_words(after) and ordered_name_words(before):
        return _tokens(after) + _tokens(before)
    return _tokens(name)


class NameParts(NamedTuple):
    """A cédula name, "<given…> <1st surname> <2nd surname> [de <husband's
    surname>]" (see ``split_full_name``)."""

    words: list[str]  # every word in order, particles dropped
    first: str | None  # 1st surname
    second: str | None  # 2nd surname
    married: list[str]  # the words after the married "de"


def _parts(core: list[str], married: list[str]) -> NameParts:
    if len(core) >= 3:
        return NameParts(core + married, core[-2], core[-1], married)
    if len(core) == 2:
        return NameParts(core + married, core[1], None, married)
    return NameParts(core + married, None, None, married)


def name_readings(full_name: str | None) -> list[NameParts]:
    """The plausible readings of a cédula name "<given…> <1st surname> <2nd
    surname> [de <husband's surname(s)>]": always the plain one (the last
    two words are the surnames), plus a married one when the last "de" has at
    least two words before it and one or two after it ("María de los Ángeles
    …" is a given name: one word before). With only two words, the second is
    the 1st surname and there is no 2nd. "Juan Carlos Pérez de León" reads
    both ways: 2nd surname «de León», or 1st "Carlos" + 2nd "Pérez" + married
    "León"; ``resolve_couple`` decides, or keeps both."""
    tokens = _tokens(full_name)
    readings = [_parts([w for w in tokens if w not in PARTICLES], [])]
    if "de" in tokens:
        i = len(tokens) - 1 - tokens[::-1].index("de")
        before = [w for w in tokens[:i] if w not in PARTICLES]
        after = [w for w in tokens[i + 1 :] if w not in PARTICLES]
        if len(before) >= 2 and 1 <= len(after) <= 2:
            readings.append(_parts(before, after))
    return readings


def split_full_name(full_name: str | None) -> NameParts:
    """The married reading when there is one, else the plain one (see
    ``name_readings``; ``resolve_couple`` decides which one holds)."""
    return name_readings(full_name)[-1]


class Reading(NamedTuple):
    readings: list[NameParts]  # every reading still plausible
    married: list[str]  # a confirmed married part ("de" + these words), or []


def resolve_couple(patients: Sequence[Patient]) -> dict[int, Reading]:
    """A married part is confirmed only when it names another patient's 1st
    surname (under any reading): the wife's "de <husband's 1st surname>".
    Then only that reading holds. Otherwise both readings stay plausible and
    the "de …" part never counts as a married marker."""
    raw = {p.id: name_readings(p.full_name) for p in patients}
    firsts = {pid: {r.first for r in rs if r.first} for pid, rs in raw.items()}
    out: dict[int, Reading] = {}
    for pid, readings in raw.items():
        others = set().union(*(f for other, f in firsts.items() if other != pid))
        married = [r for r in readings if r.married]
        if married and married[0].married[0] in others:
            out[pid] = Reading([married[0]], married[0].married)
        else:
            out[pid] = Reading(readings, [])
    return out


def _contains(tokens: list[str], part: list[str]) -> bool:
    n = len(part)
    return any(tokens[i : i + n] == part for i in range(len(tokens) - n + 1))


def distinguishes(patient: Patient, patients: Sequence[Patient], tokens: list[str]) -> bool:
    """The printed name (``report_tokens``) carries a surname the couple's
    children never carry: a confirmed married "de <husband's surname>" with
    the "de" printed (``resolve_couple``), or the patient's own 2nd surname —
    under any plausible reading — that is no patient's 1st surname under any
    plausible reading (the children carry those) and no word of another
    patient's name. When the split is unclear, a word that could be anyone's
    1st surname never distinguishes."""
    couple = resolve_couple(patients)
    own = couple.get(patient.id)
    if own is None:
        return False
    if own.married and _contains(tokens, ["de", *own.married]):
        return True
    children = {r.first for c in couple.values() for r in c.readings if r.first}
    others = set().union(*(name_words(p.full_name) for p in patients if p is not patient))
    return any(
        r.second in tokens and r.second not in children and r.second not in others
        for r in own.readings
        if r.second
    )


def _in_order(report: list[str], own: list[str]) -> bool:
    """``report`` is a subsequence of ``own``; a single letter stands for the
    next word with that initial."""
    i = 0
    for word in report:
        while i < len(own) and not (own[i] == word or (len(word) == 1 and own[i].startswith(word))):
            i += 1
        if i == len(own):
            return False
        i += 1
    return True


def match_name_variant(patients: Sequence[Patient], name: str | None) -> PatientMatch:
    """Rule 3 (see the module docstring): a tolerant name match, or nobody."""
    tokens = report_tokens(name)
    report = [w for w in tokens if w not in PARTICLES]
    if len({w for w in report if len(w) > 1}) < MIN_NAME_WORDS:
        return NO_MATCH
    hits = []
    for patient in patients:
        own = split_full_name(patient.full_name).words
        if (
            len(own) >= MIN_NAME_WORDS
            and report[0] == own[0]
            and _in_order(report, own)
            and distinguishes(patient, patients, tokens)
        ):
            hits.append(patient)
    return PatientMatch(hits[0], "name_variant") if len(hits) == 1 else NO_MATCH


class Identification(NamedTuple):
    match: PatientMatch
    # Why no patient: "no_name", "id_mismatch", "id_conflict", "ambiguous",
    # "no_names_on_file", "llm_unavailable", "llm_error", "llm_no_match",
    # "llm_low_confidence", "llm_not_distinct"; None when assigned.
    reason: str | None


Ask = Callable[[str | None, str | None, list[Patient]], object]


def identify_patient(
    patients: Sequence[Patient],
    name: str | None,
    id_number: str | None,
    *,
    ask: Ask | None = None,
) -> Identification:
    """CIP, name, name variant, then Claude (``ask``, a ``patient_llm.Asker``;
    None = never call it). See the module docstring."""
    patients = list(patients)
    key = normalize_id_number(id_number)
    on_file = {p.id: normalize_id_number(p.id_number) for p in patients if p.id_number}
    if key and on_file:
        if any(value == key for value in on_file.values()):
            match = match_patient(patients, name, id_number)
            return Identification(match, None if match.patient else "id_conflict")
        if looks_like_cedula(id_number):
            return Identification(NO_MATCH, "id_mismatch")
        # Not a cédula (a foreign lab's own number): the name decides.
        id_number_for_name = None
    else:
        id_number_for_name = id_number
    tokens = report_tokens(name)
    match = match_patient(patients, name, id_number_for_name)
    # The same words as a parent's name may still be a child's (a daughter
    # with her mother's given names and a short name on file): the exact
    # name too needs a surname the children never carry.
    if match.patient is not None and distinguishes(match.patient, patients, tokens):
        return Identification(match, None)
    match = match_name_variant(patients, name)
    if match.patient is not None:
        return Identification(match, None)

    report = name_words(name)
    if not report:
        return Identification(NO_MATCH, "no_name")
    named = [p for p in patients if p.full_name and p.slug]
    if not named:
        return Identification(NO_MATCH, "no_names_on_file")
    # Only words every patient shares (the common surname, maybe with a
    # title): nobody can tell, not even Claude.
    shared = set.intersection(*(name_words(p.full_name) for p in named))
    if len(named) > 1 and report - TITLES <= shared:
        return Identification(NO_MATCH, "ambiguous")
    if ask is None or not getattr(ask, "available", True):
        return Identification(NO_MATCH, "llm_unavailable")
    answer = ask(name, id_number, named)
    if getattr(answer, "error", None):
        return Identification(NO_MATCH, "llm_error")
    slug = getattr(answer, "patient", None)
    if slug is None:
        return Identification(NO_MATCH, "llm_no_match")
    if getattr(answer, "confidence", None) != "high":
        return Identification(NO_MATCH, "llm_low_confidence")
    chosen = next((p for p in named if p.slug == slug), None)
    if chosen is None:
        return Identification(NO_MATCH, "llm_error")
    # Claude cannot overrule the operator's rule: without a surname the
    # children never carry, it could be a son's or a daughter's report.
    if not distinguishes(chosen, patients, tokens):
        return Identification(NO_MATCH, "llm_not_distinct")
    return Identification(PatientMatch(chosen, "llm"), None)


class HintResult(NamedTuple):
    match: PatientMatch
    conflict: bool  # the document points to another patient than the hint


def apply_patient_hint(
    patients: list[Patient],
    hinted: Patient,
    match: PatientMatch,
    name: str | None,
    id_number: str | None,
) -> HintResult:
    if match.patient is not None:
        if match.patient.id == hinted.id:
            return HintResult(match, False)
        return HintResult(NO_MATCH, True)
    key = normalize_id_number(id_number)
    if key and any(p.id_number for p in patients):
        # An ID that matched nobody (or was rejected by its name): someone
        # else's document, or a relative's CIP — not for the hint to decide.
        return HintResult(NO_MATCH, False)
    report_words = name_words(name)
    if not report_words:
        return HintResult(PatientMatch(hinted, "manual"), False)
    own = name_words(hinted.full_name)
    if own and report_words <= own:
        return HintResult(PatientMatch(hinted, "manual"), False)
    return HintResult(NO_MATCH, False)


def _specimen(value: str | None) -> str:
    """A missing or unknown specimen is ``otro``: never joined to blood trends."""
    return value if value in SPECIMENS else "otro"


class AnalyteMatcher:
    """Analyte lookup by (specimen, normalized name) for one extraction run."""

    def __init__(self, db: DbSession):
        self.db = db
        self.keys: dict[tuple[str, str], Analyte] = {}
        self.created: list[Analyte] = []
        for analyte in db.scalars(select(Analyte).order_by(Analyte.id)):
            self._index(analyte)

    def _index(self, analyte: Analyte) -> None:
        for name in [analyte.canonical_name, *(analyte.aliases or [])]:
            key = normalize_key(name)
            if key:
                # The oldest analyte wins a key two of them share.
                self.keys.setdefault((analyte.specimen, key), analyte)

    def _lookup(self, raw_name: str, specimen: str, unit: str | None) -> tuple[str, str]:
        """``(key, name for a new analyte)``. A result in a non-% unit whose name
        is a percentage analyte's is looked up as the absolute count."""
        key = normalize_key(raw_name)
        name = " ".join(raw_name.split())[:180]
        hit = self.keys.get((specimen, key))
        if hit is not None and hit.default_unit == "%" and unit and normalize_key(unit) != "%":
            return f"{key} #", f"{name} absolutos"
        return key, name

    def find(self, raw_name: str, specimen: str | None, unit: str | None = None) -> Analyte | None:
        specimen = _specimen(specimen)
        key, _ = self._lookup(raw_name, specimen, unit)
        return self.keys.get((specimen, key))

    def resolve(self, raw_name: str, specimen: str | None, unit: str | None) -> Analyte:
        specimen = _specimen(specimen)
        key, name = self._lookup(raw_name, specimen, unit)
        analyte = self.keys.get((specimen, key))
        if analyte is not None:
            return analyte
        if specimen in {"orina", "heces"} and specimen not in normalize_key(name):
            name = f"{name} ({specimen})"
        # The canonical name is unique: an unrelated analyte may already use it
        # (e.g. a percentage one) — add a counter rather than fail.
        taken = set(self.db.scalars(select(Analyte.canonical_name)))
        base, n = name, 2
        while name in taken:
            name, n = f"{base} [{n}]", n + 1
        analyte = Analyte(
            canonical_name=name,
            aliases=[],
            default_unit=(unit or None) and unit[:50],
            specimen=specimen,
            needs_review=True,
        )
        self.db.add(analyte)
        self.db.flush()
        audit.record(
            self.db,
            user_id=None,
            action=audit.ACTION_CREATE,
            entity="analyte",
            entity_id=analyte.id,
            after=audit.snapshot(analyte),
        )
        self.created.append(analyte)
        self.keys[(specimen, key)] = analyte
        self._index(analyte)
        return analyte
