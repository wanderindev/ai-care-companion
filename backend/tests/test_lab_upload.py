"""Manual lab upload and analyte review (#15).

Synthetic data only: generated PDFs, fake "photos" (valid magic bytes plus
filler), made-up names, IDs and values. Claude is always a fake client.
"""

import base64
import hashlib

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.main import app
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.job import Job
from app.models.lab import Analyte, LabExtraction, LabReport, LabResult
from app.services import jobs
from app.services.attachments import HEIC, JPEG, OGG, PDF, PNG, store_upload
from app.services.labs import extract
from app.services.labs.analytes import analyte_references
from app.services.labs.matching import NO_MATCH, apply_patient_hint, match_patient
from app.services.labs.process import extraction_key
from tests.test_labs import (  # noqa: F401 — fixtures used by name
    LAB_OUTPUT,
    NOT_LAB,
    FakeMessages,
    _pdf,
    _respond,
    analytes,
    claude,
    lab_env,
    named_patients,
)

URL = "/api/v1/lab-reports/extract"


def _photo(marker: str, kind=JPEG) -> bytes:
    head = b"\xff\xd8\xff\xe0" if kind is JPEG else b"\x89PNG\r\n\x1a\n"
    return head + f"FOTO FICTICIA {marker}".encode() * 20


def _store(db, storage, data: bytes, file_type, name="pagina") -> Attachment:
    attachment = store_upload(
        db,
        storage,
        data=data,
        file_type=file_type,
        filename=f"{name}.{file_type.ext}",
        user_id=None,
    )
    db.commit()
    return attachment


@pytest.fixture
def upload_env(lab_env):
    app.dependency_overrides[get_settings] = lambda: lab_env.settings
    yield lab_env
    app.dependency_overrides.pop(get_settings, None)


def _upload(client, db, env, ids, patient_id=None) -> tuple[Job, dict]:
    resp = client.post(URL, json={"attachment_ids": ids, "patient_id": patient_id})
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert jobs.run_next(env.factory) is True
    db.expire_all()
    outcome = client.get(f"{URL}/{job_id}")
    assert outcome.status_code == 200
    return db.get(Job, job_id), outcome.json()


# ── POST /lab-reports/extract: auth, CSRF, validation ──────────────────────


def test_extract_requires_the_csrf_header(auth_client, test_db, upload_env):
    pdf = _store(test_db, upload_env.storage, _pdf(), PDF)
    auth_client.headers.pop("X-Requested-With")
    assert auth_client.post(URL, json={"attachment_ids": [pdf.id]}).status_code == 403
    assert test_db.scalar(select(func.count()).select_from(Job)) == 0


def test_extract_signed_out_is_401(db_client, test_db, storage):
    resp = db_client.post(URL, json={"attachment_ids": [1]}, headers={"X-Requested-With": "x"})
    assert resp.status_code == 401


def test_extract_validation(auth_client, test_db, upload_env, patients, monkeypatch):
    storage = upload_env.storage
    pdf = _store(test_db, storage, _pdf("A"), PDF)
    pdf2 = _store(test_db, storage, _pdf("B"), PDF)
    jpg = _store(test_db, storage, _photo("1"), JPEG)
    jpg_same = _store(test_db, storage, _photo("1"), JPEG)
    heic = _store(test_db, storage, b"\x00\x00\x00\x18ftypheic" + b"x" * 64, HEIC)
    ogg = _store(test_db, storage, b"OggS" + b"x" * 64, OGG)

    def post(ids, **extra):
        return auth_client.post(URL, json={"attachment_ids": ids, **extra})

    assert post([]).status_code == 422
    assert post([999999]).status_code == 404
    assert post([pdf.id, pdf.id]).status_code == 400
    assert post([pdf.id, pdf2.id]).json()["detail"].startswith("Sube un solo PDF")
    assert post([pdf.id, jpg.id]).status_code == 400
    assert "HEIC" in post([heic.id]).json()["detail"]
    assert post([ogg.id]).status_code == 400
    assert post([jpg.id, jpg_same.id]).json()["detail"] == "Hay fotos repetidas."
    assert post([pdf.id], patient_id=999999).json()["detail"] == "El paciente no existe."

    # Files that belong to another record (a bitácora photo) are refused;
    # files already on a lab report are fine.
    bitacora = _store(test_db, storage, _photo("bitacora"), JPEG)
    bitacora.owner_type, bitacora.owner_id = "log_entry", 1
    test_db.commit()
    refused = post([bitacora.id])
    assert refused.status_code == 400 and "otro registro" in refused.json()["detail"]
    jpg.owner_type, jpg.owner_id = "lab_report", 1
    test_db.commit()
    assert post([jpg.id, bitacora.id]).status_code == 400

    many = [_store(test_db, storage, _photo(f"p{i}"), JPEG).id for i in range(11)]
    assert "máximo 10" in post(many).json()["detail"]

    small = Settings(anthropic_api_key="test-key", lab_pdf_max_mb=1)
    big = _store(test_db, storage, _photo("big") + b"0" * (1024 * 1024), JPEG)
    app.dependency_overrides[get_settings] = lambda: small
    assert post([big.id]).status_code == 413
    app.dependency_overrides[get_settings] = lambda: Settings(anthropic_api_key="")
    assert post([pdf.id]).status_code == 503
    app.dependency_overrides[get_settings] = lambda: upload_env.settings

    assert test_db.scalar(select(func.count()).select_from(Job)) == 0
    resp = post([pdf.id], patient_id=patients[0].id)
    assert resp.status_code == 202
    job = test_db.get(Job, resp.json()["job_id"])
    # Ids only in the payload.
    assert job.payload == {"attachment_ids": [pdf.id], "patient_id": patients[0].id}
    assert job.kind == "lab_extract"


# ── photos through the pipeline ─────────────────────────────────────────────


def test_photos_are_one_report_in_page_order(auth_client, test_db, upload_env, named_patients):
    papa, _ = named_patients
    storage = upload_env.storage
    page1 = _store(test_db, storage, _photo("1"), JPEG)
    page2 = _store(test_db, storage, _photo("2", PNG), PNG)

    job, outcome = _upload(auth_client, test_db, upload_env, [page2.id, page1.id])
    assert (job.status, job.message) == ("done", "Informe de laboratorio guardado")

    (call,) = upload_env.claude.messages.calls
    content = call["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert content[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[0]["source"]["data"]) == _photo("2", PNG)
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(content[1]["source"]["data"]) == _photo("1")
    assert content[2]["text"] == extract.USER_TEXT_IMAGES

    report = test_db.scalar(select(LabReport))
    assert outcome == {
        "job_id": job.id,
        "job_status": "done",
        "outcome": "lab_report",
        "report_id": report.id,
    }
    assert (report.source, report.attachment_id) == ("upload", page2.id)
    assert (report.patient_id, report.patient_match, report.status) == (papa.id, "cip", "ok")
    for page in (page1, page2):
        test_db.refresh(page)
        assert (page.owner_type, page.owner_id) == ("lab_report", report.id)

    expected_key = hashlib.sha256(f"lab-pages:{page2.sha256},{page1.sha256}".encode()).hexdigest()
    assert extraction_key([page2, page1]) == expected_key
    extraction = test_db.scalar(select(LabExtraction))
    assert (extraction.sha256, extraction.attachment_id) == (expected_key, page2.id)

    detail = auth_client.get(f"/api/v1/lab-reports/{report.id}").json()
    assert [a["id"] for a in detail["attachments"]] == [page2.id, page1.id]
    assert detail["attachment"]["id"] == page2.id


def test_same_pages_are_never_read_twice(auth_client, test_db, upload_env, named_patients):
    storage = upload_env.storage
    a, b = (_store(test_db, storage, _photo(m), JPEG) for m in "ab")
    _, first = _upload(auth_client, test_db, upload_env, [a.id, b.id])
    assert len(upload_env.claude.messages.calls) == 1

    # The same photos uploaded again: new rows, same bytes, same order.
    a2, b2 = (_store(test_db, storage, _photo(m), JPEG) for m in "ab")
    job, again = _upload(auth_client, test_db, upload_env, [a2.id, b2.id])
    assert len(upload_env.claude.messages.calls) == 1
    assert job.message == "Estas fotos ya se habían procesado"
    assert again["outcome"] == "lab_report" and again["report_id"] == first["report_id"]

    # Another page order is another document.
    _upload(auth_client, test_db, upload_env, [b.id, a.id])
    assert len(upload_env.claude.messages.calls) == 2

    # A single photo keys on its own SHA-256, like a PDF.
    assert extraction_key([a]) == a.sha256


def test_photos_that_are_not_a_lab_report(auth_client, test_db, upload_env):
    upload_env.claude.messages = FakeMessages(_respond(NOT_LAB))
    photo = _store(test_db, upload_env.storage, _photo("x"), JPEG)
    job, outcome = _upload(auth_client, test_db, upload_env, [photo.id])
    assert job.message == "Las fotos no son de un informe de laboratorio"
    assert outcome["outcome"] == "not_lab_report" and outcome["report_id"] is None
    assert test_db.scalar(select(func.count()).select_from(LabReport)) == 0


def test_failed_photo_read_has_a_generic_message(auth_client, test_db, upload_env):
    upload_env.claude.messages = FakeMessages(lambda params: ("refusal", ""))
    photo = _store(test_db, upload_env.storage, _photo("r"), JPEG)
    job, outcome = _upload(auth_client, test_db, upload_env, [photo.id])
    assert job.status == "failed"
    assert "No se pudieron leer las fotos automáticamente." in job.error
    assert outcome["outcome"] == "failed"


def test_uploaded_pdf_uses_the_document_block(auth_client, test_db, upload_env, named_patients):
    pdf = _store(test_db, upload_env.storage, _pdf(), PDF)
    job, outcome = _upload(auth_client, test_db, upload_env, [pdf.id])
    assert job.message == "Informe de laboratorio guardado" and outcome["report_id"]
    (call,) = upload_env.claude.messages.calls
    assert call["messages"][0]["content"][0]["type"] == "document"
    assert test_db.scalar(select(LabExtraction)).sha256 == pdf.sha256


def test_outcome_endpoint_only_for_upload_jobs(auth_client, test_db):
    other = jobs.enqueue(test_db, "lab_extract", {"attachment_id": 1})
    test_db.commit()
    assert auth_client.get(f"{URL}/{other.id}").status_code == 404
    assert auth_client.get(f"{URL}/999999").status_code == 404
    queued = jobs.enqueue(test_db, "lab_extract", {"attachment_ids": [1], "patient_id": None})
    test_db.commit()
    body = auth_client.get(f"{URL}/{queued.id}").json()
    assert (body["job_status"], body["outcome"], body["report_id"]) == ("queued", None, None)


# ── the patient hint ────────────────────────────────────────────────────────


def _hint(patients, hinted, name, id_number):
    match = match_patient(patients, name, id_number)
    return apply_patient_hint(patients, hinted, match, name, id_number)


def test_patient_hint_rules(test_db, named_patients):
    papa, mama = named_patients
    both = [papa, mama]
    # The document names the hinted patient: its own method wins.
    assert _hint(both, papa, "Juan Ficticio Prueba", "8-111-2222").match == (papa, "cip")
    # The document points to the other parent: never assigned, flagged.
    result = _hint(both, mama, "Juan Ficticio Prueba", "8-111-2222")
    assert result.match == NO_MATCH and result.conflict
    assert _hint(both, mama, None, "8-111-2222").conflict
    # Nothing identifying: the hint assigns (manual).
    assert _hint(both, mama, None, None).match == (mama, "manual")
    # Only part of the hinted patient's name (married name omitted, or the
    # shared surname): the hint decides.
    assert _hint(both, mama, "Rosa Ejemplo", None).match == (mama, "manual")
    assert _hint(both, papa, "Prueba", None).match == (papa, "manual")
    # A name the hinted patient does not have, or an unknown ID: review.
    assert _hint(both, papa, "Rosa Ejemplo", None).match == NO_MATCH
    assert _hint(both, papa, "Pedro Otro", None).match == NO_MATCH
    assert _hint(both, papa, None, "8-999-9999").match == NO_MATCH


@pytest.mark.parametrize(
    "hint, name, id_number, expected",
    [
        ("mama", "PRUEBA FICTICIO, JUAN", "8-0111-02222", (None, None, "needs_review")),
        ("papa", "PRUEBA FICTICIO, JUAN", "8-0111-02222", ("papa", "cip", "ok")),
        ("mama", None, None, ("mama", "manual", "needs_review")),
        ("papa", "Persona Desconocida", None, (None, None, "needs_review")),
    ],
)
def test_patient_hint_in_the_pipeline(
    auth_client, test_db, upload_env, named_patients, hint, name, id_number, expected
):
    papa, mama = named_patients
    by_slug = {"papa": papa, "mama": mama}
    upload_env.claude.messages = FakeMessages(
        _respond({**LAB_OUTPUT, "patient_name": name, "patient_id_number": id_number})
    )
    photo = _store(test_db, upload_env.storage, _photo(hint + (name or "")), JPEG)
    _upload(auth_client, test_db, upload_env, [photo.id], patient_id=by_slug[hint].id)
    report = test_db.scalar(select(LabReport))
    patient = by_slug[expected[0]].id if expected[0] else None
    assert (report.patient_id, report.patient_match, report.status) == (
        patient,
        expected[1],
        expected[2],
    )


# ── analyte review ──────────────────────────────────────────────────────────

ANALYTES = "/api/v1/analytes"


@pytest.fixture
def admin_client(auth_client, member, test_db):
    member.role = "admin"
    test_db.commit()
    return auth_client


@pytest.fixture
def catalog(test_db, patients):
    papa, _ = patients
    glu = Analyte(canonical_name="Glucosa", aliases=["GLU"], specimen="sangre", default_unit=None)
    new = Analyte(
        canonical_name="Glucosa basal",
        aliases=["Glicemia basal", "glu"],
        specimen="sangre",
        default_unit="mg/dL",
        category="Química",
        needs_review=True,
    )
    urine = Analyte(canonical_name="Glucosa en orina", specimen="orina", needs_review=True)
    test_db.add_all([glu, new, urine])
    test_db.flush()
    report = LabReport(patient_id=papa.id, source="upload", status="ok", patient_match="cip")
    test_db.add(report)
    test_db.flush()
    rows = [
        LabResult(
            report_id=report.id,
            position=0,
            raw_name="GLUCOSA BASAL",
            value_text="98",
            unit="mg/dL",
            analyte_id=new.id,
        ),
        LabResult(
            report_id=report.id,
            position=1,
            raw_name="Glucosa basal",
            value_text="101",
            unit="mg/dL",
            analyte_id=new.id,
        ),
        LabResult(
            report_id=report.id, position=2, raw_name="Glucosa", value_text="90", analyte_id=glu.id
        ),
    ]
    test_db.add_all(rows)
    test_db.commit()
    return {"glu": glu, "new": new, "urine": urine, "results": rows}


def test_list_analytes_for_review(auth_client, catalog):
    items = auth_client.get(ANALYTES, params={"needs_review": "true"}).json()
    assert [i["canonical_name"] for i in items] == ["Glucosa basal", "Glucosa en orina"]
    basal = items[0]
    assert basal["result_count"] == 2 and basal["specimen"] == "sangre"
    assert basal["examples"][0] == {
        "raw_name": "Glucosa basal",
        "value_text": "101",
        "unit": "mg/dL",
    }
    everything = auth_client.get(ANALYTES).json()
    assert len(everything) == 3


def test_analyte_writes_are_admin_only(auth_client, catalog):
    new = catalog["new"]
    assert (
        auth_client.patch(f"{ANALYTES}/{new.id}", json={"needs_review": False}).status_code == 403
    )
    resp = auth_client.post(f"{ANALYTES}/{catalog['glu'].id}/merge", json={"source_id": new.id})
    assert resp.status_code == 403


def test_confirm_and_rename(admin_client, test_db, catalog, member):
    new = catalog["new"]
    resp = admin_client.patch(
        f"{ANALYTES}/{new.id}",
        json={"needs_review": False, "canonical_name": "  Glucosa en ayunas ", "category": ""},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_review"] is False and body["canonical_name"] == "Glucosa en ayunas"
    # The old name stays as an alias so the printed name keeps matching.
    assert body["aliases"] == ["Glicemia basal", "glu", "Glucosa basal"]
    assert body["category"] is None and body["result_count"] == 2
    entry = test_db.scalar(select(AuditLog).where(AuditLog.entity == "analyte"))
    assert (entry.action, entry.user_id, entry.entity_id) == ("update", member.id, str(new.id))
    assert entry.before["needs_review"] is True and entry.after["needs_review"] is False

    # A name another analyte already has (canonical or alias): merge instead.
    taken = admin_client.patch(f"{ANALYTES}/{new.id}", json={"canonical_name": "GLU"})
    assert taken.status_code == 409 and "Combinar" in taken.json()["detail"]
    exact = admin_client.patch(f"{ANALYTES}/{new.id}", json={"canonical_name": "Glucosa"})
    assert exact.status_code == 409
    assert admin_client.patch(f"{ANALYTES}/999999", json={"needs_review": False}).status_code == 404
    assert admin_client.patch(f"{ANALYTES}/{new.id}", json={"specimen": "sudor"}).status_code == 422


def test_merge_repoints_results_and_merges_aliases(admin_client, test_db, catalog, member):
    glu, new = catalog["glu"], catalog["new"]
    new_id = new.id
    moved_ids = sorted(r.id for r in catalog["results"][:2])
    resp = admin_client.post(f"{ANALYTES}/{glu.id}/merge", json={"source_id": new_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Normalized dedupe: "glu" is the same alias as "GLU".
    assert body["aliases"] == ["GLU", "Glucosa basal", "Glicemia basal"]
    assert (body["default_unit"], body["category"], body["result_count"]) == (
        "mg/dL",
        "Química",
        3,
    )
    test_db.expire_all()
    assert test_db.get(Analyte, new_id) is None
    assert {r.analyte_id for r in test_db.scalars(select(LabResult))} == {glu.id}

    merge, delete = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "analyte").order_by(AuditLog.id)
    ).all()
    assert (merge.action, merge.entity_id, merge.user_id) == ("merge", str(glu.id), member.id)
    assert merge.after["moved"] == {"lab_results.analyte_id": moved_ids}
    assert merge.before["source"]["canonical_name"] == "Glucosa basal"
    assert (delete.action, delete.entity_id, delete.after) == (
        "delete",
        str(new_id),
        {"merged_into": glu.id},
    )
    # The duplicate is gone: merging it again is a 404.
    again = admin_client.post(f"{ANALYTES}/{glu.id}/merge", json={"source_id": new_id})
    assert again.status_code == 404


def test_merge_specimen_mismatch_needs_confirmation(admin_client, test_db, catalog):
    glu, urine = catalog["glu"], catalog["urine"]
    url = f"{ANALYTES}/{glu.id}/merge"
    refused = admin_client.post(url, json={"source_id": urine.id})
    assert refused.status_code == 409 and "muestras son distintas" in refused.json()["detail"]
    assert test_db.get(Analyte, urine.id) is not None
    ok = admin_client.post(url, json={"source_id": urine.id, "allow_specimen_mismatch": True})
    assert ok.status_code == 200 and "Glucosa en orina" in ok.json()["aliases"]


def test_merge_rejections(admin_client, catalog):
    glu = catalog["glu"]
    assert (
        admin_client.post(f"{ANALYTES}/{glu.id}/merge", json={"source_id": glu.id}).status_code
        == 422
    )
    assert (
        admin_client.post(f"{ANALYTES}/999999/merge", json={"source_id": glu.id}).status_code == 404
    )


def test_merge_needs_the_csrf_header(admin_client, catalog):
    admin_client.headers.pop("X-Requested-With")
    resp = admin_client.post(
        f"{ANALYTES}/{catalog['glu'].id}/merge", json={"source_id": catalog["new"].id}
    )
    assert resp.status_code == 403


def test_analyte_references_discovers_fks():
    assert {f"{c.table.name}.{c.name}" for c in analyte_references()} == {"lab_results.analyte_id"}
