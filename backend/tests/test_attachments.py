"""Attachments: sniffing, dedupe, presigned URLs, linking. Synthetic bytes only."""

import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.api.attachments import storage_dep
from app.core.config import Settings, get_settings
from app.main import app
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.services import attachments as svc
from app.services.storage import Storage, StorageNotConfigured

JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.7\n%synthetic\n" + b"0" * 64


@pytest.mark.parametrize(
    ("data", "mime", "ext", "kind"),
    [
        (JPEG_BYTES, "image/jpeg", "jpg", "images"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/png", "png", "images"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", "webp", "images"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "image/heic", "heic", "images"),
        (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00", "image/heif", "heif", "images"),
        (PDF_BYTES, "application/pdf", "pdf", "documents"),
        (b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead", "audio/ogg", "ogg", "audio"),
        (b"ID3\x04\x00" + b"\x00" * 16, "audio/mpeg", "mp3", "audio"),
        (b"\xff\xfb\x90\x64" + b"\x00" * 16, "audio/mpeg", "mp3", "audio"),
        (b"\xff\xf1\x50\x80" + b"\x00" * 16, "audio/aac", "aac", "audio"),
        (b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00", "audio/mp4", "m4a", "audio"),
    ],
)
def test_sniff_allowed(data, mime, ext, kind):
    file_type = svc.sniff(data)
    assert (file_type.mime, file_type.ext, file_type.kind) == (mime, ext, kind)


@pytest.mark.parametrize(
    "data",
    [
        b"<html><script>alert(1)</script>",
        b"PK\x03\x04zip",
        b"GIF89a",
        b"\x00\x00\x00\x18ftypisom\x00\x00",  # mp4 video
        b"MZ\x90\x00",
        b"",
    ],
)
def test_sniff_rejected(data):
    assert svc.sniff(data) is None


def test_clean_filename():
    assert svc.clean_filename("C:\\fotos\\receta.jpg", svc.JPEG) == "receta.jpg"
    assert svc.clean_filename("../../etc/x.pdf", svc.PDF) == "x.pdf"
    assert svc.clean_filename("", svc.PDF) == "archivo.pdf"
    assert svc.clean_filename("a\x00b\n.jpg", svc.JPEG) == "ab.jpg"
    long = svc.clean_filename("x" * 300 + ".pdf", svc.PDF)
    assert len(long) == 200 and long.endswith(".pdf")


def _upload(client, data, name="receta.jpg", content_type="image/jpeg"):
    return client.post("/api/v1/attachments", files={"file": (name, data, content_type)})


def test_upload_requires_sign_in(db_client, storage):
    resp = db_client.post(
        "/api/v1/attachments",
        files={"file": ("a.jpg", JPEG_BYTES, "image/jpeg")},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 401
    assert db_client.get("/api/v1/attachments/1/url").status_code == 401


def test_upload_needs_csrf_header(auth_client, storage):
    resp = auth_client.post(
        "/api/v1/attachments",
        files={"file": ("a.jpg", JPEG_BYTES, "image/jpeg")},
        headers={"X-Requested-With": ""},
    )
    assert resp.status_code == 403


def test_upload_stores_and_audits(auth_client, test_db, storage, member):
    # The client's Content-Type is ignored: the bytes decide.
    resp = _upload(auth_client, PDF_BYTES, name="informe.bin", content_type="image/png")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    digest = hashlib.sha256(PDF_BYTES).hexdigest()
    assert body["mime"] == "application/pdf"
    assert body["sha256"] == digest
    assert body["size"] == len(PDF_BYTES)
    assert body["filename"] == "informe.bin"
    assert body["owner_type"] is None and body["owner_id"] is None

    row = test_db.get(Attachment, body["id"])
    year = datetime.now(UTC).year
    assert row.storage_key == f"documents/{year}/{digest}.pdf"
    assert row.uploaded_by == member.id
    obj = storage._s3.get_object(Bucket=storage.bucket, Key=row.storage_key)
    assert obj["Body"].read() == PDF_BYTES
    assert obj["ContentType"] == "application/pdf"

    entry = test_db.scalar(select(AuditLog))
    assert (entry.action, entry.entity, entry.entity_id, entry.user_id) == (
        "create",
        "attachment",
        str(row.id),
        member.id,
    )


def test_upload_dedupes_by_sha256(auth_client, test_db, storage, monkeypatch):
    first = _upload(auth_client, JPEG_BYTES, name="a.jpg").json()
    puts = []
    monkeypatch.setattr(storage, "put", lambda *a, **k: puts.append(a))
    second = _upload(auth_client, JPEG_BYTES, name="b.jpg").json()
    assert puts == []  # same bytes: no second upload
    assert first["id"] != second["id"]
    rows = test_db.scalars(select(Attachment).order_by(Attachment.id)).all()
    assert rows[0].storage_key == rows[1].storage_key
    assert [r.filename for r in rows] == ["a.jpg", "b.jpg"]


def test_upload_reuses_orphan_object(auth_client, storage, monkeypatch):
    """An object already in the bucket (row rolled back) is not uploaded again."""
    data = JPEG_BYTES + b"orphan"
    digest = hashlib.sha256(data).hexdigest()
    storage.put(f"images/{datetime.now(UTC).year}/{digest}.jpg", data, "image/jpeg")
    puts = []
    monkeypatch.setattr(storage, "put", lambda *a, **k: puts.append(a))
    assert _upload(auth_client, data).status_code == 201
    assert puts == []


def test_upload_rejects_bad_files(auth_client, storage, monkeypatch):
    resp = _upload(auth_client, b"<html>hola</html>", name="x.jpg")
    assert resp.status_code == 415
    assert _upload(auth_client, b"", name="x.jpg").status_code == 400

    monkeypatch.setattr(get_settings(), "attachment_max_mb", 1)
    resp = _upload(auth_client, JPEG_BYTES + b"\x00" * (1024 * 1024))
    assert resp.status_code == 413
    assert resp.json()["detail"] == "El archivo supera el límite de 1 MB."


def test_upload_storage_failure_is_503(auth_client, test_db, storage, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("synthetic outage")

    monkeypatch.setattr(storage, "put", boom)
    assert _upload(auth_client, JPEG_BYTES).status_code == 503
    assert test_db.scalars(select(Attachment)).all() == []


def test_storage_not_configured_is_503(auth_client):
    app.dependency_overrides.pop(storage_dep, None)
    assert not get_settings().storage_configured
    assert _upload(auth_client, JPEG_BYTES).status_code == 503
    with pytest.raises(StorageNotConfigured):
        Storage(Settings(do_spaces_key="", do_spaces_secret=""))


def test_presigned_url(auth_client, test_db, storage):
    body = _upload(auth_client, JPEG_BYTES, name="receta médica.jpg").json()
    resp = auth_client.get(f"/api/v1/attachments/{body['id']}/url")
    assert resp.status_code == 200
    out = resp.json()
    assert out["expires_in"] == 300
    url = urlparse(out["url"])
    # Signed for the public (browser-facing) endpoint, path-style.
    assert (url.scheme, url.netloc) == ("http", "localhost:9010")
    row = test_db.get(Attachment, body["id"])
    assert url.path == f"/{storage.bucket}/{row.storage_key}"
    query = parse_qs(url.query)
    assert query["X-Amz-Expires"] == ["300"]
    assert query["response-content-type"] == ["image/jpeg"]
    assert query["response-content-disposition"] == [
        "inline; filename*=UTF-8''receta%20m%C3%A9dica.jpg"
    ]

    download = auth_client.get(
        f"/api/v1/attachments/{body['id']}/url", params={"download": "true"}
    ).json()
    assert "attachment%3B" in download["url"]
    assert auth_client.get("/api/v1/attachments/999/url").status_code == 404


def test_url_ttl_is_capped():
    settings = Settings(
        do_spaces_key="k",
        do_spaces_secret="s",
        do_spaces_endpoint="https://s3.amazonaws.com",
        do_spaces_region="us-east-1",
        attachment_url_ttl_seconds=3600,
    )
    store = Storage(settings)
    assert store.url_ttl == 600
    assert "X-Amz-Expires=600" in store.presigned_get("k", filename="a", content_type="x/y")


def test_link_attachments(auth_client, test_db, storage, member):
    a = _upload(auth_client, JPEG_BYTES).json()["id"]
    b = _upload(auth_client, PDF_BYTES).json()["id"]
    rows = svc.link_attachments(
        test_db, [a, b, a], owner_type="log_entry", owner_id=7, user_id=member.id
    )
    test_db.commit()
    assert {(r.owner_type, r.owner_id) for r in rows} == {("log_entry", 7)}
    links = test_db.scalars(select(AuditLog).where(AuditLog.action == "link")).all()
    assert len(links) == 2

    # Idempotent for the same owner; refused for another owner or bad input.
    svc.link_attachments(test_db, [a], owner_type="log_entry", owner_id=7, user_id=member.id)
    with pytest.raises(svc.AttachmentLinkError):
        svc.link_attachments(test_db, [a], owner_type="log_entry", owner_id=8, user_id=None)
    with pytest.raises(svc.AttachmentLinkError):
        svc.link_attachments(test_db, [999], owner_type="log_entry", owner_id=7, user_id=None)
    with pytest.raises(svc.AttachmentLinkError):
        svc.link_attachments(test_db, [b], owner_type="patient", owner_id=1, user_id=None)
