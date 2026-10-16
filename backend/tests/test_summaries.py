"""«Resumen pre-consulta» and «Tarjeta de emergencia» (ticket #34), synthetic data only.

Service selection rules, the PDFs (real WeasyPrint child process), the
endpoints and the chat tools at the tool level. The turn-level test (SSE
``file`` event, link at persist) is in ``test_chat.py``.
"""

import io
import json
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.attachment import Attachment
from app.models.doctor import Doctor
from app.models.lab import LabReport, LabResult
from app.models.log_entry import LogEntry
from app.models.medication import MedAvoid, MedEvent, MedItem, MedListVersion
from app.services import emergency_card as card_svc
from app.services import pdf
from app.services import previsit as previsit_svc
from app.services.appointments import local_datetime, today_local
from app.services.chat import tools
from app.services.pdf import summaries as summaries_pdf

TODAY = date(2025, 6, 1)
CIP = "9-999-9999"
FULL_NAME = "Paciente Sintético Completo"


def pdf_text(content: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return " ".join(text.split()), len(reader.pages)


def entry(patient, day, doctor=None, entry_type="consulta_control", **kw):
    return LogEntry(
        patient_id=patient.id,
        entry_date=day,
        doctor_id=doctor.id if doctor else None,
        entry_type=entry_type,
        **kw,
    )


@pytest.fixture
def world(test_db, patients):
    """Papá's records with Dra. Uno (cardiology) and others; Mamá's too."""
    papa, mama = patients
    papa.full_name = FULL_NAME
    papa.id_number = CIP
    papa.birth_date = date(1940, 6, 2)
    papa.diagnoses = "Hipertensión sintética\nDiagnóstico sintético dos"
    uno = Doctor(name="Dra. Uno Sintética", specialty="Cardiología", phone="6000-0001")
    dos = Doctor(name="Dr. Dos Sintético", specialty="Geriatría", phone="6000-0002")
    tres = Doctor(name="Dr. Tres Inactivo", is_active=False)
    cuatro = Doctor(name="Dr. Cuatro de Mamá", phone="6000-0004")
    cinco = Doctor(name="Dr. Cinco Cancelado")
    test_db.add_all([uno, dos, tres, cuatro, cinco])
    test_db.flush()
    test_db.add_all(
        [
            # Older visit with Uno, then the last one (2025-03-10).
            entry(papa, date(2024, 11, 5), uno, title="Visita vieja"),
            entry(
                papa, date(2025, 3, 10), uno, title="Última consulta", indications="Seguir igual."
            ),
            # Uno again, but not a visit type: does not move the last visit.
            entry(papa, date(2025, 4, 20), uno, entry_type="examen", title="Examen pedido"),
            # Another doctor inside the period.
            entry(papa, date(2025, 4, 2), dos, title="Control geriatría"),
            # A family observation in the period.
            entry(papa, date(2025, 5, 1), entry_type="observacion_familiar", family_notes="Tos."),
            # Today, with Uno: in the period but not the "last visit".
            entry(papa, TODAY, uno, title="Hoy"),
            # Mamá's visit with Uno, after Papá's: must not count.
            entry(mama, date(2025, 5, 15), uno, title="Consulta de Mamá"),
            entry(mama, date(2025, 5, 16), cuatro, title="Otra de Mamá"),
            entry(papa, date(2023, 1, 1), tres, title="Inactivo"),
        ]
    )
    test_db.add_all(
        [
            MedItem(
                patient_id=papa.id,
                name="Medicina A",
                strength="5 mg",
                slot="desayuno",
                quantity="1/2",
                prescriber_id=dos.id,
            ),
            MedItem(
                patient_id=papa.id, name="Medicina Vieja", slot="cena", end_date=date(2020, 1, 1)
            ),
            MedItem(patient_id=mama.id, name="Medicina de Mamá", slot="cena"),
            MedAvoid(patient_id=papa.id, substance="Sustancia X", reason="reacción sintética"),
            MedAvoid(patient_id=mama.id, substance="Sustancia de Mamá"),
            MedListVersion(
                patient_id=papa.id,
                version_no=1,
                snapshot={"items": [], "avoid": []},
                change_summary="Lista inicial",
                effective_date=date(2024, 1, 1),
            ),
            MedListVersion(
                patient_id=papa.id,
                version_no=2,
                snapshot={"items": [], "avoid": []},
                change_summary="Se agregó Medicina A",
                effective_date=date(2025, 4, 3),
            ),
            MedEvent(
                patient_id=papa.id,
                event_date=date(2025, 3, 20),
                kind="suspension",
                medication="Medicina B",
                detail="por mareo",
                review_status="pending_review",
            ),
            MedEvent(
                patient_id=papa.id,
                event_date=date(2025, 1, 1),
                kind="inicio",
                medication="Fuera del período",
            ),
            MedEvent(
                patient_id=mama.id,
                event_date=date(2025, 4, 1),
                kind="inicio",
                medication="Evento de Mamá",
            ),
        ]
    )
    in_period = LabReport(
        patient_id=papa.id,
        collected_on=date(2025, 4, 10),
        lab_name="Lab Sintético",
        source="upload",
        status="ok",
    )
    before = LabReport(
        patient_id=papa.id,
        collected_on=date(2025, 2, 1),
        lab_name="Lab Antes",
        source="upload",
        status="ok",
    )
    failed = LabReport(
        patient_id=papa.id,
        collected_on=date(2025, 4, 11),
        lab_name="Lab Falló",
        source="upload",
        status="failed",
    )
    mamas = LabReport(
        patient_id=mama.id,
        collected_on=date(2025, 4, 12),
        lab_name="Lab Mamá",
        source="upload",
        status="ok",
    )
    test_db.add_all([in_period, before, failed, mamas])
    test_db.flush()
    test_db.add_all(
        [
            LabResult(
                report_id=in_period.id,
                position=0,
                raw_name="Glucosa sintética",
                value_num=Decimal("130"),
                unit="mg/dL",
                ref_low=Decimal("70"),
                ref_high=Decimal("100"),
                flag="alto",
            ),
            LabResult(
                report_id=in_period.id,
                position=1,
                raw_name="Potasio sintético",
                value_text="3.1",
                unit="mmol/L",
                ref_text="3.5–5.1",
                flag="bajo",
            ),
            LabResult(
                report_id=in_period.id,
                position=2,
                raw_name="Sodio sintético",
                value_num=Decimal("140"),
                unit="mmol/L",
                flag="normal",
            ),
        ]
    )
    test_db.add_all(
        [
            Appointment(
                patient_id=papa.id,
                doctor_id=uno.id,
                starts_at=local_datetime(TODAY + timedelta(days=3), time(9, 0)),
                reason="Control",
            ),
            Appointment(
                patient_id=papa.id,
                doctor_id=dos.id,
                starts_at=local_datetime(TODAY + timedelta(days=10), time(0, 0)),
                all_day=True,
            ),
            Appointment(
                patient_id=papa.id,
                doctor_id=cinco.id,
                status="cancelada",
                starts_at=local_datetime(TODAY + timedelta(days=5), time(9, 0)),
            ),
            Appointment(
                patient_id=papa.id,
                doctor_id=uno.id,
                status="realizada",
                starts_at=local_datetime(date(2025, 3, 10), time(9, 0)),
            ),
            Appointment(
                patient_id=mama.id,
                doctor_id=cuatro.id,
                starts_at=local_datetime(TODAY + timedelta(days=2), time(9, 0)),
            ),
        ]
    )
    test_db.commit()
    return {
        "papa": papa,
        "mama": mama,
        "uno": uno,
        "dos": dos,
        "tres": tres,
        "cuatro": cuatro,
        "cinco": cinco,
    }


# ── Pre-visit summary: selection ────────────────────────────────────────────


def test_period_starts_at_the_last_visit_with_that_doctor(test_db, world):
    s = previsit_svc.previsit_summary(test_db, world["papa"].id, world["uno"].id, TODAY)
    assert s.last_visit == date(2025, 3, 10) and s.period_from == date(2025, 3, 10)
    assert s.patient_name == "Papá" and s.doctor.name == "Dra. Uno Sintética"
    titles = [e.title for e in s.entries]
    # Oldest first, every doctor, the last visit and today included; Mamá's never.
    assert titles == [
        "Última consulta",
        "Control geriatría",
        "Examen pedido",
        None,
        "Hoy",
    ]
    assert s.entries_total == 5
    own = {e.title: e.own for e in s.entries}
    assert own["Última consulta"] and own["Hoy"] and not own["Control geriatría"]


def test_no_previous_visit_falls_back_to_twelve_months(test_db, world):
    s = previsit_svc.previsit_summary(test_db, world["papa"].id, world["cuatro"].id, TODAY)
    assert s.last_visit is None
    assert s.period_from == TODAY - timedelta(days=previsit_svc.FALLBACK_DAYS)
    assert "Visita vieja" in [e.title for e in s.entries]
    assert all(e.date >= s.period_from for e in s.entries)
    assert not any(e.own for e in s.entries)


def test_visit_today_and_other_patient_do_not_count_as_last_visit(test_db, world):
    mama = world["mama"]
    # Mamá's only visit with Uno is after today's reference: none before it.
    assert (
        previsit_svc.last_visit_date(test_db, mama.id, world["uno"].id, date(2025, 5, 15)) is None
    )
    assert previsit_svc.last_visit_date(
        test_db, mama.id, world["uno"].id, date(2025, 5, 16)
    ) == date(2025, 5, 15)


def test_med_changes_meds_labs_and_appointments(test_db, world):
    s = previsit_svc.previsit_summary(test_db, world["papa"].id, world["uno"].id, TODAY)
    assert [(c.date, c.pending_review) for c in s.med_changes] == [
        (date(2025, 3, 20), True),
        (date(2025, 4, 3), False),
    ]
    assert "Suspensión: Medicina B — por mareo" == s.med_changes[0].text
    assert "versión 2" in s.med_changes[1].text and "Medicina A" in s.med_changes[1].text
    assert [m.name for m in s.meds] == ["Medicina A"]
    assert [a.substance for a in s.avoid] == ["Sustancia X"]
    assert [r.lab_name for r in s.labs] == ["Lab Sintético"]
    results = s.labs[0].results
    assert [(r.value, r.flag_label, r.out_of_range) for r in results] == [
        ("130", "Alto", True),
        ("3.1", "Bajo", True),
        ("140", "Normal", False),
    ]
    assert results[0].ref == "70–100" and s.labs[0].out_of_range == 2
    assert [(a.date, a.own, a.time) for a in s.appointments] == [
        (TODAY + timedelta(days=3), True, "09:00"),
        (TODAY + timedelta(days=10), False, None),
    ]
    assert s.counts() == {
        "entries": 5,
        "med_changes": 2,
        "current_meds": 1,
        "avoid": 1,
        "lab_reports": 1,
        "out_of_range_results": 2,
        "upcoming_appointments": 2,
    }


def test_unknown_patient_or_doctor(test_db, world):
    with pytest.raises(previsit_svc.PrevisitError):
        previsit_svc.previsit_summary(test_db, 999, world["uno"].id, TODAY)
    with pytest.raises(previsit_svc.PrevisitError):
        previsit_svc.previsit_summary(test_db, world["papa"].id, 999, TODAY)


# ── Emergency card: selection ───────────────────────────────────────────────


def test_emergency_card_data(test_db, world):
    card = card_svc.emergency_card(test_db, world["papa"].id, TODAY)
    assert (card.full_name, card.id_number, card.age) == (FULL_NAME, CIP, 84)
    assert card.diagnoses.startswith("Hipertensión sintética")
    assert [m.name for m in card.meds] == ["Medicina A"]
    assert [a.substance for a in card.avoid] == ["Sustancia X"]
    # Ranked by most recent reference: Dos (appointment in 10 days) > Uno (in 3
    # days); inactive Tres, cancelled-only Cinco and Mamá's Cuatro are out.
    assert [d.name for d in card.doctors] == ["Dr. Dos Sintético", "Dra. Uno Sintética"]
    assert card.counts() == {"current_meds": 1, "avoid": 1, "doctors": 2}


def test_age_and_doctor_cap(test_db, patients):
    assert card_svc.age_on(date(1940, 6, 2), date(2025, 6, 1)) == 84
    assert card_svc.age_on(date(1940, 6, 1), date(2025, 6, 1)) == 85
    assert card_svc.age_on(None, TODAY) is None
    papa = patients[0]
    for i in range(12):
        d = Doctor(name=f"Médico {i:02d}")
        test_db.add(d)
        test_db.flush()
        test_db.add(entry(papa, date(2025, 1, 1) + timedelta(days=i), d))
    test_db.commit()
    card = card_svc.emergency_card(test_db, papa.id, TODAY)
    assert [d.name for d in card.doctors] == [f"Médico {i:02d}" for i in range(11, 3, -1)]
    assert card.full_name is None and card.id_number is None


# ── PDFs and endpoints ──────────────────────────────────────────────────────


def previsit_url(patient, query=""):
    return f"/api/v1/patients/{patient.id}/previsit.pdf{query}"


def card_url(patient, suffix=".pdf"):
    return f"/api/v1/patients/{patient.id}/emergency-card{suffix}"


def test_endpoints_require_auth(db_client, patients):
    p = patients[0]
    assert db_client.get(previsit_url(p, "?doctor_id=1")).status_code == 401
    assert db_client.get(card_url(p)).status_code == 401
    assert db_client.get(card_url(p, "")).status_code == 401


def test_endpoints_404_and_422(auth_client, world):
    papa, uno = world["papa"], world["uno"]
    assert (
        auth_client.get(f"/api/v1/patients/999/previsit.pdf?doctor_id={uno.id}").status_code == 404
    )
    assert auth_client.get(previsit_url(papa, "?doctor_id=999")).status_code == 404
    assert auth_client.get(previsit_url(papa)).status_code == 422
    assert auth_client.get("/api/v1/patients/999/emergency-card.pdf").status_code == 404
    assert auth_client.get("/api/v1/patients/999/emergency-card").status_code == 404


def test_previsit_pdf(auth_client, world, caplog):
    papa = world["papa"]
    # A visit before the real today, so the period is deterministic.
    with caplog.at_level("DEBUG"):
        resp = auth_client.get(previsit_url(papa, f"?doctor_id={world['uno'].id}"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["cache-control"] == "no-store"
    name = f"resumen-preconsulta-papa-{today_local().isoformat()}.pdf"
    assert resp.headers["content-disposition"] == f'inline; filename="{name}"'
    text, _pages = pdf_text(resp.content)
    for needle in (
        "RESUMEN PRE-CONSULTA",
        "Papá · Dra. Uno Sintética · Cardiología",
        "Preguntas para el doctor",
        "Medicamentos actuales",
        "EVITAR",
        "Sustancia X",
        "Próximas citas",
        "Laboratorios en el período",
    ):
        assert needle in text, needle
    # Display name only: never the private fields.
    assert FULL_NAME not in text and CIP not in text
    assert "Sintético" not in caplog.text and CIP not in caplog.text


def test_previsit_pdf_renders_flags_as_text(test_db, world):
    s = previsit_svc.previsit_summary(test_db, world["papa"].id, world["uno"].id, TODAY)
    html = pdf.render_html(summaries_pdf.PREVISIT_TEMPLATE, summaries_pdf.previsit_context(s))
    assert '<tr class="out">' in html and "ALTO" in html and "BAJO" in html
    text, _ = pdf_text(summaries_pdf.render_previsit(s))
    assert "ALTO" in text and "BAJO" in text and "ESTE MÉDICO" in text
    assert "Desde la última consulta con este médico: 10 de marzo de 2025" in text
    assert "Glucosa sintética" in text and "Lab Antes" not in text and "Lab Mamá" not in text
    assert "Consulta de Mamá" not in text and "Medicina de Mamá" not in text


def test_emergency_card_pdf_is_one_page_with_the_cip(auth_client, world, test_db):
    papa = world["papa"]
    # 15 medications, each taken twice a day (30 rows): one line each.
    for i in range(15):
        for slot, at in (("desayuno", time(7, 0)), ("noche", time(21, 0))):
            test_db.add(
                MedItem(
                    patient_id=papa.id,
                    name=f"Medicina {i:02d}",
                    strength="250 mg",
                    quantity="1 1/2",
                    slot=slot,
                    time=at,
                )
            )
    for i in range(4):
        test_db.add(MedAvoid(patient_id=papa.id, substance=f"Sustancia {i}", reason="alergia"))
    for i in range(8):
        d = Doctor(
            name=f"Dr. Especialista Sintético {i}",
            specialty="Medicina interna",
            phone=f"6000-10{i:02d}",
        )
        test_db.add(d)
        test_db.flush()
        test_db.add(entry(papa, date(2025, 1, 1) + timedelta(days=i), d))
    test_db.commit()
    resp = auth_client.get(card_url(papa))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    name = f"tarjeta-emergencia-papa-{today_local().isoformat()}.pdf"
    assert resp.headers["content-disposition"] == f'inline; filename="{name}"'
    text, pages = pdf_text(resp.content)
    assert pages == 1
    assert text.count("Medicina 14 250 mg") == 1
    assert "Medicina 14 250 mg 1½ Desayuno 07:00; 1½ Noche 21:00" in text
    for needle in (
        "TARJETA DE EMERGENCIA",
        "actualizada el",
        FULL_NAME,
        f"CIP: {CIP}",
        "Nacimiento: 2 de junio de 1940",
        "EVITAR",
        "Sustancia X",
        "Hipertensión sintética",
        "Medicina A",
        "6000-0001",
    ):
        assert needle in text, needle


def test_emergency_card_json(auth_client, world):
    resp = auth_client.get(card_url(world["papa"], ""))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["id_number"] == CIP and body["full_name"] == FULL_NAME
    assert body["updated_on"] == today_local().isoformat()
    assert [m["name"] for m in body["meds"]] == ["Medicina A"]
    assert body["med_groups"] == [{"name": "Medicina A 5 mg", "doses": ["½ Desayuno"]}]
    assert body["avoid"] == [{"substance": "Sustancia X", "reason": "reacción sintética"}]
    assert {d["name"] for d in body["doctors"]} == {"Dra. Uno Sintética", "Dr. Dos Sintético"}


# ── Chat tools ──────────────────────────────────────────────────────────────


def run(db, name, args, storage):
    ctx = tools.ToolContext(db=db, storage=(lambda: storage) if storage else None)
    result = tools.run_tool(ctx, "toolu_s", name, args)
    content = result.block["content"]
    return ctx, result, content


def test_emergency_card_tool_never_returns_private_fields(test_db, world, storage):
    ctx, result, content = run(test_db, "emergency_card", {"patient": "papa"}, storage)
    assert result.ok, content
    assert CIP not in content and FULL_NAME not in content and "1940" not in content
    out = json.loads(content)
    name = f"tarjeta-emergencia-papa-{today_local().isoformat()}.pdf"
    assert out["filename"] == name and out["file_id"] == ctx.attachment_ids[0]
    assert out["counts"] == {"current_meds": 1, "avoid": 1, "doctors": 2}
    row = test_db.get(Attachment, out["file_id"])
    assert (row.owner_type, row.mime) == (None, "application/pdf")
    assert ctx.files == [{"id": row.id, "filename": name, "mime": row.mime, "size": row.size}]
    # The family's file does carry the card's identification data.
    text, pages = pdf_text(b"".join(storage.iter_object(row.storage_key)))
    assert CIP in text and FULL_NAME in text and pages == 1


def test_previsit_tool_by_name_or_id(test_db, world, storage):
    ctx, result, content = run(
        test_db, "previsit_summary", {"patient": "papa", "doctor": "dra uno sintetica"}, storage
    )
    assert result.ok, content
    out = json.loads(content)
    assert out["doctor_id"] == world["uno"].id and out["patient"] == "papa"
    assert out["filename"] == f"resumen-preconsulta-papa-{today_local().isoformat()}.pdf"
    assert set(out["counts"]) >= {"entries", "out_of_range_results"}
    assert CIP not in content and FULL_NAME not in content
    _, result, content = run(
        test_db, "previsit_summary", {"patient": "mama", "doctor": world["cuatro"].id}, storage
    )
    assert result.ok, content
    assert len(test_db.scalars(select(Attachment)).all()) == 2


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"patient": "papa"}, "Falta"),
        ({"patient": "papa", "doctor": 999}, "No existe el médico"),
        ({"patient": "papa", "doctor": "Nadie"}, "No encuentro"),
        ({"patient": "tio", "doctor": 1}, "Paciente desconocido"),
        ({"doctor": 1}, "Falta"),
    ],
)
def test_previsit_tool_bad_input(test_db, world, storage, args, needle):
    _, result, content = run(test_db, "previsit_summary", args, storage)
    assert not result.ok and needle in content
    assert test_db.scalars(select(Attachment)).all() == []


def test_ambiguous_doctor_name(test_db, world, storage):
    test_db.add(Doctor(name="Dra. Otra", aliases=["Dra. Uno Sintética"]))
    test_db.commit()
    _, result, content = run(
        test_db, "previsit_summary", {"patient": "papa", "doctor": "Dra. Uno Sintética"}, storage
    )
    assert not result.ok and "varios médicos" in content


def test_no_connection_is_held_while_rendering(test_db, world, storage, test_engine, monkeypatch):
    seen = []

    def fake(_data):
        seen.append(test_engine.pool.checkedout())
        return b"%PDF-1.7 synthetic"

    monkeypatch.setattr(summaries_pdf, "render_previsit", fake)
    monkeypatch.setattr(summaries_pdf, "render_emergency_card", fake)
    uno_id = world["uno"].id
    test_db.close()
    for name, args in (
        ("previsit_summary", {"patient": "papa", "doctor": uno_id}),
        ("emergency_card", {"patient": "papa"}),
    ):
        _, result, content = run(test_db, name, args, storage)
        assert result.ok, content
    assert seen == [0, 0]


def test_group_meds_merges_slots_of_one_medication():
    rows = [
        previsit_svc.MedRow("Losartán", "50 mg", "1/2", None, "desayuno", "07:00", None, None),
        previsit_svc.MedRow("Otra", None, "1", None, "almuerzo", None, None, None),
        previsit_svc.MedRow("losartán", "50 mg", "1/2", None, "cena", None, None, None),
        previsit_svc.MedRow("Losartán", "100 mg", "1", None, "noche", None, None, None),
    ]
    groups = card_svc.group_meds(rows)
    assert [(g.name, g.doses) for g in groups] == [
        ("Losartán 50 mg", ["½ Desayuno 07:00", "½ Cena"]),
        ("Otra", ["1 Almuerzo"]),
        ("Losartán 100 mg", ["1 Noche"]),
    ]


def test_long_card_flows_to_a_second_page_without_cutting(auth_client, world, test_db):
    papa = world["papa"]
    for i in range(80):
        test_db.add(MedItem(patient_id=papa.id, name=f"Fármaco {i:02d}", slot="otros"))
    test_db.commit()
    resp = auth_client.get(card_url(papa))
    assert resp.status_code == 200
    reader = PdfReader(io.BytesIO(resp.content))
    assert len(reader.pages) >= 2
    first = " ".join((reader.pages[0].extract_text() or "").split())
    assert CIP in first and "EVITAR" in first and "Sustancia X" in first
    text, _ = pdf_text(resp.content)
    assert "Fármaco 79" in text and "más" not in text


def test_previsit_keeps_the_newest_lab_reports(test_db, world):
    papa = world["papa"]
    start = date(2025, 3, 11)
    for i in range(previsit_svc.MAX_LAB_REPORTS + 5):
        report = LabReport(
            patient_id=papa.id,
            collected_on=start + timedelta(days=2 * i),
            lab_name=f"Lab Semanal {i:02d}",
            source="upload",
            status="ok",
        )
        test_db.add(report)
        test_db.flush()
        if i == previsit_svc.MAX_LAB_REPORTS + 4:
            test_db.add(
                LabResult(
                    report_id=report.id,
                    raw_name="Potasio más reciente",
                    value_num=Decimal("6.1"),
                    flag="alto",
                )
            )
    test_db.commit()
    total = previsit_svc.MAX_LAB_REPORTS + 6  # + «Lab Sintético» from the fixture
    s = previsit_svc.previsit_summary(test_db, papa.id, world["uno"].id, TODAY)
    assert s.labs_total == total and len(s.labs) == previsit_svc.MAX_LAB_REPORTS
    names = [r.lab_name for r in s.labs]
    assert names[-1] == f"Lab Semanal {previsit_svc.MAX_LAB_REPORTS + 4:02d}"
    assert [r.date for r in s.labs] == sorted(r.date for r in s.labs)
    assert "Lab Semanal 00" not in names
    assert s.counts()["lab_reports"] == total
    # Over the whole period: the fixture report (2) + the newest one (1).
    assert s.counts()["out_of_range_results"] == 3
    text, _ = pdf_text(summaries_pdf.render_previsit(s))
    assert "Potasio más reciente" in text and "ALTO" in text
    assert f"Se muestran los {previsit_svc.MAX_LAB_REPORTS} más recientes de {total}" in text


def test_dose_text_keeps_day_rules_duplicates_and_unknown_quantities():
    MedRow = previsit_svc.MedRow
    weekly = MedRow(
        "Fármaco semanal",
        "2.5 mg",
        "4",
        None,
        "desayuno",
        None,
        "con agua",
        "semanal,\n  los lunes",
        days=[1],
    )
    twice = MedRow("Doble", None, "1", None, "noche", "21:00", None, None)
    rescue = MedRow("Rescate", None, None, None, "segun_necesidad", None, None, None)
    long_note = MedRow("Larga", None, "1", None, "cena", None, "x" * 80, None, days=[1, 3, 5])
    groups = card_svc.group_meds([weekly, twice, twice, rescue, long_note])
    assert [(g.name, g.doses) for g in groups] == [
        ("Fármaco semanal 2.5 mg", ["4 Desayuno — lun (semanal, los lunes; con agua)"]),
        # Two identical rows are two doses, never merged into one.
        ("Doble", ["1 Noche 21:00", "1 Noche 21:00"]),
        ("Rescate", ["Según necesidad (rescate), cantidad según indicación"]),
        # Long instructions are left out; the day rule stays.
        ("Larga", ["1 Cena — lun, mié, vie"]),
    ]


def test_card_pdf_shows_weekly_rule(auth_client, world, test_db):
    papa = world["papa"]
    test_db.add(
        MedItem(
            patient_id=papa.id,
            name="Fármaco semanal",
            quantity="4",
            slot="desayuno",
            days=[1],
            schedule_note="semanal",
        )
    )
    test_db.commit()
    text, _ = pdf_text(auth_client.get(card_url(papa)).content)
    assert "Fármaco semanal 4 Desayuno — lun (semanal)" in text
    body = auth_client.get(card_url(papa, "")).json()
    weekly = next(g for g in body["med_groups"] if g["name"] == "Fármaco semanal")
    assert weekly["doses"] == ["4 Desayuno — lun (semanal)"]
