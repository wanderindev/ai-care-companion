"""Lab report patient matching (#98): name variants, the Claude fallback and
the one-time re-match. Synthetic names only; Claude is the ``patient_llm``
fake from conftest (never the real API)."""

import json
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models.audit import AuditLog
from app.models.lab import Analyte, LabReport, LabResult
from app.services.labs import patient_llm as llm
from app.services.labs.matching import (
    NO_MATCH,
    identify_patient,
    looks_like_cedula,
    match_name_variant,
    resolve_couple,
    split_full_name,
)
from app.services.labs.rematch import rematch
from scripts.lab_reports import summary

PAPA_NAME = "Juan Carlos Ficticio Prueba"
MAMA_NAME = "Rosa Elena Ejemplo Muestra de Ficticio"


def _patients(with_ids: bool = True):
    papa = SimpleNamespace(
        id=1,
        slug="papa",
        display_name="Papá",
        full_name=PAPA_NAME,
        id_number="8-111-2222" if with_ids else None,
    )
    mama = SimpleNamespace(
        id=2,
        slug="mama",
        display_name="Mamá",
        full_name=MAMA_NAME,
        id_number="8-333-4444" if with_ids else None,
    )
    return papa, mama


def _asker(fake, answer=None):
    if answer is not None:
        fake.answer = answer
    return llm.Asker(Settings(anthropic_api_key="test-key"))


# ── deterministic rules and variants ────────────────────────────────────────


def test_cedula_shape():
    for value in ("8-111-2222", "8 0111 02222", "PE-12-345", "E-8-12345", "N-19-1234", "8AV-1-2"):
        assert looks_like_cedula(value), value
    for value in (None, "", "QX123456", "12345678", "AB-12"):
        assert not looks_like_cedula(value), value


@pytest.mark.parametrize(
    ("name", "who"),
    [
        ("FICTICIO PRUEBA, JUAN", "papa"),  # SURNAME, GIVEN; second given name missing
        ("Juan Prueba", "papa"),  # his 2nd surname
        ("Rosa de Ficticio", "mama"),  # her married part, "de" printed
        ("Juan C. Ficticio Prueba", "papa"),  # an initial for a missing word
        ("Rosa Ejemplo Muestra", "mama"),  # no married "de …" part
        ("EJEMPLO MUESTRA, ROSA", "mama"),
        ("rósa élena múestra", "mama"),  # accents and case
    ],
)
def test_name_variants_match_one_patient(name, who):
    papa, mama = _patients()
    match = match_name_variant([papa, mama], name)
    assert match == ({"papa": papa, "mama": mama}[who], "name_variant")


@pytest.mark.parametrize(
    "name",
    [
        "Juan Carlos Ficticio Otro",  # a son named after his father: extra word
        "Juan X Ficticio",  # an initial that stands for nothing missing
        "Carlos Ficticio",  # first given name missing
        "Ficticio",  # one word
        "Juan Rosa Ficticio",  # words of both
        # No surname the children never carry: could be a son or a daughter.
        "FICTICIO, JUAN",
        "Juan Carlos Ficticio",
        "EJEMPLO, ROSA",
        "Rosa Elena Ejemplo",
        "Rosa Elena Ficticio",  # married name without the "de"
        # Their children: "<given> <father's surname> <mother's surname>".
        "Rosa Ficticio Ejemplo",  # a daughter named after her mother
        "FICTICIO EJEMPLO, ROSA ELENA",
        "Rosa Elena Ficticio Ejemplo",
        "Juan Ficticio Ejemplo",  # a son
        None,
    ],
)
def test_name_variants_never_stretch(name):
    assert match_name_variant(list(_patients()), name) == NO_MATCH


def _couple():
    husband = SimpleNamespace(
        id=1, slug="papa", full_name="JUAN CARLOS PEREZ GOMEZ", id_number=None
    )
    wife = SimpleNamespace(
        id=2, slug="mama", full_name="ANA MARIA LOPEZ DIAZ DE PEREZ", id_number=None
    )
    return husband, wife


@pytest.mark.parametrize(
    ("name", "who"),
    [
        # His 2nd surname (the children never carry it).
        ("PEREZ GOMEZ, JUAN", "papa"),
        ("JUAN PEREZ GOMEZ", "papa"),
        ("JUAN C. PEREZ GOMEZ", "papa"),
        ("JUAN CARLOS PEREZ", None),  # could be a son named after him
        ("PEREZ, JUAN", None),
        # Her married part with the "de" printed, or her 2nd surname.
        ("ANA MARIA DE PEREZ", "mama"),
        ("ANA DE PEREZ", "mama"),
        ("DE PEREZ, ANA", "mama"),
        ("ANA LOPEZ DIAZ", "mama"),
        ("LOPEZ DIAZ, ANA", "mama"),
        ("ANA MARIA DIAZ", "mama"),  # 1st surname skipped, order kept: still hers
        ("ANA MARIA PEREZ", None),  # no "de": could be a daughter
        ("ANA MARIA LOPEZ", None),  # her 1st surname, which the children carry
        ("LOPEZ, ANA", None),
        ("ANA PEREZ", None),
        ("ANA M. PEREZ", None),
        # The couple's children.
        ("ANA PEREZ LOPEZ", None),
        ("PEREZ LOPEZ, ANA MARIA", None),
        ("ANA MARIA PEREZ LOPEZ", None),
        ("JUAN PEREZ LOPEZ", None),
        ("PEREZ LOPEZ, JUAN", None),
    ],
)
def test_name_variants_tell_a_child_from_the_mother(name, who):
    husband, wife = _couple()
    expected = {"papa": (husband, "name_variant"), "mama": (wife, "name_variant")}
    assert match_name_variant([husband, wife], name) == expected.get(who, NO_MATCH)


def test_child_with_a_foreign_id_is_not_mama():
    husband, wife = _couple()
    husband.id_number, wife.id_number = "8-111-2222", "8-333-4444"
    husband.display_name, wife.display_name = "Papá", "Mamá"
    found = identify_patient([husband, wife], "ANA MARIA PEREZ LOPEZ", "QX123456")
    assert found == (NO_MATCH, "llm_unavailable")


@pytest.mark.parametrize(
    ("full_name", "parts"),
    [
        ("JUAN CARLOS PEREZ GOMEZ", (["juan", "carlos", "perez", "gomez"], "perez", "gomez", [])),
        (
            "Ana María López Díaz de Pérez",
            (["ana", "maria", "lopez", "diaz", "perez"], "lopez", "diaz", ["perez"]),
        ),
        ("Rosa Ejemplo de Prueba", (["rosa", "ejemplo", "prueba"], "ejemplo", None, ["prueba"])),
        # "de los" inside the given names is not a married part.
        (
            "María de los Ángeles Ruiz Soto",
            (["maria", "angeles", "ruiz", "soto"], "ruiz", "soto", []),
        ),
        ("Juan Pérez", (["juan", "perez"], "perez", None, [])),
        (None, ([], None, None, [])),
    ],
)
def test_split_full_name(full_name, parts):
    assert tuple(split_full_name(full_name)) == parts


def test_particle_second_surname_never_makes_a_first_surname_distinctive():
    # Papá's 2nd surname is «de León»; Mamá has no married part on file.
    husband = SimpleNamespace(
        id=1, slug="papa", full_name="JUAN CARLOS PEREZ DE LEON", id_number=None
    )
    wife = SimpleNamespace(id=2, slug="mama", full_name="ANA MARIA LOPEZ DIAZ", id_number=None)
    both = [husband, wife]
    for name in ("JUAN PEREZ", "JUAN CARLOS PEREZ", "PEREZ, JUAN", "JUAN CARLOS PEREZ LOPEZ"):
        assert match_name_variant(both, name) == NO_MATCH, name
        assert identify_patient(both, name, None).match == NO_MATCH, name
    assert match_name_variant(both, "JUAN PEREZ DE LEON") == (husband, "name_variant")
    assert match_name_variant(both, "PEREZ DE LEON, JUAN") == (husband, "name_variant")
    assert match_name_variant(both, "ANA LOPEZ DIAZ") == (wife, "name_variant")
    for name in ("ANA LOPEZ", "ANA MARIA LOPEZ", "LOPEZ, ANA"):
        assert match_name_variant(both, name) == NO_MATCH, name
    # His «de León» is no married part: it names nobody's 1st surname.
    couple = resolve_couple(both)
    assert couple[husband.id].married == [] and len(couple[husband.id].readings) == 2


def test_married_part_with_a_particle_second_surname_husband():
    husband = SimpleNamespace(
        id=1, slug="papa", full_name="JUAN CARLOS PEREZ DE LEON", id_number=None
    )
    wife = SimpleNamespace(
        id=2, slug="mama", full_name="ANA MARIA LOPEZ DIAZ DE PEREZ", id_number=None
    )
    both = [husband, wife]
    couple = resolve_couple(both)
    assert couple[wife.id].married == ["perez"]
    assert match_name_variant(both, "ANA DE PEREZ") == (wife, "name_variant")
    assert match_name_variant(both, "JUAN PEREZ DE LEON") == (husband, "name_variant")
    assert match_name_variant(both, "JUAN CARLOS PEREZ") == NO_MATCH
    assert match_name_variant(both, "ANA MARIA PEREZ") == NO_MATCH


def test_exact_name_also_needs_a_distinguishing_surname():
    # Mamá with no 2nd surname on file: her daughter's name has the same words.
    husband = SimpleNamespace(
        id=1, slug="papa", full_name="JUAN CARLOS PEREZ GOMEZ", id_number=None
    )
    wife = SimpleNamespace(id=2, slug="mama", full_name="ANA LOPEZ DE PEREZ", id_number=None)
    both = [husband, wife]
    found = identify_patient(both, "ANA PEREZ LOPEZ", None)
    assert found == (NO_MATCH, "llm_unavailable")
    assert identify_patient(both, "ANA LOPEZ DE PEREZ", None).match == (wife, "name")
    assert identify_patient(both, "JUAN CARLOS PEREZ GOMEZ", None).match == (husband, "name")


def test_prompt_warns_that_a_child_is_not_the_mother():
    prompt = llm.SYSTEM_PROMPT
    assert "propio segundo apellido" in prompt
    assert "hijo o hija de la pareja" in prompt
    assert "NO de la madre" in prompt
    assert 'nunca tiene confidence "high"' in prompt


def test_variant_tie_is_no_match():
    papa, _ = _patients()
    twin = SimpleNamespace(**{**vars(papa), "id": 3, "slug": "otro"})
    assert match_name_variant([papa, twin], "Juan Prueba") == NO_MATCH


def test_identify_keeps_the_cip_rules_and_never_asks_claude(patient_llm):
    papa, mama = _patients()
    both = [papa, mama]
    ask = _asker(patient_llm)
    assert identify_patient(both, "FICTICIO, JUAN", "8-111-2222", ask=ask) == ((papa, "cip"), None)
    # Insured relative's cédula: Papá's number on Mamá's report → review.
    found = identify_patient(both, "Rosa Ejemplo Muestra", "8-111-2222", ask=ask)
    assert found == (NO_MATCH, "id_conflict")
    # A cédula that matches nobody: someone else (a son), never the name.
    found = identify_patient(both, PAPA_NAME, "9-999-9999", ask=ask)
    assert found == (NO_MATCH, "id_mismatch")
    assert patient_llm.calls == []


def test_identify_name_then_variant():
    papa, mama = _patients()
    both = [papa, mama]
    assert identify_patient(both, "PRUEBA FICTICIO, JUAN CARLOS", None) == ((papa, "name"), None)
    assert identify_patient(both, "EJEMPLO MUESTRA, ROSA", None) == ((mama, "name_variant"), None)
    # A foreign lab's own patient number is not a cédula: the name decides.
    found = identify_patient(both, "FICTICIO PRUEBA, JUAN", "QX123456")
    assert found == ((papa, "name_variant"), None)
    # No IDs on file: the printed one cannot be compared.
    bare = list(_patients(with_ids=False))
    assert identify_patient(bare, "Juan Prueba", "8-111-2222").match == (bare[0], "name_variant")


def test_identify_unassigned_reasons_without_claude(patient_llm):
    both = list(_patients())
    ask = _asker(patient_llm)
    assert identify_patient(both, None, None, ask=ask).reason == "no_name"
    # Only the surname both share: nobody can tell, no call.
    assert identify_patient(both, "Sr. Ficticio", None, ask=ask).reason == "ambiguous"
    assert patient_llm.calls == []
    assert identify_patient(both, "Juan X Ficticio", None).reason == "llm_unavailable"
    no_key = llm.Asker(Settings(anthropic_api_key=""))
    assert identify_patient(both, "Juan X Ficticio", None, ask=no_key).reason == "llm_unavailable"
    blank = [
        SimpleNamespace(id=1, slug="papa", display_name="Papá", full_name=None, id_number=None)
    ]
    assert identify_patient(blank, "Juan Ficticio", None, ask=ask).reason == "no_names_on_file"


def test_claude_decides_only_with_high_confidence(patient_llm):
    papa, mama = _patients()
    both = [papa, mama]
    ask = _asker(patient_llm, {"patient": "papa", "confidence": "high", "reason_code": "initials"})
    # A name with his 2nd surname: Claude may decide it.
    assert identify_patient(both, "Juan X Prueba", None, ask=ask) == ((papa, "llm"), None)
    # A child-like name (no surname the children never carry): never a parent,
    # whatever Claude says.
    found = identify_patient(both, "Juan X Ficticio", None, ask=ask)
    assert found == (NO_MATCH, "llm_not_distinct")
    patient_llm.answer = {"patient": "mama", "confidence": "high", "reason_code": "initials"}
    found = identify_patient(both, "Rosa X Ejemplo", None, ask=ask)
    assert found == (NO_MATCH, "llm_not_distinct")

    patient_llm.answer = {"patient": "papa", "confidence": "medium", "reason_code": "initials"}
    found = identify_patient(both, "Juan X Ficticio", None, ask=ask)
    assert found == (NO_MATCH, "llm_low_confidence")

    patient_llm.answer = {"patient": None, "confidence": "high", "reason_code": "different_person"}
    found = identify_patient(both, "Juan Carlos Ficticio Otro", None, ask=ask)
    assert found == (NO_MATCH, "llm_no_match")

    patient_llm.answer = {"patient": "tio", "confidence": "high", "reason_code": "same_name"}
    assert identify_patient(both, "Juan X Ficticio", None, ask=ask).reason == "llm_error"
    patient_llm.answer = "not json"
    assert identify_patient(both, "Juan X Ficticio", None, ask=ask).reason == "llm_error"

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    patient_llm.error = anthropic.APIStatusError(
        "error", response=httpx2.Response(529, request=request), body=None
    )
    assert identify_patient(both, "Juan X Ficticio", None, ask=ask).reason == "llm_error"
    assert ask.usage.as_dict()["requests"] == 7


def test_claude_sees_only_the_printed_name_id_and_patient_names(patient_llm):
    both = list(_patients())
    ask = _asker(patient_llm)
    identify_patient(both, "Juan X Ficticio", "QX123456", ask=ask)
    (call,) = patient_llm.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["effort"] == "low"
    schema = call["output_config"]["format"]["schema"]
    assert schema["required"] == ["patient", "confidence", "reason_code"]
    assert schema["properties"]["patient"]["anyOf"][0]["enum"] == ["papa", "mama"]
    (message,) = call["messages"]
    assert message["content"] == patient_llm_text()
    # No ID number on file is ever sent.
    assert "8-111-2222" not in json.dumps(call) and "8-333-4444" not in json.dumps(call)


def patient_llm_text() -> str:
    return "\n".join(
        [
            "Nombre impreso en el informe: Juan X Ficticio",
            "Identificación impresa en el informe: QX123456",
            "Pacientes posibles:",
            f'- "papa" (Papá): {PAPA_NAME}',
            f'- "mama" (Mamá): {MAMA_NAME}',
        ]
    )


# ── re-match ────────────────────────────────────────────────────────────────


@pytest.fixture
def named(test_db, patients):
    papa, mama = patients
    papa.full_name, papa.id_number = PAPA_NAME, "8-111-2222"
    mama.full_name, mama.id_number = MAMA_NAME, "8-333-4444"
    test_db.commit()
    return papa, mama


def _report(db, name, id_number=None, *, values=("5.2",), **fields) -> LabReport:
    analyte = db.scalar(select(Analyte).where(Analyte.canonical_name == "Prueba X"))
    if analyte is None:
        analyte = Analyte(canonical_name="Prueba X", aliases=[], specimen="sangre")
        db.add(analyte)
        db.flush()
    raw = {"is_lab_report": True, "patient_name": name, "patient_id_number": id_number}
    report = LabReport(
        source="whatsapp",
        status=fields.pop("status", "needs_review"),
        raw_extraction=fields.pop("raw_extraction", raw),
        **fields,
    )
    for i, value in enumerate(values):
        number = None if value == "250.000" else Decimal(value)
        report.results.append(
            LabResult(
                analyte_id=analyte.id, position=i, raw_name="X", value_text=value, value_num=number
            )
        )
    db.add(report)
    db.commit()
    return report


def _audits(db) -> int:
    return db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.entity == "lab_report", AuditLog.action == "update"
        )
    )


def test_rematch_assigns_audits_and_is_idempotent(test_db, named, patient_llm, caplog):
    papa, mama = named
    variant = _report(test_db, "FICTICIO PRUEBA, JUAN")
    ambiguous_value = _report(test_db, "EJEMPLO MUESTRA, ROSA", values=("5.2", "250.000"))
    no_results = _report(test_db, "Juan Prueba", values=())
    by_cip = _report(test_db, None, "8-0333-04444")
    by_claude = _report(test_db, "Juan X Prueba")
    child_like = _report(test_db, "Juan X Ficticio")
    shared = _report(test_db, "Ficticio")
    mismatch = _report(test_db, PAPA_NAME, "9-999-9999")
    blank = _report(test_db, None, raw_extraction=None)
    by_person = _report(test_db, "Rosa Elena Ejemplo", patient_id=papa.id, patient_match="manual")
    patient_llm.answer = {"patient": "papa", "confidence": "high", "reason_code": "initials"}
    ask = llm.Asker(Settings(anthropic_api_key="test-key"))

    with caplog.at_level("INFO", logger="app"):
        dry = rematch(test_db, ask=ask, dry_run=True)
    assert dry.candidates == 9 and dry.already_assigned == 1
    assert dry.assigned == {"name_variant": 3, "cip": 1, "llm": 1}
    assert dry.unassigned == {"ambiguous": 1, "id_mismatch": 1, "llm_not_distinct": 1}
    assert dry.skipped == {"no_extraction": 1}
    assert dry.marked_ok == 2  # not the one Claude chose: a person confirms it
    test_db.expire_all()
    assert (
        test_db.scalar(select(func.count(LabReport.id)).where(LabReport.patient_id.is_(None))) == 9
    )
    assert _audits(test_db) == 0
    for secret in ("Ficticio", "FICTICIO", "Rosa", "EJEMPLO", "8-0333"):
        assert secret not in caplog.text

    done = rematch(test_db, ask=ask)
    assert (done.assigned, done.unassigned, done.marked_ok) == (
        dry.assigned,
        dry.unassigned,
        dry.marked_ok,
    )
    test_db.expire_all()
    rows = {r.id: r for r in test_db.scalars(select(LabReport))}
    got = {
        report.id: (
            rows[report.id].patient_id,
            rows[report.id].patient_match,
            rows[report.id].status,
        )
        for report in (
            variant,
            ambiguous_value,
            no_results,
            by_cip,
            by_claude,
            child_like,
            shared,
            mismatch,
        )
    }
    assert got == {
        variant.id: (papa.id, "name_variant", "ok"),
        ambiguous_value.id: (mama.id, "name_variant", "needs_review"),
        no_results.id: (papa.id, "name_variant", "needs_review"),
        by_cip.id: (mama.id, "cip", "ok"),
        by_claude.id: (papa.id, "llm", "needs_review"),
        child_like.id: (None, None, "needs_review"),
        shared.id: (None, None, "needs_review"),
        mismatch.id: (None, None, "needs_review"),
    }
    # The person's choice and the report without an extraction are untouched.
    assert (rows[by_person.id].patient_id, rows[by_person.id].patient_match) == (papa.id, "manual")
    assert rows[blank.id].patient_id is None
    assert _audits(test_db) == 5
    entry = test_db.scalar(
        select(AuditLog).where(AuditLog.entity_id == str(by_claude.id), AuditLog.action == "update")
    )
    assert entry.user_id is None
    assert (entry.before["patient_id"], entry.after["patient_id"]) == (None, papa.id)
    assert entry.after["patient_match"] == "llm"

    again = rematch(test_db, ask=ask)
    assert again.candidates == 4 and sum(again.assigned.values()) == 0
    assert _audits(test_db) == 5

    text = summary(done, dry_run=False)
    assert "Assigned: 5 (cip 1, llm 1, name_variant 3)" in text
    assert "Still unassigned: 3 (ambiguous 1, id_mismatch 1, llm_not_distinct 1)" in text
    assert "Skipped: 1 (no_extraction 1)" in text
    assert "Would assign: 5" in summary(dry, dry_run=True)


def test_rematch_keeps_a_status_someone_set(test_db, named, patient_llm):
    papa, _ = named
    report = _report(test_db, "Juan Prueba", status="ok")
    stats = rematch(test_db, ask=patient_llm_asker())
    assert stats.marked_ok == 0
    test_db.expire_all()
    report = test_db.get(LabReport, report.id)
    assert (report.patient_id, report.patient_match, report.status) == (
        papa.id,
        "name_variant",
        "ok",
    )


def patient_llm_asker():
    return llm.Asker(Settings(anthropic_api_key="test-key"))
