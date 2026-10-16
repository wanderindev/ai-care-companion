"""Bitácora API: CRUD, filters, sorting, accent-insensitive search, attachments.

Synthetic data only: invented doctors, titles and notes.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.services import log_entries as svc

URL = "/api/v1/log-entries"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.7\n%synthetic\n" + b"0" * 64


@pytest.fixture
def doctors(test_db):
    rows = [
        Doctor(name="Dra. Ana Pérez", aliases=["Anita"], specialty="Geriatría"),
        Doctor(name="Dr. Bruno Zúñiga", aliases=[]),
    ]
    test_db.add_all(rows)
    test_db.commit()
    return rows


def _entry(**overrides):
    body = {
        "patient_id": 1,
        "entry_date": "2026-03-10",
        "entry_type": "consulta_control",
        "title": "Control general",
    }
    body.update(overrides)
    return body


def _create(client, **overrides):
    resp = client.post(URL, json=_entry(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


def _upload(client, data=JPEG_BYTES, name="receta.jpg"):
    resp = client.post("/api/v1/attachments", files={"file": (name, data, "image/jpeg")})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_requires_sign_in(db_client):
    assert db_client.get(URL).status_code == 401
    assert db_client.get(f"{URL}/1").status_code == 401
    headers = {"X-Requested-With": "fetch"}
    assert db_client.post(URL, json=_entry(), headers=headers).status_code == 401
    assert db_client.patch(f"{URL}/1", json={}, headers=headers).status_code == 401
    assert db_client.delete(f"{URL}/1", headers=headers).status_code == 401


def test_write_needs_csrf_header(auth_client, patients):
    del auth_client.headers["X-Requested-With"]
    assert auth_client.post(URL, json=_entry()).status_code == 403


def test_create_and_get(auth_client, test_db, patients, doctors, member):
    ana = doctors[0]
    created = _create(
        auth_client,
        doctor_id=ana.id,
        entry_time="09:30:00",
        channel="presencial",
        indications="Tomar **paracetamol** si hay fiebre.",
        family_notes="  ",
        origin="import_docx",  # ignored: clients cannot pick the origin
        origin_ref={"forged": True},
    )
    assert created["origin"] == "manual"
    assert created["origin_ref"] is None
    assert created["review_status"] == "ok"
    assert created["family_notes"] is None
    assert created["doctor"] == {"id": ana.id, "name": "Dra. Ana Pérez"}
    assert created["attachments"] == []
    assert created["created_by"] == member.id

    got = auth_client.get(f"{URL}/{created['id']}").json()
    assert got == created

    audit = test_db.scalars(select(AuditLog).where(AuditLog.entity == "log_entry")).one()
    assert (audit.action, audit.user_id, audit.before) == ("create", member.id, None)
    assert audit.after["title"] == "Control general"
    assert "search_vector" not in audit.after

    assert auth_client.get(f"{URL}/999").status_code == 404


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"patient_id": 99}, 400),
        ({"doctor_id": 99}, 400),
        ({"entry_date": (date.today() + timedelta(days=5)).isoformat()}, 400),
        ({"entry_date": "1850-01-01"}, 400),
        ({"entry_type": "cirugia"}, 422),
        ({"channel": "fax"}, 422),
        ({"title": None}, 422),  # no doctor, source or title
        ({"title": " "}, 422),
    ],
)
def test_create_validation(auth_client, test_db, patients, overrides, status):
    assert auth_client.post(URL, json=_entry(**overrides)).status_code == status
    assert test_db.scalars(select(LogEntry)).all() == []


def test_create_allows_today_and_source_only(auth_client, patients):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    _create(auth_client, entry_date=tomorrow)
    entry = _create(auth_client, title=None, source_label="Terapia respiratoria")
    assert entry["source_label"] == "Terapia respiratoria"


def test_patch_review_and_fields(auth_client, test_db, patients, doctors, member):
    entry = _create(auth_client, review_status="pending_review")
    resp = auth_client.patch(f"{URL}/{entry['id']}", json={"review_status": "ok"})
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "ok"
    assert resp.json()["title"] == "Control general"  # untouched

    resp = auth_client.patch(
        f"{URL}/{entry['id']}", json={"doctor_id": doctors[1].id, "channel": "whatsapp"}
    )
    assert resp.json()["doctor"]["name"] == "Dr. Bruno Zúñiga"

    updates = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "log_entry", AuditLog.action == "update")
    ).all()
    assert len(updates) == 2
    assert (updates[0].before["review_status"], updates[0].after["review_status"]) == (
        "pending_review",
        "ok",
    )

    # A no-op patch adds no audit row.
    auth_client.patch(f"{URL}/{entry['id']}", json={"channel": "whatsapp"})
    assert test_db.query(AuditLog).filter_by(entity="log_entry", action="update").count() == 2


def test_patch_validation(auth_client, test_db, patients):
    entry = _create(auth_client)
    url = f"{URL}/{entry['id']}"
    assert auth_client.patch(url, json={"entry_date": None}).status_code == 422
    assert auth_client.patch(url, json={"review_status": "maybe"}).status_code == 422
    assert auth_client.patch(url, json={"patient_id": 77}).status_code == 400
    assert auth_client.patch(url, json={"title": None}).status_code == 400  # nothing left
    assert auth_client.patch(f"{URL}/999", json={"title": "x"}).status_code == 404
    assert test_db.get(LogEntry, entry["id"]).title == "Control general"


def test_attachments_link_unlink(auth_client, test_db, patients, storage, member):
    a, b, c = (
        _upload(auth_client),
        _upload(auth_client, PDF_BYTES, "lab.pdf"),
        _upload(auth_client, JPEG_BYTES + b"c"),
    )
    entry = _create(auth_client, attachment_ids=[a, b])
    assert [x["id"] for x in entry["attachments"]] == [a, b]
    assert entry["attachments"][1] == {
        "id": b,
        "filename": "lab.pdf",
        "mime": "application/pdf",
        "size": len(PDF_BYTES),
    }
    listed = auth_client.get(URL).json()["items"][0]
    assert [x["id"] for x in listed["attachments"]] == [a, b]
    assert "origin_ref" not in listed

    # Replace the set: b stays, a is unlinked, c is linked.
    resp = auth_client.patch(f"{URL}/{entry['id']}", json={"attachment_ids": [b, c]})
    assert [x["id"] for x in resp.json()["attachments"]] == [b, c]
    row_a = test_db.get(Attachment, a)
    test_db.refresh(row_a)
    assert (row_a.owner_type, row_a.owner_id) == (None, None)
    unlink = test_db.scalars(select(AuditLog).where(AuditLog.action == "unlink")).one()
    assert (unlink.entity_id, unlink.user_id) == (str(a), member.id)
    assert unlink.before["owner_id"] == entry["id"]

    # Another entry cannot take a linked file.
    other = auth_client.post(URL, json=_entry(attachment_ids=[b]))
    assert other.status_code == 409
    assert auth_client.post(URL, json=_entry(attachment_ids=[999])).status_code == 409
    assert test_db.query(LogEntry).count() == 1

    # Omitting attachment_ids leaves them alone; [] removes all.
    auth_client.patch(f"{URL}/{entry['id']}", json={"title": "Otro"})
    assert len(auth_client.get(f"{URL}/{entry['id']}").json()["attachments"]) == 2
    resp = auth_client.patch(f"{URL}/{entry['id']}", json={"attachment_ids": []})
    assert resp.json()["attachments"] == []


def test_delete_keeps_audit_and_unlinks_files(auth_client, test_db, patients, storage, member):
    a = _upload(auth_client)
    entry = _create(auth_client, attachment_ids=[a], indications="Reposo.")
    resp = auth_client.delete(f"{URL}/{entry['id']}")
    assert resp.status_code == 204
    assert auth_client.get(f"{URL}/{entry['id']}").status_code == 404
    assert auth_client.get(URL).json()["total"] == 0

    row = test_db.get(Attachment, a)
    test_db.refresh(row)
    assert (row.owner_type, row.owner_id) == (None, None)  # kept, unlinked
    deleted = test_db.scalars(select(AuditLog).where(AuditLog.action == "delete")).one()
    assert deleted.user_id == member.id
    assert deleted.before["indications"] == "Reposo."
    assert deleted.before["attachment_ids"] == [a]
    assert deleted.after is None
    # Self-contained: who/when created and last edited it survive the row.
    assert deleted.before["created_by"] == member.id
    assert deleted.before["updated_by"] == member.id
    assert deleted.before["created_at"] and deleted.before["updated_at"]
    assert "search_vector" not in deleted.before
    assert auth_client.delete(f"{URL}/{entry['id']}").status_code == 404


# ── Listing ──────────────────────────────────────────────────────────────────


@pytest.fixture
def bitacora(test_db, patients, doctors):
    """Six synthetic entries across both patients."""
    papa, mama = patients
    ana, bruno = doctors
    rows = [
        LogEntry(  # 0
            patient_id=papa.id,
            entry_date=date(2026, 1, 5),
            doctor_id=ana.id,
            entry_type="consulta_enfermedad",
            channel="presencial",
            title="Neumonía bilateral",
            indications="Antibiótico por siete días.",
        ),
        LogEntry(  # 1
            patient_id=papa.id,
            entry_date=date(2026, 2, 1),
            doctor_id=bruno.id,
            entry_type="teleconsulta",
            channel="whatsapp",
            title="Control de presión",
            review_status="pending_review",
            origin="import_docx",
            origin_ref={"original": "texto sintético"},
        ),
        LogEntry(  # 2
            patient_id=mama.id,
            entry_date=date(2026, 2, 1),
            entry_type="terapia",
            channel="domicilio",
            source_label="RehabSur - correo",
            family_notes="Se cansó menos al caminar.",
        ),
        LogEntry(  # 3
            patient_id=mama.id,
            entry_date=date(2025, 12, 20),
            doctor_id=ana.id,
            entry_type="vacuna",
            title="Vacuna de la influenza",
        ),
        LogEntry(  # 4
            patient_id=papa.id,
            entry_date=date(2026, 3, 1),
            entry_type="observacion_familiar",
            title="Tos nocturna",
            family_notes="Internet recomienda miel.",
        ),
        LogEntry(  # 5
            patient_id=mama.id,
            entry_date=date(2026, 2, 1),
            entry_time=None,
            entry_type="examen",
            source_label="Laboratorio Central",
            title="Hemograma",
        ),
    ]
    test_db.add_all(rows)
    test_db.commit()
    return [r.id for r in rows]


def test_list_default_sort_and_paging(auth_client, bitacora):
    e = bitacora
    resp = auth_client.get(URL)
    body = resp.json()
    assert body["total"] == 6 and body["limit"] == 50 and body["offset"] == 0
    # Date desc; same day: created_at desc then id desc (5, 2, 1 inserted together).
    assert _ids(resp) == [e[4], e[5], e[2], e[1], e[0], e[3]]
    page = auth_client.get(URL, params={"limit": 2, "offset": 2})
    assert _ids(page) == [e[2], e[1]]
    assert page.json()["total"] == 6
    assert auth_client.get(URL, params={"limit": 0}).status_code == 422
    assert auth_client.get(URL, params={"limit": 500}).status_code == 422


def test_list_sorts(auth_client, bitacora):
    e = bitacora
    asc = _ids(auth_client.get(URL, params={"order": "asc"}))
    assert asc[0] == e[3] and asc[-1] == e[4]
    # Doctor name (Ana < Bruno), then entries without doctor by source label, last.
    by_doctor = _ids(auth_client.get(URL, params={"sort": "doctor", "order": "asc"}))
    assert by_doctor == [e[0], e[3], e[1], e[5], e[2], e[4]]
    by_doctor_desc = _ids(auth_client.get(URL, params={"sort": "doctor"}))
    assert by_doctor_desc[:3] == [e[1], e[0], e[3]]
    assert by_doctor_desc[-1] == e[4]  # nulls stay last
    by_type = _ids(auth_client.get(URL, params={"sort": "type", "order": "asc"}))
    assert by_type == [e[0], e[5], e[4], e[1], e[2], e[3]]
    assert auth_client.get(URL, params={"sort": "title"}).status_code == 422


def test_list_filters(auth_client, patients, doctors, bitacora):
    e = bitacora
    papa, mama = patients
    ana, _ = doctors
    assert set(_ids(auth_client.get(URL, params={"patient_id": papa.id}))) == {e[0], e[1], e[4]}
    both = auth_client.get(URL, params={"patient_id": [papa.id, mama.id]})
    assert both.json()["total"] == 6
    assert set(_ids(auth_client.get(URL, params={"doctor_id": ana.id}))) == {e[0], e[3]}
    types = auth_client.get(URL, params={"entry_type": ["vacuna", "terapia"]})
    assert set(_ids(types)) == {e[2], e[3]}
    assert _ids(auth_client.get(URL, params={"channel": "whatsapp"})) == [e[1]]
    assert _ids(auth_client.get(URL, params={"review_status": "pending_review"})) == [e[1]]
    in_range = auth_client.get(URL, params={"date_from": "2026-01-05", "date_to": "2026-02-01"})
    assert set(_ids(in_range)) == {e[0], e[1], e[2], e[5]}
    combined = auth_client.get(
        URL, params={"patient_id": mama.id, "date_from": "2026-01-01", "entry_type": "examen"}
    )
    assert _ids(combined) == [e[5]]
    assert auth_client.get(URL, params={"entry_type": "cirugia"}).status_code == 422


def test_list_omits_origin_ref_detail_has_it(auth_client, bitacora):
    item = auth_client.get(URL, params={"review_status": "pending_review"}).json()["items"][0]
    assert "origin_ref" not in item and item["origin"] == "import_docx"
    detail = auth_client.get(f"{URL}/{bitacora[1]}").json()
    assert detail["origin_ref"] == {"original": "texto sintético"}


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("neumonia", [0]),  # no accent finds "Neumonía"
        ("NEUMONÍA", [0]),
        ("neumo", [0]),  # prefix while typing
        ("neumonia bilateral", [0]),
        ("neumonia gripe", []),  # every word must match
        ("antibioticos", [0]),  # stemming: plural finds singular
        ("rehabsur", [2]),  # source_label
        ("caminar", [2]),  # family notes
        ("influenza", [3]),
        ("perez", [0, 3]),  # doctor's name
        ("anita", [0, 3]),  # doctor's alias
        ("zuniga presion", [1]),  # doctor + text
        ("laboratorio", [5]),
        ("miel", [4]),
        ("':* & | !", None),  # only punctuation: no search filter
    ],
)
def test_search(auth_client, bitacora, q, expected):
    found = _ids(auth_client.get(URL, params={"q": q}))
    if expected is None:
        assert len(found) == 6
    else:
        assert sorted(found) == sorted(bitacora[i] for i in expected)


def test_search_vector_follows_updates(auth_client, bitacora):
    entry_id = bitacora[4]
    auth_client.patch(f"{URL}/{entry_id}", json={"title": "Úlcera en el talón"})
    assert _ids(auth_client.get(URL, params={"q": "ulcera"})) == [entry_id]
    assert _ids(auth_client.get(URL, params={"q": "tos"})) == []


def test_tsquery_text():
    assert svc.tsquery_text("  Neumonía, bilateral! ") == "Neumonía:* & bilateral:*"
    assert svc.tsquery_text("a_b") == "a:* & b:*"
    assert svc.tsquery_text("':*&|!()") is None
    assert svc.tsquery_text(None) is None


def test_create_entry_service_for_importer(test_db, patients, member):
    """The importer path: origin and origin_ref set by the caller, pending review."""
    entry = svc.create_entry(
        test_db,
        {
            "patient_id": patients[0].id,
            "entry_date": date(2025, 6, 1),
            "entry_type": "otro",
            "source_label": "Clínica Norte",
            "indications": "Texto sintético sin título.",
            "review_status": "pending_review",
        },
        user_id=None,
        origin="import_docx",
        origin_ref={"position": 3, "hash": "abc"},
    )
    test_db.commit()
    assert (entry.origin, entry.review_status) == ("import_docx", "pending_review")
    assert entry.origin_ref == {"position": 3, "hash": "abc"}
    with pytest.raises(svc.LogEntryError):
        svc.create_entry(test_db, {"origin": "manual"}, user_id=None)


def test_create_entry_service_rules(test_db, patients):
    """The importer gets the same rules as the API: a header, a known origin."""
    base = {"patient_id": patients[0].id, "entry_date": date(2025, 6, 1), "entry_type": "otro"}
    with pytest.raises(svc.LogEntryError, match="médico, la fuente o un título"):
        svc.create_entry(test_db, {**base, "indications": "Solo texto."}, user_id=None)
    with pytest.raises(svc.LogEntryError, match="Origen"):
        svc.create_entry(test_db, {**base, "title": "X"}, user_id=None, origin="fax")
    test_db.rollback()
    svc.create_entry(test_db, {**base, "title": "Solo título"}, user_id=None, origin="import_docx")
    svc.create_entry(test_db, {**base, "source_label": "Solo fuente"}, user_id=None)
    test_db.commit()
    assert test_db.query(LogEntry).count() == 2


@pytest.mark.parametrize("sort", ["date", "doctor", "type"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_search_with_every_sort(auth_client, bitacora, sort, order):
    params = {"q": "perez", "sort": sort, "order": order}
    found = _ids(auth_client.get(URL, params=params))
    assert sorted(found) == sorted([bitacora[0], bitacora[3]])
    neumonia = auth_client.get(URL, params={**params, "q": "neumonia"})
    assert _ids(neumonia) == [bitacora[0]]


@pytest.mark.parametrize("q", ["⑴", "ŉ", "ː", "¼", "neumonia ⑴", "x'y", "a\\b", "ŉeumonia"])
def test_search_odd_characters(auth_client, bitacora, q):
    """Characters that unaccent expands into tsquery syntax never cause a 500."""
    assert auth_client.get(URL, params={"q": q}).status_code == 200


# ── Bulk «Marcar todas como revisadas» (#58) ─────────────────────────────────

REVIEW_URL = f"{URL}/review"


@pytest.fixture
def pending(test_db, bitacora, patients, doctors):
    """Three more pending imports on top of ``bitacora`` (whose #1 is pending)."""
    papa, mama = patients
    ana, _ = doctors
    rows = [
        LogEntry(  # papa, Ana, matches «neumo»
            patient_id=papa.id,
            entry_date=date(2026, 1, 20),
            doctor_id=ana.id,
            entry_type="consulta_control",
            title="Seguimiento de neumonía",
            review_status="pending_review",
            origin="import_docx",
        ),
        LogEntry(  # papa, no doctor, older
            patient_id=papa.id,
            entry_date=date(2025, 11, 3),
            entry_type="examen",
            title="Radiografía de tórax",
            review_status="pending_review",
            origin="import_docx",
        ),
        LogEntry(  # mama
            patient_id=mama.id,
            entry_date=date(2026, 1, 21),
            entry_type="terapia",
            title="Fisioterapia",
            review_status="pending_review",
            origin="import_docx",
        ),
    ]
    test_db.add_all(rows)
    test_db.commit()
    return {"bitacora": bitacora, "new": [r.id for r in rows]}


@pytest.fixture
def admin_client(auth_client, test_db, member):
    member.role = "admin"
    test_db.commit()
    return auth_client


def _status(test_db, ids):
    test_db.expire_all()
    rows = test_db.scalars(select(LogEntry).where(LogEntry.id.in_(ids)))
    return {r.id: r.review_status for r in rows}


def _review_audits(test_db):
    return test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "log_entry", AuditLog.action == "update")
    ).all()


def test_bulk_review_by_filter(admin_client, test_db, patients, pending, member):
    papa, _ = patients
    b, (papa_ana, papa_plain, mama_new) = pending["bitacora"], pending["new"]
    body = {"filter": {"patient_id": [papa.id], "date_from": "2026-01-01"}}
    # The list with the same filter + «Por revisar» shows what the action changes.
    params = {"patient_id": papa.id, "date_from": "2026-01-01", "review_status": "pending_review"}
    assert sorted(_ids(admin_client.get(URL, params=params))) == sorted([b[1], papa_ana])

    resp = admin_client.post(REVIEW_URL, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"updated": 2, "ids": sorted([b[1], papa_ana])}

    status = _status(test_db, [*b, papa_ana, papa_plain, mama_new])
    assert status[b[1]] == status[papa_ana] == "ok"
    # Outside the filter (older date, other patient): still pending.
    assert status[papa_plain] == status[mama_new] == "pending_review"

    audits = _review_audits(test_db)
    assert sorted(int(a.entity_id) for a in audits) == sorted([b[1], papa_ana])
    for a in audits:
        assert a.user_id == member.id
        assert (a.before["review_status"], a.after["review_status"]) == ("pending_review", "ok")

    # Nothing left to do: a second call changes and audits nothing.
    assert admin_client.post(REVIEW_URL, json=body).json() == {"updated": 0, "ids": []}
    assert len(_review_audits(test_db)) == 2


def test_bulk_review_filter_search_and_doctor(admin_client, doctors, pending):
    ana, _ = doctors
    papa_ana = pending["new"][0]
    resp = admin_client.post(REVIEW_URL, json={"filter": {"q": "neumo", "doctor_id": [ana.id]}})
    assert resp.json() == {"updated": 1, "ids": [papa_ana]}


def test_bulk_review_by_ids_skips_reviewed(admin_client, test_db, pending):
    b, (papa_ana, _, mama_new) = pending["bitacora"], pending["new"]
    # b[0] is already reviewed: ignored, no audit row; unknown ids are ignored.
    resp = admin_client.post(REVIEW_URL, json={"ids": [b[0], papa_ana, mama_new, 99999]})
    assert resp.json() == {"updated": 2, "ids": sorted([papa_ana, mama_new])}
    audited = sorted(int(a.entity_id) for a in _review_audits(test_db))
    assert audited == sorted([papa_ana, mama_new])


def test_bulk_review_empty_filter_reviews_all_pending(admin_client, test_db, pending):
    resp = admin_client.post(REVIEW_URL, json={"filter": {}})
    assert resp.json()["updated"] == 4
    assert set(_status(test_db, [*pending["bitacora"], *pending["new"]]).values()) == {"ok"}


@pytest.mark.parametrize(
    "body",
    [{}, {"ids": [1], "filter": {}}, {"ids": []}, {"filter": {"entry_type": ["nope"]}}],
)
def test_bulk_review_validation(admin_client, pending, body):
    assert admin_client.post(REVIEW_URL, json=body).status_code == 422


def test_bulk_review_invalid_entry_rolls_back(admin_client, test_db, pending):
    b, (papa_ana, papa_plain, _) = pending["bitacora"], pending["new"]
    # An imported entry that no longer validates (no header): the batch fails whole.
    bad = test_db.get(LogEntry, papa_plain)
    bad.title = None
    test_db.commit()
    resp = admin_client.post(REVIEW_URL, json={"ids": [b[1], papa_ana, papa_plain]})
    assert resp.status_code == 400
    assert "03/11/2025" in resp.json()["detail"]
    assert set(_status(test_db, [b[1], papa_ana, papa_plain]).values()) == {"pending_review"}
    assert _review_audits(test_db) == []


def test_bulk_review_needs_admin(auth_client, test_db, pending):
    resp = auth_client.post(REVIEW_URL, json={"filter": {}})
    assert resp.status_code == 403
    assert "ok" not in _status(test_db, pending["new"]).values()


def test_bulk_review_needs_csrf_and_sign_in(admin_client, test_db, pending):
    del admin_client.headers["X-Requested-With"]
    assert admin_client.post(REVIEW_URL, json={"filter": {}}).status_code == 403
    assert "ok" not in _status(test_db, pending["new"]).values()
    admin_client.cookies.clear()
    headers = {"X-Requested-With": "fetch"}
    resp = admin_client.post(REVIEW_URL, json={"filter": {}}, headers=headers)
    assert resp.status_code == 401
