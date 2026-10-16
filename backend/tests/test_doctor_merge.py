"""Merging duplicate doctors (POST /doctors/{id}/merge). Synthetic names only."""

import threading
from datetime import UTC, date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.services import log_entries as entries_svc
from app.services.doctors import MergeError, doctor_references, match_doctor, merge_doctors

URL = "/api/v1/doctors/{}/merge"
DOCTOR_AUDIT = select(AuditLog).where(AuditLog.entity == "doctor")


@pytest.fixture
def admin_client(auth_client, member, test_db):
    member.role = "admin"
    test_db.commit()
    return auth_client


@pytest.fixture
def pair(test_db, patients):
    """Target "Dra. Lucía Gómez" and its duplicate "Dra. Gómez", with entries."""
    target = Doctor(
        name="Dra. Lucía Gómez",
        aliases=["Lucy"],
        specialty="Neurología",
        phone=None,
        notes="Atiende martes.",
    )
    source = Doctor(
        name="Dra. Gómez",
        aliases=["Lucy", "Doctora Gomez L", "Dra. Lucía Gómez"],
        specialty="Medicina general",
        phone="6000-1111",
        clinic="Clínica Sintética",
        notes="Usar WhatsApp.",
        is_active=False,
    )
    other = Doctor(name="Dr. Tomás Ruiz", aliases=[])
    test_db.add_all([target, source, other])
    test_db.commit()

    def entry(doctor, patient, day):
        row = entries_svc.create_entry(
            test_db,
            {
                "patient_id": patient.id,
                "doctor_id": doctor.id,
                "entry_date": date(2026, 1, day),
                "entry_type": "consulta_control",
                "title": "Control sintético",
            },
            user_id=None,
        )
        test_db.flush()
        return row.id

    papa, mama = patients
    ids = {
        "source": [entry(source, papa, 1), entry(source, mama, 2)],
        "target": [entry(target, papa, 3)],
        "other": [entry(other, papa, 4)],
    }
    test_db.commit()
    return target, source, other, ids


def _doctor_ids(test_db):
    test_db.expire_all()
    return {entry.id: entry.doctor_id for entry in test_db.scalars(select(LogEntry))}


def test_doctor_references_discovers_fks():
    assert [(c.table.name, c.name) for c in doctor_references()] == [
        ("appointments", "doctor_id"),
        ("log_entries", "doctor_id"),
        ("med_items", "prescriber_id"),
    ]


def test_merge_repoints_med_prescriber(admin_client, test_db, pair, patients):
    from app.models.medication import MedItem

    target, source, _other, _ids = pair
    target_id, source_id = target.id, source.id
    med = MedItem(patient_id=patients[0].id, name="Medicina A", slot="desayuno")
    med.prescriber_id = source_id
    test_db.add(med)
    test_db.commit()
    med_id = med.id
    resp = admin_client.post(URL.format(target_id), json={"source_id": source_id})
    assert resp.status_code == 200, resp.text
    test_db.expire_all()
    assert test_db.get(MedItem, med_id).prescriber_id == target_id


def test_merge_repoints_appointments(admin_client, test_db, pair, patients):
    from datetime import datetime, timezone

    from app.models.appointment import Appointment
    from app.services.appointments import create_appointment

    target, source, other, _ids = pair
    target_id, source_id = target.id, source.id
    appt = create_appointment(
        test_db,
        {
            "patient_id": patients[0].id,
            "doctor_id": source_id,
            "starts_at": datetime(2026, 2, 1, 15, 0, tzinfo=UTC),
        },
        user_id=None,
    )
    test_db.commit()
    appt_id = appt.id
    resp = admin_client.post(URL.format(target_id), json={"source_id": source_id})
    assert resp.status_code == 200, resp.text
    test_db.expire_all()
    assert test_db.get(Appointment, appt_id).doctor_id == target_id
    merge_row = test_db.scalars(DOCTOR_AUDIT.where(AuditLog.action == "merge")).one()
    assert merge_row.after["moved"]["appointments.doctor_id"] == [appt_id]


def test_merge_moves_entries_merges_aliases_and_deletes_source(admin_client, test_db, pair, member):
    target, source, other, ids = pair
    target_id, source_id = target.id, source.id
    resp = admin_client.post(URL.format(target_id), json={"source_id": source_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Source name + aliases kept as aliases, deduplicated, never the target's name.
    assert body["aliases"] == ["Lucy", "Dra. Gómez", "Doctora Gomez L"]
    # Empty contact fields filled, non-empty ones kept; notes appended.
    assert body["specialty"] == "Neurología"
    assert body["phone"] == "6000-1111"
    assert body["clinic"] == "Clínica Sintética"
    assert body["notes"] == "Atiende martes.\n\nUsar WhatsApp."
    assert body["is_active"] is True

    owners = _doctor_ids(test_db)
    assert {owners[i] for i in ids["source"] + ids["target"]} == {target_id}
    assert {owners[i] for i in ids["other"]} == {other.id}
    assert test_db.get(Doctor, source_id) is None
    assert test_db.get(LogEntry, ids["source"][0]).updated_by == member.id
    assert test_db.get(Doctor, target_id).updated_by == member.id
    # The old name now finds the target.
    assert match_doctor(test_db, "dra gomez").id == target_id

    merge_row, delete_row = test_db.scalars(DOCTOR_AUDIT.order_by(AuditLog.id)).all()
    assert (merge_row.action, merge_row.entity, merge_row.entity_id, merge_row.user_id) == (
        "merge",
        "doctor",
        str(target_id),
        member.id,
    )
    assert merge_row.before["target"]["phone"] is None
    assert merge_row.before["source"]["name"] == "Dra. Gómez"
    assert merge_row.before["source"]["created_at"]  # self-contained snapshot
    assert merge_row.after["target"]["phone"] == "6000-1111"
    assert merge_row.after["source_id"] == source_id
    assert merge_row.after["moved"] == {"log_entries.doctor_id": sorted(ids["source"])}
    assert (delete_row.action, delete_row.entity_id) == ("delete", str(source_id))
    assert delete_row.before["aliases"] == ["Lucy", "Doctora Gomez L", "Dra. Lucía Gómez"]
    assert delete_row.after == {"merged_into": target_id}

    # Entries list by doctor reflects the move.
    listed = admin_client.get("/api/v1/log-entries", params={"doctor_id": target_id}).json()
    assert listed["total"] == 3


def test_merge_without_entries(admin_client, test_db, pair):
    target, _, other, ids = pair
    resp = admin_client.post(URL.format(other.id), json={"source_id": target.id})
    assert resp.status_code == 200, resp.text
    merge_row = test_db.scalars(select(AuditLog).where(AuditLog.action == "merge")).one()
    assert merge_row.after["moved"] == {"log_entries.doctor_id": ids["target"]}


def test_merge_requires_admin(auth_client, test_db, pair):
    target, source, _, ids = pair
    resp = auth_client.post(URL.format(target.id), json={"source_id": source.id})
    assert resp.status_code == 403
    assert test_db.get(Doctor, source.id) is not None
    assert test_db.scalar(DOCTOR_AUDIT) is None


def test_merge_requires_sign_in_and_csrf(db_client, pair):
    target, source, _, _ = pair
    resp = db_client.post(
        URL.format(target.id),
        json={"source_id": source.id},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 401
    assert db_client.post(URL.format(target.id), json={"source_id": source.id}).status_code == 403


def test_merge_rejections(admin_client, test_db, pair):
    target, source, other, _ = pair
    resp = admin_client.post(URL.format(target.id), json={"source_id": target.id})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "No se puede combinar un médico consigo mismo."
    assert admin_client.post(URL.format(999), json={"source_id": source.id}).status_code == 404
    assert admin_client.post(URL.format(target.id), json={"source_id": 999}).status_code == 404
    # Inactive target: reactivate it first.
    resp = admin_client.post(URL.format(source.id), json={"source_id": other.id})
    assert resp.status_code == 409
    assert "inactivo" in resp.json()["detail"]
    for bad in ({}, {"source_id": 0}, {"source_id": "x"}):
        assert admin_client.post(URL.format(target.id), json=bad).status_code == 422
    test_db.expire_all()
    assert len(test_db.scalars(select(Doctor)).all()) == 3
    assert test_db.scalar(DOCTOR_AUDIT) is None


def test_merge_twice_finds_source_gone(admin_client, pair):
    target, source, _, _ = pair
    assert (
        admin_client.post(URL.format(target.id), json={"source_id": source.id}).status_code == 200
    )
    assert (
        admin_client.post(URL.format(target.id), json={"source_id": source.id}).status_code == 404
    )


def test_merge_locks_both_rows(test_engine, test_db, pair):
    """A merge waits for (here: times out on) a row another transaction holds."""
    target, source, _, _ = pair
    Session = sessionmaker(bind=test_engine)
    holder = Session()
    try:
        holder.execute(text("SELECT id FROM doctors WHERE id = :id FOR UPDATE"), {"id": source.id})
        merger = Session()
        try:
            merger.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                merge_doctors(merger, target_id=target.id, source_id=source.id, user_id=None)
        finally:
            merger.rollback()
            merger.close()
    finally:
        holder.rollback()
        holder.close()


def test_concurrent_merges_of_the_same_duplicate(test_engine, pair, patients):
    """Two merges of one duplicate into different targets: one wins, the other 404s."""
    target, source, other, ids = pair
    Session = sessionmaker(bind=test_engine)
    barrier = threading.Barrier(2)
    results = {}

    def run(name, target_id):
        session = Session()
        try:
            barrier.wait()
            merge_doctors(session, target_id=target_id, source_id=source.id, user_id=None)
            session.commit()
            results[name] = "ok"
        except MergeError as exc:
            session.rollback()
            results[name] = exc.status
        finally:
            session.close()

    threads = [
        threading.Thread(target=run, args=("a", target.id)),
        threading.Thread(target=run, args=("b", other.id)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results.values(), key=str) == [404, "ok"]

    check = Session()
    try:
        owners = {e.id: e.doctor_id for e in check.scalars(select(LogEntry))}
        winner = target.id if results["a"] == "ok" else other.id
        assert {owners[i] for i in ids["source"]} == {winner}
        assert check.get(Doctor, source.id) is None
    finally:
        check.close()
