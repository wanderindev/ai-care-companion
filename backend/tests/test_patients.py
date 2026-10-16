"""Patients API, audit helper and the patients CLI. Synthetic data only."""

import io

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditLog
from app.models.patient import Patient
from scripts import patients as patients_script


def test_patients_require_sign_in(db_client, patients):
    assert db_client.get("/api/v1/patients").status_code == 401
    resp = db_client.patch(
        f"/api/v1/patients/{patients[0].id}",
        json={"notes": "x"},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 401


def test_list_patients(auth_client, patients):
    resp = auth_client.get("/api/v1/patients")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["slug"] for p in body] == ["papa", "mama"]
    assert body[0]["display_name"] == "Papá"
    assert body[0]["full_name"] is None


def test_patch_patient_updates_and_audits(auth_client, test_db, patients, member):
    papa = patients[0]
    resp = auth_client.patch(
        f"/api/v1/patients/{papa.id}",
        json={
            "full_name": "  Paciente Uno Prueba ",
            "id_number": "0-000-0000",
            "birth_date": "1940-01-02",
            "diagnoses": "Diagnóstico sintético",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["full_name"] == "Paciente Uno Prueba"
    assert body["birth_date"] == "1940-01-02"
    assert body["display_name"] == "Papá"  # untouched

    test_db.expire_all()
    row = test_db.get(Patient, papa.id)
    assert row.updated_by == member.id
    log = test_db.scalars(select(AuditLog)).all()
    assert len(log) == 1
    entry = log[0]
    assert (entry.user_id, entry.action, entry.entity, entry.entity_id) == (
        member.id,
        "update",
        "patient",
        str(papa.id),
    )
    assert entry.before["full_name"] is None
    assert entry.after["full_name"] == "Paciente Uno Prueba"
    assert entry.after["birth_date"] == "1940-01-02"
    assert "created_at" not in entry.after


def test_patch_blank_clears_and_noop_is_not_audited(auth_client, test_db, patients):
    papa = patients[0]
    auth_client.patch(f"/api/v1/patients/{papa.id}", json={"notes": "Nota"})
    resp = auth_client.patch(f"/api/v1/patients/{papa.id}", json={"notes": "   "})
    assert resp.json()["notes"] is None
    auth_client.patch(f"/api/v1/patients/{papa.id}", json={})
    assert len(test_db.scalars(select(AuditLog)).all()) == 2


@pytest.mark.parametrize(
    "payload",
    [{"display_name": ""}, {"display_name": None}, {"birth_date": "no-date"}, {"slug": "x"}],
)
def test_patch_rejects_invalid(auth_client, patients, payload):
    resp = auth_client.patch(f"/api/v1/patients/{patients[0].id}", json=payload)
    # Unknown fields (slug) are ignored, not applied.
    if payload == {"slug": "x"}:
        assert resp.status_code == 200 and resp.json()["slug"] == "papa"
    else:
        assert resp.status_code == 422


def test_patch_unknown_patient(auth_client, patients):
    assert auth_client.patch("/api/v1/patients/999", json={"notes": "x"}).status_code == 404


def test_patch_needs_csrf_header(auth_client, patients):
    resp = auth_client.patch(
        f"/api/v1/patients/{patients[0].id}", json={"notes": "x"}, headers={"X-Requested-With": ""}
    )
    assert resp.status_code == 403


# ── CLI ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_db(test_engine, test_db, monkeypatch):
    monkeypatch.setattr(patients_script, "SessionLocal", sessionmaker(bind=test_engine))
    return test_db


def test_cli_set_and_list(cli_db, patients, member, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("Paciente Dos Prueba\n"))
    rc = patients_script.main(
        ["set", "mama", "--full-name", "-", "--birth-date", "1945-03-04", "--by", member.email]
    )
    assert rc == 0
    cli_db.expire_all()
    mama = cli_db.scalar(select(Patient).where(Patient.slug == "mama"))
    assert mama.full_name == "Paciente Dos Prueba"
    assert str(mama.birth_date) == "1945-03-04"
    assert cli_db.scalar(select(AuditLog)).user_id == member.id

    capsys.readouterr()
    assert patients_script.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "filled: full_name, birth_date" in out
    assert "Paciente Dos Prueba" not in out  # values are never printed


def test_cli_errors(cli_db, patients):
    assert patients_script.main(["set", "tio", "--notes", "x"]) == 1
    assert patients_script.main(["set", "papa"]) == 1
    assert patients_script.main(["set", "papa", "--birth-date", "02/01/1940"]) == 1
    assert patients_script.main(["set", "papa", "--notes", "x", "--by", "no@example.com"]) == 1
