"""Appointments: API CRUD, filters, validation, audit; the bitácora's "Próxima
cita"; the backfill from imported entries.

Synthetic data only: invented doctors, places and notes.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.appointment import Appointment
from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.services import log_entries as entries_svc
from app.services.appointments_backfill import backfill

URL = "/api/v1/appointments"
ENTRIES = "/api/v1/log-entries"
APPT_AUDIT = select(AuditLog).where(AuditLog.entity == "appointment").order_by(AuditLog.id)


@pytest.fixture
def doctors(test_db):
    rows = [
        Doctor(name="Dra. Ana Pérez", aliases=["Anita"]),
        Doctor(name="Dr. Bruno Zúñiga", aliases=[]),
    ]
    test_db.add_all(rows)
    test_db.commit()
    return rows


def _appt(**overrides):
    body = {"patient_id": 1, "starts_at": "2026-11-03T14:30:00-05:00", "reason": "Control"}
    body.update(overrides)
    return body


def _create(client, **overrides):
    resp = client.post(URL, json=_appt(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


# ── API ──────────────────────────────────────────────────────────────────────


def test_requires_sign_in(db_client):
    assert db_client.get(URL).status_code == 401
    assert db_client.get(f"{URL}/1").status_code == 401
    headers = {"X-Requested-With": "fetch"}
    assert db_client.post(URL, json=_appt(), headers=headers).status_code == 401
    assert db_client.patch(f"{URL}/1", json={}, headers=headers).status_code == 401
    assert db_client.delete(f"{URL}/1", headers=headers).status_code == 401


def test_write_needs_csrf_header(auth_client, patients):
    del auth_client.headers["X-Requested-With"]
    assert auth_client.post(URL, json=_appt()).status_code == 403


def test_create_get_and_audit(auth_client, test_db, patients, doctors, member):
    ana = doctors[0]
    created = _create(
        auth_client,
        doctor_id=ana.id,
        # UTC in, Panama out.
        starts_at="2026-11-03T19:30:00Z",
        location="Consultorio 4",
        notes="  ",
        status="realizada",  # ignored: new appointments are programada
        created_from_entry_id=5,  # ignored: only the bitácora sets it
    )
    assert created["starts_at"] == "2026-11-03T14:30:00-05:00"
    assert created["status"] == "programada"
    assert created["created_from_entry_id"] is None
    assert created["duration_min"] == 30
    assert created["all_day"] is False
    assert created["notes"] is None
    assert created["doctor"] == {"id": ana.id, "name": "Dra. Ana Pérez"}
    assert created["created_by"] == member.id
    assert auth_client.get(f"{URL}/{created['id']}").json() == created
    assert auth_client.get(f"{URL}/999").status_code == 404

    row = test_db.scalars(APPT_AUDIT).one()
    assert (row.action, row.user_id, row.before) == ("create", member.id, None)
    assert row.after["location"] == "Consultorio 4"


def test_naive_time_is_panama_and_all_day_is_midnight(auth_client, patients):
    naive = _create(auth_client, starts_at="2026-11-03T09:00:00")
    assert naive["starts_at"] == "2026-11-03T09:00:00-05:00"
    # 02:00 UTC on the 4th is still the 3rd in Panama.
    all_day = _create(auth_client, starts_at="2026-11-04T02:00:00Z", all_day=True)
    assert all_day["starts_at"] == "2026-11-03T00:00:00-05:00"
    assert all_day["all_day"] is True


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"patient_id": 99}, 400),
        ({"doctor_id": 99}, 400),
        ({"duration_min": 0}, 422),
        ({"duration_min": 1441}, 422),
        ({"starts_at": "1850-01-01T10:00:00-05:00"}, 400),
        ({"starts_at": "mañana"}, 422),
        ({"reason": "x" * 301}, 422),
        ({"location": "x" * 301}, 422),
        ({"patient_id": None}, 422),
    ],
)
def test_create_validation(auth_client, patients, overrides, status):
    assert auth_client.post(URL, json=_appt(**overrides)).status_code == status


def test_patch_cancel_realizada_and_delete(auth_client, test_db, patients, member):
    appt = _create(auth_client)
    url = f"{URL}/{appt['id']}"

    resp = auth_client.patch(url, json={"status": "cancelada", "notes": "Llamaron para mover"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelada"
    assert resp.json()["updated_by"] == member.id

    # A no-op PATCH is not audited.
    assert auth_client.patch(url, json={"status": "cancelada"}).status_code == 200

    entry = entries_svc.create_entry(
        test_db,
        {
            "patient_id": patients[0].id,
            "entry_date": date(2026, 10, 1),
            "entry_type": "consulta_control",
            "title": "Control",
        },
        user_id=None,
    )
    other = entries_svc.create_entry(
        test_db,
        {
            "patient_id": patients[1].id,
            "entry_date": date(2026, 10, 1),
            "entry_type": "consulta_control",
            "title": "Control",
        },
        user_id=None,
    )
    test_db.commit()
    bad = auth_client.patch(url, json={"status": "realizada", "result_entry_id": other.id})
    assert bad.status_code == 400
    assert "otro paciente" in bad.json()["detail"]
    assert auth_client.patch(url, json={"result_entry_id": 999}).status_code == 400
    assert auth_client.patch(url, json={"status": None}).status_code == 422
    assert auth_client.patch(url, json={"status": "perdida"}).status_code == 422
    assert auth_client.patch(url, json={"duration_min": 0}).status_code == 422

    done = auth_client.patch(url, json={"status": "realizada", "result_entry_id": entry.id})
    assert done.json()["status"] == "realizada"
    assert done.json()["result_entry_id"] == entry.id

    # Moving to an all-day keeps the day and goes to midnight.
    moved = auth_client.patch(url, json={"all_day": True}).json()
    assert moved["starts_at"] == "2026-11-03T00:00:00-05:00"

    assert auth_client.delete(url).status_code == 204
    assert auth_client.get(url).status_code == 404
    assert auth_client.delete(url).status_code == 404

    actions = [(r.action, r.user_id) for r in test_db.scalars(APPT_AUDIT)]
    assert actions == [
        ("create", member.id),
        ("update", member.id),
        ("update", member.id),
        ("update", member.id),
        ("delete", member.id),
    ]
    deleted = test_db.scalars(APPT_AUDIT).all()[-1]
    assert deleted.before["status"] == "realizada"
    assert deleted.before["created_by"] == member.id  # self-contained snapshot


def test_list_filters_and_order(auth_client, patients, doctors):
    papa, mama = patients
    a = _create(auth_client, starts_at="2026-11-03T10:00:00-05:00")
    b = _create(auth_client, starts_at="2026-11-01T08:00:00-05:00", doctor_id=doctors[0].id)
    c = _create(auth_client, patient_id=mama.id, starts_at="2026-11-02T23:30:00-05:00")
    d = _create(auth_client, starts_at="2026-12-01T00:00:00-05:00", all_day=True)
    auth_client.patch(f"{URL}/{d['id']}", json={"status": "cancelada"})

    assert _ids(auth_client.get(URL)) == [b["id"], c["id"], a["id"], d["id"]]
    assert _ids(auth_client.get(URL, params={"order": "desc"})) == [
        d["id"],
        a["id"],
        c["id"],
        b["id"],
    ]
    assert _ids(auth_client.get(URL, params={"patient_id": mama.id})) == [c["id"]]
    both = auth_client.get(URL, params=[("patient_id", papa.id), ("patient_id", mama.id)])
    assert len(_ids(both)) == 4
    assert _ids(auth_client.get(URL, params={"doctor_id": doctors[0].id})) == [b["id"]]
    assert _ids(auth_client.get(URL, params={"status": "cancelada"})) == [d["id"]]
    two = auth_client.get(URL, params=[("status", "cancelada"), ("status", "programada")])
    assert len(_ids(two)) == 4
    # Days are Panama days: 23:30 on the 2nd (04:30 UTC on the 3rd) is the 2nd.
    day = auth_client.get(URL, params={"date_from": "2026-11-02", "date_to": "2026-11-02"})
    assert _ids(day) == [c["id"]]
    assert _ids(auth_client.get(URL, params={"date_from": "2026-11-03"})) == [a["id"], d["id"]]
    assert _ids(auth_client.get(URL, params={"date_to": "2026-11-01"})) == [b["id"]]

    page = auth_client.get(URL, params={"limit": 2, "offset": 1}).json()
    assert ([i["id"] for i in page["items"]], page["total"]) == ([c["id"], a["id"]], 4)

    assert auth_client.get(URL, params={"status": "perdida"}).status_code == 422
    bad_range = auth_client.get(URL, params={"date_from": "2026-11-05", "date_to": "2026-11-01"})
    assert bad_range.status_code == 400


# ── "Marcar como realizada" ─────────────────────────────────────────────────


PAST = "2026-03-03T09:00:00-05:00"


def _visit(**overrides):
    body = {
        "patient_id": 1,
        "entry_date": "2026-03-03",
        "entry_type": "consulta_control",
        "title": "Control",
        "indications": "Seguir igual.",
    }
    body.update(overrides)
    return body


def test_complete_creates_entry_and_links_it(auth_client, test_db, patients, doctors, member):
    ana = doctors[0]
    appt = _create(auth_client, doctor_id=ana.id, starts_at=PAST)
    resp = auth_client.post(f"{URL}/{appt['id']}/complete", json=_visit(doctor_id=ana.id))
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    assert (entry["origin"], entry["doctor"]["id"], entry["title"]) == ("manual", ana.id, "Control")

    done = auth_client.get(f"{URL}/{appt['id']}").json()
    assert (done["status"], done["result_entry_id"]) == ("realizada", entry["id"])
    rows = test_db.scalars(APPT_AUDIT).all()
    assert [r.action for r in rows] == ["create", "update"]
    assert rows[-1].after["status"] == "realizada"
    assert rows[-1].user_id == member.id
    assert test_db.get(LogEntry, entry["id"]) is not None

    # A second completion (e.g. another sibling at the same time) is refused.
    again = auth_client.post(f"{URL}/{appt['id']}/complete", json=_visit())
    assert again.status_code == 409
    assert "ya se registró" in again.json()["detail"]
    assert len(test_db.scalars(select(LogEntry)).all()) == 1


def test_complete_is_atomic(auth_client, test_db, patients):
    appt = _create(auth_client, starts_at=PAST)
    # Another patient's entry: nothing is created, the appointment is untouched.
    wrong = auth_client.post(f"{URL}/{appt['id']}/complete", json=_visit(patient_id=patients[1].id))
    assert wrong.status_code == 400
    assert "otro paciente" in wrong.json()["detail"]
    assert test_db.scalars(select(LogEntry)).all() == []
    assert auth_client.get(f"{URL}/{appt['id']}").json()["status"] == "programada"

    # Invalid entry body (no doctor, source or title): 422, nothing written.
    assert (
        auth_client.post(f"{URL}/{appt['id']}/complete", json=_visit(title=None)).status_code == 422
    )

    cancelled = _create(auth_client, starts_at=PAST)
    auth_client.patch(f"{URL}/{cancelled['id']}", json={"status": "cancelada"})
    refused = auth_client.post(f"{URL}/{cancelled['id']}/complete", json=_visit())
    assert refused.status_code == 409
    assert auth_client.post(f"{URL}/999/complete", json=_visit()).status_code == 404
    assert test_db.scalars(select(LogEntry)).all() == []


def test_complete_requires_sign_in(db_client):
    headers = {"X-Requested-With": "fetch"}
    assert db_client.post(f"{URL}/1/complete", json=_visit(), headers=headers).status_code == 401


# ── Bitácora "Próxima cita" ─────────────────────────────────────────────────


def _entry(**overrides):
    body = {
        "patient_id": 1,
        "entry_date": "2026-03-10",
        "entry_type": "consulta_control",
        "title": "Control general",
    }
    body.update(overrides)
    return body


def _linked(test_db, entry_id):
    test_db.expire_all()
    return test_db.scalar(select(Appointment).where(Appointment.created_from_entry_id == entry_id))


def test_entry_creates_next_appointment(auth_client, test_db, patients, doctors, member):
    ana = doctors[0]
    resp = auth_client.post(
        ENTRIES,
        json=_entry(
            doctor_id=ana.id,
            next_appointment={"date": "2026-04-14", "time": "14:00", "location": "Piso 3"},
        ),
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    nxt = entry["next_appointment"]
    assert nxt["starts_at"] == "2026-04-14T14:00:00-05:00"
    assert nxt["all_day"] is False
    assert nxt["doctor"] == {"id": ana.id, "name": "Dra. Ana Pérez"}  # the entry's doctor
    assert nxt["location"] == "Piso 3"
    assert nxt["status"] == "programada"

    appt = _linked(test_db, entry["id"])
    assert (appt.patient_id, appt.created_by) == (1, member.id)
    assert auth_client.get(f"{ENTRIES}/{entry['id']}").json()["next_appointment"] == nxt
    assert [r.action for r in test_db.scalars(APPT_AUDIT)] == ["create"]

    # Explicit null doctor; no time → all day.
    other = auth_client.post(
        ENTRIES,
        json=_entry(doctor_id=ana.id, next_appointment={"date": "2026-04-20", "doctor_id": None}),
    ).json()["next_appointment"]
    assert (other["doctor_id"], other["all_day"]) == (None, True)
    assert other["starts_at"] == "2026-04-20T00:00:00-05:00"

    # Entries without one have none.
    plain = auth_client.post(ENTRIES, json=_entry()).json()
    assert plain["next_appointment"] is None


@pytest.mark.parametrize(
    ("next_appointment", "status"),
    [
        ({"date": "2026-03-09"}, 400),  # before the entry
        ({"date": "2026-04-01", "doctor_id": 99}, 400),
        ({"date": "pronto"}, 422),
        ({"time": "10:00"}, 422),
    ],
)
def test_entry_next_appointment_validation(
    auth_client, test_db, patients, next_appointment, status
):
    resp = auth_client.post(ENTRIES, json=_entry(next_appointment=next_appointment))
    assert resp.status_code == status
    # Same transaction: the entry is not saved either.
    assert test_db.scalars(select(LogEntry)).first() is None


def test_entry_edit_updates_cancels_and_restores(auth_client, test_db, patients, doctors):
    ana, bruno = doctors
    entry = auth_client.post(
        ENTRIES, json=_entry(doctor_id=ana.id, next_appointment={"date": "2026-04-14"})
    ).json()
    url = f"{ENTRIES}/{entry['id']}"
    appt_id = entry["next_appointment"]["id"]

    # Other edits leave it alone.
    assert auth_client.patch(url, json={"title": "Otro"}).status_code == 200
    assert _linked(test_db, entry["id"]).starts_at.date() == date(2026, 4, 14)

    changed = auth_client.patch(
        url,
        json={"next_appointment": {"date": "2026-04-15", "time": "09:15", "doctor_id": bruno.id}},
    ).json()["next_appointment"]
    assert changed["id"] == appt_id
    assert changed["starts_at"] == "2026-04-15T09:15:00-05:00"
    assert changed["doctor_id"] == bruno.id
    # Sending the same values again changes (and audits) nothing.
    same = {"date": "2026-04-15", "time": "09:15", "doctor_id": bruno.id}
    assert auth_client.patch(url, json={"next_appointment": same}).status_code == 200

    cleared = auth_client.patch(url, json={"next_appointment": None}).json()
    assert cleared["next_appointment"]["status"] == "cancelada"
    assert _linked(test_db, entry["id"]).id == appt_id  # cancelled, not deleted

    restored = auth_client.patch(url, json={"next_appointment": {"date": "2026-04-16"}}).json()
    assert restored["next_appointment"]["id"] == appt_id
    assert restored["next_appointment"]["status"] == "programada"
    assert restored["next_appointment"]["doctor_id"] == ana.id  # left out → entry's doctor

    # Moving the entry to the other patient moves its programada appointment.
    mama = patients[1]
    assert auth_client.patch(url, json={"patient_id": mama.id}).status_code == 200
    assert _linked(test_db, entry["id"]).patient_id == mama.id

    actions = [r.action for r in test_db.scalars(APPT_AUDIT)]
    assert actions == ["create", "update", "update", "update", "update"]

    # Once realizada it is never touched from the entry form.
    auth_client.patch(f"{URL}/{appt_id}", json={"status": "realizada"})
    for body in ({"next_appointment": None}, {"next_appointment": {"date": "2026-05-01"}}):
        resp = auth_client.patch(url, json=body)
        assert resp.status_code == 400
        assert "ya se registró como realizada" in resp.json()["detail"]
    assert _linked(test_db, entry["id"]).status == "realizada"
    # An edit without next_appointment still works.
    assert auth_client.patch(url, json={"title": "Otra vez"}).status_code == 200

    # Clearing when there is none is a no-op.
    plain = auth_client.post(ENTRIES, json=_entry()).json()
    resp = auth_client.patch(f"{ENTRIES}/{plain['id']}", json={"next_appointment": None})
    assert resp.status_code == 200
    assert resp.json()["next_appointment"] is None


def test_entry_edit_keeps_fields_it_does_not_send(auth_client, patients):
    entry = auth_client.post(ENTRIES, json=_entry(next_appointment={"date": "2026-04-14"})).json()
    appt_id = entry["next_appointment"]["id"]
    auth_client.patch(f"{URL}/{appt_id}", json={"reason": "Revisar placas", "duration_min": 45})
    auth_client.patch(
        f"{ENTRIES}/{entry['id']}",
        json={"next_appointment": {"date": "2026-04-15", "location": "Sala 2"}},
    )
    appt = auth_client.get(f"{URL}/{appt_id}").json()
    assert (appt["reason"], appt["duration_min"], appt["location"]) == (
        "Revisar placas",
        45,
        "Sala 2",
    )
    assert appt["starts_at"] == "2026-04-15T00:00:00-05:00"


def test_deleting_entry_keeps_appointments(auth_client, test_db, patients):
    entry = auth_client.post(ENTRIES, json=_entry(next_appointment={"date": "2026-04-14"})).json()
    appt_id = entry["next_appointment"]["id"]
    result = auth_client.post(ENTRIES, json=_entry(entry_date="2026-04-14")).json()
    auth_client.patch(
        f"{URL}/{appt_id}", json={"status": "realizada", "result_entry_id": result["id"]}
    )
    assert auth_client.delete(f"{ENTRIES}/{entry['id']}").status_code == 204
    assert auth_client.delete(f"{ENTRIES}/{result['id']}").status_code == 204
    kept = auth_client.get(f"{URL}/{appt_id}").json()
    assert (kept["created_from_entry_id"], kept["result_entry_id"]) == (None, None)
    assert kept["status"] == "realizada"


# ── Backfill ─────────────────────────────────────────────────────────────────


def _imported(test_db, patient, day, doctor=None, next_appointment=None, index=0):
    origin_ref = {"source_sha256": "0" * 64, "entry_index": index, "text_sha256": str(index)}
    if next_appointment is not ...:
        origin_ref["next_appointment"] = next_appointment
    return entries_svc.create_entry(
        test_db,
        {
            "patient_id": patient.id,
            "entry_date": day,
            "doctor_id": doctor.id if doctor else None,
            "entry_type": "consulta_control",
            "title": "Control sintético",
            "review_status": "pending_review",
        },
        user_id=None,
        origin="import_docx",
        origin_ref=origin_ref,
    )


def _na(day, time=None, doctor=None, text="Próxima cita sintética"):
    return {"date": day, "time": time, "doctor": doctor, "text": text}


@pytest.fixture
def imported(test_db, patients, doctors, member):
    papa, mama = patients
    ana, bruno = doctors
    rows = {
        # Past, visit recorded with the same doctor one day later → realizada.
        "done": _imported(test_db, papa, date(2025, 1, 10), ana, _na("2025-02-10", "14:00"), 1),
        "visit": _imported(test_db, papa, date(2025, 2, 11), ana, None, 2),
        # Past, named doctor via alias, no visit recorded → programada.
        "open": _imported(
            test_db, papa, date(2025, 3, 1), bruno, _na("2025-03-20", None, "Anita"), 3
        ),
        # Past, a visit that day but with another doctor → programada.
        "other_doc": _imported(test_db, papa, date(2025, 4, 1), bruno, _na("2025-04-15"), 4),
        "visit_ana": _imported(test_db, papa, date(2025, 4, 15), ana, None, 5),
        # Future → programada; unknown doctor name → no doctor.
        "future": _imported(
            test_db, mama, date(2026, 9, 1), ana, _na("2027-01-05", "09:30", "Dr. Nadie"), 6
        ),
        # Skipped: no date, date before the entry, null next_appointment.
        "no_date": _imported(test_db, papa, date(2025, 5, 1), ana, _na(None), 7),
        "bad_date": _imported(test_db, papa, date(2025, 5, 2), ana, _na("2025-02-30"), 8),
        "before": _imported(test_db, papa, date(2025, 6, 1), ana, _na("2025-05-01"), 9),
        "none": _imported(test_db, papa, date(2025, 6, 2), ana, None, 10),
        "no_key": _imported(test_db, papa, date(2025, 6, 3), ana, ..., 11),
    }
    test_db.commit()
    return {k: v.id for k, v in rows.items()}


def test_backfill_creates_matches_and_is_idempotent(test_db, imported, doctors, member):
    ana, bruno = doctors
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (report.candidates, report.created, report.done) == (7, 4, 1)
    assert (report.past_open, report.upcoming, report.doctor_unmatched) == (2, 1, 1)
    assert (report.no_date, report.before_entry, report.existing, report.failed) == (2, 1, 0, 0)

    by_entry = {a.created_from_entry_id: a for a in test_db.scalars(select(Appointment))}
    assert set(by_entry) == {imported[k] for k in ("done", "open", "other_doc", "future")}

    done = by_entry[imported["done"]]
    assert (done.status, done.result_entry_id, done.doctor_id) == (
        "realizada",
        imported["visit"],
        ana.id,
    )
    assert done.starts_at == datetime(2025, 2, 10, 19, 0, tzinfo=UTC)
    assert done.all_day is False
    assert done.notes == "Próxima cita sintética"
    assert done.created_by == member.id

    opened = by_entry[imported["open"]]
    assert (opened.status, opened.doctor_id, opened.all_day) == ("programada", ana.id, True)
    assert opened.starts_at == datetime(2025, 3, 20, 5, 0, tzinfo=UTC)

    other = by_entry[imported["other_doc"]]
    assert (other.status, other.doctor_id, other.result_entry_id) == (
        "programada",
        bruno.id,
        None,
    )
    future = by_entry[imported["future"]]
    assert (future.status, future.doctor_id) == ("programada", None)

    audits = test_db.scalars(APPT_AUDIT).all()
    assert [(r.action, r.user_id) for r in audits] == [("create", member.id)] * 4

    again = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (again.created, again.existing) == (0, 4)
    assert len(test_db.scalars(select(Appointment)).all()) == 4


def test_backfill_uses_each_result_entry_once(test_db, patients, doctors, member):
    papa = patients[0]
    ana = doctors[0]
    first = _imported(test_db, papa, date(2025, 1, 1), ana, _na("2025-02-01"), 1)
    second = _imported(test_db, papa, date(2025, 1, 5), ana, _na("2025-02-02"), 2)
    visit = _imported(test_db, papa, date(2025, 2, 1), ana, None, 3)
    test_db.commit()
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (report.done, report.past_open) == (1, 1)
    by_entry = {a.created_from_entry_id: a for a in test_db.scalars(select(Appointment))}
    assert by_entry[first.id].result_entry_id == visit.id
    assert by_entry[second.id].result_entry_id is None


def test_backfill_skips_repeated_next_appointment(test_db, patients, doctors, member):
    """Two entries naming the same visit: the earlier one gets it, the later is
    counted; another doctor the same day is a different appointment."""
    papa = patients[0]
    ana, bruno = doctors
    first = _imported(test_db, papa, date(2026, 9, 1), ana, _na("2027-03-05", "10:00"), 1)
    # Lab entry by another doctor repeating "con la Dra. Pérez" → same appointment.
    repeat = _imported(
        test_db, papa, date(2026, 9, 20), bruno, _na("2027-03-05", None, "Dra. Ana Pérez"), 2
    )
    other_doctor = _imported(test_db, papa, date(2026, 9, 21), bruno, _na("2027-03-05"), 3)
    test_db.commit()
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (report.created, report.duplicate_same_day) == (2, 1)
    linked = {a.created_from_entry_id for a in test_db.scalars(select(Appointment))}
    assert linked == {first.id, other_doctor.id}
    assert repeat.id not in linked
    # Re-running counts it again and creates nothing.
    again = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    assert (again.created, again.existing, again.duplicate_same_day) == (0, 2, 1)


def test_backfill_dedupes_against_existing_and_no_doctor(test_db, patients, doctors, member):
    """An appointment already in the calendar (made in the app, no doctor)
    absorbs an imported one that day; one without doctor absorbs any doctor."""
    from app.services.appointments import create_appointment, local_datetime

    papa, mama = patients
    ana = doctors[0]
    create_appointment(
        test_db,
        {"patient_id": papa.id, "starts_at": local_datetime(date(2027, 4, 1)), "all_day": True},
        user_id=member.id,
    )
    with_doctor = _imported(test_db, papa, date(2026, 9, 1), ana, _na("2027-04-01", "08:00"), 1)
    # Imported without a doctor, then one with a doctor the same day.
    no_doctor = _imported(test_db, mama, date(2026, 9, 1), None, _na("2027-04-02"), 2)
    later = _imported(test_db, mama, date(2026, 9, 2), ana, _na("2027-04-02", "09:00"), 3)
    test_db.commit()
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (report.created, report.duplicate_same_day) == (1, 2)
    linked = {a.created_from_entry_id for a in test_db.scalars(select(Appointment))}
    assert linked == {None, no_doctor.id}
    assert with_doctor.id not in linked and later.id not in linked


def test_backfill_unknown_doctor_name_is_not_a_wildcard(test_db, patients, doctors, member):
    papa = patients[0]
    ana = doctors[0]
    rows = [
        # Known doctor, then an unknown one the same day: two visits.
        _imported(test_db, papa, date(2026, 9, 1), ana, _na("2027-03-05"), 1),
        _imported(test_db, papa, date(2026, 9, 2), None, _na("2027-03-05", None, "Dr. Zeta"), 2),
        # The same unknown name again: the same visit.
        _imported(test_db, papa, date(2026, 9, 3), None, _na("2027-03-05", None, "Dr Zeta"), 3),
        # Unknown first, known after: still two visits.
        _imported(test_db, papa, date(2026, 9, 4), None, _na("2027-03-06", None, "Dr. Equis"), 4),
        _imported(test_db, papa, date(2026, 9, 5), ana, _na("2027-03-06"), 5),
    ]
    test_db.commit()
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (report.created, report.duplicate_same_day, report.doctor_unmatched) == (4, 1, 3)
    linked = {a.created_from_entry_id for a in test_db.scalars(select(Appointment))}
    assert linked == {rows[0].id, rows[1].id, rows[3].id, rows[4].id}


def test_backfill_different_times_are_different_visits(test_db, patients, doctors, member):
    papa = patients[0]
    ana = doctors[0]
    morning = _imported(test_db, papa, date(2026, 9, 1), ana, _na("2027-05-01", "09:00"), 1)
    afternoon = _imported(test_db, papa, date(2026, 9, 2), ana, _na("2027-05-01", "15:00"), 2)
    untimed = _imported(test_db, papa, date(2026, 9, 3), ana, _na("2027-05-01"), 3)
    test_db.commit()
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (report.created, report.duplicate_same_day) == (2, 1)
    linked = {a.created_from_entry_id for a in test_db.scalars(select(Appointment))}
    assert linked == {morning.id, afternoon.id}
    assert untimed.id not in linked


def test_backfill_deleted_is_not_recreated_through_a_repeat(
    auth_client, test_db, patients, doctors, member
):
    papa = patients[0]
    ana = doctors[0]
    first = _imported(test_db, papa, date(2026, 9, 1), ana, _na("2027-03-05", "10:00"), 1)
    _imported(test_db, papa, date(2026, 9, 20), ana, _na("2027-03-05"), 2)
    test_db.commit()
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (report.created, report.duplicate_same_day) == (1, 1)
    appt = test_db.scalar(select(Appointment).where(Appointment.created_from_entry_id == first.id))
    assert auth_client.delete(f"{URL}/{appt.id}").status_code == 204
    again = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (again.created, again.deleted_before, again.duplicate_same_day) == (0, 1, 1)
    assert test_db.scalars(select(Appointment)).first() is None


def test_backfill_does_not_recreate_deleted(auth_client, test_db, imported, member):
    report = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert report.created == 4
    appt = test_db.scalar(
        select(Appointment).where(Appointment.created_from_entry_id == imported["future"])
    )
    assert auth_client.delete(f"{URL}/{appt.id}").status_code == 204
    again = backfill(test_db, user_id=member.id, today=date(2026, 10, 8))
    test_db.commit()
    assert (again.created, again.existing, again.deleted_before) == (0, 3, 1)


def test_backfill_script_dry_run_and_run(
    test_engine, test_db, imported, member, monkeypatch, capsys
):
    from scripts import appointments as script

    monkeypatch.setattr(script, "SessionLocal", sessionmaker(bind=test_engine))
    assert script.main(["backfill", "--by", "ANA@example.com", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run, nothing written" in out
    assert "would create 4" in out
    assert "sintética" not in out  # counts only
    test_db.expire_all()
    assert test_db.scalars(select(Appointment)).first() is None

    assert script.main(["backfill", "--by", "ana@example.com"]) == 0
    assert "created 4" in capsys.readouterr().out
    assert len(test_db.scalars(select(Appointment)).all()) == 4

    assert script.main(["backfill", "--by", "nadie@example.com"]) == 1


def test_backfill_ignores_today_plus_slack(test_db, patients, doctors, member):
    """An appointment in the future is never matched, even with an entry nearby."""
    papa = patients[0]
    ana = doctors[0]
    today = date.today()
    _imported(
        test_db, papa, today - timedelta(days=30), ana, _na((today + timedelta(1)).isoformat()), 1
    )
    _imported(test_db, papa, today, ana, None, 2)
    test_db.commit()
    report = backfill(test_db, user_id=member.id, today=today)
    assert (report.upcoming, report.done) == (1, 0)
