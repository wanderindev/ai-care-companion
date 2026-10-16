"""Sync API for the WhatsApp Web extension (#43): token auth, CORS, last
message, missing media, media uploads for stored messages, completion.

Synthetic chats, names and files only; S3 is moto (``storage`` fixture).
"""

import io
import os
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.services.daily_analysis.jobs as djobs
import app.services.media_classification.jobs  # noqa: F401  -- registers its hooks
import app.services.transcription.jobs  # noqa: F401  -- registers its hooks
from app.core.config import Settings
from app.main import app, install_extension_cors
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.extension_token import ExtensionToken
from app.models.job import Job
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.auth import hash_token
from app.services.whatsapp_import import media_upload, process_import, upload
from app.services.whatsapp_import import process as import_process

URL = "/api/v1/imports/whatsapp"
TOKEN_URL = "/api/v1/extension/token"
CHUNK = 1024
ORIGIN = "chrome-extension://" + "abcdefghijklmnop" * 2

PDF = b"%PDF-1.4\n" + os.urandom(1500)
JPG = b"\xff\xd8\xff\xe0" + os.urandom(1500)
OPUS = b"OggS" + os.urandom(1500)
DOCX = b"PK\x03\x04" + os.urandom(800)

# A WhatsApp Web export: text only, every file "<… omitted>". Two identical
# image messages in the same minute are "twins".
WEB = "\n".join(
    [
        "[6/17/25, 3:45:12 PM] Ana Prueba: Hola",
        "[6/17/25, 3:46:00 PM] Ana Prueba: <voice message omitted>",
        "[6/17/25, 3:47:00 PM] Beto Ficticio: <image omitted> receta nueva",
        "[6/18/25, 9:01:00 AM] Beto Ficticio: <document omitted> Lab Prueba 0001.pdf",
        "[6/18/25, 9:05:00 AM] Ana Prueba: <image omitted>",
        "[6/18/25, 9:05:00 AM] Ana Prueba: <image omitted>",
    ]
)


def _zip(chat: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("WhatsApp Chat with Familia Prueba.txt", chat)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr("moto.s3.models.S3_UPLOAD_PART_MIN_SIZE", 256)
    monkeypatch.setattr(upload, "chunk_bytes", lambda settings: CHUNK)


@pytest.fixture
def admin_client(auth_client, member, test_db):
    member.role = "admin"
    member.can_import = True
    test_db.commit()
    return auth_client


@pytest.fixture
def token(admin_client):
    resp = admin_client.post(TOKEN_URL)
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


@pytest.fixture
def ext(db_client, token):
    """A cookie-less client authenticated only with the extension's token."""
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        c.headers["X-Requested-With"] = "n16chat-extension"
        yield c


def _upload_export(client, data: bytes, **extra) -> dict:
    resp = client.post(
        URL, json={"mode": "daily", "filename": "chat.zip", "size": len(data), **extra}
    )
    assert resp.status_code == 201, resp.text
    imp = resp.json()
    for index in range(imp["chunk_count"]):
        chunk = data[index * imp["chunk_size"] : (index + 1) * imp["chunk_size"]]
        put = client.put(
            f"{URL}/{imp['id']}/chunks/{index}",
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert put.status_code == 200, put.text
    done = client.post(f"{URL}/{imp['id']}/complete")
    assert done.status_code == 200, done.text
    return done.json()


def _run_import(test_db, storage, import_id):
    imp = process_import(test_db, storage, Settings(), import_id, lambda p, m: None)
    test_db.commit()
    return imp


def _media_jobs(db, *, deadline: bool = False) -> list[Job]:
    db.expire_all()
    rows = db.scalars(select(Job).where(Job.kind == "whatsapp_media").order_by(Job.id))
    return [j for j in rows if bool(j.payload.get("deadline")) == deadline]


def _only_runnable(db, kind: str, *, deadline: bool = False) -> None:
    """Park every other queued job; make the matching ones due now."""
    for job in db.scalars(select(Job).where(Job.status == "queued")):
        if job.kind == kind and bool(job.payload.get("deadline")) == deadline:
            job.run_after = datetime.now(UTC) - timedelta(seconds=1)
        else:
            job.status = "done"
    db.commit()


def _media(client, import_id, message_id, data, name="file.bin"):
    return client.post(
        f"{URL}/{import_id}/messages/{message_id}/media",
        files={"file": (name, data, "application/octet-stream")},
    )


# ── Token ────────────────────────────────────────────────────────────────────


def test_token_lifecycle_is_hashed_and_audited_without_secret(admin_client, test_db, member):
    assert admin_client.get(TOKEN_URL).json() == {
        "active": False,
        "created_at": None,
        "last_used_at": None,
    }
    created = admin_client.post(TOKEN_URL)
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    first = body["token"]
    assert first.startswith("n16x_") and len(first) > 40
    assert body["api_base"].endswith("/api/v1")
    status = admin_client.get(TOKEN_URL).json()
    assert status["active"] and "token" not in status

    (row,) = test_db.scalars(select(ExtensionToken)).all()
    assert row.token_hash == hash_token(first) and first not in row.token_hash

    second = admin_client.post(TOKEN_URL).json()["token"]
    assert second != first
    test_db.expire_all()
    rows = test_db.scalars(select(ExtensionToken).order_by(ExtensionToken.id)).all()
    assert [r.revoked_at is None for r in rows] == [False, True]

    assert admin_client.delete(TOKEN_URL).status_code == 204
    assert admin_client.get(TOKEN_URL).json()["active"] is False

    audits = test_db.scalars(select(AuditLog).where(AuditLog.entity == "extension_token")).all()
    assert len(audits) == 4  # create, revoke + create, revoke
    dump = repr([(a.before, a.after) for a in audits])
    for secret in (first, second, hash_token(first), hash_token(second)):
        assert secret not in dump


def test_token_endpoints_need_a_cookie_admin(auth_client, test_db, member, token):
    member.role = "member"
    test_db.commit()
    assert auth_client.get(TOKEN_URL).status_code == 403
    assert auth_client.post(TOKEN_URL).status_code == 403
    # A token cannot mint or read tokens.
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}", "X-Requested-With": "x"})
        assert c.get(TOKEN_URL).status_code == 401
        assert c.post(TOKEN_URL).status_code == 401


# ── Bearer auth on the import endpoints ─────────────────────────────────────


def test_bearer_token_opens_only_the_import_endpoints(ext, test_db, token):
    assert ext.get(URL).status_code == 200
    assert ext.get(f"{URL}/last").status_code == 200
    # Everything else still needs the session cookie.
    assert ext.get("/api/v1/patients").status_code == 401
    assert ext.get("/api/v1/auth/me").status_code == 401
    test_db.expire_all()
    row = test_db.scalars(select(ExtensionToken)).one()
    assert row.last_used_at is not None


def test_bearer_token_rejections(ext, admin_client, test_db, member, token, storage):
    # The CSRF rule still holds for token requests.
    resp = ext.post(
        URL,
        json={"mode": "daily", "filename": "c.zip", "size": 10},
        headers={"X-Requested-With": ""},
    )
    assert resp.status_code == 403

    for header in ("Bearer nope", "Basic abc", "Bearer ", f"Token {token}"):
        r = admin_client.get(URL, headers={"Authorization": header})
        # A bad header is judged alone, even next to a valid admin cookie.
        assert r.status_code == 401, header
        assert "WWW-Authenticate" in r.headers

    member.role = "member"
    test_db.commit()
    assert ext.get(URL).status_code == 403  # no longer an admin
    member.role = "admin"
    member.is_active = False
    test_db.commit()
    assert ext.get(URL).status_code == 401  # deactivated user
    member.is_active = True
    test_db.commit()
    assert ext.get(URL).status_code == 200
    assert admin_client.delete(TOKEN_URL).status_code == 204
    assert ext.get(URL).status_code == 401  # revoked


# ── CORS ─────────────────────────────────────────────────────────────────────


def test_extension_origins_setting_keeps_only_valid_ids():
    settings = Settings(
        extension_origins=f" {ORIGIN}/ , https://evil.example, chrome-extension://short, *"
    )
    assert settings.extension_origin_list == (ORIGIN,)
    assert Settings().extension_origin_list == ()


def test_cors_only_for_the_configured_extension():
    def make(origins: str) -> TestClient:
        target = FastAPI()

        @target.get("/x")
        def x():
            return {"ok": True}

        install_extension_cors(target, Settings(extension_origins=origins))
        return TestClient(target)

    preflight = {
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization, x-requested-with, content-type",
    }
    client = make(ORIGIN)
    resp = client.options("/x", headers=preflight)
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in resp.headers
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()
    got = client.get("/x", headers={"Origin": ORIGIN})
    assert got.headers["access-control-allow-origin"] == ORIGIN
    other = client.get("/x", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers

    # Default (empty): no CORS at all.
    plain = make("")
    assert "access-control-allow-origin" not in plain.get("/x", headers={"Origin": ORIGIN}).headers
    assert plain.options("/x", headers=preflight).status_code == 405


# ── The extension's run ─────────────────────────────────────────────────────


def test_last_message(ext, test_db, storage):
    assert ext.get(f"{URL}/last").json() == {
        "last_message_at": None,
        "last_import_id": None,
        "last_import_finished_at": None,
    }
    imp = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    body = ext.get(f"{URL}/last").json()
    # 9:05 AM Panamá, returned on the Panamá clock.
    assert body["last_message_at"] == "2025-06-18T09:05:00-05:00"
    assert body["last_import_id"] == imp["id"] and body["last_import_finished_at"]


def test_full_run_lists_attaches_and_completes(ext, test_db, storage, token):
    started = _upload_export(ext, _zip(WEB), expects_media=True)
    assert started["awaiting_media"] is True
    import_id = started["id"]

    # Not processed yet: listing works, uploading waits for the import.
    msgs_url = f"{URL}/media-missing?import_id={import_id}"
    assert ext.get(msgs_url).json() == {"items": [], "has_more": False}
    _run_import(test_db, storage, import_id)

    listing = ext.get(msgs_url).json()
    assert listing["has_more"] is False
    items = listing["items"]
    assert [i["media_kind"] for i in items] == ["audio", "image", "pdf", "image", "image"]
    audio, image, pdf, twin_a, twin_b = items
    assert audio["sent_at"] == "2025-06-17T15:46:00-05:00"
    assert image["sender"] == "Beto Ficticio" and image["caption"] == "receta nueva"
    assert pdf["media_filename"] == "Lab Prueba 0001.pdf"
    assert (twin_a["twin_index"], twin_a["twin_count"]) == (0, 2)
    assert (twin_b["twin_index"], twin_b["twin_count"]) == (1, 2)
    assert (image["twin_index"], image["twin_count"]) == (0, 1)
    assert [i["id"] for i in ext.get(f"{msgs_url}&kind=image&limit=1").json()["items"]] == [
        image["id"]
    ]
    assert ext.get(f"{msgs_url}&limit=2").json()["has_more"] is True
    assert ext.get(f"{msgs_url}&kind=nope").status_code == 422

    # Uploads: wrong kind, unknown type, then the right files.
    assert _media(ext, import_id, image["id"], PDF).status_code == 422
    assert _media(ext, import_id, image["id"], DOCX).status_code == 415
    assert _media(ext, import_id, image["id"], b"").status_code == 400
    assert _media(ext, import_id, 999_999, JPG).status_code == 404
    text_only = test_db.scalar(select(WhatsappMessage).where(WhatsappMessage.text == "Hola"))
    assert _media(ext, import_id, text_only.id, JPG).status_code == 409

    first = _media(ext, import_id, image["id"], JPG, "IMG-1.jpg")
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True and first.json()["media_kind"] == "image"
    again = _media(ext, import_id, image["id"], JPG)
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["attachment_id"] == first.json()["attachment_id"]
    assert _media(ext, import_id, image["id"], JPG + b"x").status_code == 409
    assert _media(ext, import_id, audio["id"], OPUS).status_code == 201
    assert _media(ext, import_id, pdf["id"], PDF).status_code == 201

    test_db.expire_all()
    row = test_db.get(WhatsappMessage, image["id"])
    stored = test_db.get(Attachment, row.attachment_id)
    assert row.media_import_id == import_id
    assert (stored.owner_type, stored.owner_id) == ("whatsapp_message", row.id)
    # The exported file name wins over the uploaded one.
    pdf_row = test_db.get(WhatsappMessage, pdf["id"])
    assert test_db.get(Attachment, pdf_row.attachment_id).filename == "Lab Prueba 0001.pdf"
    remaining = ext.get(msgs_url).json()["items"]
    assert [i["id"] for i in remaining] == [twin_a["id"], twin_b["id"]]
    audit_rows = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "whatsapp_message")
    ).all()
    assert len(audit_rows) == 3 and "receta" not in repr([a.after for a in audit_rows])

    # Completion queues the media job; no more uploads afterwards.
    done = ext.post(f"{URL}/{import_id}/media/complete")
    assert done.status_code == 200 and done.json()["media_completed_at"]
    assert done.json()["awaiting_media"] is True  # until the job ran
    assert ext.post(f"{URL}/{import_id}/media/complete").status_code == 200  # idempotent
    assert [j.payload for j in _media_jobs(test_db)] == [{"import_id": import_id}]
    # The deadline job queued when the import finished (it will find the run
    # completed and do nothing).
    (deadline,) = _media_jobs(test_db, deadline=True)
    assert deadline.payload == {"import_id": import_id, "deadline": True}
    assert _media(ext, import_id, twin_a["id"], JPG).status_code == 409

    # The job runs the after_media hooks: the same processing as a with-media
    # export (transcription for the audio, classification for photo + PDF).
    factory = sessionmaker(bind=test_db.get_bind(), autoflush=False)
    import app.services.whatsapp_import.jobs  # noqa: F401  -- registers whatsapp_media

    for other in test_db.scalars(select(Job).where(Job.kind != "whatsapp_media")):
        other.status = "done"  # the import job (run by hand above) and the analysis
    test_db.commit()
    assert jobs.run_next(factory)
    test_db.expire_all()
    imp = test_db.get(WhatsappImport, import_id)
    assert imp.awaiting_media is False
    assert imp.stats["media"]["uploaded"] == {"audio": 1, "image": 1, "pdf": 1}
    assert imp.stats["media"]["omitted"]  # from the text import, kept
    kinds = sorted(
        (j.kind, j.payload["attachment_id"])
        for j in test_db.scalars(
            select(Job).where(Job.kind.in_(("transcribe_audio", "classify_media")))
        )
    )
    expected = sorted(
        [
            ("transcribe_audio", test_db.get(WhatsappMessage, audio["id"]).attachment_id),
            ("classify_media", test_db.get(WhatsappMessage, image["id"]).attachment_id),
            ("classify_media", test_db.get(WhatsappMessage, pdf["id"]).attachment_id),
        ]
    )
    assert kinds == expected
    # Running it again changes nothing.
    media_upload.finish_media(test_db, import_id)
    assert len(test_db.scalars(select(Job).where(Job.kind == "transcribe_audio")).all()) == 1


def test_twins_are_grouped_at_the_minute(ext, test_db, storage):
    # Two caption-less voice notes in one minute with different seconds (a
    # Web export) plus one in the next minute: WhatsApp Web shows only h:mm.
    chat = "\n".join(
        [
            "[6/17/25, 3:46:10 PM] Ana Prueba: <voice message omitted>",
            "[6/17/25, 3:46:52 PM] Ana Prueba: <voice message omitted>",
            "[6/17/25, 3:47:01 PM] Ana Prueba: <voice message omitted>",
        ]
    )
    imp = _upload_export(ext, _zip(chat), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    items = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    assert [(i["twin_index"], i["twin_count"]) for i in items] == [(0, 2), (1, 2), (0, 1)]
    assert items[0]["sent_at"] == "2025-06-17T15:46:10-05:00"
    # A twin that already has its file still counts.
    assert _media(ext, imp["id"], items[0]["id"], OPUS).status_code == 201
    rest = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    assert [(i["twin_index"], i["twin_count"]) for i in rest] == [(1, 2), (0, 1)]


def test_twins_are_per_media_kind(ext, test_db, storage):
    # A photo and caption-less voice notes from one sender in one minute:
    # the extension counts bubbles of the item's kind, so a photo is never a
    # voice note's twin (else a wrong bubble could fill the count).
    chat = "\n".join(
        [
            "[6/17/25, 9:41:05 AM] Ana Prueba: <image omitted>",
            "[6/17/25, 9:41:30 AM] Ana Prueba: <voice message omitted>",
            "[6/17/25, 9:41:50 AM] Ana Prueba: <voice message omitted>",
        ]
    )
    imp = _upload_export(ext, _zip(chat), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    items = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    assert [(i["media_kind"], i["twin_index"], i["twin_count"]) for i in items] == [
        ("image", 0, 1),
        ("audio", 0, 2),
        ("audio", 1, 2),
    ]


def test_wa_key_is_recorded_and_guards_the_match(ext, test_db, storage):
    imp = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    items = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    assert all(i["wa_key"] is None for i in items)
    audio, image, *_ = items
    bubble = "AB" * 16  # a synthetic data-id

    def send(message_id, data, wa_key):
        return ext.post(
            f"{URL}/{imp['id']}/messages/{message_id}/media",
            files={"file": ("f.bin", data, "application/octet-stream")},
            data={"wa_key": wa_key},
        )

    assert send(audio["id"], OPUS, "abc").status_code == 422
    assert send(audio["id"], OPUS, bubble.lower()).status_code == 422
    assert send(audio["id"], OPUS, bubble).status_code == 201
    test_db.expire_all()
    assert test_db.get(WhatsappMessage, audio["id"]).wa_key == bubble
    # A retry with the same key and bytes is fine; another key is not.
    assert send(audio["id"], OPUS, bubble).status_code == 200
    other_key = send(audio["id"], OPUS, "F" * 32)
    assert other_key.status_code == 409
    assert other_key.headers["X-Error-Code"] == "wa_key_conflict"
    # The same bubble cannot become another message's file.
    assert send(image["id"], JPG, bubble).status_code == 409
    assert send(image["id"], JPG, "E" * 32).status_code == 201
    # Without wa_key it still works (the field is optional).
    assert _media(ext, imp["id"], items[2]["id"], PDF).status_code == 201
    test_db.expire_all()
    assert test_db.get(WhatsappMessage, items[2]["id"]).wa_key is None
    # A retry that now names the bubble records it, audited.
    assert send(items[2]["id"], PDF, "C" * 32).status_code == 200
    test_db.expire_all()
    assert test_db.get(WhatsappMessage, items[2]["id"]).wa_key == "C" * 32
    keyed = test_db.scalars(
        select(AuditLog)
        .where(AuditLog.entity == "whatsapp_message", AuditLog.entity_id == str(items[2]["id"]))
        .order_by(AuditLog.id)
    ).all()
    assert [a.after["wa_key"] for a in keyed] == [None, "C" * 32]


def test_wa_key_race_on_the_unique_index_is_a_conflict(ext, test_db, storage, monkeypatch):
    imp = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    audio, image, *_ = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    bubble = "CD" * 16

    def send(message_id, data):
        return ext.post(
            f"{URL}/{imp['id']}/messages/{message_id}/media",
            files={"file": ("f.bin", data, "application/octet-stream")},
            data={"wa_key": bubble},
        )

    assert send(audio["id"], OPUS).status_code == 201
    # As if a concurrent request had passed the check before this one saved.
    monkeypatch.setattr(media_upload, "wa_key_owner", lambda db, key, row_id: None)
    resp = send(image["id"], JPG)
    assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "wa_key_conflict"
    test_db.expire_all()
    assert test_db.get(WhatsappMessage, image["id"]).attachment_id is None


def test_other_integrity_errors_are_not_a_wa_key_conflict(ext, test_db, storage, monkeypatch):
    imp = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    (item, *_) = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]

    class Diag:
        constraint_name = "some_other_constraint"

    class Orig(Exception):
        diag = Diag()

    def broken(*args, **kwargs):
        raise IntegrityError("INSERT …", {}, Orig())

    monkeypatch.setattr(media_upload, "attach_media", broken)
    with pytest.raises(IntegrityError):
        ext.post(
            f"{URL}/{imp['id']}/messages/{item['id']}/media",
            files={"file": ("f.bin", OPUS, "application/octet-stream")},
            data={"wa_key": "EF" * 16},
        )


def test_closed_run_answers_a_stable_error_code(ext, test_db, storage):
    imp = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    (item, *_) = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    assert ext.post(f"{URL}/{imp['id']}/media/complete").status_code == 200
    resp = _media(ext, imp["id"], item["id"], OPUS)
    assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "run_closed"


def test_media_for_a_message_an_earlier_run_stored(ext, test_db, storage):
    first = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, first["id"])
    assert ext.post(f"{URL}/{first['id']}/media/complete").status_code == 200
    test_db.expire_all()
    assert test_db.get(WhatsappImport, first["id"]).awaiting_media is False  # no files: at once
    assert not _media_jobs(test_db)

    later = WEB + "\n[6/19/25, 8:00:00 AM] Ana Prueba: Buenos días"
    second = _upload_export(ext, _zip(later), expects_media=True)
    _run_import(test_db, storage, second["id"])
    old = ext.get(f"{URL}/media-missing?since=2025-06-17T00:00:00").json()["items"]
    assert len(old) == 5 and {i["import_id"] for i in old} == {first["id"]}
    # The new run's own list is empty: nothing new with media.
    assert ext.get(f"{URL}/media-missing?import_id={second['id']}").json()["items"] == []
    # Through a finished run: refused; through the current one: accepted.
    assert _media(ext, first["id"], old[0]["id"], OPUS).status_code == 409
    assert _media(ext, second["id"], old[0]["id"], OPUS).status_code == 201
    test_db.expire_all()
    row = test_db.get(WhatsappMessage, old[0]["id"])
    assert (row.import_id, row.media_import_id) == (first["id"], second["id"])


def test_media_only_for_extension_runs(ext, test_db, storage):
    imp = _upload_export(ext, _zip(WEB))  # a normal import (expects_media false)
    assert imp["awaiting_media"] is False
    _run_import(test_db, storage, imp["id"])
    (item, *_) = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    assert _media(ext, imp["id"], item["id"], OPUS).status_code == 409
    assert ext.post(f"{URL}/{imp['id']}/media/complete").status_code == 409
    assert ext.post(f"{URL}/999/media/complete").status_code == 404
    assert _media(ext, 999, item["id"], OPUS).status_code == 404


def test_upload_size_cap(ext, test_db, storage, monkeypatch):
    imp = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    (item, *_) = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    from app.api import whatsapp_imports as api_module

    small = Settings(whatsapp_media_max_mb=0)
    app.dependency_overrides[api_module.get_settings] = lambda: small
    try:
        resp = _media(ext, imp["id"], item["id"], OPUS)
    finally:
        app.dependency_overrides.pop(api_module.get_settings, None)
    assert resp.status_code == 413


def test_logs_carry_no_message_content(ext, test_db, storage, caplog):
    caplog.set_level("DEBUG", logger="app")
    imp = _upload_export(ext, _zip(WEB), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    items = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    _media(ext, imp["id"], items[1]["id"], JPG, "IMG-secreto.jpg")
    ext.post(f"{URL}/{imp['id']}/media/complete")
    media_upload.finish_media(test_db, imp["id"])
    for needle in ("receta", "Beto", "Ficticio", "secreto", "Lab Prueba", "n16x_"):
        assert needle not in caplog.text


# ── The daily analysis waits for the extension's files ──────────────────────


def _done_import(db, *, awaiting: bool, finished_ago: timedelta) -> WhatsappImport:
    imp = WhatsappImport(
        mode="daily",
        status="done",
        filename="c.zip",
        size=1,
        chunk_size=1,
        chunk_count=1,
        stats={"messages": {"total": 1, "new": 1, "duplicate": 0, "system_skipped": 0}},
        awaiting_media=awaiting,
        finished_at=datetime.now(UTC) - finished_ago,
    )
    db.add(imp)
    db.commit()
    return imp


def test_upload_window_is_bounded(test_db):
    waiting = _done_import(test_db, awaiting=True, finished_ago=timedelta(minutes=5))
    expired = _done_import(test_db, awaiting=True, finished_ago=media_upload.MEDIA_WAIT)
    assert not media_upload.window_over(waiting)
    assert media_upload.window_over(expired)
    media_upload.check_accepting(waiting)
    with pytest.raises(upload.UploadError) as exc:
        media_upload.check_accepting(expired)
    assert exc.value.status == 409


def test_analysis_requeues_while_the_extension_uploads(test_db, test_engine):
    imp = _done_import(test_db, awaiting=True, finished_ago=timedelta(minutes=5))
    djobs.enqueue_analysis(test_db, imp.id, waits=2)
    test_db.commit()
    assert jobs.run_next(sessionmaker(bind=test_engine, autoflush=False))
    test_db.expire_all()
    rows = list(test_db.scalars(select(Job).where(Job.kind == djobs.JOB_KIND).order_by(Job.id)))
    assert [j.status for j in rows] == ["done", "queued"]
    assert rows[1].payload == {"import_id": imp.id, "waits": 2}  # no wait spent
    assert test_db.get(WhatsappImport, imp.id).stats["analysis"]["status"] == "waiting"


def test_after_media_hooks_registered():
    from app.services.labs import jobs as lab_jobs
    from app.services.media_classification import jobs as mc_jobs
    from app.services.transcription import jobs as tr_jobs

    hooks = import_process.AFTER_MEDIA_HOOKS
    assert tr_jobs.enqueue_transcriptions in hooks
    assert mc_jobs.enqueue_classifications in hooks
    assert lab_jobs.enqueue_lab_extractions in hooks
    # Text-only hooks never run twice.
    assert lab_jobs.record_lab_links not in hooks
    assert djobs.enqueue_analysis_after_import not in hooks


# ── Runs the extension never completed ──────────────────────────────────────


def _factory(engine):
    import app.services.whatsapp_import.jobs  # noqa: F401  -- registers whatsapp_media

    return sessionmaker(bind=engine, autoflush=False)


def _started_run(ext, test_db, storage, chat=WEB) -> tuple[int, list[dict]]:
    imp = _upload_export(ext, _zip(chat), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    items = ext.get(f"{URL}/media-missing?import_id={imp['id']}").json()["items"]
    return imp["id"], items


def _transcribe_jobs(db) -> list[Job]:
    db.expire_all()
    return list(db.scalars(select(Job).where(Job.kind == "transcribe_audio")))


def test_deadline_job_finalizes_an_abandoned_run(ext, test_db, test_engine, storage):
    factory = _factory(test_engine)
    import_id, items = _started_run(ext, test_db, storage)
    (deadline,) = _media_jobs(test_db, deadline=True)
    soonest = datetime.now(UTC) + media_upload.MEDIA_WAIT - timedelta(minutes=1)
    assert deadline.run_after > soonest
    assert _media(ext, import_id, items[0]["id"], OPUS).status_code == 201
    # ...and the extension dies before «complete».
    _only_runnable(test_db, "whatsapp_media", deadline=True)
    assert jobs.run_next(factory)
    test_db.expire_all()
    imp = test_db.get(WhatsappImport, import_id)
    assert imp.media_completed_at is not None and imp.awaiting_media is True
    assert [j.payload for j in _media_jobs(test_db)] == [{"import_id": import_id}]
    assert _media(ext, import_id, items[1]["id"], JPG).status_code == 409  # finalized

    _only_runnable(test_db, "whatsapp_media")
    assert jobs.run_next(factory)
    test_db.expire_all()
    assert test_db.get(WhatsappImport, import_id).awaiting_media is False
    assert len(_transcribe_jobs(test_db)) == 1


def test_deadline_without_files_just_clears(ext, test_db, test_engine, storage):
    factory = _factory(test_engine)
    import_id, _items = _started_run(ext, test_db, storage)
    _only_runnable(test_db, "whatsapp_media", deadline=True)
    assert jobs.run_next(factory)
    test_db.expire_all()
    imp = test_db.get(WhatsappImport, import_id)
    assert imp.awaiting_media is False and imp.media_completed_at is not None
    assert not _media_jobs(test_db)


def test_uploads_refused_past_the_bound(ext, test_db, storage):
    import_id, items = _started_run(ext, test_db, storage)
    imp = test_db.get(WhatsappImport, import_id)
    imp.finished_at = datetime.now(UTC) - media_upload.MEDIA_WAIT - timedelta(minutes=1)
    test_db.commit()
    resp = _media(ext, import_id, items[0]["id"], OPUS)
    assert resp.status_code == 409 and "plazo" in resp.json()["detail"]
    # A late «complete» still works (it finalizes).
    assert ext.post(f"{URL}/{import_id}/media/complete").status_code == 200


def test_a_new_run_finalizes_the_older_ones(ext, test_db, storage):
    first_id, items = _started_run(ext, test_db, storage)
    assert _media(ext, first_id, items[0]["id"], OPUS).status_code == 201
    # A plain import does not touch extension runs.
    plain = ext.post(URL, json={"mode": "daily", "filename": "c.zip", "size": 10})
    assert plain.status_code == 201
    test_db.expire_all()
    assert test_db.get(WhatsappImport, first_id).media_completed_at is None
    # No «complete»; the next extension run starts.
    second = ext.post(
        URL, json={"mode": "daily", "filename": "c.zip", "size": 10, "expects_media": True}
    )
    assert second.status_code == 201
    test_db.expire_all()
    assert test_db.get(WhatsappImport, first_id).media_completed_at is not None
    assert [j.payload for j in _media_jobs(test_db)] == [{"import_id": first_id}]


def test_completing_a_run_finalizes_older_ones(ext, test_db, storage):
    first_id, _ = _started_run(ext, test_db, storage)
    second_id, _ = _started_run(ext, test_db, storage, WEB + "\n[6/20/25, 8:00:00 AM] Ana: y")
    # Starting the second run already finalized the first (no files: cleared).
    test_db.expire_all()
    first = test_db.get(WhatsappImport, first_id)
    assert first.awaiting_media is False and first.media_completed_at is not None
    # An older run still open (e.g. its finalization raced): «complete» closes it.
    first.awaiting_media, first.media_completed_at = True, None
    test_db.commit()
    assert ext.post(f"{URL}/{second_id}/media/complete").status_code == 200
    test_db.expire_all()
    first = test_db.get(WhatsappImport, first_id)
    assert first.awaiting_media is False and first.media_completed_at is not None


def test_analysis_past_the_bound_finalizes_and_waits_for_the_media_job(
    ext, test_db, test_engine, storage
):
    factory = _factory(test_engine)
    import_id, items = _started_run(ext, test_db, storage)
    assert _media(ext, import_id, items[0]["id"], OPUS).status_code == 201
    imp = test_db.get(WhatsappImport, import_id)
    imp.finished_at = datetime.now(UTC) - media_upload.MEDIA_WAIT - timedelta(minutes=1)
    test_db.commit()
    _only_runnable(test_db, djobs.JOB_KIND)
    assert jobs.run_next(factory)
    test_db.expire_all()
    imp = test_db.get(WhatsappImport, import_id)
    assert imp.media_completed_at is not None and imp.awaiting_media is True
    assert [j.payload for j in _media_jobs(test_db)] == [{"import_id": import_id}]
    queued = test_db.scalars(
        select(Job).where(Job.kind == djobs.JOB_KIND, Job.status == "queued")
    ).one()
    assert queued.payload == {"import_id": import_id, "waits": 1}  # a wait spent
    assert imp.stats["analysis"]["status"] == "waiting"


def test_analysis_past_the_bound_without_files_goes_ahead(test_db, test_engine, monkeypatch):
    from types import SimpleNamespace

    from app.services.daily_analysis import analyze

    monkeypatch.setattr(djobs, "get_settings", lambda: Settings(anthropic_api_key="test-key"))
    monkeypatch.setattr(analyze, "make_client", lambda key: SimpleNamespace())
    imp = _done_import(test_db, awaiting=True, finished_ago=timedelta(hours=2))
    djobs.enqueue_analysis(test_db, imp.id)
    test_db.commit()
    assert jobs.run_next(_factory(test_engine))
    test_db.expire_all()
    imp = test_db.get(WhatsappImport, imp.id)
    assert imp.awaiting_media is False and imp.media_completed_at is not None
    rows = test_db.scalars(select(Job).where(Job.kind == djobs.JOB_KIND)).all()
    assert [j.status for j in rows] == ["done"]
    assert imp.stats["analysis"]["status"] == "done"


WEB_WITH_MEDIA = "\n".join(
    [
        "[6/17/25, 3:45:12 PM] Ana Prueba: Hola",
        "[6/17/25, 3:46:00 PM] Ana Prueba: <attached: 00000001-AUDIO-2025-06-17.opus>",
    ]
)


def test_with_media_zip_in_an_extension_run_runs_media_hooks_once(ext, test_db, storage):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("_chat.txt", WEB_WITH_MEDIA)
        archive.writestr("00000001-AUDIO-2025-06-17.opus", OPUS)
    imp = _upload_export(ext, buffer.getvalue(), expects_media=True)
    _run_import(test_db, storage, imp["id"])
    test_db.expire_all()
    stored = test_db.scalars(
        select(WhatsappMessage).where(WhatsappMessage.attachment_id.is_not(None))
    ).all()
    assert len(stored) == 1
    assert _transcribe_jobs(test_db) == []  # not at the end of the import...
    assert ext.post(f"{URL}/{imp['id']}/media/complete").status_code == 200
    media_upload.finish_media(test_db, imp["id"])
    assert len(_transcribe_jobs(test_db)) == 1  # ...but once, at finish_media
    media_upload.finish_media(test_db, imp["id"])
    assert len(_transcribe_jobs(test_db)) == 1
