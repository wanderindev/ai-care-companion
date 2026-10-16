"""Medication checks (ticket #29): the EVITAR name match and the out-of-date
banner. Synthetic data only."""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.medication import MedCheckDismissal, MedEvent, MedListVersion
from app.services import log_entries as entries_svc
from app.services import meds as meds_svc
from app.services.meds_checks import avoid_terms, avoid_warnings, matches, normalize


def url(patient, suffix=""):
    return f"/api/v1/patients/{patient.id}/meds{suffix}"


# ── Normalizer and matching ──────────────────────────────────────────────────


def test_normalize():
    assert normalize("  Ácido   ACETILSALICÍLICO, 100mg ") == "acido acetilsalicilico 100mg"
    assert normalize("Acetaminofén/Paracetamol") == "acetaminofen paracetamol"
    assert normalize(None) == normalize("") == ""


# The table is shared with the frontend matcher (``npm run check:meds``).
CASES = json.loads((Path(__file__).parent / "avoid_match_cases.json").read_text("utf-8"))


@pytest.mark.parametrize(("substance", "text"), CASES["match"])
def test_matches(substance, text):
    assert matches(text, avoid_terms(substance))


@pytest.mark.parametrize(("substance", "text"), CASES["no_match"])
def test_does_not_match(substance, text):
    assert not matches(text, avoid_terms(substance))


def test_trademark_signs_and_leading_words():
    assert normalize("Aspirina™ 81 mg") == "aspirina 81 mg"
    assert normalize("Tylenol®") == "tylenol"
    assert {"aspirina 100 mg", "aspirina", "acido acetilsalicilico"} <= avoid_terms(
        "Aspirina 100 mg"
    )


def test_avoid_warnings_shape_and_strength():
    items = [
        {"id": 7, "name": "Medicina A", "strength": "Ibuprofeno 400 mg"},
        {"name": "Advil"},
        {"name": "Medicina B"},
    ]
    avoid = [
        {"substance": "Ibuprofeno", "reason": "reacción sintética"},
        {"substance": "Sustancia X", "reason": None},
    ]
    assert avoid_warnings(items, avoid) == [
        {
            "item_index": 0,
            "item_id": 7,
            "name": "Medicina A",
            "substance": "Ibuprofeno",
            "reason": "reacción sintética",
        },
        {
            "item_index": 1,
            "item_id": None,
            "name": "Advil",
            "substance": "Ibuprofeno",
            "reason": "reacción sintética",
        },
    ]


# ── dry_run warnings ─────────────────────────────────────────────────────────


def test_dry_run_returns_warnings_and_save_is_not_blocked(auth_client, patients):
    papa = patients[0]
    body = {
        "expected_version": 0,
        "items": [
            {"name": "Medicina A", "slot": "desayuno"},
            {"name": "Ibuprofeno", "strength": "400 mg", "slot": "segun_necesidad"},
        ],
        "avoid": [{"substance": "Advil", "reason": "reacción sintética"}],
    }
    resp = auth_client.put(url(papa), json={**body, "dry_run": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["saved"] is False
    assert data["warnings"] == [
        {
            "item_index": 1,
            "item_id": None,
            "name": "Ibuprofeno",
            "substance": "Advil",
            "reason": "reacción sintética",
        }
    ]
    saved = auth_client.put(url(papa), json=body)
    assert saved.status_code == 200
    assert saved.json()["saved"] is True and len(saved.json()["warnings"]) == 1


def test_no_warnings_without_match(auth_client, patients):
    resp = auth_client.put(
        url(patients[0]),
        json={
            "expected_version": 0,
            "dry_run": True,
            "items": [{"name": "Medicina A", "slot": "desayuno"}],
            "avoid": [{"substance": "AINE"}],
        },
    )
    assert resp.json()["warnings"] == []


# ── Out-of-date banner ───────────────────────────────────────────────────────


def _entry(test_db, patient, day, events=None, origin="import_docx", index=0):
    origin_ref = None
    if origin == "import_docx":
        origin_ref = {"source_sha256": "0" * 64, "entry_index": index, "text_sha256": str(index)}
        if events is not None:
            origin_ref["medication_events"] = events
    return entries_svc.create_entry(
        test_db,
        {
            "patient_id": patient.id,
            "entry_date": day,
            "entry_type": "consulta_control",
            "title": f"Control sintético {index}",
            "review_status": "pending_review",
        },
        user_id=None,
        origin=origin,
        origin_ref=origin_ref,
    )


def _version(test_db, patient, effective_date):
    meds_svc.save_list(
        test_db,
        patient.id,
        items=[{"name": "Medicina A", "slot": "desayuno"}],
        avoid=[],
        expected_version=0,
        user_id=None,
        effective_date=effective_date,
    )
    test_db.commit()


@pytest.fixture
def scenario(test_db, patients):
    papa, mama = patients
    _version(test_db, papa, date(2025, 6, 1))
    rows = {
        "older": _entry(test_db, papa, date(2025, 5, 1), [{"kind": "start", "medication": "M1"}]),
        "same_day": _entry(
            test_db, papa, date(2025, 6, 1), [{"kind": "start", "medication": "M2"}], index=1
        ),
        "newer": _entry(
            test_db,
            papa,
            date(2025, 7, 1),
            [
                {"kind": "stop", "medication": "Medicina A", "detail": "  "},
                {"kind": "pause", "medication": "M3"},  # unknown kind: skipped
                {"kind": "change", "medication": "Medicina B", "detail": "de 1 a ½"},
            ],
            index=2,
        ),
        "newer_no_events": _entry(test_db, papa, date(2025, 7, 2), [], index=3),
        "manual": _entry(test_db, papa, date(2025, 7, 3), origin="manual"),
        "other_patient": _entry(
            test_db, mama, date(2025, 8, 1), [{"kind": "start", "medication": "M4"}], index=4
        ),
    }
    test_db.commit()
    return {k: v.id for k, v in rows.items()}


def test_checks_requires_auth(db_client, patients):
    assert db_client.get(url(patients[0], "/checks")).status_code == 401
    resp = db_client.post(
        url(patients[0], "/checks/1/dismiss"), headers={"X-Requested-With": "fetch"}
    )
    assert resp.status_code == 401


def test_checks_unknown_patient(auth_client):
    assert auth_client.get("/api/v1/patients/999999/meds/checks").status_code == 404


def test_checks_without_version(auth_client, test_db, patients):
    _entry(test_db, patients[0], date(2025, 7, 1), [{"kind": "start", "medication": "M1"}])
    test_db.commit()
    assert auth_client.get(url(patients[0], "/checks")).json() == {
        "reference_date": None,
        "total": 0,
        "entries": [],
    }


def test_checks_lists_newer_entries_with_events(auth_client, patients, scenario):
    data = auth_client.get(url(patients[0], "/checks")).json()
    assert data["reference_date"] == "2025-06-01"
    assert data["total"] == 1
    assert data["entries"] == [
        {
            "id": scenario["newer"],
            "entry_date": "2025-07-01",
            "title": "Control sintético 2",
            "events": [
                {"kind": "suspension", "medication": "Medicina A", "detail": None},
                {"kind": "cambio_dosis", "medication": "Medicina B", "detail": "de 1 a ½"},
            ],
        }
    ]
    # The other patient has no version: nothing to check.
    assert auth_client.get(url(patients[1], "/checks")).json()["total"] == 0


def test_checks_same_day_as_effective_date_is_reflected(auth_client, patients, scenario):
    data = auth_client.get(url(patients[0], "/checks")).json()
    assert scenario["same_day"] not in [e["id"] for e in data["entries"]]


def test_checks_reference_falls_back_to_changed_at(auth_client, test_db, patients, scenario):
    # Without effective_date the save's own day still counts (>=): the save
    # says nothing about that day's visit. 17:00 UTC = 12:00 in Panamá.
    version = test_db.scalar(select(MedListVersion))
    version.effective_date = None
    version.changed_at = version.changed_at.replace(year=2025, month=7, day=1, hour=17)
    test_db.commit()
    data = auth_client.get(url(patients[0], "/checks")).json()
    assert data["reference_date"] == "2025-07-01"
    assert [e["id"] for e in data["entries"]] == [scenario["newer"]]
    version.changed_at = version.changed_at.replace(day=2)
    test_db.commit()
    data = auth_client.get(url(patients[0], "/checks")).json()
    assert (data["reference_date"], data["total"]) == ("2025-07-02", 0)


def test_checks_use_med_events_rows_and_skip_discarded(
    auth_client, test_db, patients, scenario, member
):
    from app.services.med_events_backfill import backfill

    backfill(test_db, user_id=member.id)
    test_db.commit()
    rows = test_db.scalars(
        select(MedEvent)
        .where(MedEvent.source_entry_id == scenario["newer"])
        .order_by(MedEvent.source_index)
    ).all()
    assert len(rows) == 2
    # An edited row wins over the extracted text; a discarded one is gone.
    resp = auth_client.patch(
        url(patients[0], f"/events/{rows[0].id}"), json={"medication": "Medicina A2"}
    )
    assert resp.status_code == 200
    assert auth_client.delete(url(patients[0], f"/events/{rows[1].id}")).status_code == 204
    data = auth_client.get(url(patients[0], "/checks")).json()
    assert data["entries"][0]["events"] == [
        {"kind": "suspension", "medication": "Medicina A2", "detail": None}
    ]
    # Discarding the last one leaves the entry without events: not reported.
    assert auth_client.delete(url(patients[0], f"/events/{rows[0].id}")).status_code == 204
    assert auth_client.get(url(patients[0], "/checks")).json()["total"] == 0


def test_dismiss(auth_client, test_db, patients, scenario, member):
    papa, mama = patients
    target = url(papa, f"/checks/{scenario['newer']}/dismiss")
    resp = auth_client.post(target)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0
    assert auth_client.get(url(papa, "/checks")).json()["entries"] == []
    # Idempotent: one row, one audit entry.
    assert auth_client.post(target).status_code == 200
    rows = test_db.scalars(select(MedCheckDismissal)).all()
    assert [(r.entry_id, r.patient_id, r.created_by) for r in rows] == [
        (scenario["newer"], papa.id, member.id)
    ]
    audits = test_db.scalars(select(AuditLog).where(AuditLog.entity == "med_check_dismissal")).all()
    assert len(audits) == 1 and audits[0].user_id == member.id


def test_dismiss_other_patient_or_missing_entry(auth_client, patients, scenario):
    papa = patients[0]
    assert (
        auth_client.post(url(papa, f"/checks/{scenario['other_patient']}/dismiss")).status_code
        == 404
    )
    assert auth_client.post(url(papa, "/checks/999999/dismiss")).status_code == 404


def test_dismiss_requires_csrf_header(auth_client, patients, scenario):
    del auth_client.headers["X-Requested-With"]
    resp = auth_client.post(url(patients[0], f"/checks/{scenario['newer']}/dismiss"))
    assert resp.status_code == 403
