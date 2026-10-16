"""WhatsApp import (#12): chunked upload, import job, message and media dedupe.

Synthetic chats, names and files only; S3 is moto (``storage`` fixture).
"""

import io
import os
import zipfile

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.attachment import Attachment
from app.models.job import Job
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.whatsapp_import import after_import, pdf_messages, process_import, upload
from app.services.whatsapp_import import jobs as import_jobs
from app.services.whatsapp_import import process as import_process

URL = "/api/v1/imports/whatsapp"
CHUNK = 1024

PDF = b"%PDF-1.4\n" + os.urandom(1500)
JPG = b"\xff\xd8\xff\xe0" + os.urandom(1500)
OPUS = b"OggS" + os.urandom(1500)
MP4 = b"\x00\x00\x00\x18ftypisom" + os.urandom(1500)
DOCX = b"PK\x03\x04" + os.urandom(800)

WITH_MEDIA = "\n".join(
    [
        "1/3/24, 09:00 - Ana Prueba creó el grupo",
        "1/3/24, 09:01 - Ana Prueba: Hola, subo los resultados",
        "1/3/24, 09:02 - Beto Ficticio: LAB-0001.pdf (archivo adjunto)",
        "1/3/24, 09:03 - Beto Ficticio: IMG-20240301-WA0001.jpg (archivo adjunto)",
        "foto de la receta",
        "1/3/24, 09:04 - Ana Prueba: PTT-20240301-WA0002.opus (archivo adjunto)",
        "2/3/24, 10:00 - Ana Prueba: VID-20240302-WA0003.mp4 (archivo adjunto)",
        "2/3/24, 10:01 - Beto Ficticio: NOTAS.docx (archivo adjunto)",
        "2/3/24, 10:02 - Beto Ficticio: FALTA-0002.pdf (archivo adjunto)",
    ]
)
MEDIA_FILES = {
    "LAB-0001.pdf": PDF,
    "IMG-20240301-WA0001.jpg": JPG,
    "PTT-20240301-WA0002.opus": OPUS,
    "VID-20240302-WA0003.mp4": MP4,
    "NOTAS.docx": DOCX,
}
# The same period exported without media (Android): every file is a generic
# "<Multimedia omitido>", captions stay.
WITHOUT_MEDIA = "\n".join(
    [
        "1/3/24, 09:00 - Ana Prueba creó el grupo",
        "1/3/24, 09:01 - Ana Prueba: Hola, subo los resultados",
        "1/3/24, 09:02 - Beto Ficticio: <Multimedia omitido>",
        "1/3/24, 09:03 - Beto Ficticio: <Multimedia omitido>",
        "foto de la receta",
        "1/3/24, 09:04 - Ana Prueba: <Multimedia omitido>",
        "2/3/24, 10:00 - Ana Prueba: <Multimedia omitido>",
        "2/3/24, 10:01 - Beto Ficticio: <Multimedia omitido>",
        "2/3/24, 10:02 - Beto Ficticio: <Multimedia omitido>",
    ]
)


def _zip(chat: str, media: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("WhatsApp Chat with Familia Prueba.txt", chat)
        for name, data in (media or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    """1 KB chunks (moto accepts parts down to 256 bytes here)."""
    monkeypatch.setattr("moto.s3.models.S3_UPLOAD_PART_MIN_SIZE", 256)
    monkeypatch.setattr(upload, "chunk_bytes", lambda settings: CHUNK)


@pytest.fixture
def admin_client(auth_client, member, test_db):
    member.role = "admin"
    member.can_import = True
    test_db.commit()
    return auth_client


def _start(client, data: bytes, mode="backfill", **extra):
    resp = client.post(URL, json={"mode": mode, "filename": "chat.zip", "size": len(data), **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put(client, imp, index, data: bytes):
    chunk = data[index * imp["chunk_size"] : (index + 1) * imp["chunk_size"]]
    return client.put(
        f"{URL}/{imp['id']}/chunks/{index}",
        content=chunk,
        headers={"Content-Type": "application/octet-stream"},
    )


def _upload(client, data: bytes, mode="backfill", **extra) -> dict:
    imp = _start(client, data, mode, **extra)
    for index in range(imp["chunk_count"]):
        assert _put(client, imp, index, data).status_code == 200
    resp = client.post(f"{URL}/{imp['id']}/complete")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _process(test_db, storage, import_id, settings=None):
    seen = []
    imp = process_import(
        test_db, storage, settings or Settings(), import_id, lambda p, m: seen.append((p, m))
    )
    return imp, seen


def _messages(test_db):
    return list(test_db.scalars(select(WhatsappMessage).order_by(WhatsappMessage.sent_at)))


# ── upload ──────────────────────────────────────────────────────────────────


def test_chunks_out_of_order_with_retry_then_complete(admin_client, test_db, storage):
    data = _zip(WITH_MEDIA, MEDIA_FILES)
    imp = _start(admin_client, data, patient_focus_id=None)
    assert imp["status"] == "uploading" and imp["chunk_size"] == CHUNK
    assert imp["chunk_count"] == -(-len(data) // CHUNK) and imp["received_chunks"] == []

    order = list(reversed(range(imp["chunk_count"])))
    for index in order[:-1]:
        assert _put(admin_client, imp, index, data).status_code == 200
    # A dropped connection: the same chunk again replaces the first copy.
    assert _put(admin_client, imp, order[0], data).status_code == 200

    resp = admin_client.post(f"{URL}/{imp['id']}/complete")
    assert resp.status_code == 409  # chunk 0 is missing
    received = admin_client.get(f"{URL}/{imp['id']}").json()["received_chunks"]
    assert received == list(range(1, imp["chunk_count"]))

    assert _put(admin_client, imp, 0, data).status_code == 200
    done = admin_client.post(f"{URL}/{imp['id']}/complete").json()
    assert done["status"] == "queued" and done["job_id"]
    # Completing twice is harmless.
    again = admin_client.post(f"{URL}/{imp['id']}/complete").json()
    assert again["job_id"] == done["job_id"]

    job = test_db.get(Job, done["job_id"])
    assert (job.kind, job.payload, job.status) == (
        "whatsapp_import",
        {"import_id": imp["id"]},
        "queued",
    )
    row = test_db.get(WhatsappImport, imp["id"])
    assert row.upload_id is None and row.staging_key.startswith("archives/")
    body = storage._s3.get_object(Bucket=storage.bucket, Key=row.staging_key)["Body"].read()
    assert body == data


def test_chunk_validation(admin_client, storage):
    data = _zip(WITH_MEDIA, MEDIA_FILES)
    imp = _start(admin_client, data)
    last = imp["chunk_count"] - 1
    url = f"{URL}/{imp['id']}/chunks"
    headers = {"Content-Type": "application/octet-stream"}
    assert (
        admin_client.put(f"{url}/{last + 1}", content=b"x" * 10, headers=headers).status_code == 400
    )
    assert admin_client.put(f"{url}/0", content=b"x" * 10, headers=headers).status_code == 400
    too_big = admin_client.put(f"{url}/0", content=b"x" * (CHUNK + 1), headers=headers)
    assert too_big.status_code == 413
    assert admin_client.put(f"{URL}/999/chunks/0", content=b"x", headers=headers).status_code == 404


def test_total_size_limit_and_patient_focus(admin_client, storage, patients):
    resp = admin_client.post(
        URL, json={"mode": "daily", "filename": "chat.zip", "size": 9000 * 1024 * 1024}
    )
    assert resp.status_code == 413
    resp = admin_client.post(
        URL, json={"mode": "daily", "filename": "chat.zip", "size": 10, "patient_focus_id": 999}
    )
    assert resp.status_code == 400
    resp = admin_client.post(URL, json={"mode": "nope", "filename": "chat.zip", "size": 10})
    assert resp.status_code == 422
    ok = _start(admin_client, b"x" * 10, mode="backfill", patient_focus_id=patients[1].id)
    assert ok["patient_focus_id"] == patients[1].id and ok["chunk_count"] == 1


def test_admin_only_and_csrf(auth_client, admin_client, test_db, member, storage):
    member.role = "member"
    test_db.commit()
    assert auth_client.get(URL).status_code == 403
    resp = auth_client.post(URL, json={"mode": "daily", "filename": "a.zip", "size": 10})
    assert resp.status_code == 403
    member.role = "admin"
    test_db.commit()
    resp = admin_client.post(
        URL,
        json={"mode": "daily", "filename": "a.zip", "size": 10},
        headers={"X-Requested-With": ""},
    )
    assert resp.status_code == 403
    assert test_db.scalar(select(func.count()).select_from(WhatsappImport)) == 0


def test_cancel_aborts_the_upload(admin_client, test_db, storage):
    data = _zip(WITH_MEDIA, MEDIA_FILES)
    imp = _start(admin_client, data)
    assert _put(admin_client, imp, 0, data).status_code == 200
    resp = admin_client.delete(f"{URL}/{imp['id']}")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert _put(admin_client, imp, 1, data).status_code == 409
    assert admin_client.post(f"{URL}/{imp['id']}/complete").status_code == 409
    uploads = storage._s3.list_multipart_uploads(Bucket=storage.bucket).get("Uploads", [])
    assert uploads == []


# ── the import job ──────────────────────────────────────────────────────────


def test_backfill_import_stores_messages_and_media(admin_client, test_db, storage, member):
    data = _zip(WITH_MEDIA, MEDIA_FILES)
    imp = _upload(admin_client, data, mode="backfill")
    row, progress = _process(test_db, storage, imp["id"])

    assert row.status == "done" and progress[-1] == (100, "Listo")
    assert all(m is None or "Ana" not in m for _, m in progress)
    messages = _messages(test_db)
    assert len(messages) == 7  # the system line is not stored
    by_kind = {
        m.media_kind: m for m in messages if m.media_kind and "FALTA" not in m.media_filename
    }
    assert by_kind["pdf"].attachment_id and by_kind["image"].text == "foto de la receta"
    for kind in ("pdf", "image", "audio", "video"):
        attachment = test_db.get(Attachment, by_kind[kind].attachment_id)
        assert (attachment.owner_type, attachment.owner_id) == (
            "whatsapp_message",
            by_kind[kind].id,
        )
    assert test_db.get(Attachment, by_kind["video"].attachment_id).mime == "video/mp4"
    assert by_kind["other"].attachment_id is None  # .docx: unsupported

    assert row.stats == {
        "messages": {"total": 7, "new": 7, "duplicate": 0, "system_skipped": 1},
        "media": {
            "uploaded": {"audio": 1, "image": 1, "pdf": 1, "video": 1},
            "missing": {"pdf": 1},
            "unsupported": {"other": 1},
        },
        "unparsed_lines": 0,
        "unreferenced_files": 0,
        "date_order": "dmy",
    }
    assert row.range_from.isoformat() == "2024-03-01T14:00:00+00:00"
    assert row.range_to.isoformat() == "2024-03-02T15:02:00+00:00"
    # The zip is kept as an attachment owned by the import.
    zip_row = test_db.get(Attachment, row.attachment_id)
    assert (zip_row.owner_type, zip_row.owner_id, zip_row.mime) == (
        "whatsapp_import",
        row.id,
        "application/zip",
    )
    assert row.staging_key is None and zip_row.size == len(data)
    assert [m.id for m in pdf_messages(test_db, row.id)] == [by_kind["pdf"].id]

    listed = admin_client.get(URL).json()
    assert listed[0]["status"] == "done" and listed[0]["uploaded_by_name"] == member.display_name
    assert listed[0]["stats"]["messages"]["new"] == 7


def test_labs_only_uploads_only_pdfs(admin_client, test_db, storage):
    imp = _upload(admin_client, _zip(WITH_MEDIA, MEDIA_FILES), mode="labs_only")
    row, _ = _process(test_db, storage, imp["id"])
    messages = _messages(test_db)
    assert len(messages) == 7
    assert [m.media_kind for m in messages if m.attachment_id] == ["pdf"]
    assert row.stats["media"] == {
        "uploaded": {"pdf": 1},
        "skipped_mode": {"audio": 1, "image": 1, "other": 1, "video": 1},
        "missing": {"pdf": 1},
    }


def test_reupload_of_the_same_zip_is_harmless(admin_client, test_db, storage):
    data = _zip(WITH_MEDIA, MEDIA_FILES)
    first, _ = _process(test_db, storage, _upload(admin_client, data)["id"])
    second_out = _upload(admin_client, data)
    staging = test_db.get(WhatsappImport, second_out["id"]).staging_key
    second, _ = _process(test_db, storage, second_out["id"])

    assert len(_messages(test_db)) == 7
    assert second.stats["messages"] == {
        "total": 7,
        "new": 0,
        "duplicate": 7,
        "system_skipped": 1,
    }
    assert second.stats["media"]["already_stored"] == {"audio": 1, "image": 1, "pdf": 1, "video": 1}
    assert "uploaded" not in second.stats["media"]
    # The zip dedupes: two rows, one object; the second upload's object is gone.
    zips = [
        test_db.get(Attachment, first.attachment_id),
        test_db.get(Attachment, second.attachment_id),
    ]
    assert zips[0].storage_key == zips[1].storage_key and zips[0].sha256 == zips[1].sha256
    assert not storage.exists(staging)
    assert pdf_messages(test_db, second.id) == []


def test_without_media_then_with_media_attaches_files_to_existing(admin_client, test_db, storage):
    bare, _ = _process(test_db, storage, _upload(admin_client, _zip(WITHOUT_MEDIA))["id"])
    assert bare.stats["messages"]["new"] == 7
    assert bare.stats["media"] == {"omitted": {"other": 6}}
    assert all(m.attachment_id is None for m in _messages(test_db))

    full, _ = _process(
        test_db,
        storage,
        _upload(admin_client, _zip(WITH_MEDIA, MEDIA_FILES), mode="labs_only")["id"],
    )
    messages = _messages(test_db)
    assert len(messages) == 7  # nothing duplicated
    assert full.stats["messages"]["new"] == 0 and full.stats["messages"]["duplicate"] == 7
    assert full.stats["media"]["added_to_existing"] == {"pdf": 1}
    pdf = next(m for m in messages if m.attachment_id)
    assert pdf.import_id == bare.id and pdf.media_import_id == full.id
    assert (pdf.media_kind, pdf.media_filename) == ("pdf", "LAB-0001.pdf")
    assert [m.id for m in pdf_messages(test_db, full.id)] == [pdf.id]

    # A later backfill of the same export adds the remaining kinds.
    rest, _ = _process(test_db, storage, _upload(admin_client, _zip(WITH_MEDIA, MEDIA_FILES))["id"])
    assert rest.stats["media"]["added_to_existing"] == {"audio": 1, "image": 1, "video": 1}
    assert rest.stats["media"]["already_stored"] == {"pdf": 1}


LRM = "‎"


@pytest.mark.parametrize(
    ("omitted", "pages", "tag"),
    [("documento omitido", "2 páginas", "adjunto"), ("document omitted", "1 page", "attached")],
)
def test_without_media_then_with_media_ios_documents(
    admin_client, test_db, storage, omitted, pages, tag
):
    """iOS writes a document as "<name> • N páginas" before the marker."""
    lines = [
        "[1/3/24, 09:01:00] Ana Prueba: Hola",
        "[1/3/24, 09:02:00] Beto Ficticio: {LRM}Informe Prueba.pdf • {LRM}{pages} {LRM}{media}",
        "[1/3/24, 09:03:00] Beto Ficticio: {LRM}imagen omitida",
    ]
    bare_chat = "\n".join(lines).format(LRM=LRM, pages=pages, media=omitted)
    full_chat = (
        "\n".join(lines[:2]).format(
            LRM=LRM, pages=pages, media=f"<{tag}: 00000013-Informe Prueba.pdf>"
        )
        + f"\n[1/3/24, 09:03:00] Beto Ficticio: {LRM}<{tag}: 00000014-PHOTO-1.jpg>"
    )
    bare, _ = _process(test_db, storage, _upload(admin_client, _zip(bare_chat))["id"])
    assert bare.stats["media"] == {"omitted": {"image": 1, "pdf": 1}}

    media = {"00000013-Informe Prueba.pdf": PDF, "00000014-PHOTO-1.jpg": JPG}
    full, _ = _process(
        test_db, storage, _upload(admin_client, _zip(full_chat, media), mode="labs_only")["id"]
    )
    messages = _messages(test_db)
    assert len(messages) == 3  # nothing duplicated
    assert full.stats["messages"]["new"] == 0
    assert full.stats["media"]["added_to_existing"] == {"pdf": 1}
    assert full.stats["media"]["skipped_mode"] == {"image": 1}
    pdf = next(m for m in messages if m.attachment_id)
    assert pdf.import_id == bare.id and pdf.text == ""
    assert [m.id for m in pdf_messages(test_db, full.id)] == [pdf.id]


def test_overlapping_exports_store_each_message_once(admin_client, test_db, storage):
    older = "\n".join(WITH_MEDIA.split("\n")[:3])
    newer = "\n".join([*WITH_MEDIA.split("\n")[1:3], "3/3/24, 08:00 - Ana Prueba: mensaje nuevo"])
    _process(test_db, storage, _upload(admin_client, _zip(older, MEDIA_FILES))["id"])
    second, _ = _process(test_db, storage, _upload(admin_client, _zip(newer, MEDIA_FILES))["id"])
    assert len(_messages(test_db)) == 3
    assert second.stats["messages"] == {"total": 3, "new": 1, "duplicate": 2, "system_skipped": 0}
    assert second.stats["unreferenced_files"] == 4


def test_media_over_the_per_file_cap_is_skipped(admin_client, test_db, storage):
    big = {**MEDIA_FILES, "VID-20240302-WA0003.mp4": MP4 + os.urandom(1024 * 1024)}
    imp = _upload(admin_client, _zip(WITH_MEDIA, big))
    row, _ = _process(test_db, storage, imp["id"], Settings(whatsapp_media_max_mb=1))
    assert row.stats["media"]["too_large"] == {"video": 1}
    assert row.stats["media"]["uploaded"] == {"audio": 1, "image": 1, "pdf": 1}


def test_not_a_whatsapp_export_fails_for_good(admin_client, test_db, storage):
    imp = _upload(admin_client, b"esto no es un zip" * 100)
    staging = test_db.get(WhatsappImport, imp["id"]).staging_key
    with pytest.raises(jobs.PermanentJobError):
        _process(test_db, storage, imp["id"])
    row = test_db.get(WhatsappImport, imp["id"])
    assert row.status == "failed" and row.staging_key is None
    assert not storage.exists(staging)


def test_worker_runs_the_job_and_hooks(admin_client, test_db, test_engine, storage, monkeypatch):
    calls = []
    monkeypatch.setattr(import_process, "AFTER_IMPORT_HOOKS", [])
    after_import(lambda db, imp: calls.append((imp.id, [m.id for m in pdf_messages(db, imp.id)])))
    monkeypatch.setattr(import_jobs, "get_storage", lambda: storage)
    imp = _upload(admin_client, _zip(WITH_MEDIA, MEDIA_FILES), mode="labs_only")

    factory = sessionmaker(bind=test_engine, autoflush=False)
    assert jobs.run_next(factory) is True
    test_db.expire_all()
    job = test_db.get(Job, imp["job_id"])
    assert (job.status, job.progress, job.error) == ("done", 100, None)
    assert test_db.get(WhatsappImport, imp["id"]).status == "done"
    assert len(calls) == 1 and calls[0][0] == imp["id"] and len(calls[0][1]) == 1

    # A job that gave up shows the import as failed.
    job.status = "failed"
    row = test_db.get(WhatsappImport, imp["id"])
    row.status = "processing"
    test_db.commit()
    assert admin_client.get(f"{URL}/{imp['id']}").json()["status"] == "failed"


def test_retry_after_a_crash_mid_media_counts_once(admin_client, test_db, storage, monkeypatch):
    imp = _upload(admin_client, _zip(WITH_MEDIA, MEDIA_FILES))
    real = import_process.store_file
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ConnectionError("storage down")
        return real(*args, **kwargs)

    monkeypatch.setattr(import_process, "store_file", flaky)
    monkeypatch.setattr(import_process, "COMMIT_EVERY_FILES", 1)
    with pytest.raises(ConnectionError):
        _process(test_db, storage, imp["id"])
    test_db.rollback()
    row, _ = _process(test_db, storage, imp["id"])
    assert row.status == "done"
    assert row.stats["messages"]["new"] == 7
    assert row.stats["media"]["uploaded"] == {"audio": 1, "image": 1, "pdf": 1, "video": 1}
    owned = test_db.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(Attachment.owner_type == "whatsapp_message")
    )
    assert owned == 4


# ── day/month order (#117) ──────────────────────────────────────────────────

# Stored on 9 October (a day > 12 makes the file day/month).
BASELINE = "\n".join(
    [
        "9/10/26, 10:00 - Ana Prueba: hola",
        "9/10/26, 10:05 - Beto Ficticio: b",
        "13/10/26, 9:00 - Ana Prueba: x",
    ]
)
# A US-order sync export of 9-10 October: every date reads both ways.
SYNC_US = "\n".join(
    [
        "10/9/26, 10:00 - Ana Prueba: hola",
        "10/9/26, 10:05 - Beto Ficticio: b",
        "10/9/26, 11:00 - Ana Prueba: único",
        "10/9/26, 11:30 - Beto Ficticio: transcrito",
        "10/9/26, 12:00 - Ana Prueba: ok",
        "10/9/26, 12:00 - Ana Prueba: ok",
        "10/10/26, 8:00 - Ana Prueba: c",
    ]
)


def _days(test_db, import_id):
    rows = test_db.scalars(
        select(WhatsappMessage)
        .where(WhatsappMessage.import_id == import_id)
        .order_by(WhatsappMessage.sent_at, WhatsappMessage.id)
    )
    return [m.sent_at.astimezone(import_process.PANAMA_TZ).date().isoformat() for m in rows]


def test_date_order_hint_is_validated_and_breaks_a_tie(admin_client, test_db, storage):
    data = _zip(SYNC_US)
    resp = admin_client.post(
        URL, json={"mode": "daily", "filename": "c.zip", "size": len(data), "date_order": "xyz"}
    )
    assert resp.status_code == 422
    imp = _upload(admin_client, data, mode="daily", date_order="mdy")
    row, _ = _process(test_db, storage, imp["id"])
    assert row.stats["date_order"] == "mdy" and row.stats["date_order_hint"] == "mdy"
    assert set(_days(test_db, imp["id"])) == {"2026-10-09", "2026-10-10"}


def test_daily_import_of_swapped_twins_is_refused(admin_client, test_db, storage):
    base = _upload(admin_client, _zip(BASELINE))
    _process(test_db, storage, base["id"])
    before = test_db.scalar(select(func.count()).select_from(WhatsappMessage))
    # The uploader's order is wrong: 10/9 read as 10 September. Most new
    # messages repeat 9 October's at the same minute: nothing is stored.
    chat = "\n".join(SYNC_US.split("\n")[:2] + ["10/10/26, 8:00 - Ana Prueba: c"])
    imp = _upload(admin_client, _zip(chat), mode="daily", date_order="dmy")
    with pytest.raises(jobs.PermanentJobError, match="ambiguo"):
        _process(test_db, storage, imp["id"])
    assert test_db.get(WhatsappImport, imp["id"]).status == "failed"
    assert test_db.scalar(select(func.count()).select_from(WhatsappMessage)) == before
    # The same file in the right order is a plain overlap.
    imp = _upload(admin_client, _zip(chat), mode="daily", date_order="mdy")
    row, _ = _process(test_db, storage, imp["id"])
    assert row.status == "done" and row.stats["messages"] == {
        "total": 3,
        "new": 1,
        "duplicate": 2,
        "system_skipped": 0,
    }


def test_fix_date_order_cli(admin_client, test_db, test_engine, storage, monkeypatch, capsys):
    from app.models.audit import AuditLog
    from app.services.whatsapp import parse_chat_text
    from scripts import whatsapp_imports as cli

    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=test_engine))
    base = _upload(admin_client, _zip(BASELINE))
    _process(test_db, storage, base["id"])
    # A backfill (no guard) read in the wrong order, like production's import 7.
    bad = _upload(admin_client, _zip(SYNC_US), mode="backfill", date_order="dmy")
    _process(test_db, storage, bad["id"])
    assert _days(test_db, bad["id"]).count("2026-09-10") == 6
    transcribed = test_db.scalar(
        select(WhatsappMessage).where(WhatsappMessage.text == "transcrito")
    )
    transcribed.transcript = "texto"
    row = test_db.get(WhatsappImport, bad["id"])
    row.stats = {**row.stats, "analysis": {"days_done": ["2026-09-10", "2026-10-10"]}}
    test_db.commit()
    hashes = set(test_db.scalars(select(WhatsappMessage.dedupe_hash)))

    args = ["fix-date-order", str(bad["id"]), "--from", "dmy", "--to", "mdy"]
    assert cli.main([*args, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would delete as duplicates: 2" in out and "Would re-date: 3" in out
    assert f"Refused: 1 (transcript 1); message ids: {transcribed.id}" in out
    assert "Dry run" in out and "Prueba" not in out and "único" not in out
    test_db.expire_all()
    assert set(test_db.scalars(select(WhatsappMessage.dedupe_hash))) == hashes

    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "Deleted as duplicates: 2" in out and "Re-dated: 3" in out
    assert "Analysis days cleared: 1" in out
    test_db.expire_all()
    assert _days(test_db, bad["id"]) == ["2026-09-10"] + ["2026-10-09"] * 3 + ["2026-10-10"]
    # The re-dated rows now hash as the right-order export would.
    right = {m.dedupe_hash for m in parse_chat_text(SYNC_US, date_order="mdy").messages}
    stored = set(test_db.scalars(select(WhatsappMessage.dedupe_hash)))
    assert len(right & stored) == 6  # all but the refused one
    stats = test_db.get(WhatsappImport, bad["id"]).stats
    assert stats["analysis"]["days_done"] == ["2026-10-10"]
    audited = test_db.scalars(select(AuditLog.action).where(AuditLog.entity == "whatsapp_message"))
    assert sorted(audited) == ["delete", "delete", "update", "update", "update"]
    assert stats["date_order"] == "mdy" and stats["date_order_fixed_from"] == "dmy"
    # Running it again is refused (it would swap the dates back).
    assert cli.main(args) == 1
    assert "already" in capsys.readouterr().out
    assert cli.main(["fix-date-order", "999999", "--from", "dmy", "--to", "mdy"]) == 1
