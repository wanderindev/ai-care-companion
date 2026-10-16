"""Suggestions and «Pendientes»: payload validation per kind, approve through
the real services (entries with source files, appointments, medication
changes incl. a stale base, doctors incl. duplicates), edited approvals,
reject, double decisions, auth/CSRF/audit, the count endpoint.

Synthetic data only: invented people, messages and medicines.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.doctor import Doctor
from app.models.log_entry import LogEntry
from app.models.medication import MedItem
from app.models.suggestion import Suggestion
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import suggestions as svc
from app.services.attachments import JPEG, MP4, OGG, store_upload

URL = "/api/v1/suggestions"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"synthetic-photo" * 10
OGG_BYTES = b"OggS" + b"synthetic-voice" * 10
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"synthetic-video" * 10


@pytest.fixture
def papa(patients):
    return patients[0]


@pytest.fixture
def imp(test_db):
    row = WhatsappImport(
        mode="daily", status="done", filename="c.zip", size=1, chunk_size=1, chunk_count=1
    )
    test_db.add(row)
    test_db.commit()
    return row


def _message(db, imp, minute, *, text="", media=None, transcript=None, storage=None):
    attachment = None
    kind = None
    if media is not None:
        data, file_type, kind = media
        attachment = store_upload(
            db, storage, data=data, file_type=file_type, filename=f"F{minute}", user_id=None
        )
    message = WhatsappMessage(
        import_id=imp.id,
        sent_at=datetime(2026, 5, 1, 10, minute, tzinfo=UTC),
        sender="Persona Prueba",
        text=text,
        media_kind=kind,
        attachment_id=attachment.id if attachment else None,
        media_import_id=imp.id if attachment else None,
        transcript=transcript,
        dedupe_hash=f"hash-{minute}",
    )
    db.add(message)
    db.flush()
    if attachment is not None:
        attachment.owner_type, attachment.owner_id = "whatsapp_message", message.id
    db.commit()
    return message


@pytest.fixture
def messages(test_db, imp, storage):
    return [
        _message(test_db, imp, 1, text="Fuimos al control, todo bien"),
        _message(
            test_db,
            imp,
            2,
            media=(OGG_BYTES, OGG, "audio"),
            transcript="Dijo que siga igual",
            storage=storage,
        ),
        _message(test_db, imp, 3, media=(JPEG_BYTES, JPEG, "image"), storage=storage),
        _message(test_db, imp, 4, media=(MP4_BYTES, MP4, "video"), storage=storage),
    ]


def _entry_payload(papa, **kw):
    return {
        "patient_id": papa.id,
        "entry_date": "2026-05-01",
        "entry_type": "consulta_control",
        "title": "Control sintético",
        "indications": "Seguir igual.",
        **kw,
    }


def _suggest(db, kind, payload, **kw):
    row = svc.create_suggestion(db, kind=kind, payload=payload, rationale="Porque sí", **kw)
    db.commit()
    return row


def _audit(db, entity):
    return list(db.scalars(select(AuditLog).where(AuditLog.entity == entity).order_by(AuditLog.id)))


# ── Validation (create_suggestion) ───────────────────────────────────────────


def test_create_suggestion_normalizes_and_audits(test_db, papa, messages):
    row = _suggest(
        test_db,
        "create_entry",
        _entry_payload(papa, title="  Control  ", channel=None),
        source_message_ids=[messages[0].id, messages[1].id, messages[0].id],
    )
    assert row.status == "pending"
    assert row.patient_id == papa.id
    assert row.source == "whatsapp"
    assert row.source_message_ids == [messages[0].id, messages[1].id]
    assert row.payload["entry_date"] == "2026-05-01"
    assert "next_appointment" not in row.payload  # unset stays unset
    [entry] = _audit(test_db, "suggestion")
    assert entry.action == "create" and entry.user_id is None


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("create_entry", {"patient_id": 1, "entry_date": "2026-05-01", "entry_type": "otro"}),
        ("update_entry", {"entry_id": 1, "changes": {}}),
        ("create_appointment", {"patient_id": 1, "reason": "Sin fecha"}),
        ("med_change", {"action": "add", "medication": "X", "item": {"name": "X"}}),
        ("med_change", {"action": "stop", "medication": "X"}),
        ("med_change", {"action": "change", "medication": "X", "item_id": 1, "item": {}}),
        ("med_change", {"action": "change", "medication": "X", "item_id": 1, "item": {"id": 1}}),
        ("doctor", {"name": ""}),
        ("lab_report", {"anything": True}),
        ("nonsense", {}),
    ],
)
def test_create_suggestion_rejects_invalid_payloads(test_db, papa, kind, payload):
    with pytest.raises(svc.SuggestionError):
        svc.create_suggestion(
            test_db, kind=kind, payload=payload, rationale=None, patient_id=papa.id
        )


def test_create_suggestion_checks_references(test_db, papa):
    with pytest.raises(svc.SuggestionError):  # unknown message
        svc.create_suggestion(
            test_db,
            kind="create_entry",
            payload=_entry_payload(papa),
            rationale=None,
            source_message_ids=[999],
        )
    with pytest.raises(svc.SuggestionError):  # entry to update does not exist
        svc.create_suggestion(
            test_db,
            kind="update_entry",
            payload={"entry_id": 999, "changes": {"title": "X"}},
            rationale=None,
        )
    with pytest.raises(svc.SuggestionError):  # med_change needs the patient
        svc.create_suggestion(
            test_db,
            kind="med_change",
            payload={"action": "stop", "medication": "X", "item_id": 1},
            rationale=None,
        )


# ── Auth, list, count ────────────────────────────────────────────────────────


def test_requires_sign_in_and_csrf(db_client, auth_client, test_db, papa):
    row = _suggest(test_db, "create_entry", _entry_payload(papa))
    auth_client.cookies.clear()
    assert auth_client.get(URL).status_code == 401
    assert auth_client.get(f"{URL}/count").status_code == 401
    assert auth_client.post(f"{URL}/{row.id}/approve").status_code == 401


def test_csrf_header_required(auth_client, test_db, papa):
    row = _suggest(test_db, "create_entry", _entry_payload(papa))
    del auth_client.headers["X-Requested-With"]
    assert auth_client.post(f"{URL}/{row.id}/approve").status_code == 403
    assert auth_client.post(f"{URL}/{row.id}/reject").status_code == 403
    test_db.refresh(row)
    assert row.status == "pending"


def test_list_and_count(auth_client, test_db, patients, messages):
    papa, mama = patients
    a = _suggest(
        test_db, "create_entry", _entry_payload(papa), source_message_ids=[m.id for m in messages]
    )
    _suggest(test_db, "create_entry", _entry_payload(mama))
    doc = _suggest(test_db, "doctor", {"name": "Dra. Nueva Prueba"})
    assert auth_client.get(f"{URL}/count").json() == {"pending": 3}
    assert auth_client.get(f"{URL}/count", params={"patient_id": papa.id}).json() == {"pending": 2}

    body = auth_client.get(URL, params={"patient_id": papa.id}).json()
    assert body["total"] == 2
    assert [i["id"] for i in body["items"]] == [doc.id, a.id]  # newest first
    item = body["items"][1]
    assert [m["id"] for m in item["source_messages"]] == [m.id for m in messages]
    audio = item["source_messages"][1]
    assert audio["media_kind"] == "audio"
    assert audio["transcript"] == "Dijo que siga igual"
    assert audio["mime"] == "audio/ogg"
    assert body["items"][0]["context"] == {"duplicates": []}

    auth_client.post(f"{URL}/{a.id}/reject")
    assert auth_client.get(f"{URL}/count").json() == {"pending": 2}
    rejected = auth_client.get(URL, params={"status": "rejected"}).json()
    assert [i["id"] for i in rejected["items"]] == [a.id]
    assert rejected["items"][0]["decided_by_name"] == "Ana"


# ── Entries ──────────────────────────────────────────────────────────────────


def test_approve_create_entry_with_source_files(auth_client, test_db, papa, member, messages):
    row = _suggest(
        test_db, "create_entry", _entry_payload(papa), source_message_ids=[m.id for m in messages]
    )
    resp = auth_client.post(f"{URL}/{row.id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == member.id
    assert body["result_ref"]["entity"] == "log_entry"

    entry = test_db.get(LogEntry, body["result_ref"]["id"])
    assert entry.origin == "whatsapp"
    assert entry.origin_ref == {"suggestion_id": row.id, "message_ids": [m.id for m in messages]}
    assert entry.created_by == member.id
    files = list(
        test_db.scalars(
            select(Attachment).where(
                Attachment.owner_type == "log_entry", Attachment.owner_id == entry.id
            )
        )
    )
    # Audio and photo, not the video; new rows on the same stored objects.
    assert sorted(f.mime for f in files) == ["audio/ogg", "image/jpeg"]
    for message in messages[1:3]:
        original = test_db.get(Attachment, message.attachment_id)
        assert original.owner_type == "whatsapp_message"  # still the message's
        assert original.storage_key in {f.storage_key for f in files}

    [entry_audit] = _audit(test_db, "log_entry")
    assert entry_audit.user_id == member.id and entry_audit.action == "create"
    decision = _audit(test_db, "suggestion")[-1]
    assert decision.action == "approve" and decision.user_id == member.id
    assert decision.after["status"] == "approved"

    # Decided once.
    again = auth_client.post(f"{URL}/{row.id}/approve")
    assert again.status_code == 409
    assert auth_client.post(f"{URL}/{row.id}/reject").status_code == 409
    assert test_db.scalar(select(LogEntry.id).where(LogEntry.id != entry.id)) is None


def test_approve_edited_payload(auth_client, test_db, papa):
    row = _suggest(test_db, "create_entry", _entry_payload(papa))
    edited = _entry_payload(papa, title="Título corregido", channel="whatsapp")
    resp = auth_client.post(f"{URL}/{row.id}/approve", json={"payload": edited})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "edited"
    entry = test_db.get(LogEntry, resp.json()["result_ref"]["id"])
    assert entry.title == "Título corregido"
    test_db.refresh(row)
    assert row.payload["title"] == "Control sintético"  # the proposal is kept
    decision = _audit(test_db, "suggestion")[-1]
    assert decision.after["applied_payload"]["title"] == "Título corregido"


def test_same_payload_is_approved_not_edited(auth_client, test_db, papa):
    row = _suggest(test_db, "create_entry", _entry_payload(papa))
    resp = auth_client.post(f"{URL}/{row.id}/approve", json={"payload": row.payload})
    assert resp.json()["status"] == "approved"


def test_invalid_approval_keeps_pending(auth_client, test_db, papa):
    row = _suggest(test_db, "create_entry", _entry_payload(papa))
    future = (date.today() + timedelta(days=10)).isoformat()
    resp = auth_client.post(
        f"{URL}/{row.id}/approve", json={"payload": _entry_payload(papa, entry_date=future)}
    )
    assert resp.status_code == 422
    assert "fecha" in resp.json()["detail"]
    bad_shape = auth_client.post(f"{URL}/{row.id}/approve", json={"payload": {"title": "x"}})
    assert bad_shape.status_code == 422
    test_db.refresh(row)
    assert row.status == "pending"
    assert test_db.scalar(select(LogEntry.id)) is None
    assert auth_client.post(f"{URL}/999/approve").status_code == 404


def test_approve_update_entry(auth_client, test_db, papa, member, messages):
    from app.services import log_entries

    entry = log_entries.create_entry(
        test_db, _entry_payload(papa, entry_date=date(2026, 4, 30)), user_id=None
    )
    test_db.commit()
    row = _suggest(
        test_db,
        "update_entry",
        {"entry_id": entry.id, "changes": {"family_notes": "Añadido desde WhatsApp"}},
        source_message_ids=[messages[2].id],
    )
    listed = auth_client.get(URL).json()["items"][0]
    assert listed["context"]["entry"]["title"] == "Control sintético"
    resp = auth_client.post(f"{URL}/{row.id}/approve")
    assert resp.status_code == 200, resp.text
    test_db.refresh(entry)
    assert entry.family_notes == "Añadido desde WhatsApp"
    assert entry.indications == "Seguir igual."
    assert entry.updated_by == member.id
    [photo] = test_db.scalars(
        select(Attachment).where(
            Attachment.owner_type == "log_entry", Attachment.owner_id == entry.id
        )
    )
    assert photo.mime == "image/jpeg"


def test_update_entry_target_deleted(auth_client, test_db, papa):
    from app.services import log_entries

    entry = log_entries.create_entry(
        test_db, _entry_payload(papa, entry_date=date(2026, 4, 30)), user_id=None
    )
    test_db.commit()
    row = _suggest(test_db, "update_entry", {"entry_id": entry.id, "changes": {"title": "X"}})
    log_entries.delete_entry(test_db, entry, user_id=None)
    test_db.commit()
    resp = auth_client.post(f"{URL}/{row.id}/approve")
    assert resp.status_code == 422
    assert (
        auth_client.post(f"{URL}/{row.id}/reject", json={"reason": "Ya no aplica"}).json()[
            "decision_note"
        ]
        == "Ya no aplica"
    )


# ── Appointments ─────────────────────────────────────────────────────────────


def test_approve_create_appointment(auth_client, test_db, papa, member, messages):
    row = _suggest(
        test_db,
        "create_appointment",
        {"patient_id": papa.id, "starts_at": "2026-11-03T09:00:00", "reason": "Control"},
        source_message_ids=[messages[1].id],
    )
    resp = auth_client.post(f"{URL}/{row.id}/approve")
    assert resp.status_code == 200, resp.text
    appt = test_db.get(Appointment, resp.json()["result_ref"]["id"])
    assert appt.status == "programada" and appt.created_by == member.id
    assert appt.reason == "Control"
    [audio] = test_db.scalars(
        select(Attachment).where(
            Attachment.owner_type == "appointment", Attachment.owner_id == appt.id
        )
    )
    assert audio.mime == "audio/ogg"
    assert _audit(test_db, "appointment")[0].user_id == member.id


# ── Medication changes ───────────────────────────────────────────────────────


def _meds_url(papa):
    return f"/api/v1/patients/{papa.id}/meds"


@pytest.fixture
def med_list(auth_client, papa):
    resp = auth_client.put(
        _meds_url(papa),
        json={
            "expected_version": 0,
            "items": [
                {"name": "Medicina A", "slot": "desayuno", "quantity": "1"},
                {"name": "Medicina B", "slot": "cena", "quantity": "½"},
            ],
            "avoid": [{"substance": "Sustancia Z"}],
        },
    )
    assert resp.status_code == 200, resp.text
    return {i["name"]: i for g in resp.json()["slots"] for i in g["items"]}


def _current_names(auth_client, papa):
    body = auth_client.get(_meds_url(papa)).json()
    return body["version_no"], {i["name"]: i for g in body["slots"] for i in g["items"]}


def test_med_change_add_with_preview(auth_client, test_db, papa, member, med_list):
    row = _suggest(
        test_db,
        "med_change",
        {
            "action": "add",
            "medication": "Sustancia Z forte",
            "item": {"name": "Sustancia Z forte", "slot": "noche", "quantity": "1"},
            "base_version": 1,
        },
        patient_id=papa.id,
    )
    preview = auth_client.post(f"{URL}/{row.id}/preview")
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["version_no"] == 1 and data["stale"] is False
    assert [i["name"] for i in data["diff"]["added"]] == ["Sustancia Z forte"]
    assert data["warnings"][0]["substance"] == "Sustancia Z"  # EVITAR, never blocking
    assert _current_names(auth_client, papa)[0] == 1  # nothing saved

    resp = auth_client.post(
        f"{URL}/{row.id}/approve", json={"expected_version": 1, "change_summary": "Nueva"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result_ref"]["version_no"] == 2
    version, items = _current_names(auth_client, papa)
    assert version == 2 and set(items) == {"Medicina A", "Medicina B", "Sustancia Z forte"}
    assert items["Medicina A"]["id"] == med_list["Medicina A"]["id"]  # kept, not recreated
    versions = _audit(test_db, "med_list_version")
    assert versions[-1].user_id == member.id and versions[-1].after["version_no"] == 2


def test_med_change_keeps_desde_and_note(auth_client, test_db, papa, med_list):
    # #135: approving a med_change keeps the list's «Desde» and note.
    body = auth_client.get(_meds_url(papa)).json()
    items = [
        {k: i[k] for k in ("id", "name", "slot", "quantity")}
        for g in body["slots"]
        for i in g["items"]
    ]
    resp = auth_client.put(
        _meds_url(papa),
        json={
            "expected_version": 1,
            "items": items,
            "avoid": body["avoid"],
            "effective_date": "2026-09-23",
            "list_note": "| Hora | Dosis |\n|---|---|\n| 6:00 | 1 |",
        },
    )
    assert resp.json()["version_no"] == 2
    row = _suggest(
        test_db,
        "med_change",
        {
            "action": "add",
            "medication": "Medicina C",
            "item": {"name": "Medicina C", "slot": "noche", "quantity": "1"},
        },
        patient_id=papa.id,
    )
    preview = auth_client.post(f"{URL}/{row.id}/preview").json()
    assert preview["diff"]["list_changed"] == []
    resp = auth_client.post(f"{URL}/{row.id}/approve", json={})
    assert resp.status_code == 200, resp.text
    body = auth_client.get(_meds_url(papa)).json()
    assert body["version_no"] == 3
    assert body["effective_date"] == "2026-09-23"
    assert body["list_note"].startswith("| Hora | Dosis |")


def test_med_change_dose_on_stale_base(auth_client, test_db, papa, med_list):
    row = _suggest(
        test_db,
        "med_change",
        {
            "action": "change",
            "medication": "Medicina A",
            "item_id": med_list["Medicina A"]["id"],
            "item": {"quantity": "2"},
            "base_version": 1,
        },
        patient_id=papa.id,
    )
    # Someone saves the list in between (version 2).
    auth_client.put(
        _meds_url(papa),
        json={
            "expected_version": 1,
            "items": [
                {"id": med_list["Medicina A"]["id"], "name": "Medicina A", "slot": "desayuno"},
                {"id": med_list["Medicina B"]["id"], "name": "Medicina B", "slot": "cena"},
                {"name": "Medicina C", "slot": "almuerzo"},
            ],
            "avoid": [],
        },
    )
    listed = auth_client.get(URL).json()["items"][0]
    assert listed["context"]["stale"] is True
    assert listed["context"]["item"]["name"] == "Medicina A"
    preview = auth_client.post(f"{URL}/{row.id}/preview").json()
    assert preview["stale"] is True and preview["version_no"] == 2
    # A preview of an older version is refused at approval time.
    old = auth_client.post(f"{URL}/{row.id}/approve", json={"expected_version": 1})
    assert old.status_code == 409
    resp = auth_client.post(f"{URL}/{row.id}/approve", json={"expected_version": 2})
    assert resp.status_code == 200, resp.text
    version, items = _current_names(auth_client, papa)
    assert version == 3
    assert items["Medicina A"]["quantity"] == "2"
    assert "Medicina C" in items  # the in-between change survives


def test_med_change_stop_and_item_gone(auth_client, test_db, papa, med_list):
    b_id = med_list["Medicina B"]["id"]
    stop = _suggest(
        test_db,
        "med_change",
        {"action": "stop", "medication": "Medicina B", "item_id": b_id},
        patient_id=papa.id,
    )
    again = _suggest(
        test_db,
        "med_change",
        {
            "action": "change",
            "medication": "Medicina B",
            "item_id": b_id,
            "item": {"time": "20:00"},
        },
        patient_id=papa.id,
    )
    assert auth_client.post(f"{URL}/{stop.id}/approve").status_code == 200
    assert "Medicina B" not in _current_names(auth_client, papa)[1]
    assert test_db.get(MedItem, b_id).end_date is not None  # ended, not deleted
    resp = auth_client.post(f"{URL}/{again.id}/approve")
    assert resp.status_code == 422
    assert "Medicina B" in resp.json()["detail"]
    test_db.refresh(again)
    assert again.status == "pending"


def test_med_change_edited_payload(auth_client, test_db, papa, med_list):
    a_id = med_list["Medicina A"]["id"]
    row = _suggest(
        test_db,
        "med_change",
        {
            "action": "change",
            "medication": "Medicina A",
            "item_id": a_id,
            "item": {"quantity": "2"},
        },
        patient_id=papa.id,
    )
    edited = {**row.payload, "item": {"quantity": "3"}}
    preview = auth_client.post(f"{URL}/{row.id}/preview", json={"payload": edited}).json()
    assert preview["diff"]["changed"]
    resp = auth_client.post(f"{URL}/{row.id}/approve", json={"payload": edited})
    assert resp.json()["status"] == "edited"
    assert _current_names(auth_client, papa)[1]["Medicina A"]["quantity"] == "3"


def test_preview_only_for_med_change(auth_client, test_db, papa):
    row = _suggest(test_db, "create_entry", _entry_payload(papa))
    assert auth_client.post(f"{URL}/{row.id}/preview").status_code == 400
    assert auth_client.post(f"{URL}/999/preview").status_code == 404


# ── Doctors ──────────────────────────────────────────────────────────────────


def test_approve_doctor(auth_client, test_db, member):
    row = _suggest(test_db, "doctor", {"name": "Dr. Nuevo Prueba", "specialty": "Neurología"})
    resp = auth_client.post(f"{URL}/{row.id}/approve")
    assert resp.status_code == 200, resp.text
    doctor = test_db.get(Doctor, resp.json()["result_ref"]["id"])
    assert doctor.specialty == "Neurología" and doctor.created_by == member.id
    assert _audit(test_db, "doctor")[0].user_id == member.id


def test_doctor_duplicate_and_link(auth_client, test_db):
    existing = Doctor(name="Dra. Ana Prueba", aliases=["Anita"])
    test_db.add(existing)
    test_db.commit()
    row = _suggest(test_db, "doctor", {"name": "ana prueba"})
    listed = auth_client.get(URL).json()["items"][0]
    assert listed["context"]["duplicates"][0]["id"] == existing.id
    resp = auth_client.post(f"{URL}/{row.id}/approve")
    assert resp.status_code == 409
    assert resp.json()["doctor"] == {"id": existing.id, "name": "Dra. Ana Prueba"}
    linked = auth_client.post(f"{URL}/{row.id}/approve", json={"doctor_id": existing.id})
    assert linked.status_code == 200, linked.text
    assert linked.json()["result_ref"] == {"entity": "doctor", "id": existing.id, "linked": True}
    assert test_db.scalar(select(Doctor.id).where(Doctor.id != existing.id)) is None


def test_doctor_id_only_for_doctor_kind(auth_client, test_db, papa):
    row = _suggest(test_db, "create_entry", _entry_payload(papa))
    assert auth_client.post(f"{URL}/{row.id}/approve", json={"doctor_id": 1}).status_code == 400


def test_lab_report_has_no_apply_path(auth_client, test_db):
    row = Suggestion(kind="lab_report", payload={}, source="whatsapp")
    test_db.add(row)
    test_db.commit()
    assert auth_client.post(f"{URL}/{row.id}/approve").status_code == 422
    assert auth_client.post(f"{URL}/{row.id}/reject").status_code == 200


# ── Hardening ────────────────────────────────────────────────────────────────


def test_med_change_name_must_match_item(test_db, papa, med_list):
    a_id = med_list["Medicina A"]["id"]
    with pytest.raises(svc.SuggestionError, match="no coincide"):
        svc.create_suggestion(
            test_db,
            kind="med_change",
            payload={"action": "stop", "medication": "Medicina B", "item_id": a_id},
            rationale=None,
            patient_id=papa.id,
        )
    with pytest.raises(svc.SuggestionError):  # unknown item
        svc.create_suggestion(
            test_db,
            kind="med_change",
            payload={"action": "stop", "medication": "Medicina A", "item_id": 999_999},
            rationale=None,
            patient_id=papa.id,
        )
    # Accents, case and a strength suffix do not matter.
    row = _suggest(
        test_db,
        "med_change",
        {"action": "stop", "medication": "MEDICINA Á 250 mg", "item_id": a_id},
        patient_id=papa.id,
    )
    assert row.status == "pending"


def test_med_change_mismatch_at_approval(auth_client, test_db, papa, med_list):
    a_id = med_list["Medicina A"]["id"]
    row = _suggest(
        test_db,
        "med_change",
        {"action": "stop", "medication": "Medicina A", "item_id": a_id},
        patient_id=papa.id,
    )
    # Someone renames the item since: the proposal no longer points at it.
    auth_client.put(
        _meds_url(papa),
        json={
            "expected_version": 1,
            "items": [
                {"id": a_id, "name": "Otra cosa", "slot": "desayuno"},
                {"id": med_list["Medicina B"]["id"], "name": "Medicina B", "slot": "cena"},
            ],
            "avoid": [],
        },
    )
    assert auth_client.post(f"{URL}/{row.id}/preview").status_code == 422
    resp = auth_client.post(f"{URL}/{row.id}/approve")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "La sugerencia no coincide con el medicamento actual."
    test_db.refresh(row)
    assert row.status == "pending"
    assert "Otra cosa" in _current_names(auth_client, papa)[1]


def test_producers_cannot_set_attachment_ids(test_db, papa):
    from app.services import log_entries

    with pytest.raises(svc.SuggestionError, match="archivos"):
        svc.create_suggestion(
            test_db,
            kind="create_entry",
            payload=_entry_payload(papa, attachment_ids=[]),
            rationale=None,
        )
    entry = log_entries.create_entry(
        test_db, _entry_payload(papa, entry_date=date(2026, 4, 30)), user_id=None
    )
    test_db.commit()
    with pytest.raises(svc.SuggestionError, match="archivos"):
        svc.create_suggestion(
            test_db,
            kind="update_entry",
            payload={"entry_id": entry.id, "changes": {"attachment_ids": []}},
            rationale=None,
        )


def test_preview_of_decided_suggestion(auth_client, test_db, papa, med_list):
    row = _suggest(
        test_db,
        "med_change",
        {
            "action": "change",
            "medication": "Medicina B",
            "item_id": med_list["Medicina B"]["id"],
            "item": {"quantity": "1"},
        },
        patient_id=papa.id,
    )
    auth_client.post(f"{URL}/{row.id}/reject")
    assert auth_client.post(f"{URL}/{row.id}/preview").status_code == 409


def test_unchanged_form_submit_stays_approved(auth_client, test_db, papa):
    # What EntrySheet sends for an untouched new entry: every field, nulls, no files.
    entry = _suggest(test_db, "create_entry", _entry_payload(papa, entry_time="10:00"))
    form = {
        **_entry_payload(papa),
        "entry_time": "10:00",
        "doctor_id": None,
        "source_label": None,
        "channel": None,
        "family_notes": None,
        "attachment_ids": [],
    }
    resp = auth_client.post(f"{URL}/{entry.id}/approve", json={"payload": form})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    # AppointmentSheet sends the Panamá offset, duration and nulls.
    appt = _suggest(
        test_db,
        "create_appointment",
        {"patient_id": papa.id, "starts_at": "2026-11-03T09:00:00", "reason": "Control"},
    )
    form = {
        "patient_id": papa.id,
        "starts_at": "2026-11-03T09:00:00-05:00",
        "all_day": False,
        "duration_min": 30,
        "doctor_id": None,
        "location": None,
        "reason": "Control",
        "notes": None,
    }
    resp = auth_client.post(f"{URL}/{appt.id}/approve", json={"payload": form})
    assert resp.json()["status"] == "approved"
    other = _suggest(test_db, "create_appointment", {**form, "reason": "Otro"})
    moved = {**form, "reason": "Otro", "starts_at": "2026-11-03T10:00:00-05:00"}
    resp = auth_client.post(f"{URL}/{other.id}/approve", json={"payload": moved})
    assert resp.json()["status"] == "edited"
