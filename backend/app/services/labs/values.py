"""Text → value helpers for lab results: keys, numbers, ranges, flags.

Numbers (``read_number``) are tolerant of the ways labs print them:

- ``4.5`` / ``4,5`` / ``12,50`` / ``0,125`` → one reading (a single separator
  not followed by exactly three digits, or after a zero integer part, is the
  decimal separator),
- ``1,234.5`` / ``1.234,5`` → 1234.5 (both separators: the last is the decimal),
- ``1.234.567`` / ``1,234,567`` → thousands (the same separator repeated),
- ``250.000`` / ``4,500`` / ``1.500`` are **ambiguous** (250 or 250000?): a
  single ``.`` or ``,`` before exactly three digits. ``resolve_value``: when
  the report's other numbers show which decimal separator the lab uses
  (``decimal_convention``), that reading — unless the result's reference
  range makes only the other reading plausible (within 30x of the range's
  magnitude: a unit mismatch or a misread), then nothing. With no convention,
  the reading the range alone makes plausible. Otherwise ``value_num`` stays
  NULL (the raw ``value_text`` is kept) and the report goes to
  ``needs_review``. Ambiguous range bounds are resolved only by the report's
  convention.

``read_value`` also accepts a comparator prefix (``<``, ``>``, ``<=``,
``>=``, ``≤``, ``≥``, "menor de/que", "mayor de/que"): ``"<5"`` → 5 with ``<``.
Anything else (``Negativo``, ``1+``, ``4.5 x10^3``) is not numeric; the raw
text is always kept in ``value_text`` by the caller.

Flags (``compute_flag``): the lab's own flag wins (``H``/``L``/``*`` arrive
already mapped to ``alto``/``bajo``/``anormal`` by the extraction). Only when
the lab printed no flag is one computed, and only for a plain number with a
numeric range: below ``ref_low`` → ``bajo``, above ``ref_high`` → ``alto``,
otherwise ``normal``. A value with a comparator, a text value, a missing
range, or a value more than 30x off the range's magnitude (likely a unit
mismatch: ``7500`` against ``4.0 - 10.0``) gets no computed flag (NULL).
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_NON_KEY = re.compile(r"[^\w%#]+")
_MARKS = re.compile(r"([%#])")


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_key(value: str | None) -> str:
    """Comparison key for an analyte name: no accents, casefolded, punctuation
    as spaces, single spaces. ``%`` and ``#`` stay (as words): "Neutrófilos %"
    and "NEU#" must not collapse onto "Neutrófilos"."""
    if not value:
        return ""
    folded = strip_accents(value).casefold().replace("_", " ")
    spaced = _MARKS.sub(r" \1 ", _NON_KEY.sub(" ", folded))
    return " ".join(spaced.split())


_NUMBER = re.compile(r"^[+-]?(?:\d[\d.,]*|[.,]\d+)$")
_TOKEN = re.compile(r"\d[\d.,]*\d|\d")
_COMPARATORS = (
    ("<=", "<="),
    (">=", ">="),
    ("≤", "<="),
    ("≥", ">="),
    ("=<", "<="),
    ("=>", ">="),
    ("<", "<"),
    (">", ">"),
    ("menor o igual a", "<="),
    ("mayor o igual a", ">="),
    ("menor de", "<"),
    ("menor que", "<"),
    ("menor a", "<"),
    ("mayor de", ">"),
    ("mayor que", ">"),
    ("mayor a", ">"),
    ("hasta", "<="),
)
# A reading is plausible against a reference range when it is within this
# factor of the range's magnitude; the two readings differ by 1000x, so at
# most one of them can be.
PLAUSIBLE_FACTOR = Decimal(30)


@dataclass(frozen=True)
class Reading:
    """A printed number: ``candidates`` is empty (not a number), one value
    (certain) or ``(decimal reading, thousands reading)`` when a single ``.``
    or ``,`` (``separator``) before exactly three digits could be either."""

    candidates: tuple[Decimal, ...] = ()
    separator: str | None = None
    comparator: str | None = None

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) == 2

    @property
    def certain(self) -> Decimal | None:
        return self.candidates[0] if len(self.candidates) == 1 else None


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def read_number(text: str | None) -> Reading:
    """Every reading of a plain printed number (see the module docstring)."""
    if text is None:
        return Reading()
    raw = text.strip().replace(" ", "").replace(" ", "")
    if not raw or not _NUMBER.match(raw):
        return Reading()
    sign = ""
    if raw[0] in "+-":
        sign, raw = ("-" if raw[0] == "-" else ""), raw[1:]
    dots, commas = raw.count("."), raw.count(",")
    if dots and commas:
        decimal_sep = "." if raw.rfind(".") > raw.rfind(",") else ","
        thousands = "," if decimal_sep == "." else "."
        if raw.count(decimal_sep) > 1:
            return Reading()
        integer, _, fraction = raw.partition(decimal_sep)
        groups = integer.split(thousands)
        if any(len(g) != 3 for g in groups[1:]) or not groups[0]:
            return Reading()
        normalized = "".join(groups) + "." + fraction
    elif dots > 1 or commas > 1:
        sep = "." if dots else ","
        groups = raw.split(sep)
        if not groups[0] or any(len(g) != 3 for g in groups[1:]):
            return Reading()
        normalized = "".join(groups)
    elif dots or commas:
        sep = "." if dots else ","
        integer, _, fraction = raw.partition(sep)
        if len(fraction) == 3 and integer.lstrip("0"):
            as_decimal = _decimal(sign + integer + "." + fraction)
            as_thousands = _decimal(sign + integer + fraction)
            if as_decimal is None or as_thousands is None:
                return Reading()
            return Reading((as_decimal, as_thousands), sep)
        normalized = (integer or "0") + "." + fraction
    else:
        normalized = raw
    if normalized.endswith("."):
        normalized = normalized[:-1]
    number = _decimal(sign + normalized)
    return Reading((number,)) if number is not None else Reading()


def read_value(text: str | None) -> Reading:
    """``read_number`` after an optional comparator prefix (``"<5"``)."""
    if text is None:
        return Reading()
    raw = text.strip()
    lowered = strip_accents(raw).casefold()
    for prefix, comparator in _COMPARATORS:
        if lowered.startswith(prefix):
            reading = read_number(raw[len(prefix) :])
            if not reading.candidates:
                return Reading()
            return Reading(reading.candidates, reading.separator, comparator)
    return read_number(raw)


def decimal_convention(texts: Iterable[str | None]) -> str | None:
    """The decimal separator a report uses, when its unambiguous numbers agree
    on one (``"12,1"`` → ``,``; ``"4.25"`` or ``"1,234.5"`` → ``.``; a
    repeated separator, ``"1.234.567"``, is thousands → the other one)."""
    seen: set[str] = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for token in _TOKEN.findall(text):
            dots, commas = token.count("."), token.count(",")
            if dots and commas:
                seen.add("." if token.rfind(".") > token.rfind(",") else ",")
            elif dots > 1 or commas > 1:
                seen.add("," if dots else ".")
            elif dots or commas:
                sep = "." if dots else ","
                integer, _, fraction = token.partition(sep)
                if len(fraction) != 3 or not integer.lstrip("0"):
                    seen.add(sep)
    return seen.pop() if len(seen) == 1 else None


def _by_convention(reading: Reading, convention: str | None) -> Decimal | None:
    if reading.certain is not None:
        return reading.certain
    if reading.ambiguous and convention:
        return reading.candidates[0 if reading.separator == convention else 1]
    return None


def _magnitude(ref_low: Decimal | None, ref_high: Decimal | None) -> Decimal | None:
    bounds = [abs(b) for b in (ref_low, ref_high) if b is not None and b != 0]
    return (bounds[0] * bounds[-1]).sqrt() if bounds else None


def plausible(value: Decimal, ref_low: Decimal | None, ref_high: Decimal | None) -> bool:
    """Whether ``value`` is within ``PLAUSIBLE_FACTOR`` of the range's magnitude
    (True when there is no usable range)."""
    magnitude = _magnitude(ref_low, ref_high)
    if magnitude is None:
        return True
    return magnitude / PLAUSIBLE_FACTOR <= abs(value) <= magnitude * PLAUSIBLE_FACTOR


def _by_range(
    reading: Reading, ref_low: Decimal | None, ref_high: Decimal | None
) -> Decimal | None:
    if _magnitude(ref_low, ref_high) is None:
        return None
    fits = [c for c in reading.candidates if plausible(c, ref_low, ref_high)]
    return fits[0] if len(fits) == 1 else None


def resolve_value(
    reading: Reading,
    *,
    convention: str | None = None,
    ref_low: Decimal | None = None,
    ref_high: Decimal | None = None,
) -> Decimal | None:
    """The number a reading stands for: certain; else the report's decimal
    convention (a fact about how the lab prints numbers), unless the result's
    reference range makes only the *other* reading plausible (a unit mismatch
    or a misread: None); else, with no convention, the reading the range makes
    plausible; else None (the caller keeps only the text and asks for a
    review)."""
    if not reading.ambiguous:
        return reading.certain
    by_range = _by_range(reading, ref_low, ref_high)
    if convention:
        by_convention = _by_convention(reading, convention)
        if by_range is not None and by_range != by_convention:
            return None
        return by_convention
    return by_range


def parse_number(text: str | None, convention: str | None = None) -> Decimal | None:
    """A plain printed number when it has one reading (or ``convention`` picks
    one), else None."""
    return _by_convention(read_number(text), convention)


def parse_value(
    text: str | None, convention: str | None = None
) -> tuple[Decimal | None, str | None]:
    """``(number, comparator)`` for a printed result value; the number is None
    when the value is not numeric or its reading is ambiguous."""
    reading = read_value(text)
    if not reading.candidates:
        return None, None
    return _by_convention(reading, convention), reading.comparator


_RANGE = re.compile(r"^\s*([+-]?[\d.,]+)\s*(?:-|–|—|a|hasta)\s*([+-]?[\d.,]+)\s*$")


def parse_range(
    low: str | None, high: str | None, text: str | None, convention: str | None = None
) -> tuple[Decimal | None, Decimal | None]:
    """Numeric reference limits: from the extracted low/high when given, else
    from a printed range such as ``"3.5 - 5.0"``, ``"< 200"`` or ``"> 40"``.
    An ambiguous bound (``"150.000"``) needs ``convention`` or stays None."""
    ref_low, ref_high = parse_number(low, convention), parse_number(high, convention)
    if ref_low is not None or ref_high is not None or not text:
        return ref_low, ref_high
    match = _RANGE.match(strip_accents(text).casefold())
    if match:
        a, b = parse_number(match[1], convention), parse_number(match[2], convention)
        if a is not None and b is not None and a <= b:
            return a, b
        return None, None
    number, comparator = parse_value(text, convention)
    if number is None:
        return None, None
    if comparator in {"<", "<="}:
        return None, number
    if comparator in {">", ">="}:
        return number, None
    return None, None


def compute_flag(
    lab_flag: str | None,
    value: Decimal | None,
    comparator: str | None,
    ref_low: Decimal | None,
    ref_high: Decimal | None,
) -> str | None:
    """The lab's flag when it printed one; else computed from a plain number."""
    if lab_flag:
        return lab_flag
    if value is None or comparator is not None or (ref_low is None and ref_high is None):
        return None
    if not plausible(value, ref_low, ref_high):
        return None  # likely a unit mismatch (7500 /mm³ vs 4.0 - 10.0 ×10³)
    if ref_low is not None and value < ref_low:
        return "bajo"
    if ref_high is not None and value > ref_high:
        return "alto"
    return "normal"


_ID_PARTS = re.compile(r"[A-Za-z]+|\d+")


def normalize_id_number(value: str | None) -> str:
    """Comparison key for a Panamanian cédula (CIP) or other ID: letters
    uppercased, numeric groups without leading zeros, separators (dashes,
    spaces, dots) unified: ``8-0123-00456`` and ``8 123 456`` → ``8-123-456``."""
    if not value:
        return ""
    parts = _ID_PARTS.findall(strip_accents(value))
    return "-".join(p.upper() if p.isalpha() else (p.lstrip("0") or "0") for p in parts)
