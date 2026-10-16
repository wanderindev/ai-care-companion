"""Chat tools against synthetic data (no Claude involved)."""

import json
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.models.appointment import Appointment
from app.models.audit import AuditLog
from app.models.chat import ChatSession
from app.models.doctor import Doctor
from app.models.lab import LabReport, LabResult
from app.models.log_entry import LogEntry
from app.models.medication import MedAvoid, MedEvent, MedItem, MedListVersion
from app.models.suggestion import Suggestion
from app.services.appointments import local_datetime, today_local
from app.services.chat import tools


def call(db, name, args=None, tool_use_id="toolu_1"):
    run = tools.run_tool(tools.ToolContext(db=db), tool_use_id, name, args or {})
    content = run.block["content"]
    return run, (json.loads(content) if run.ok else content)


@pytest.fixture
def data(test_db, patients):
    papa, mama = patients
    perez = Doctor(name="Dra. Ana Pérez", aliases=["Dra. Pérez"], specialty="Cardiología")
    ruiz = Doctor(name="Dr. Luis Ruiz", specialty="Geriatría", is_active=False)
    test_db.add_all([perez, ruiz])
    test_db.flush()
    e1 = LogEntry(
        patient_id=papa.id,
        entry_date=date(2025, 3, 12),
        entry_time=time(10, 30),
        doctor_id=perez.id,
        entry_type="consulta_control",
        channel="presencial",
        title="Control de presión",
        indications="Seguir con el antihipertensivo. Repetir perfil lipídico.",
    )
    e2 = LogEntry(
        patient_id=mama.id,
        entry_date=date(2025, 5, 2),
        entry_type="observacion_familiar",
        title="Tos seca",
        family_notes="Tos seca por las noches.",
        review_status="pending_review",
    )
    test_db.add_all([e1, e2])
    test_db.flush()
    upcoming = Appointment(
        patient_id=papa.id,
        doctor_id=perez.id,
        starts_at=local_datetime(today_local() + timedelta(days=7), time(9, 0)),
        reason="Control",
        created_from_entry_id=e1.id,
    )
    past = Appointment(
        patient_id=papa.id,
        starts_at=local_datetime(date(2024, 1, 5), time(9, 0)),
        status="realizada",
    )
    test_db.add_all([upcoming, past])
    test_db.add_all(
        [
            MedItem(patient_id=papa.id, name="Losartán", strength="50 mg", slot="desayuno"),
            MedItem(
                patient_id=papa.id,
                name="Aspirina",
                slot="cena",
                end_date=date(2020, 1, 1),  # suspended: not current
            ),
            MedAvoid(patient_id=papa.id, substance="Penicilina", reason="Alergia"),
            MedListVersion(
                patient_id=papa.id,
                version_no=1,
                snapshot={"items": [], "avoid": []},
                change_summary="Lista inicial",
                effective_date=date(2025, 3, 12),
            ),
            MedEvent(
                patient_id=papa.id,
                event_date=date(2025, 3, 12),
                kind="inicio",
                medication="Losartán 50 mg",
                source_entry_id=e1.id,
            ),
            MedEvent(
                patient_id=papa.id,
                event_date=date(2024, 6, 1),
                kind="suspension",
                medication="Aspirina",
            ),
        ]
    )
    report = LabReport(
        patient_id=papa.id,
        collected_on=date(2025, 4, 1),
        lab_name="Lab Uno",
        source="upload",
        status="ok",
    )
    test_db.add(report)
    test_db.flush()
    test_db.add_all(
        [
            LabResult(
                report_id=report.id,
                position=0,
                raw_name="Glucosa",
                value_text="130",
                value_num=Decimal("130"),
                unit="mg/dL",
                ref_low=Decimal("70"),
                ref_high=Decimal("100"),
                flag="alto",
            ),
            LabResult(
                report_id=report.id,
                position=1,
                raw_name="Creatinina",
                value_text="0.9",
                value_num=Decimal("0.9"),
                unit="mg/dL",
            ),
        ]
    )
    test_db.commit()
    return {
        "papa": papa.id,
        "mama": mama.id,
        "perez": perez.id,
        "e1": e1.id,
        "e2": e2.id,
        "report": report.id,
        "upcoming": upcoming.id,
    }


def test_definitions_are_static_and_complete():
    names = [d["name"] for d in tools.definitions()]
    assert names == [
        "list_patients",
        "list_doctors",
        "search_log_entries",
        "get_log_entry",
        "list_appointments",
        "get_current_meds",
        "get_med_history",
        "search_lab_results",
        "get_lab_report",
        "generate_pdf",
        "generate_xlsx",
        "previsit_summary",
        "emergency_card",
        "propose_change",
    ]
    assert tools.definitions() == tools.definitions()
    for d in tools.definitions():
        assert d["input_schema"]["type"] == "object"
        assert d["input_schema"]["additionalProperties"] is False


def test_list_patients_hides_private_fields(test_db, patients):
    papa = patients[0]
    papa.full_name = "Nombre Sintético Completo"
    papa.id_number = "0-000-0000"
    papa.birth_date = date(1940, 1, 1)
    papa.diagnoses = "Hipertensión"
    test_db.commit()
    run, result = call(test_db, "list_patients")
    assert run.ok
    assert result[0] == {
        "id": papa.id,
        "patient": "papa",
        "name": "Papá",
        "diagnoses": "Hipertensión",
    }
    text = run.block["content"]
    assert "0-000-0000" not in text and "1940" not in text and "Completo" not in text


def test_list_doctors(test_db, data):
    _, result = call(test_db, "list_doctors")
    assert [d["name"] for d in result] == ["Dra. Ana Pérez"]
    _, result = call(test_db, "list_doctors", {"q": "perez"})
    assert result[0]["id"] == data["perez"]
    _, result = call(test_db, "list_doctors", {"q": "geriatria", "include_inactive": True})
    assert result[0]["inactive"] is True


def test_search_log_entries(test_db, data):
    _, result = call(test_db, "search_log_entries", {"q": "presion"})
    assert result["total"] == 1
    item = result["items"][0]
    assert item["id"] == data["e1"]
    assert item["date"] == "2025-03-12" and item["time"] == "10:30"
    assert item["doctor"] == "Dra. Ana Pérez" and item["patient"] == "papa"
    # The doctor's name is searchable too.
    _, result = call(test_db, "search_log_entries", {"q": "perez"})
    assert [i["id"] for i in result["items"]] == [data["e1"]]
    _, result = call(test_db, "search_log_entries", {"patient": "mama"})
    assert [i["id"] for i in result["items"]] == [data["e2"]]
    assert result["items"][0]["pending_review"] is True
    _, result = call(test_db, "search_log_entries", {"date_from": "2025-04-01"})
    assert [i["id"] for i in result["items"]] == [data["e2"]]
    _, result = call(test_db, "search_log_entries", {"limit": 1, "order": "asc"})
    assert result["total"] == 2 and [i["id"] for i in result["items"]] == [data["e1"]]


def test_get_log_entry(test_db, data):
    _, result = call(test_db, "get_log_entry", {"id": data["e1"]})
    assert result["indications"].startswith("Seguir con el antihipertensivo")
    assert result["next_appointment"]["id"] == data["upcoming"]
    run, message = call(test_db, "get_log_entry", {"id": 999999})
    assert not run.ok and run.block["is_error"] is True and "999999" in message


def test_list_appointments_defaults_to_upcoming(test_db, data):
    _, result = call(test_db, "list_appointments")
    assert [a["id"] for a in result["items"]] == [data["upcoming"]]
    assert result["items"][0]["time"] == "09:00"
    assert result["items"][0]["from_entry_id"] == data["e1"]
    _, result = call(test_db, "list_appointments", {"date_to": "2024-12-31"})
    assert [a["status"] for a in result["items"]] == ["realizada"]


def test_get_current_meds(test_db, data):
    _, result = call(test_db, "get_current_meds", {"patient": "papa"})
    assert [i["name"] for i in result["items"]] == ["Losartán"]
    assert result["avoid"][0]["substance"] == "Penicilina"
    assert result["version"] == 1 and result["effective_date"] == "2025-03-12"
    run, message = call(test_db, "get_current_meds", {})
    assert not run.ok and "patient" in message
    run, message = call(test_db, "get_current_meds", {"patient": "tio"})
    assert not run.ok and "desconocido" in message


def test_get_med_history(test_db, data):
    _, result = call(test_db, "get_med_history", {"patient": str(data["papa"])})
    assert [e["kind"] for e in result["events"]] == ["inicio", "suspension"]
    assert result["events"][0]["source_entry_id"] == data["e1"]
    assert result["list_versions"][0]["summary"] == "Lista inicial"
    _, result = call(test_db, "get_med_history", {"patient": "papa", "medication": "LOSARTAN"})
    assert result["events_total"] == 1


def test_search_lab_results_and_report(test_db, data):
    _, result = call(test_db, "search_lab_results", {"q": "glucosa", "patient": "papa"})
    assert result["total"] == 1
    row = result["items"][0]
    assert row == {
        "report_id": data["report"],
        "date": "2025-04-01",
        "patient": "papa",
        "lab": "Lab Uno",
        "test": "Glucosa",
        "value": "130",
        "unit": "mg/dL",
        "ref": "70–100",
        "flag": "alto",
    }
    _, result = call(test_db, "search_lab_results", {"out_of_range_only": True})
    assert [r["test"] for r in result["items"]] == ["Glucosa"]
    _, report = call(test_db, "get_lab_report", {"id": data["report"]})
    assert [r["test"] for r in report["results"]] == ["Glucosa", "Creatinina"]


@pytest.mark.parametrize(
    ("name", "args", "needle"),
    [
        ("search_log_entries", {"date_from": "12/03/2025"}, "AAAA-MM-DD"),
        ("search_log_entries", {"entry_type": "fiesta"}, "entry_type"),
        ("search_log_entries", {"limit": "muchos"}, "limit"),
        ("search_log_entries", {"bogus": 1}, "bogus"),
        ("list_appointments", {"status": "perdida"}, "status"),
        ("no_such_tool", {}, "desconocida"),
    ],
)
def test_bad_input_is_an_error_result(test_db, patients, name, args, needle):
    run, message = call(test_db, name, args)
    assert not run.ok and run.block["is_error"] is True
    assert needle in message


def test_cap_result_drops_items_and_says_so():
    value = {"total": 500, "items": [{"id": i, "text": "x" * 100} for i in range(500)]}
    text = tools.cap_result(value, max_chars=2_000)
    assert len(text) <= 2_000
    data = json.loads(text)
    assert data["truncated"] == tools.TRUNCATED_NOTE
    assert data["shown"] == len(data["items"]) and 0 < data["shown"] < 500
    assert len(tools.cap_result("y" * 5_000, max_chars=1_000)) <= 1_000

    """The read-only tools (the file tools store files: test_chat_files.py,
    test_summaries.py)."""


FILE_TOOLS = {"generate_pdf", "generate_xlsx", "previsit_summary", "emergency_card"}
# propose_change writes a suggestion: tested below.
WRITE_TOOLS = FILE_TOOLS | {"propose_change"}


def test_tools_never_write(test_db, data):
    """The read-only tools (the file tools store files: test_chat_files.py)."""

    def audit_rows():
        return test_db.execute(text("SELECT count(*) FROM audit_log")).scalar()

    before = audit_rows()
    for definition in tools.definitions():
        if definition["name"] in WRITE_TOOLS:
            continue
        props = definition["input_schema"]["properties"]
        args = {"patient": "papa"} if "patient" in props else {}
        if "id" in props:
            args = {"id": data["report"] if "lab" in definition["name"] else data["e1"]}
        run, _ = call(test_db, definition["name"], args)
        assert run.ok, definition["name"]
        assert not test_db.dirty and not test_db.new
    assert audit_rows() == before


# ── propose_change (#41) ─────────────────────────────────────────────────────


@pytest.fixture
def proposer(test_db, data, member):
    """Runs propose_change as ``member`` in a chat session."""
    session = ChatSession(title="Pruebas", created_by=member.id)
    test_db.add(session)
    test_db.commit()
    ids = {"session": session.id, "member": member.id}

    def propose(args):
        ctx = tools.ToolContext(db=test_db, user_id=ids["member"], session_id=ids["session"])
        run = tools.run_tool(ctx, "toolu_p", "propose_change", args)
        content = run.block["content"]
        return run, (json.loads(content) if run.ok else content), ctx

    propose.ids = ids
    return propose


def suggestions_in(test_db):
    test_db.expire_all()
    return list(test_db.scalars(select(Suggestion).order_by(Suggestion.id)))


ENTRY = {
    "entry_date": "2025-06-10",
    "entry_type": "consulta_control",
    "title": "Control de cardiología",
    "indications": "Seguir igual. Repetir electrocardiograma.",
}


def test_propose_entry_creates_a_pending_chat_suggestion(test_db, data, proposer):
    entries_before = test_db.scalar(select(func.count()).select_from(LogEntry))
    run, result, ctx = proposer(
        {
            "kind": "create_entry",
            "patient": "papa",
            "payload": {**ENTRY, "doctor_id": data["perez"]},
            "rationale": "Ana contó la consulta de hoy.",
        }
    )
    assert run.ok, result
    [row] = suggestions_in(test_db)
    assert (row.kind, row.source, row.status) == ("create_entry", "chat", "pending")
    assert row.patient_id == data["papa"] and row.payload["patient_id"] == data["papa"]
    assert row.chat_session_id == proposer.ids["session"]
    assert row.created_by == proposer.ids["member"]
    assert row.source_message_ids == [] and row.rationale == "Ana contó la consulta de hoy."
    # Nothing written to the bitácora: only the proposal.
    assert test_db.scalar(select(func.count()).select_from(LogEntry)) == entries_before
    assert result["suggestion_id"] == row.id and result["status"] == "pending"
    assert result["patient"] == "papa" and result["note"] == tools.PROPOSAL_NOTE
    assert result["summary"] == "10/06/2025 · Control de cardiología"
    assert ctx.suggestions == [
        {
            "id": row.id,
            "kind": "create_entry",
            "patient_id": data["papa"],
            "summary": result["summary"],
        }
    ]
    audit_row = test_db.scalar(
        select(AuditLog).where(AuditLog.entity == "suggestion", AuditLog.action == "create")
    )
    assert audit_row.user_id == proposer.ids["member"]


def _entry(**changes):
    return {"kind": "create_entry", "patient": "papa", "payload": {**ENTRY, **changes}}


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"kind": "lab_report", "payload": {"x": 1}}, '"kind"'),
        ({"kind": "create_entry", "payload": ENTRY}, '"patient"'),
        ({"kind": "create_entry", "patient": "tio", "payload": ENTRY}, "desconocido"),
        ({"kind": "create_entry", "patient": "papa", "payload": {}}, '"payload"'),
        (_entry(patient_id=2), "patient_id"),
        (_entry(entry_type="fiesta"), "entry_type"),
        (_entry(doctor_id=999), "No existe el médico 999"),
        (_entry(entry_date="2999-01-01"), "hechos pasados"),
        (_entry(attachment_ids=[1]), "archivos"),
        ({"kind": "create_appointment", "patient": "mama", "payload": {"time": "10:00"}}, '"date"'),
        (
            {
                "kind": "create_appointment",
                "patient": "mama",
                "payload": {"date": "2030-01-01", "time": "10h"},
            },
            "HH:MM",
        ),
        (
            {"kind": "update_entry", "payload": {"entry_id": 999, "changes": {"title": "x"}}},
            "No existe",
        ),
    ],
)
def test_invalid_proposals_are_error_results(test_db, data, proposer, args, needle):
    run, message, ctx = proposer({"rationale": "Lo dijo Ana.", **args})
    assert not run.ok and run.block["is_error"] is True
    assert needle in message
    assert suggestions_in(test_db) == [] and ctx.suggestions == []


def test_missing_rationale_is_an_error(test_db, data, proposer):
    run, message, _ = proposer({"kind": "create_entry", "patient": "papa", "payload": ENTRY})
    assert not run.ok and "rationale" in message


def test_duplicates_of_records_and_pending_proposals_are_refused(test_db, data, proposer):
    same_as_e1 = {
        "entry_date": "2025-03-12",
        "entry_type": "consulta_control",
        "doctor_id": data["perez"],
        "title": "Control de presión",
    }
    run, message, _ = proposer(
        {"kind": "create_entry", "patient": "papa", "payload": same_as_e1, "rationale": "r"}
    )
    assert not run.ok and message == tools.DUPLICATE_MESSAGES["entry"]

    args = {"kind": "create_entry", "patient": "mama", "payload": ENTRY, "rationale": "r"}
    assert proposer(args)[0].ok
    run, message, _ = proposer(args)
    assert not run.ok and message == tools.DUPLICATE_MESSAGES["pending"]
    assert len(suggestions_in(test_db)) == 1

    run, message, _ = proposer(
        {"kind": "doctor", "payload": {"name": "Dra. Pérez"}, "rationale": "r"}
    )
    assert not run.ok and message == tools.DUPLICATE_MESSAGES["doctor"]


def test_propose_appointment_from_date_and_time(test_db, data, proposer):
    run, result, _ = proposer(
        {
            "kind": "create_appointment",
            "patient": "mama",
            "payload": {"date": "2030-02-05", "time": "9:30", "reason": "Control"},
            "rationale": "Ana avisó la cita.",
        }
    )
    assert run.ok, result
    run, result2, _ = proposer(
        {
            "kind": "create_appointment",
            "patient": "papa",
            "payload": {"date": "2030-02-06", "reason": "Vacuna"},
            "rationale": "r",
        }
    )
    assert run.ok, result2
    timed, all_day = suggestions_in(test_db)
    assert timed.payload["starts_at"].startswith("2030-02-05T09:30")
    assert timed.payload["all_day"] is False and timed.patient_id == data["mama"]
    assert all_day.payload["all_day"] is True
    assert result["summary"] == "05/02/2030 · 09:30 · Control"
    assert result2["summary"] == "06/02/2030 · Vacuna"


def test_propose_med_change(test_db, data, proposer):
    item = test_db.scalar(select(MedItem).where(MedItem.name == "Losartán"))
    run, message, _ = proposer(
        {
            "kind": "med_change",
            "patient": "papa",
            "payload": {"action": "stop", "medication": "Metformina", "item_id": item.id},
            "rationale": "r",
        }
    )
    assert not run.ok and "no coincide" in message
    run, message, _ = proposer(
        {
            "kind": "med_change",
            "patient": "papa",
            "payload": {
                "action": "add",
                "medication": "Losartán",
                "item": {"name": "Losartán", "slot": "cena"},
            },
            "rationale": "r",
        }
    )
    assert not run.ok and message == tools.DUPLICATE_MESSAGES["medication"]

    run, result, _ = proposer(
        {
            "kind": "med_change",
            "patient": "papa",
            "payload": {
                "action": "change",
                "medication": "Losartán",
                "item_id": item.id,
                "item": {"strength": "100 mg"},
            },
            "rationale": "La cardióloga subió la dosis.",
        }
    )
    assert run.ok, result
    [row] = suggestions_in(test_db)
    assert row.payload["base_version"] == 1  # the current list version
    assert result["summary"] == "Cambiar Losartán"


def test_propose_update_entry_appends_text(test_db, data, proposer):
    run, message, _ = proposer(
        {
            "kind": "update_entry",
            "patient": "mama",
            "payload": {"entry_id": data["e1"], "changes": {"add_family_notes": "x"}},
            "rationale": "r",
        }
    )
    assert not run.ok and "otro paciente" in message
    run, result, _ = proposer(
        {
            "kind": "update_entry",
            "payload": {
                "entry_id": data["e1"],
                "changes": {"add_indications": "Además: caminar 20 minutos."},
            },
            "rationale": "Ana recordó otra indicación.",
        }
    )
    assert run.ok, result
    [row] = suggestions_in(test_db)
    assert row.patient_id == data["papa"]
    assert row.payload["changes"] == {
        "indications": "Seguir con el antihipertensivo. Repetir perfil lipídico.\n\n"
        "Además: caminar 20 minutos."
    }
    assert result["summary"] == "12/03/2025 · Control de presión"


def _update(changes, entry_id):
    return {"kind": "update_entry", "payload": {"entry_id": entry_id, "changes": changes}}


def test_update_entry_text_is_append_only(test_db, data, proposer):
    e1 = data["e1"]
    for field_name in ("indications", "family_notes"):
        run, message, _ = proposer({**_update({field_name: "Texto nuevo."}, e1), "rationale": "r"})
        assert not run.ok and f'"add_{field_name}"' in message
    # Text the entry already has adds nothing.
    run, message, _ = proposer(
        {**_update({"add_indications": "repetir perfil lipídico"}, e1), "rationale": "r"}
    )
    assert not run.ok and "ya contiene" in message
    assert suggestions_in(test_db) == []
    # With another change, the known text is dropped and the rest stays.
    run, result, _ = proposer(
        {
            **_update({"add_indications": "Repetir perfil lipídico.", "entry_time": "11:00"}, e1),
            "rationale": "r",
        }
    )
    assert run.ok, result
    [row] = suggestions_in(test_db)
    assert row.payload["changes"] == {"entry_time": "11:00:00"}


def test_bool_ids_and_missing_item_id_are_clear_errors(test_db, data, proposer):
    run, message, _ = proposer({**_update({"title": "x"}, True), "rationale": "r"})
    assert not run.ok and "No existe esa entrada" in message
    for action in ("stop", "change"):
        payload = {"action": action, "medication": "Losartán"}
        if action == "change":
            payload["item"] = {"strength": "100 mg"}
        run, message, _ = proposer(
            {"kind": "med_change", "patient": "papa", "payload": payload, "rationale": "r"}
        )
        assert not run.ok and '"item_id"' in message
    assert suggestions_in(test_db) == []


def test_propose_doctor_has_no_patient(test_db, data, proposer):
    run, result, _ = proposer(
        {
            "kind": "doctor",
            "patient": "papa",
            "payload": {"name": "Dr. Tomás Vega", "specialty": "Neurología"},
            "rationale": "Nuevo neurólogo.",
        }
    )
    assert run.ok, result
    [row] = suggestions_in(test_db)
    assert row.patient_id is None and "patient" not in result
    assert result["summary"] == "Dr. Tomás Vega"
