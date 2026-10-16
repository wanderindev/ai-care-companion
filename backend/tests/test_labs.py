"""Lab extraction (#13): values, matching, the lab_extract job and the import hook.

Synthetic data only: a tiny generated PDF with obviously fake text, fake
names and IDs. Claude is always a fake client (never the real API); S3 is
moto (``storage`` fixture).
"""

import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.job import Job
from app.models.lab import Analyte, LabExtraction, LabReport, LabResult
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.attachments import PDF, store_upload
from app.services.labs import extract
from app.services.labs import jobs as lab_jobs
from app.services.labs import process as lab_process
from app.services.labs.matching import AnalyteMatcher, match_patient
from app.services.labs.values import (
    compute_flag,
    decimal_convention,
    normalize_id_number,
    normalize_key,
    parse_number,
    parse_range,
    parse_value,
    read_value,
    resolve_value,
)
from app.services.whatsapp_import import AFTER_IMPORT_HOOKS

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _migration():
    path = BACKEND_DIR / "alembic" / "versions" / "0007_labs.py"
    spec = importlib.util.spec_from_file_location("migration_0007", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED = _migration().ANALYTES


def _pdf(marker: str = "A") -> bytes:
    """A minimal one-page PDF with obviously synthetic text."""
    content = f"BT /F1 12 Tf 72 720 Td (LABORATORIO FICTICIO {marker} - DATOS DE PRUEBA) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        " /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out + trailer.encode()


# ── values ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("4.5", Decimal("4.5")),
        ("4,5", Decimal("4.5")),
        ("12,50", Decimal("12.50")),
        ("0,125", Decimal("0.125")),
        ("0.500", Decimal("0.500")),
        ("4.25", Decimal("4.25")),
        ("1,500", None),  # ambiguous: 1.5 or 1500
        ("250.000", None),  # ambiguous: 250 or 250000
        ("1,234.5", Decimal("1234.5")),
        ("1.234,5", Decimal("1234.5")),
        ("1.234.567", Decimal("1234567")),
        (" 98 ", Decimal("98")),
        ("-2.1", Decimal("-2.1")),
        (".5", Decimal("0.5")),
        ("7.", Decimal("7")),
        ("Negativo", None),
        ("1+", None),
        ("4.5 x10^3", None),
        ("1,2,3", None),
        ("1.2.3,4.5", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("<5", (Decimal("5"), "<")),
        ("< 0,01", (Decimal("0.01"), "<")),
        (">100", (Decimal("100"), ">")),
        ("<=3.5", (Decimal("3.5"), "<=")),
        ("≥ 60", (Decimal("60"), ">=")),
        ("Menor de 10", (Decimal("10"), "<")),
        ("mayor que 1.5", (Decimal("1.5"), ">")),
        ("95", (Decimal("95"), None)),
        ("<Negativo", (None, None)),
        ("Trazas", (None, None)),
    ],
)
def test_parse_value(text, expected):
    assert parse_value(text) == expected


D = Decimal


@pytest.mark.parametrize(
    "value, convention, low, high, expected",
    [
        # Unambiguous values ignore range and convention.
        ("4.5", ",", None, None, D("4.5")),
        ("1.234.567", None, None, None, D("1234567")),
        # The result's range decides when only one reading is plausible.
        ("250.000", None, D("150000"), D("450000"), D("250000")),
        ("250,000", None, D("150000"), D("450000"), D("250000")),
        ("4,500", None, D("4.0"), D("10.0"), D("4.5")),
        ("4,500", None, D("4000"), D("10000"), D("4500")),
        ("2.500", None, D("0.4"), D("4.0"), D("2.5")),
        ("1.050", None, None, D("1.2"), D("1.05")),
        # The report's convention is a fact; a range that only fits the other
        # reading means a unit mismatch or a misread: nothing is stored.
        # Scenario A: '.'-decimal hemogram, counts printed per mm³ against ×10³ ranges.
        ("4,500", ".", D("4.0"), D("10.0"), None),
        ("250,000", ".", D("150"), D("450"), None),
        # Scenario B: '.'-decimal lab with fixed 3 decimals, ferritin 2.5 (very low).
        ("2.500", ".", D("30"), D("400"), None),
        # Convention and range agree.
        ("250,000", ".", D("150000"), D("450000"), D("250000")),
        ("2.500", ".", D("0.4"), D("4.0"), D("2.500")),
        # No usable range: the report's decimal separator decides.
        ("250.000", ".", None, None, D("250.000")),
        ("250.000", ",", None, None, D("250000")),
        ("4,500", ".", None, None, D("4500")),
        ("4,500", ",", None, None, D("4.500")),
        ("<1,500", ",", None, None, D("1.500")),
        # Neither: stays ambiguous (NULL).
        ("250.000", None, None, None, None),
        ("4,500", None, D("0"), None, None),
        # A range halfway between both readings decides nothing.
        ("1.500", None, D("40"), D("56"), None),
    ],
)
def test_resolve_ambiguous_values(value, convention, low, high, expected):
    reading = read_value(value)
    assert resolve_value(reading, convention=convention, ref_low=low, ref_high=high) == expected


def test_read_value_and_decimal_convention():
    assert read_value("250.000").candidates == (D("250.000"), D("250000"))
    assert read_value("250.000").separator == "."
    assert read_value("<4,500").comparator == "<" and read_value("<4,500").ambiguous
    assert read_value("12,1").certain == D("12.1")
    assert read_value("Negativo").candidates == ()

    assert decimal_convention(["12,1", "13.0 - 17.0"]) is None  # mixed
    assert decimal_convention(["12,1", "4,500", None, "98"]) == ","
    assert decimal_convention(["4.25", "150,000 - 450,000"]) == "."
    assert decimal_convention(["1.234.567"]) == ","
    assert decimal_convention(["1,234.5"]) == "."
    assert decimal_convention(["250.000", "98", "Negativo"]) is None
    assert parse_number("150.000", ".") == D("150.000")
    assert parse_range(None, None, "150.000 - 450.000", ",") == (D("150000"), D("450000"))
    assert parse_range(None, None, "150.000 - 450.000") == (None, None)


def test_parse_range_and_flags():
    assert parse_range("3.5", "5,0", "3.5 - 5,0") == (Decimal("3.5"), Decimal("5.0"))
    assert parse_range(None, None, "70 - 100") == (Decimal("70"), Decimal("100"))
    assert parse_range(None, None, "0.4 a 4.0") == (Decimal("0.4"), Decimal("4.0"))
    assert parse_range(None, None, "< 200") == (None, Decimal("200"))
    assert parse_range(None, None, "> 40") == (Decimal("40"), None)
    assert parse_range(None, None, "Negativo") == (None, None)
    assert parse_range(None, None, "10 - 2") == (None, None)

    low, high = Decimal("70"), Decimal("100")
    assert compute_flag(None, Decimal("65"), None, low, high) == "bajo"
    assert compute_flag(None, Decimal("120"), None, low, high) == "alto"
    assert compute_flag(None, Decimal("100"), None, low, high) == "normal"
    # The lab's own flag always wins.
    assert compute_flag("alto", Decimal("90"), None, low, high) == "alto"
    # No computed flag for bounded values, text values or missing ranges.
    assert compute_flag(None, Decimal("5"), "<", low, high) is None
    assert compute_flag(None, None, None, low, high) is None
    assert compute_flag(None, Decimal("5"), None, None, None) is None
    assert compute_flag(None, Decimal("250"), None, None, Decimal("200")) == "alto"
    # More than 30x off the range's magnitude: likely a unit mismatch, no flag
    # (the lab's own flag is still kept).
    assert compute_flag(None, Decimal("7500"), None, Decimal("4.0"), Decimal("10.0")) is None
    assert compute_flag(None, Decimal("0.05"), None, Decimal("4.0"), Decimal("10.0")) is None
    assert compute_flag("alto", Decimal("7500"), None, Decimal("4.0"), Decimal("10.0")) == "alto"
    assert compute_flag(None, Decimal("150"), None, Decimal("4.0"), Decimal("10.0")) == "alto"


def test_keys_and_id_numbers():
    assert normalize_key("  Vitamina  B-12 ") == "vitamina b 12"
    assert normalize_key("ÁCIDO ÚRICO") == "acido urico"
    assert normalize_key("Neutrófilos %") == "neutrofilos %"
    assert normalize_key("NEU#") == "neu #"
    assert normalize_key("25(OH)D") == "25 oh d"
    assert normalize_id_number("8-0123-00456") == "8-123-456"
    assert normalize_id_number("8 123 456") == "8-123-456"
    assert normalize_id_number("pe-12-345") == "PE-12-345"
    assert normalize_id_number(None) == ""


def test_seed_keys_are_unique_per_specimen():
    seen: dict[tuple[str, str], str] = {}
    for name, aliases, _unit, _category, specimen in SEED:
        for alias in {normalize_key(a) for a in [name, *aliases]}:
            other = seen.setdefault((specimen, alias), name)
            assert other == name, f"{alias!r} is both {other!r} and {name!r}"
    assert 50 <= len(SEED) <= 80
    names = {s[0] for s in SEED}
    for required in ("Hemoglobina", "Glucosa", "TSH", "T4 libre", "Vitamina B12", "PSA total"):
        assert required in names


# ── patient matching (synthetic names, shared surname on purpose) ───────────


def _patients():
    papa = SimpleNamespace(id=1, full_name="Juan Ficticio Prueba", id_number="8-111-2222")
    mama = SimpleNamespace(id=2, full_name="Rosa Ejemplo de Prueba", id_number="8-333-4444")
    return papa, mama


NONE = (None, None)


def test_match_patient_by_cip():
    papa, mama = _patients()
    both = [papa, mama]
    assert match_patient(both, None, "8-0333-04444") == (mama, "cip")
    assert match_patient(both, "ROSA EJEMPLO DE PRUEBA", "8-333-4444") == (mama, "cip")
    # A name with fewer words of the same patient is consistent with the CIP.
    assert match_patient(both, "PRUEBA, ROSA", "8-333-4444") == (mama, "cip")
    assert match_patient(both, "Juan Prueba", "8 111 2222") == (papa, "cip")
    # No full name on file: the CIP alone decides.
    bare = SimpleNamespace(id=1, full_name=None, id_number="8-111-2222")
    assert match_patient([bare], "Cualquier Nombre", "8-111-2222") == (bare, "cip")
    # An ID that matches nobody is not overridden by the name.
    assert match_patient(both, "Juan Ficticio Prueba", "9-999-9999") == NONE


def test_cip_of_the_insured_relative_is_not_trusted():
    papa, mama = _patients()
    both = [papa, mama]
    # Mamá's lab on a CSS form carrying Papá's cédula as "asegurado".
    assert match_patient(both, "Rosa Ejemplo de Prueba", "8-111-2222") == NONE
    assert match_patient(both, "ROSA PRUEBA", "8-111-2222") == NONE
    # A sibling's lab with Papá's cédula: shares his surnames, not his name.
    assert match_patient(both, "Carlos Ficticio Prueba", "8-111-2222") == NONE


def test_one_word_name_confirms_a_cip_only_when_distinctive():
    papa, mama = _patients()
    both = [papa, mama]
    # Only the surname both parents share: it could be Mamá's lab → review.
    assert match_patient(both, "PRUEBA", "8-111-2222") == NONE
    assert match_patient(both, "Sra. de Prueba", "8-333-4444") == NONE  # "sra" lacks
    # A word only that patient's name has does confirm it.
    assert match_patient(both, "FICTICIO", "8-111-2222") == (papa, "cip")
    assert match_patient(both, "Rosa", "8-333-4444") == (mama, "cip")


def test_match_patient_by_complete_name_only():
    papa, mama = _patients()
    both = [papa, mama]
    assert match_patient(both, "PRUEBA FICTICIO, JUAN", None) == (papa, "name")
    assert match_patient(both, "Rosa Ejemplo Prueba", None) == (mama, "name")
    no_ids = [SimpleNamespace(id=p.id, full_name=p.full_name, id_number=None) for p in both]
    assert match_patient(no_ids, "Juan Ficticio Prueba", "8-111-2222") == (no_ids[0], "name")
    # Partial names are left for review, even when only one patient fits.
    assert match_patient(both, "ROSA PRUEBA", None) == NONE
    assert match_patient(both, "rosa ejemplo", None) == NONE
    assert match_patient(both, "Juan Prueba", None) == NONE


def test_son_named_after_father_is_not_papa():
    papa, mama = _patients()
    both = [papa, mama]
    assert match_patient(both, "Juan Ficticio Prueba Otro", None) == NONE
    assert match_patient(both, "Juan Carlos Ficticio Prueba", None) == NONE
    assert match_patient(both, "Juan Ficticio Ejemplo", None) == NONE


def test_match_patient_never_guesses_between_papa_and_mama():
    papa, mama = _patients()
    both = [papa, mama]
    # Only the shared surname: ambiguous, no match.
    assert match_patient(both, "Sr. Prueba", None) == NONE
    assert match_patient(both, "Prueba", None) == NONE
    assert match_patient(both, "Juan Rosa Prueba", None) == NONE
    # Two patients with the same stored words: nobody.
    twin = SimpleNamespace(id=3, full_name="Juan Ficticio Prueba", id_number=None)
    assert match_patient([papa, twin], "Juan Ficticio Prueba", None) == NONE
    # Nothing on file to compare with.
    blank = [SimpleNamespace(id=1, full_name=None, id_number=None)]
    assert match_patient(blank, "Juan Ficticio", None) == NONE
    assert match_patient(both, None, None) == NONE


# ── analytes ────────────────────────────────────────────────────────────────


@pytest.fixture
def analytes(test_db):
    rows = [
        Analyte(
            canonical_name=name,
            aliases=aliases,
            default_unit=unit,
            category=category,
            specimen=specimen,
        )
        for name, aliases, unit, category, specimen in SEED
    ]
    test_db.add_all(rows)
    test_db.commit()
    return {a.canonical_name: a for a in rows}


def test_analyte_alias_matching(test_db, analytes):
    matcher = AnalyteMatcher(test_db)
    b12 = analytes["Vitamina B12"]
    for raw in ("VIT B12", "Vitamina B-12", "cobalamina", "  vitamina   b12 "):
        assert matcher.find(raw, "sangre") is b12
    assert matcher.find("T.S.H.", "sangre") is None  # dots split the letters
    assert matcher.find("TSH", "sangre") is analytes["TSH"]
    assert matcher.find("Ácido Urico", "sangre") is analytes["Ácido úrico"]
    # Specimen separates same-named tests.
    assert matcher.find("Glucosa", "sangre") is analytes["Glucosa"]
    assert matcher.find("Glucosa", "orina") is analytes["Glucosa en orina"]
    # A missing or unknown specimen never finds a blood analyte.
    assert matcher.find("Glucosa", None) is None
    assert matcher.find("Glucosa", "suero raro") is None
    assert matcher.find("Hemoglobina", "orina") is analytes["Sangre en orina"]
    # A percentage name with an absolute unit is the absolute count.
    assert matcher.find("Neutrófilos", "sangre", "%") is analytes["Neutrófilos"]
    assert matcher.find("Neutrófilos", "sangre", "10^3/uL") is analytes["Neutrófilos absolutos"]
    assert matcher.find("NEU#", "sangre") is analytes["Neutrófilos absolutos"]


def test_unknown_analyte_is_created_for_review_once(test_db, analytes):
    matcher = AnalyteMatcher(test_db)
    new = matcher.resolve("Prueba Inventada X", "sangre", "U/L")
    assert (new.canonical_name, new.needs_review, new.default_unit) == (
        "Prueba Inventada X",
        True,
        "U/L",
    )
    assert matcher.resolve("PRUEBA INVENTADA X", "sangre", None) is new
    urine = matcher.resolve("Cristales", "orina", None)
    assert urine.canonical_name == "Cristales (orina)" and urine.specimen == "orina"
    # A percentage name in an absolute unit becomes "<name> absolutos".
    mono = matcher.resolve("Monocitos", "sangre", "10³/µL")
    assert mono.canonical_name == "Monocitos absolutos" and mono.needs_review
    assert matcher.resolve("Monocitos", "sangre", "10³/µL") is mono
    # An unknown specimen is "otro": never joined to the blood analyte.
    other = matcher.resolve("Glucosa", "desconocido", None)
    assert other is not analytes["Glucosa"]
    assert (other.specimen, other.canonical_name, other.needs_review) == (
        "otro",
        "Glucosa [2]",
        True,
    )
    test_db.commit()
    audited = test_db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.entity == "analyte")
    )
    assert audited == 4


# ── the job, end to end with a fake Claude ──────────────────────────────────


LAB_OUTPUT = {
    "is_lab_report": True,
    "patient_name": "PRUEBA FICTICIO, JUAN",
    "patient_id_number": "8-0111-02222",
    "lab_name": "Laboratorio Ficticio",
    "collected_on": "2026-03-02",
    "reported_on": "2026-03-03",
    "results": [
        {
            "panel": "Hemograma",
            "name": "Hemoglobina",
            "value": "12,1",
            "unit": "g/dL",
            "ref_low": "13.0",
            "ref_high": "17.0",
            "ref_text": "13.0 - 17.0",
            "flag": None,
            "specimen": "sangre",
        },
        {
            "panel": "Química",
            "name": "GLICEMIA",
            "value": "98",
            "unit": "mg/dL",
            "ref_low": None,
            "ref_high": None,
            "ref_text": "70 - 100",
            "flag": None,
            "specimen": "sangre",
        },
        {
            "panel": "Perfil tiroideo",
            "name": "TSH",
            "value": "<0.01",
            "unit": "µUI/mL",
            "ref_low": "0.4",
            "ref_high": "4.0",
            "ref_text": "0.4 - 4.0",
            "flag": "bajo",
            "specimen": "sangre",
        },
        {
            "panel": "Urinálisis",
            "name": "Glucosa",
            "value": "Negativo",
            "unit": None,
            "ref_low": None,
            "ref_high": None,
            "ref_text": "Negativo",
            "flag": None,
            "specimen": "orina",
        },
        {
            "panel": None,
            "name": "Marcador Inventado",
            "value": "1.234,5",
            "unit": "U/L",
            "ref_low": None,
            "ref_high": "1000",
            "ref_text": "< 1000",
            "flag": None,
            "specimen": "sangre",
        },
        {
            "panel": None,
            "name": "  ",
            "value": "1",
            "unit": None,
            "ref_low": None,
            "ref_high": None,
            "ref_text": None,
            "flag": None,
            "specimen": "sangre",
        },
    ],
}
NOT_LAB = {
    "is_lab_report": False,
    "patient_name": None,
    "patient_id_number": None,
    "lab_name": None,
    "collected_on": None,
    "reported_on": None,
    "results": [],
}


class FakeStream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.response


class FakeMessages:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def stream(self, **params):
        self.calls.append(params)
        result = self.respond(params)
        if isinstance(result, Exception):
            raise result
        stop_reason, payload = result
        return FakeStream(
            SimpleNamespace(
                stop_reason=stop_reason,
                content=[SimpleNamespace(type="text", text=payload)],
                usage=SimpleNamespace(
                    input_tokens=3000,
                    output_tokens=800,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=1200,
                ),
            )
        )


def _respond(output):
    return lambda params: ("end_turn", json.dumps(output))


@pytest.fixture
def claude(monkeypatch):
    """A settable fake Anthropic client behind ``extract.make_client``."""
    state = SimpleNamespace(messages=FakeMessages(_respond(LAB_OUTPUT)))
    monkeypatch.setattr(
        extract,
        "make_client",
        lambda key: SimpleNamespace(beta=SimpleNamespace(messages=state.messages)),
    )
    return state


@pytest.fixture
def lab_env(monkeypatch, storage, claude, test_engine):
    settings = Settings(anthropic_api_key="test-key", extraction_model="claude-opus-5-5")
    monkeypatch.setattr(lab_jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(lab_jobs, "get_storage", lambda: storage)
    return SimpleNamespace(
        settings=settings,
        storage=storage,
        claude=claude,
        factory=sessionmaker(bind=test_engine, autoflush=False),
    )


def _store_pdf(db, storage, data: bytes) -> Attachment:
    attachment = store_upload(
        db, storage, data=data, file_type=PDF, filename="informe.pdf", user_id=None
    )
    db.commit()
    return attachment


def _run(db, env, attachment_id, source_message_id=None) -> Job:
    job = lab_process.enqueue_extraction(db, attachment_id, source_message_id=source_message_id)
    db.commit()
    assert jobs.run_next(env.factory) is True
    db.expire_all()
    return db.get(Job, job.id)


@pytest.fixture
def named_patients(test_db, patients):
    papa, mama = patients
    papa.full_name, papa.id_number = "Juan Ficticio Prueba", "8-111-2222"
    mama.full_name, mama.id_number = "Rosa Ejemplo de Prueba", "8-333-4444"
    test_db.commit()
    return papa, mama


def test_lab_report_becomes_rows(test_db, lab_env, named_patients, analytes, caplog):
    papa, _ = named_patients
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    with caplog.at_level("INFO", logger="app"):
        job = _run(test_db, lab_env, attachment.id)

    assert (job.status, job.progress, job.error) == ("done", 100, None)
    assert job.message == "Informe de laboratorio guardado"
    assert job.payload == {"attachment_id": attachment.id, "source_message_id": None}

    (call,) = lab_env.claude.messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["schema"] == extract.SCHEMA
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    document = call["messages"][0]["content"][0]
    assert document["type"] == "document"
    assert document["source"]["media_type"] == "application/pdf"
    assert call["fallbacks"] == "default" and call["betas"] == [extract.FALLBACK_BETA]

    report = test_db.scalar(select(LabReport))
    assert (report.patient_id, report.patient_match, report.status) == (papa.id, "cip", "ok")
    assert (report.source, report.source_message_id, report.attachment_id) == (
        "upload",
        None,
        attachment.id,
    )
    assert str(report.collected_on) == "2026-03-02" and report.lab_name == "Laboratorio Ficticio"
    assert report.raw_extraction == LAB_OUTPUT
    # An unlinked upload now belongs to its report.
    assert (attachment.owner_type, attachment.owner_id) == ("lab_report", report.id)

    results = list(test_db.scalars(select(LabResult).order_by(LabResult.position)))
    assert [r.raw_name for r in results] == [
        "Hemoglobina",
        "GLICEMIA",
        "TSH",
        "Glucosa",
        "Marcador Inventado",
    ]
    hb, glu, tsh, urine, unknown = results
    assert (hb.value_num, hb.ref_low, hb.ref_high, hb.flag) == (
        Decimal("12.1"),
        Decimal("13.0"),
        Decimal("17.0"),
        "bajo",
    )
    assert hb.analyte_id == analytes["Hemoglobina"].id and hb.panel == "Hemograma"
    assert (glu.analyte_id, glu.ref_low, glu.ref_high, glu.flag) == (
        analytes["Glucosa"].id,
        Decimal("70"),
        Decimal("100"),
        "normal",
    )
    assert (tsh.value_text, tsh.value_num, tsh.comparator, tsh.flag) == (
        "<0.01",
        Decimal("0.01"),
        "<",
        "bajo",
    )
    assert urine.analyte_id == analytes["Glucosa en orina"].id
    assert (urine.value_text, urine.value_num, urine.flag) == ("Negativo", None, None)
    assert unknown.value_num == Decimal("1234.5") and unknown.flag == "alto"
    new = test_db.get(Analyte, unknown.analyte_id)
    assert new.needs_review and new.canonical_name == "Marcador Inventado"

    extraction = test_db.scalar(select(LabExtraction))
    assert (extraction.outcome, extraction.report_id, extraction.sha256) == (
        "lab_report",
        report.id,
        attachment.sha256,
    )
    assert extraction.usage["input_tokens"] == 3000 and extraction.usage["cost_usd"] > 0

    entry = test_db.scalar(select(AuditLog).where(AuditLog.entity == "lab_report"))
    assert entry.user_id is None and entry.action == "create"
    assert len(entry.after["results"]) == 5

    # Logs carry ids, outcomes and counts only.
    logged = caplog.text
    for secret in ("FICTICIO", "Ficticio", "8-0111", "12,1", "Laboratorio", "GLICEMIA"):
        assert secret not in logged
    assert "input=3000" in logged


def test_unmatched_patient_needs_review(test_db, lab_env, named_patients, analytes):
    lab_env.claude.messages = FakeMessages(
        _respond({**LAB_OUTPUT, "patient_name": "Prueba", "patient_id_number": None})
    )
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    assert _run(test_db, lab_env, attachment.id).status == "done"
    report = test_db.scalar(select(LabReport))
    assert (report.patient_id, report.patient_match, report.status) == (
        None,
        None,
        "needs_review",
    )


def test_new_reports_use_name_variants_and_claude(
    test_db, lab_env, named_patients, analytes, patient_llm
):
    """#98: an import goes through the same matching as the re-match."""
    papa, mama = named_patients
    # Names in the couple's cédula shape: her married part is his 1st surname.
    papa.full_name = "Juan Carlos Ficticio Prueba"
    mama.full_name = "Rosa Elena Ejemplo Muestra de Ficticio"
    test_db.commit()
    lab_env.claude.messages = FakeMessages(
        _respond({**LAB_OUTPUT, "patient_name": "DE FICTICIO, ROSA", "patient_id_number": None})
    )
    _run(test_db, lab_env, _store_pdf(test_db, lab_env.storage, _pdf("V")).id)
    report = test_db.scalar(select(LabReport).order_by(LabReport.id.desc()))
    assert (report.patient_id, report.patient_match, report.status) == (
        mama.id,
        "name_variant",
        "ok",
    )
    assert patient_llm.calls == []

    patient_llm.answer = {"patient": "papa", "confidence": "high", "reason_code": "initials"}
    lab_env.claude.messages = FakeMessages(
        _respond({**LAB_OUTPUT, "patient_name": "Juan Q Prueba", "patient_id_number": None})
    )
    _run(test_db, lab_env, _store_pdf(test_db, lab_env.storage, _pdf("W")).id)
    report = test_db.scalar(select(LabReport).order_by(LabReport.id.desc()))
    # Claude's choice is kept but a person confirms it.
    assert (report.patient_id, report.patient_match, report.status) == (
        papa.id,
        "llm",
        "needs_review",
    )
    (call,) = patient_llm.calls
    assert call["model"] == lab_env.settings.classification_model
    # Only the printed name and ID and the patients' names reach Claude.
    text = call["messages"][0]["content"]
    assert "Juan Q Prueba" in text and "Hemoglobina" not in text and "8-111" not in text

    for marker, (name, answer) in {
        "Z": ("Juan Q Prueba", "low"),
        # A child-like name: a high answer still assigns no parent.
        "Y": ("Juan Q Ficticio", "high"),
    }.items():
        patient_llm.answer = {"patient": "papa", "confidence": answer, "reason_code": "initials"}
        lab_env.claude.messages = FakeMessages(
            _respond({**LAB_OUTPUT, "patient_name": name, "patient_id_number": None})
        )
        _run(test_db, lab_env, _store_pdf(test_db, lab_env.storage, _pdf(marker)).id)
        report = test_db.scalar(select(LabReport).order_by(LabReport.id.desc()))
        assert (report.patient_id, report.patient_match, report.status) == (
            None,
            None,
            "needs_review",
        )


def _result(name, value, ref_low=None, ref_high=None, ref_text=None):
    return {
        "panel": None,
        "name": name,
        "value": value,
        "unit": None,
        "ref_low": ref_low,
        "ref_high": ref_high,
        "ref_text": ref_text,
        "flag": None,
        "specimen": "sangre",
    }


def test_ambiguous_value_is_kept_as_text_and_reviewed(test_db, lab_env, named_patients):
    output = {
        **LAB_OUTPUT,
        "results": [
            _result("Hemoglobina", "13.5", "13.0", "17.0"),  # "." decimal…
            _result("TSH", "2,25", ref_text="0,4 - 4,0"),  # …and "," decimal: no convention
            _result("Plaquetas", "250,000", "150000", "450000"),  # the range decides
            _result("Leucocitos", "4.500", ref_text="4.000 - 10.000"),  # nothing decides
        ],
    }
    lab_env.claude.messages = FakeMessages(_respond(output))
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    assert _run(test_db, lab_env, attachment.id).status == "done"
    report = test_db.scalar(select(LabReport))
    assert (report.patient_match, report.status) == ("cip", "needs_review")
    hb, tsh, plt, wbc = report.results
    assert (hb.value_num, tsh.value_num, tsh.ref_high) == (D("13.5"), D("2.25"), D("4.0"))
    assert (plt.value_num, plt.flag) == (D("250000"), "normal")
    assert (wbc.value_text, wbc.value_num, wbc.ref_low, wbc.flag) == ("4.500", None, None, None)


def test_report_convention_resolves_ambiguous_values(test_db, lab_env, named_patients):
    output = {
        **LAB_OUTPUT,
        "results": [
            _result("Hemoglobina", "13,5", "13,0", "17,0"),  # this lab writes "," decimals
            _result("Plaquetas", "250.000", ref_text="150.000 - 450.000"),
        ],
    }
    lab_env.claude.messages = FakeMessages(_respond(output))
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    _run(test_db, lab_env, attachment.id)
    report = test_db.scalar(select(LabReport))
    assert report.status == "ok"
    plt = report.results[1]
    assert (plt.value_num, plt.ref_low, plt.ref_high) == (D("250000"), D("150000"), D("450000"))


def test_convention_and_range_disagreeing_is_reviewed(test_db, lab_env, named_patients):
    output = {
        **LAB_OUTPUT,
        "results": [
            _result("Hemoglobina", "13.5", "13.0", "17.0"),  # this lab writes "." decimals
            _result("Leucocitos", "4,500", "4.0", "10.0"),  # per mm³ vs a ×10³ range
            _result("Ferritina", "2.500", "30", "400"),  # 2.5, not 2500
        ],
    }
    lab_env.claude.messages = FakeMessages(_respond(output))
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    _run(test_db, lab_env, attachment.id)
    report = test_db.scalar(select(LabReport))
    assert report.status == "needs_review"
    _hb, wbc, ferritin = report.results
    assert (wbc.value_text, wbc.value_num, wbc.flag) == ("4,500", None, None)
    assert (ferritin.value_text, ferritin.value_num, ferritin.flag) == ("2.500", None, None)


def test_insured_relatives_cip_goes_to_review(test_db, lab_env, named_patients):
    output = {**LAB_OUTPUT, "patient_name": "Rosa Ejemplo de Prueba"}  # Papá's CIP
    lab_env.claude.messages = FakeMessages(_respond(output))
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    _run(test_db, lab_env, attachment.id)
    report = test_db.scalar(select(LabReport))
    assert (report.patient_id, report.patient_match, report.status) == (None, None, "needs_review")


def test_whatsapp_source_message(test_db, lab_env, named_patients, analytes):
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    imp, (message,) = _import_with_messages(test_db, [attachment])
    _run(test_db, lab_env, attachment.id, source_message_id=message.id)
    report = test_db.scalar(select(LabReport))
    assert (report.source, report.source_message_id) == ("whatsapp", message.id)


def test_not_a_lab_report_is_recorded_without_rows(test_db, lab_env):
    lab_env.claude.messages = FakeMessages(_respond(NOT_LAB))
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    job = _run(test_db, lab_env, attachment.id)
    assert job.status == "done" and job.message == "El PDF no es un informe de laboratorio"
    assert test_db.scalar(select(func.count()).select_from(LabReport)) == 0
    extraction = test_db.scalar(select(LabExtraction))
    assert extraction.outcome == "not_lab_report" and extraction.report_id is None
    assert attachment.owner_type is None


def test_same_sha_is_never_sent_twice(test_db, lab_env, named_patients, analytes):
    data = _pdf()
    first = _store_pdf(test_db, lab_env.storage, data)
    second = _store_pdf(test_db, lab_env.storage, data)  # another row, same bytes
    assert first.sha256 == second.sha256 and first.id != second.id

    assert _run(test_db, lab_env, first.id).status == "done"
    job = _run(test_db, lab_env, second.id)
    assert job.status == "done" and job.message == "Este PDF ya se había procesado"
    assert _run(test_db, lab_env, first.id).status == "done"
    assert len(lab_env.claude.messages.calls) == 1
    assert test_db.scalar(select(func.count()).select_from(LabReport)) == 1
    assert test_db.scalar(select(func.count()).select_from(LabExtraction)) == 1


def test_no_api_key_fails_permanently(test_db, lab_env, monkeypatch):
    monkeypatch.setattr(lab_jobs, "get_settings", lambda: Settings(anthropic_api_key=""))
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    job = _run(test_db, lab_env, attachment.id)
    assert job.status == "failed" and job.attempts == 1
    assert "no está configurada" in job.error
    assert lab_env.claude.messages.calls == []
    # Nothing recorded: once the key is set the same file can be processed.
    assert test_db.scalar(select(func.count()).select_from(LabExtraction)) == 0


def test_oversize_pdf_is_recorded_and_never_sent(test_db, lab_env, monkeypatch):
    monkeypatch.setattr(
        lab_jobs,
        "get_settings",
        lambda: Settings(anthropic_api_key="test-key", lab_pdf_max_mb=1),
    )
    big = _pdf() + b"%" + b"0" * (1024 * 1024)
    attachment = _store_pdf(test_db, lab_env.storage, big)
    job = _run(test_db, lab_env, attachment.id)
    assert job.status == "failed" and "demasiado grande" in job.error
    assert lab_env.claude.messages.calls == []
    extraction = test_db.scalar(select(LabExtraction))
    assert (extraction.outcome, extraction.error) == ("failed", "too_large")
    # Recorded: a later job for the same bytes does not try again.
    assert _run(test_db, lab_env, attachment.id).status == "done"


def _api_error(status):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "error", response=httpx2.Response(status, request=request), body=None
    )


def test_api_errors(test_db, lab_env):
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())

    # Transient: retried by the job's backoff, nothing recorded.
    lab_env.claude.messages = FakeMessages(lambda params: _api_error(529))
    job = _run(test_db, lab_env, attachment.id)
    assert (job.status, job.attempts) == ("queued", 1)
    assert "no respondió" in job.error
    assert test_db.scalar(select(func.count()).select_from(LabExtraction)) == 0
    job.status = "failed"
    test_db.commit()

    # Key/model problem: fails at once, nothing recorded.
    lab_env.claude.messages = FakeMessages(lambda params: _api_error(401))
    job = _run(test_db, lab_env, attachment.id)
    assert (job.status, job.attempts) == ("failed", 1)
    assert test_db.scalar(select(func.count()).select_from(LabExtraction)) == 0

    # The file itself is the problem: recorded as failed, never retried.
    lab_env.claude.messages = FakeMessages(lambda params: _api_error(400))
    job = _run(test_db, lab_env, attachment.id)
    assert job.status == "failed" and "No se pudo leer el PDF" in job.error
    extraction = test_db.scalar(select(LabExtraction))
    assert (extraction.outcome, extraction.error) == ("failed", "api_error_400")


@pytest.mark.parametrize(
    "stop_reason, payload, error",
    [
        ("refusal", "", "refusal"),
        ("max_tokens", "{", "truncated"),
        ("end_turn", "not json", "invalid_json"),
        ("end_turn", '{"results": []}', "invalid_json"),
    ],
)
def test_bad_model_output_is_recorded(test_db, lab_env, stop_reason, payload, error):
    lab_env.claude.messages = FakeMessages(lambda params: (stop_reason, payload))
    attachment = _store_pdf(test_db, lab_env.storage, _pdf())
    assert _run(test_db, lab_env, attachment.id).status == "failed"
    extraction = test_db.scalar(select(LabExtraction))
    assert (extraction.outcome, extraction.error) == ("failed", error)


def test_invalid_payload_and_missing_or_non_pdf_attachment(test_db, lab_env):
    job = jobs.enqueue(test_db, lab_process.JOB_KIND, {"attachment_id": "x"})
    test_db.commit()
    jobs.run_next(lab_env.factory)
    test_db.expire_all()
    assert test_db.get(Job, job.id).status == "failed"

    assert _run(test_db, lab_env, 999).status == "failed"
    from app.services.attachments import OGG

    # A single photo is accepted (#37: WhatsApp photos classified as lab
    # reports); anything else is not.
    audio = store_upload(
        test_db,
        lab_env.storage,
        data=b"OggS" + b"\x00" * 40,
        file_type=OGG,
        filename="nota.ogg",
        user_id=None,
    )
    test_db.commit()
    job = _run(test_db, lab_env, audio.id)
    assert job.status == "failed" and "no es un PDF" in job.error
    assert lab_env.claude.messages.calls == []


# ── the WhatsApp after_import hook ──────────────────────────────────────────


def _import_with_messages(db, attachments):
    imp = WhatsappImport(
        mode="labs_only",
        status="done",
        filename="chat.zip",
        size=1,
        chunk_size=1,
        chunk_count=1,
    )
    db.add(imp)
    db.flush()
    messages = []
    for n, attachment in enumerate(attachments):
        message = WhatsappMessage(
            import_id=imp.id,
            sent_at=datetime(2026, 3, 1, 9, n, tzinfo=UTC),
            sender="Ana Prueba",
            media_kind="pdf",
            attachment_id=attachment.id,
            media_import_id=imp.id,
            dedupe_hash=f"{n:064d}",
        )
        db.add(message)
        messages.append(message)
    db.commit()
    return imp, messages


def test_after_import_queues_one_job_per_new_pdf(test_db, lab_env):
    assert lab_jobs.enqueue_lab_extractions in AFTER_IMPORT_HOOKS
    processed = _store_pdf(test_db, lab_env.storage, _pdf("viejo"))
    test_db.add(LabExtraction(sha256=processed.sha256, outcome="not_lab_report"))
    test_db.commit()
    again = _store_pdf(test_db, lab_env.storage, _pdf("viejo"))  # already processed bytes
    one = _store_pdf(test_db, lab_env.storage, _pdf("uno"))
    twin = _store_pdf(test_db, lab_env.storage, _pdf("uno"))  # same bytes, same import
    two = _store_pdf(test_db, lab_env.storage, _pdf("dos"))
    imp, messages = _import_with_messages(test_db, [again, one, twin, two])

    lab_jobs.enqueue_lab_extractions(test_db, imp)
    test_db.commit()
    queued = list(test_db.scalars(select(Job).order_by(Job.id)))
    assert [j.kind for j in queued] == ["lab_extract", "lab_extract"]
    assert [j.payload for j in queued] == [
        {"attachment_id": one.id, "source_message_id": messages[1].id},
        {"attachment_id": two.id, "source_message_id": messages[3].id},
    ]
    assert all(j.status == "queued" and j.message == "En cola" for j in queued)
    assert lab_env.claude.messages.calls == []

    empty = WhatsappImport(
        mode="daily", status="done", filename="c.zip", size=1, chunk_size=1, chunk_count=1
    )
    test_db.add(empty)
    test_db.commit()
    lab_jobs.enqueue_lab_extractions(test_db, empty)
    assert test_db.scalar(select(func.count()).select_from(Job)) == 2


def test_worker_loads_the_lab_handler():
    from app.worker import HANDLER_MODULES, load_handlers

    load_handlers(HANDLER_MODULES)
    assert lab_process.JOB_KIND in jobs.HANDLERS


# ── a deleted report is never extracted again (#115) ────────────────────────


def test_deleted_report_is_never_extracted_again(
    test_db, lab_env, named_patients, analytes, auth_client, member
):
    from app.models.lab import LabLink
    from app.services.media_classification import jobs as classify_jobs

    member.role = "admin"
    test_db.commit()
    data = _pdf("pariente")
    attachment = _store_pdf(test_db, lab_env.storage, data)
    assert _run(test_db, lab_env, attachment.id).status == "done"
    report_id = test_db.scalar(select(LabReport.id))
    new_analyte = test_db.scalar(
        select(Analyte).where(Analyte.canonical_name == "Marcador Inventado")
    )
    assert new_analyte.needs_review
    link = LabLink(
        url="https://lab.example.com/r/1",
        url_hash="f" * 64,
        host="lab.example.com",
        status="fetched",
        attachment_id=attachment.id,
        report_id=report_id,
    )
    test_db.add(link)
    test_db.commit()

    resp = auth_client.delete(f"/api/v1/lab-reports/{report_id}")
    assert resp.status_code == 204, resp.text
    test_db.expire_all()
    assert test_db.get(LabReport, report_id) is None
    assert test_db.scalar(select(func.count()).select_from(LabResult)) == 0
    # The page file is kept, only unlinked; the orphan analyte is gone.
    kept = test_db.get(Attachment, attachment.id)
    assert (kept.owner_type, kept.owner_id) == (None, None)
    assert test_db.get(Analyte, new_analyte.id) is None
    assert test_db.get(Analyte, analytes["Glucosa"].id) is not None
    # The extraction record stays, so the file is never sent to Claude again.
    extraction = test_db.scalar(select(LabExtraction))
    assert (extraction.outcome, extraction.report_id) == ("lab_report", None)
    link = test_db.get(LabLink, link.id)
    assert (link.status, link.report_id) == ("fetched", None)

    # Re-run on the same attachment, and on a new copy of the same bytes.
    job = _run(test_db, lab_env, attachment.id)
    assert job.status == "done" and job.message == "Este PDF ya se había procesado"
    again = _store_pdf(test_db, lab_env.storage, data)
    assert _run(test_db, lab_env, again.id).status == "done"
    # A later import with the same file queues nothing, nor does the classifier.
    imp, messages = _import_with_messages(test_db, [again])
    queued = test_db.scalar(select(func.count()).select_from(Job))
    lab_jobs.enqueue_lab_extractions(test_db, imp)
    assert classify_jobs._queue_lab_extraction(test_db, again, messages[0].id) is False
    test_db.commit()
    assert test_db.scalar(select(func.count()).select_from(Job)) == queued
    assert len(lab_env.claude.messages.calls) == 1
    assert test_db.scalar(select(func.count()).select_from(LabReport)) == 0
