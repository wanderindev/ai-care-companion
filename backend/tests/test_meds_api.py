"""Medication list API: current list, full-list saves, versions (synthetic data)."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.medication import MedAvoid, MedEvent, MedItem, MedListVersion
from app.services import log_entries as entries_svc
from app.services.appointments import today_local


def url(patient, suffix=""):
    return f"/api/v1/patients/{patient.id}/meds{suffix}"


def med(name, slot="desayuno", **kw):
    return {"name": name, "slot": slot, "quantity": "1", **kw}


def flat(body):
    return [i for group in body["slots"] for i in group["items"]]


def by_name(body):
    return {i["name"]: i for i in flat(body)}


@pytest.fixture
def papa(patients):
    return patients[0]


@pytest.fixture
def saved(auth_client, papa):
    """Version 1: A and B at breakfast, C at dinner, one EVITAR row."""
    resp = auth_client.put(
        url(papa),
        json={
            "expected_version": 0,
            "items": [
                med("Medicina A", strength="250 mg", time="06:00", days=[5, 1, 3, 3]),
                med("Medicina B"),
                med("Medicina C", slot="cena", quantity="½"),
            ],
            "avoid": [{"substance": "Sustancia X", "reason": "reacción sintética"}],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


OUTPUT_ONLY = {"prescriber", "sort_order", "created_by", "updated_by", "created_at", "updated_at"}


def keep(body, **changes):
    """The list as the editor would send it back, with ``changes`` by name."""
    items = []
    for i in flat(body):
        data = {k: v for k, v in i.items() if k not in OUTPUT_ONLY}
        data.update(changes.get(i["name"], {}))
        items.append(data)
    return items


def test_requires_auth(db_client, patients):
    assert db_client.get(url(patients[0])).status_code == 401


def test_current_list_reads_version_before_items(auth_client, patients, monkeypatch):
    # A save committing between the two reads must give NEW items + OLD
    # version_no (the next save is a 409), never OLD items + NEW version_no.
    from app.services import meds as svc

    calls = []
    real_latest, real_items = svc.latest_version, svc.current_items

    def latest(*args, **kwargs):
        calls.append("version")
        return real_latest(*args, **kwargs)

    def items(*args, **kwargs):
        calls.append("items")
        return real_items(*args, **kwargs)

    monkeypatch.setattr(svc, "latest_version", latest)
    monkeypatch.setattr(svc, "current_items", items)
    assert auth_client.get(url(patients[0])).status_code == 200
    assert calls == ["version", "items"]


def test_unknown_patient(auth_client, patients):
    assert auth_client.get("/api/v1/patients/999/meds").status_code == 404
    resp = auth_client.put("/api/v1/patients/999/meds", json={"expected_version": 0})
    assert resp.status_code == 404


def test_empty_list(auth_client, papa):
    body = auth_client.get(url(papa)).json()
    assert body["version_no"] == 0 and body["avoid"] == []
    assert [g["slot"] for g in body["slots"]] == [
        "al_despertar",
        "desayuno",
        "manana",
        "almuerzo",
        "tarde",
        "cena",
        "noche",
        "madrugada",
        "segun_necesidad",
        "otros",
    ]
    assert body["slots"][-1]["label"] == "Otros medicamentos"
    assert "id_number" not in body


def test_first_save_creates_version_1(auth_client, papa, saved, test_db, member):
    assert saved["saved"] is True and saved["version_no"] == 1
    assert saved["change_summary"] == "Lista inicial: 3 medicamentos y 1 sustancia en EVITAR."
    assert saved["changed_by"] == member.id
    breakfast = saved["slots"][1]["items"]
    assert [(i["name"], i["sort_order"]) for i in breakfast] == [
        ("Medicina A", 0),
        ("Medicina B", 1),
    ]
    a = breakfast[0]
    assert a["time"] == "06:00" and a["days"] == [1, 3, 5] and a["created_by"] == member.id
    assert saved["slots"][5]["items"][0]["quantity"] == "½"
    assert saved["avoid"][0]["substance"] == "Sustancia X"
    assert [d["name"] for d in saved["diff"]["added"]] == ["Medicina A", "Medicina B", "Medicina C"]
    version = test_db.scalars(select(MedListVersion)).one()
    assert version.snapshot["items"][0]["name"] == "Medicina A"
    assert version.snapshot["avoid"][0]["substance"] == "Sustancia X"
    entities = {r.entity for r in test_db.scalars(select(AuditLog))}
    assert {"med_item", "med_avoid", "med_list_version"} <= entities


def test_stale_version_is_409(auth_client, papa, saved):
    resp = auth_client.put(url(papa), json={"expected_version": 0, "items": []})
    assert resp.status_code == 409
    assert "Recarga" in resp.json()["detail"]


def test_no_changes_creates_no_version(auth_client, papa, saved, test_db):
    resp = auth_client.put(
        url(papa),
        json={
            "expected_version": 1,
            "items": keep(saved),
            "avoid": [
                {"id": r["id"], "substance": r["substance"], "reason": r["reason"]}
                for r in saved["avoid"]
            ],
        },
    )
    body = resp.json()
    assert resp.status_code == 200 and body["saved"] is False
    assert body["version_no"] == 1 and body["auto_summary"] == "Sin cambios."
    assert test_db.query(MedListVersion).count() == 1


def test_save_changes_add_remove(auth_client, papa, saved, test_db):
    items = [
        i for i in keep(saved, **{"Medicina A": {"quantity": "½"}}) if i["name"] != "Medicina B"
    ]
    items.append(med("Medicina D", slot="noche", quantity=None))
    resp = auth_client.put(url(papa), json={"expected_version": 1, "items": items, "avoid": []})
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["saved"] is True and body["version_no"] == 2
    assert body["auto_summary"] == (
        "Se agregó Medicina D (Noche); se suspendió Medicina B; Medicina A cambió de 1 a ½; "
        "se quitó Sustancia X de EVITAR."
    )
    assert body["change_summary"] == body["auto_summary"]
    names = by_name(body)
    assert set(names) == {"Medicina A", "Medicina C", "Medicina D"}
    assert names["Medicina A"]["quantity"] == "½"
    # Removed: still in the table, ended yesterday, hidden from the list.
    test_db.expire_all()
    b = test_db.scalars(select(MedItem).where(MedItem.name == "Medicina B")).one()
    assert b.end_date == today_local() - timedelta(days=1)
    assert test_db.query(MedAvoid).count() == 0
    assert "Medicina B" not in by_name(auth_client.get(url(papa)).json())


def test_custom_change_summary(auth_client, papa, saved):
    items = keep(saved, **{"Medicina C": {"quantity": "1"}})
    resp = auth_client.put(
        url(papa),
        json={"expected_version": 1, "items": items, "change_summary": "  Ajuste del médico  "},
    )
    body = resp.json()
    assert body["change_summary"] == "Ajuste del médico"
    assert body["auto_summary"].startswith("Medicina C cambió de ½ a 1")


def test_dry_run_saves_nothing(auth_client, papa, saved, test_db):
    items = keep(saved, **{"Medicina C": {"quantity": "2"}})
    resp = auth_client.put(url(papa), json={"expected_version": 1, "items": items, "dry_run": True})
    body = resp.json()
    assert body["saved"] is False and body["version_no"] == 1
    assert body["auto_summary"].startswith("Medicina C cambió de ½ a 2")
    assert body["diff"]["changed"][0]["fields"][0]["field"] == "quantity"
    assert by_name(body)["Medicina C"]["quantity"] == "½"
    assert test_db.query(MedListVersion).count() == 1


def test_reorder_creates_version(auth_client, papa, saved):
    items = keep(saved)
    items[0], items[1] = items[1], items[0]
    body = auth_client.put(
        url(papa), json={"expected_version": 1, "items": items, "avoid": saved["avoid"]}
    ).json()
    assert body["saved"] is True
    assert body["auto_summary"] == "Se reordenó Desayuno."
    assert [i["name"] for i in body["slots"][1]["items"]] == ["Medicina B", "Medicina A"]


@pytest.mark.parametrize(
    ("mutate", "status"),
    [
        (lambda items: items.append({**items[0]}), 400),  # duplicate id
        (lambda items: items.append({**items[0], "id": 9999}), 400),  # unknown id
        (lambda items: items[0].update(prescriber_id=9999), 400),
        (lambda items: items[0].update(slot="merienda"), 422),
        (lambda items: items[0].update(days=[0]), 422),
        (lambda items: items[0].update(name="  "), 422),
        (
            lambda items: items[0].update(start_date="2026-05-02", end_date="2026-05-01"),
            422,
        ),
    ],
)
def test_invalid_saves(auth_client, papa, saved, test_db, mutate, status):
    items = keep(saved)
    mutate(items)
    resp = auth_client.put(url(papa), json={"expected_version": 1, "items": items})
    assert resp.status_code == status, resp.text
    assert test_db.query(MedListVersion).count() == 1


def test_source_entry_must_be_same_patient(auth_client, test_db, patients):
    papa, mama = patients
    entry = entries_svc.create_entry(
        test_db,
        {
            "patient_id": mama.id,
            "entry_date": date(2026, 1, 5),
            "entry_type": "consulta_control",
            "title": "Control sintético",
        },
        user_id=None,
    )
    test_db.commit()
    body = {"expected_version": 0, "items": [med("Medicina A", source_entry_id=entry.id)]}
    assert auth_client.put(url(papa), json=body).status_code == 400
    resp = auth_client.put(url(mama), json=body)
    assert resp.status_code == 200
    assert flat(resp.json())[0]["source_entry_id"] == entry.id


def test_prescriber_in_snapshot(auth_client, papa, test_db):
    doctor = Doctor(name="Dr. Sintético", aliases=[])
    test_db.add(doctor)
    test_db.commit()
    body = auth_client.put(
        url(papa),
        json={"expected_version": 0, "items": [med("Medicina A", prescriber_id=doctor.id)]},
    ).json()
    assert flat(body)[0]["prescriber"] == {"id": doctor.id, "name": "Dr. Sintético"}
    detail = auth_client.get(url(papa, "/versions/1")).json()
    assert detail["snapshot"]["items"][0]["prescriber_name"] == "Dr. Sintético"


def test_dates_visibility_and_expiry(auth_client, papa, test_db):
    today = today_local()
    future = (today + timedelta(days=10)).isoformat()
    body = auth_client.put(
        url(papa),
        json={
            "expected_version": 0,
            "items": [
                med("Ya terminó", end_date=(today - timedelta(days=1)).isoformat()),
                med("Termina hoy", end_date=today.isoformat()),
                med("Empieza luego", start_date=future),
            ],
        },
    ).json()
    assert set(by_name(body)) == {"Termina hoy", "Empieza luego"}
    # The day passes: the expired item disappears without a save, and the next
    # save records it as suspended (diff vs the previous snapshot).
    test_db.query(MedItem).filter(MedItem.name == "Termina hoy").update(
        {"end_date": today - timedelta(days=1)}
    )
    test_db.commit()
    current = auth_client.get(url(papa)).json()
    assert set(by_name(current)) == {"Empieza luego"}
    resp = auth_client.put(url(papa), json={"expected_version": 1, "items": keep(current)}).json()
    assert resp["saved"] is True and resp["auto_summary"] == "Se suspendió Termina hoy."


def test_versions_list_and_detail(auth_client, papa, saved, member):
    items = keep(saved, **{"Medicina B": {"quantity": "¼"}})
    auth_client.put(
        url(papa), json={"expected_version": 1, "items": items, "avoid": saved["avoid"]}
    )
    versions = auth_client.get(url(papa, "/versions")).json()
    assert [v["version_no"] for v in versions] == [2, 1]
    assert versions[0]["changed_by_name"] == "Ana"
    assert versions[0]["item_count"] == 3 and versions[0]["avoid_count"] == 1
    assert "snapshot" not in versions[0]

    v2 = auth_client.get(url(papa, "/versions/2")).json()
    assert v2["previous_version_no"] == 1
    assert v2["change_summary"] == "Medicina B cambió de 1 a ¼."
    (change,) = v2["diff"]["changed"]
    assert change["fields"][0]["before"] == "1" and change["fields"][0]["after"] == "¼"
    assert v2["next_version_no"] is None and v2["effective_date"] is None
    v1 = auth_client.get(url(papa, "/versions/1")).json()
    assert v1["previous_version_no"] is None and len(v1["diff"]["added"]) == 3
    assert v1["next_version_no"] == 2
    assert auth_client.get(url(papa, "/versions/3")).status_code == 404


NOTE = "| Hora | Medicina |\n|---|---|\n| 06:00 | A |"


def _save(auth_client, papa, body, version, **extra):
    return auth_client.put(
        url(papa),
        json={"expected_version": version, "items": keep(body), "avoid": body["avoid"], **extra},
    )


def test_list_fields_kept_when_omitted(auth_client, papa, saved, test_db):
    # #135: «Desde» and the note are set, then an editor save without them keeps them.
    resp = _save(auth_client, papa, saved, 1, effective_date="2026-09-23", list_note=NOTE).json()
    assert resp["saved"] is True and resp["version_no"] == 2
    assert resp["effective_date"] == "2026-09-23" and resp["list_note"] == NOTE
    assert resp["auto_summary"] == "«Desde»: 23/09/2026; se agregó la nota de la lista."
    assert [f["field"] for f in resp["diff"]["list_changed"]] == ["effective_date", "list_note"]

    items = keep(resp, **{"Medicina B": {"quantity": "2"}})
    resp = auth_client.put(
        url(papa), json={"expected_version": 2, "items": items, "avoid": resp["avoid"]}
    ).json()
    assert resp["saved"] is True and resp["version_no"] == 3
    assert resp["effective_date"] == "2026-09-23" and resp["list_note"] == NOTE
    assert resp["diff"]["list_changed"] == []
    v3 = test_db.scalars(select(MedListVersion).where(MedListVersion.version_no == 3)).one()
    assert v3.effective_date == date(2026, 9, 23)
    assert v3.snapshot["effective_date"] == "2026-09-23" and v3.snapshot["list_note"] == NOTE


def test_list_fields_update_and_clear(auth_client, papa, saved):
    _save(auth_client, papa, saved, 1, effective_date="2026-09-23", list_note=NOTE)
    body = auth_client.get(url(papa)).json()
    # Only the list fields change: still a new version, described in the summary.
    preview = _save(
        auth_client, papa, body, 2, effective_date="2026-10-10", list_note=NOTE + "\n", dry_run=True
    ).json()
    assert preview["saved"] is False
    assert preview["auto_summary"] == "«Desde» pasó de 23/09/2026 a 10/10/2026."
    resp = _save(
        auth_client, papa, body, 2, effective_date="2026-10-10", list_note="| Nueva |\n|---|"
    ).json()
    assert resp["version_no"] == 3 and resp["effective_date"] == "2026-10-10"
    assert resp["auto_summary"] == (
        "«Desde» pasó de 23/09/2026 a 10/10/2026; se modificó la nota de la lista."
    )
    v3 = auth_client.get(url(papa, "/versions/3")).json()
    assert [f["field"] for f in v3["diff"]["list_changed"]] == ["effective_date", "list_note"]
    assert v3["effective_date"] == "2026-10-10"

    resp = _save(auth_client, papa, resp, 3, effective_date=None, list_note="  ").json()
    assert resp["version_no"] == 4
    assert resp["effective_date"] is None and resp["list_note"] is None
    assert resp["auto_summary"] == "Se quitó la fecha «Desde»; se quitó la nota de la lista."
    # Nothing sent, nothing changed: no version.
    assert _save(auth_client, papa, resp, 4).json()["saved"] is False


def test_list_note_too_long(auth_client, papa, saved):
    resp = _save(auth_client, papa, saved, 1, list_note="x" * 20_001)
    assert resp.status_code == 422


def test_previous_desde_read_from_column(auth_client, papa, saved, test_db):
    # A column fixed by hand (snapshot key still null) is not a phantom change.
    _save(auth_client, papa, saved, 1, effective_date="2026-09-23")
    v2 = test_db.scalars(select(MedListVersion).where(MedListVersion.version_no == 2)).one()
    v2.snapshot = {**v2.snapshot, "effective_date": None}
    test_db.commit()
    body = auth_client.get(url(papa)).json()
    assert _save(auth_client, papa, body, 2).json()["saved"] is False


def test_lists_are_per_patient(auth_client, patients, saved):
    mama = patients[1]
    body = auth_client.get(url(mama)).json()
    assert body["version_no"] == 0 and flat(body) == []
    # An id from the other patient's list is not on this one.
    resp = auth_client.put(url(mama), json={"expected_version": 0, "items": keep(saved)[:1]})
    assert resp.status_code == 400


def test_events_require_auth(db_client, patients):
    assert db_client.get(url(patients[0], "/events")).status_code == 401


def test_events_empty(auth_client, patients):
    papa = patients[0]
    assert auth_client.get(url(papa, "/events")).json() == {"items": [], "next_cursor": None}
    assert auth_client.get("/api/v1/patients/999999/meds/events").status_code == 404


def test_events_newest_first_with_source_entry(auth_client, test_db, patients):
    papa, mama = patients
    entry = entries_svc.create_entry(
        test_db,
        {
            "patient_id": papa.id,
            "entry_date": date(2025, 3, 10),
            "entry_type": "consulta_control",
            "title": "Control sintético",
        },
        user_id=None,
    )
    test_db.add_all(
        [
            MedEvent(
                patient_id=papa.id,
                event_date=date(2025, 3, 10),
                kind="inicio",
                medication="Medicina A",
                detail="1 en el desayuno",
                source_entry_id=entry.id,
            ),
            MedEvent(
                patient_id=papa.id,
                event_date=date(2025, 6, 1),
                kind="suspension",
                medication="Medicina A",
                review_status="pending_review",
            ),
            MedEvent(
                patient_id=mama.id,
                event_date=date(2025, 7, 1),
                kind="cambio_dosis",
                medication="Medicina Z",
            ),
        ]
    )
    test_db.commit()
    events = auth_client.get(url(papa, "/events")).json()["items"]
    assert [(e["event_date"], e["kind"]) for e in events] == [
        ("2025-06-01", "suspension"),
        ("2025-03-10", "inicio"),
    ]
    assert events[0]["review_status"] == "pending_review"
    assert events[0]["source_entry_id"] is None and events[0]["source_entry_date"] is None
    assert events[1]["source_entry_id"] == entry.id
    assert events[1]["source_entry_date"] == "2025-03-10"
    assert events[1]["detail"] == "1 en el desayuno"
    limited = auth_client.get(url(papa, "/events?limit=1")).json()
    assert [e["kind"] for e in limited["items"]] == ["suspension"]
    assert limited["next_cursor"]
    assert auth_client.get(url(papa, "/events?limit=0")).status_code == 422
