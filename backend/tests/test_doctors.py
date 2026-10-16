"""Doctors API and the alias-matching helper. Synthetic names only."""

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.services.doctors import match_doctor, match_doctors, normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dra. Ana Pérez", "ana perez"),
        ("ana perez", "ana perez"),
        ("  DRA   ANA  PÉREZ ", "ana perez"),
        ("Dr. José Núñez", "jose nunez"),
        ("Lic. María-José Ortíz", "maria jose ortiz"),
        ("Licda. Inés Ruiz", "ines ruiz"),
        ("Doctora Ana", "ana"),
        ("Dra.", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.fixture
def directory(test_db):
    rows = [
        Doctor(name="Dra. Ana Pérez", aliases=["Anita", "Dra. Pérez"], specialty="Geriatría"),
        Doctor(name="Dr. José Núñez", aliases=[]),
        Doctor(name="Lic. Marta Ríos", aliases=["Terapia respiratoria"], is_active=False),
    ]
    test_db.add_all(rows)
    test_db.commit()
    return rows


def test_match_doctors(test_db, directory):
    ana, jose, marta = directory
    assert match_doctors(test_db, "Ana Perez") == [ana]
    assert match_doctors(test_db, "dra ana pérez") == [ana]
    assert match_doctors(test_db, "ANITA") == [ana]
    assert match_doctors(test_db, "Pérez") == [ana]  # alias "Dra. Pérez" sans title
    assert match_doctors(test_db, "Jose Nunez") == [jose]
    assert match_doctors(test_db, "terapia respiratoria") == [marta]
    assert match_doctors(test_db, "terapia respiratoria", include_inactive=False) == []
    assert match_doctors(test_db, "Ana") == []  # no partial matches
    assert match_doctors(test_db, "Dr.") == []
    assert match_doctor(test_db, "anita") is ana


def test_match_doctor_ambiguous(test_db, directory):
    test_db.add(Doctor(name="Dr. Pablo Pérez", aliases=["Dr. Pérez"]))
    test_db.commit()
    assert len(match_doctors(test_db, "Perez")) == 2
    assert match_doctor(test_db, "Perez") is None


def test_doctors_require_sign_in(db_client):
    assert db_client.get("/api/v1/doctors").status_code == 401
    resp = db_client.post(
        "/api/v1/doctors", json={"name": "Dr. X"}, headers={"X-Requested-With": "fetch"}
    )
    assert resp.status_code == 401


def test_list_and_match_endpoint(auth_client, directory):
    names = [d["name"] for d in auth_client.get("/api/v1/doctors").json()]
    assert names == ["Dr. José Núñez", "Dra. Ana Pérez", "Lic. Marta Ríos"]
    active = auth_client.get("/api/v1/doctors", params={"include_inactive": "false"}).json()
    assert len(active) == 2
    match = auth_client.get("/api/v1/doctors", params={"match": "anita"}).json()
    assert [d["name"] for d in match] == ["Dra. Ana Pérez"]


def test_create_doctor_audits(auth_client, test_db, member):
    resp = auth_client.post(
        "/api/v1/doctors",
        json={
            "name": " Dr. Luis Núñez ",
            "aliases": ["Luis", "  ", "Dr Luis Nunez", "Luchito", "luis"],
            "specialty": "Cardiología",
            "phone": "",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Dr. Luis Núñez"
    # Blank and name-equivalent/duplicate aliases are dropped.
    assert body["aliases"] == ["Luis", "Luchito"]
    assert body["phone"] is None
    assert body["is_active"] is True

    row = test_db.get(Doctor, body["id"])
    assert (row.created_by, row.updated_by) == (member.id, member.id)
    entry = test_db.scalar(select(AuditLog))
    assert (entry.action, entry.entity, entry.entity_id, entry.user_id) == (
        "create",
        "doctor",
        str(body["id"]),
        member.id,
    )
    assert entry.before is None and entry.after["aliases"] == ["Luis", "Luchito"]


def test_create_duplicate_conflicts(auth_client, directory):
    for payload in (
        {"name": "Ana Perez"},
        {"name": "Dra. Ana Gil", "aliases": ["anita"]},
        {"name": "Anita"},
    ):
        resp = auth_client.post("/api/v1/doctors", json=payload)
        assert resp.status_code == 409, payload
        assert resp.json()["detail"] == (
            "Ya existe un médico con ese nombre o alias: Dra. Ana Pérez."
        )


def test_create_validation(auth_client):
    assert auth_client.post("/api/v1/doctors", json={"name": ""}).status_code == 422
    assert auth_client.post("/api/v1/doctors", json={}).status_code == 422


def test_patch_doctor(auth_client, test_db, directory, member):
    ana = directory[0]
    resp = auth_client.patch(
        f"/api/v1/doctors/{ana.id}",
        json={"aliases": ["Anita", "Dra. Anita"], "phone": "6000-0000", "is_active": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["aliases"] == ["Anita"]  # "Dra. Anita" normalizes to "Anita"
    assert body["phone"] == "6000-0000"
    assert body["is_active"] is False
    assert body["specialty"] == "Geriatría"  # untouched

    entry = test_db.scalar(select(AuditLog))
    assert entry.action == "update" and entry.user_id == member.id
    assert entry.before["phone"] is None and entry.after["phone"] == "6000-0000"


def test_patch_doctor_conflict_and_errors(auth_client, test_db, directory):
    ana, jose, _ = directory
    resp = auth_client.patch(f"/api/v1/doctors/{jose.id}", json={"aliases": ["Anita"]})
    assert resp.status_code == 409
    assert resp.json()["detail"].endswith(": Dra. Ana Pérez.")
    test_db.expire_all()
    assert test_db.get(Doctor, jose.id).aliases == []
    # Renaming to its own alias is not a conflict with itself.
    resp = auth_client.patch(f"/api/v1/doctors/{ana.id}", json={"name": "Dra. Pérez"})
    assert resp.status_code == 200
    assert auth_client.patch("/api/v1/doctors/999", json={"phone": "1"}).status_code == 404
    assert auth_client.patch(f"/api/v1/doctors/{ana.id}", json={"name": None}).status_code == 422
    assert auth_client.patch(f"/api/v1/doctors/{ana.id}", json={"aliases": None}).status_code == 422
