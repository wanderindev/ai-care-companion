"""Lab portal links (#16): detection, recording, the SSRF-guarded fetch, the
fetch job, the API and the scan script.

Synthetic data only: made-up hosts under ``.example``, generated PDFs. The
network is never touched: DNS is a fake resolver and HTTP a MockTransport.
"""

import functools
from datetime import UTC, datetime

import httpx2 as httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.job import Job
from app.models.lab import LabExtraction, LabLink, LabReport
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.attachments import PDF, store_upload
from app.services.labs import fetch, links
from app.services.labs import jobs as lab_jobs
from app.services.whatsapp_import import AFTER_IMPORT_HOOKS
from scripts import lab_links as lab_links_script
from tests.test_labs import (  # noqa: F401 — fixtures used by name
    _pdf,
    analytes,
    claude,
    lab_env,
    named_patients,
)

LAB_URL = "https://resultados.labprueba.example/r/abc123.pdf"
ALLOWED = ("resultados.labprueba.example", "*.portal.example")
PUBLIC_IP = "93.184.216.34"
MB = 1024 * 1024


# ── detection ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Aquí está: https://resultados.labprueba.example/r/abc123.pdf.",
        "ver https://otro.example/consulta?tipo=Resultados&id=9",
        "https://otro.example/Laboratório/orden/55",
        "(https://otro.example/descargar/PDF/77)",
        "*https://otro.example/lab/9*",
        "HTTPS://OTRO.EXAMPLE/ver-resultado/1",
    ],
)
def test_detects_lab_urls(text):
    assert len(links.find_lab_urls(text, ())) == 1


@pytest.mark.parametrize(
    "text",
    [
        "https://maps.example/lugar/clinica",
        "https://videos.example/watch?v=label",
        "https://otro.example/collaborate/labrador",
        "ftp://resultados.example/x.pdf",
        "no url here, resultados.pdf",
    ],
)
def test_ignores_other_urls(text):
    assert links.find_lab_urls(text, ()) == []


def test_allow_listed_host_is_a_lab_link_without_keywords():
    assert links.find_lab_urls("https://a.portal.example/x/9", ALLOWED) == [
        ("https://a.portal.example/x/9", "a.portal.example")
    ]
    assert links.find_lab_urls("https://a.portal.example/x/9", ()) == []


def test_normalization_and_dedupe():
    text = (
        "https://Resultados.LabPrueba.example:443/r/abc123.pdf#p=2 y otra vez "
        "https://resultados.labprueba.example/r/abc123.pdf, y http://x.example:8080/lab?T=AbC"
    )
    assert links.find_lab_urls(text, ()) == [
        (LAB_URL, "resultados.labprueba.example"),
        ("http://x.example:8080/lab?T=AbC", "x.example"),
    ]
    assert links.url_hash(LAB_URL) == links.url_hash(links.normalize_url(LAB_URL)[0])


def test_patient_guess(patients):
    papa, mama = patients
    assert links.guess_patient("Resultados de PAPÁ", patients) == papa.id
    assert links.guess_patient("los de mama", patients) == mama.id
    assert links.guess_patient("papá y mamá", patients) is None
    assert links.guess_patient("los resultados", patients) is None
    assert links.guess_patient("papaya", patients) is None


# ── recording: the after_import hook and the scan script ───────────────────


def _messages(db, texts, *, import_id=None):
    if import_id is None:
        imp = WhatsappImport(
            mode="daily", status="done", filename="c.zip", size=1, chunk_size=1, chunk_count=1
        )
        db.add(imp)
        db.flush()
        import_id = imp.id
    start = db.scalar(select(WhatsappMessage.id).order_by(WhatsappMessage.id.desc())) or 0
    rows = []
    for n, text in enumerate(texts):
        row = WhatsappMessage(
            import_id=import_id,
            sent_at=datetime(2026, 4, 1, 10, n, tzinfo=UTC),
            sender="Ana Prueba",
            text=text,
            dedupe_hash=f"{start + n + 1:064d}",
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return db.get(WhatsappImport, import_id), rows


@pytest.fixture
def link_settings(monkeypatch):
    settings = Settings(lab_link_hosts=" Resultados.LabPrueba.example , *.portal.example ")
    monkeypatch.setattr(lab_jobs, "get_settings", lambda: settings)
    return settings


def test_hook_records_links_and_queues_only_allow_listed(test_db, patients, link_settings):
    assert lab_jobs.record_lab_links in AFTER_IMPORT_HOOKS
    imp, messages = _messages(
        test_db,
        [
            f"Los de papá: {LAB_URL}",
            "Mamá: https://otro.example/resultados/77 y http://resultados.labprueba.example/x",
            "Sin enlaces",
            f"repetido {LAB_URL}",
        ],
    )
    lab_jobs.record_lab_links(test_db, imp)
    test_db.commit()

    rows = list(test_db.scalars(select(LabLink).order_by(LabLink.id)))
    assert [(r.url, r.message_id, r.status) for r in rows] == [
        (LAB_URL, messages[0].id, "pending"),
        ("https://otro.example/resultados/77", messages[1].id, "pending"),
        ("http://resultados.labprueba.example/x", messages[1].id, "pending"),
    ]
    assert [r.patient_id for r in rows] == [patients[0].id, patients[1].id, patients[1].id]
    assert [r.reason for r in rows] == [None, "not_allowed", "not_allowed"]
    queued = list(test_db.scalars(select(Job)))
    assert [(j.kind, j.payload, j.max_attempts) for j in queued] == [
        ("lab_link_fetch", {"lab_link_id": rows[0].id}, links.FETCH_MAX_ATTEMPTS)
    ]
    assert rows[0].job_id == queued[0].id
    audits = test_db.scalars(select(AuditLog).where(AuditLog.entity == "lab_link")).all()
    assert sorted(a.entity_id for a in audits) == sorted(str(r.id) for r in rows)

    # A second import repeating a link adds nothing.
    imp2, _ = _messages(test_db, [f"otra vez {LAB_URL}"])
    lab_jobs.record_lab_links(test_db, imp2)
    test_db.commit()
    assert len(test_db.scalars(select(LabLink)).all()) == 3


def test_scan_script_records_old_messages_and_queues_untried(test_db, patients):
    _messages(test_db, [f"viejo {LAB_URL}", "https://a.portal.example/q?lab=1", "hola"])
    nothing_allowed = Settings()
    assert lab_links_script.scan(test_db, nothing_allowed) == (2, 2, 0)
    assert not test_db.scalars(select(Job)).all()
    # Idempotent; after the operator allows the host, the untried link is queued.
    allowed = Settings(lab_link_hosts="*.portal.example")
    assert lab_links_script.scan(test_db, allowed) == (2, 0, 1)
    job = test_db.scalars(select(Job)).one()
    link = test_db.get(LabLink, job.payload["lab_link_id"])
    assert (link.host, link.reason, link.job_id) == ("a.portal.example", None, job.id)
    assert lab_links_script.scan(test_db, allowed) == (2, 0, 0)


# ── the SSRF-guarded fetch ─────────────────────────────────────────────────


def _resolver(mapping):
    def resolve(host, port):
        return mapping.get(host, [PUBLIC_IP])

    return resolve


def _transport(handler, seen=None):
    def wrapped(request):
        if seen is not None:
            seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def _resp(status=200, body=b"", headers=None):
    """A streamed response, as a real transport gives (MockTransport's
    ``content=`` responses are already read)."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def _pdf_response(request):
    return _resp(200, _pdf("enlace"), {"Content-Type": "text/html"})


def _fetch(url=LAB_URL, handler=_pdf_response, *, resolve=None, seen=None, cap=MB):
    return fetch.fetch_pdf(
        url,
        ALLOWED,
        cap,
        transport=_transport(handler, seen),
        resolver=_resolver(resolve or {}),
    )


def test_fetch_pins_the_validated_ip_and_sends_no_credentials():
    seen = []
    result = _fetch(seen=seen)
    assert result.data is not None and result.data.startswith(b"%PDF-")
    (request,) = seen
    assert request.url.host == PUBLIC_IP
    assert request.headers["host"] == "resultados.labprueba.example"
    assert request.extensions["sni_hostname"] == "resultados.labprueba.example"
    assert request.headers["user-agent"] == fetch.USER_AGENT
    assert "cookie" not in request.headers and "authorization" not in request.headers


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.17.0.2",
        "192.168.1.5",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::a9fe:a9fe",
    ],
)
def test_fetch_refuses_non_public_addresses(ip):
    seen = []
    result = _fetch(resolve={"resultados.labprueba.example": [ip]}, seen=seen)
    assert result.reason == fetch.BLOCKED and seen == []


def test_fetch_refuses_when_any_resolved_address_is_private():
    result = _fetch(resolve={"resultados.labprueba.example": [PUBLIC_IP, "10.0.0.1"]})
    assert result.reason == fetch.BLOCKED


@pytest.mark.parametrize(
    "url",
    [
        "http://resultados.labprueba.example/r/abc.pdf",
        "https://evil.example/resultados.pdf",
        "https://user:pw@resultados.labprueba.example/r.pdf",
        "https://127.0.0.1/resultados.pdf",
    ],
)
def test_fetch_refuses_urls_outside_the_rules(url):
    seen = []
    assert _fetch(url, seen=seen).reason == fetch.BLOCKED
    assert seen == []


def test_fetch_checks_every_redirect_hop():
    def redirect_to(location):
        def handler(request):
            if request.headers["host"] == "resultados.labprueba.example":
                return _resp(302, headers={"Location": location})
            return _pdf_response(request)

        return handler

    # To an allowed host that resolves privately, to a non-allow-listed host,
    # to plain http: all blocked before any request is sent there.
    resolve = {"b.portal.example": ["192.168.0.10"]}
    for location in (
        "https://b.portal.example/r.pdf",
        "https://evil.example/r.pdf",
        "http://c.portal.example/r.pdf",
    ):
        seen = []
        assert _fetch(handler=redirect_to(location), resolve=resolve, seen=seen).reason == (
            fetch.BLOCKED
        )
        assert len(seen) == 1
    # To an allowed public host: followed.
    seen = []
    result = _fetch(handler=redirect_to("https://c.portal.example/r.pdf"), seen=seen)
    assert result.data is not None
    assert [r.headers["host"] for r in seen] == ["resultados.labprueba.example", "c.portal.example"]


def test_fetch_caps_redirects():
    seen = []
    loop = _fetch(handler=lambda r: _resp(302, headers={"Location": "/again"}), seen=seen)
    assert loop.reason == fetch.TOO_MANY_REDIRECTS
    assert len(seen) == fetch.MAX_REDIRECTS + 1


def test_fetch_reasons():
    html = _fetch(handler=lambda r: _resp(200, b"<html>login</html>"))
    assert html.reason == fetch.NOT_PDF
    pdf_type = _fetch(
        handler=lambda r: _resp(200, b"not a pdf", {"Content-Type": "application/pdf"})
    )
    assert pdf_type.reason == fetch.NOT_PDF
    assert _fetch(handler=lambda r: _resp(404)).reason == fetch.HTTP_ERROR
    assert _fetch(handler=lambda r: _resp(302)).reason == fetch.HTTP_ERROR
    big = _fetch(handler=lambda r: _resp(200, b"%PDF-" + b"0" * 2000), cap=1000)
    assert big.reason == fetch.TOO_LARGE
    declared = _fetch(
        handler=lambda r: _resp(200, b"%PDF-", headers={"Content-Length": "5000"}),
        cap=1000,
    )
    assert declared.reason == fetch.TOO_LARGE


def test_fetch_transient_errors_raise():
    def timeout(request):
        raise httpx.ConnectTimeout("slow")

    for handler in (timeout, lambda r: _resp(503), lambda r: _resp(429)):
        with pytest.raises(fetch.TransientFetchError):
            _fetch(handler=handler)


# ── the lab_link_fetch job ─────────────────────────────────────────────────


@pytest.fixture
def fetch_env(lab_env, monkeypatch):
    settings = Settings(
        anthropic_api_key="test-key",
        extraction_model="claude-opus-5-5",
        lab_link_hosts=",".join(ALLOWED),
    )
    monkeypatch.setattr(lab_jobs, "get_settings", lambda: settings)
    state = {"handler": _pdf_response, "seen": [], "resolve": {}}
    original = fetch.fetch_pdf

    def fake(url, patterns, cap):
        return original(
            url,
            patterns,
            cap,
            transport=_transport(lambda r: state["handler"](r), state["seen"]),
            resolver=_resolver(state["resolve"]),
        )

    monkeypatch.setattr(fetch, "fetch_pdf", fake)
    lab_env.settings = settings
    lab_env.http = state
    return lab_env


def _link(db, url=LAB_URL, message=None):
    imp, (msg,) = _messages(db, [f"resultados {url}"]) if message is None else (None, (message,))
    created = links.record_links(db, [msg], Settings(lab_link_hosts=",".join(ALLOWED)))
    db.commit()
    return created[0]


def _run_all(env, db, limit=10):
    for _ in range(limit):
        if not jobs.run_next(env.factory):
            break
    db.expire_all()


def test_fetch_job_stores_pdf_and_runs_extraction(test_db, fetch_env, named_patients, analytes):
    link = _link(test_db)
    _run_all(fetch_env, test_db)

    link = test_db.get(LabLink, link.id)
    assert (link.status, link.reason) == ("fetched", None)
    attachment = test_db.get(Attachment, link.attachment_id)
    assert attachment.mime == PDF.mime and attachment.filename == "resultado.pdf"
    kinds = [(j.kind, j.status) for j in test_db.scalars(select(Job).order_by(Job.id))]
    assert kinds == [("lab_link_fetch", "done"), ("lab_extract", "done")]
    extract_job = test_db.scalars(select(Job).where(Job.kind == "lab_extract")).one()
    assert extract_job.payload == {
        "attachment_id": attachment.id,
        "source_message_id": link.message_id,
        "lab_link_id": link.id,
    }
    report = test_db.get(LabReport, link.report_id)
    assert (report.source, report.source_message_id) == ("link", link.message_id)
    # The URL never reaches job payloads or messages.
    for job in test_db.scalars(select(Job)):
        assert "labprueba" not in str(job.payload) and "labprueba" not in (job.message or "")


def test_fetch_job_reuses_an_already_read_pdf(test_db, fetch_env):
    earlier = store_upload(
        test_db,
        fetch_env.storage,
        data=_pdf("enlace"),
        file_type=PDF,
        filename="a.pdf",
        user_id=None,
    )
    test_db.add(LabExtraction(sha256=earlier.sha256, outcome="not_lab_report"))
    test_db.commit()
    link = _link(test_db)
    _run_all(fetch_env, test_db)
    link = test_db.get(LabLink, link.id)
    assert link.status == "fetched" and link.report_id is None
    assert [j.kind for j in test_db.scalars(select(Job))] == ["lab_link_fetch"]


def test_fetch_job_keeps_non_pdf_pending(test_db, fetch_env, caplog):
    fetch_env.http["handler"] = lambda r: _resp(200, b"<html>Ingrese</html>")
    link = _link(test_db)
    with caplog.at_level("INFO", logger="app"):
        _run_all(fetch_env, test_db)
    link = test_db.get(LabLink, link.id)
    assert (link.status, link.reason, link.attachment_id) == ("pending", "not_pdf", None)
    job = test_db.get(Job, link.job_id)
    assert job.status == "done" and "pendiente" in job.message
    assert "labprueba" not in caplog.text and str(link.id) in caplog.text


def test_fetch_job_blocked_is_not_retried(test_db, fetch_env):
    fetch_env.http["resolve"] = {"resultados.labprueba.example": ["169.254.169.254"]}
    link = _link(test_db)
    _run_all(fetch_env, test_db)
    link = test_db.get(LabLink, link.id)
    assert (link.status, link.reason) == ("pending", "blocked")
    job = test_db.get(Job, link.job_id)
    assert (job.status, job.attempts) == ("done", 1)
    assert fetch_env.http["seen"] == []


def test_fetch_job_retries_transient_then_leaves_pending(test_db, fetch_env):
    fetch_env.http["handler"] = lambda r: _resp(502)
    link = _link(test_db)
    for attempt in range(1, links.FETCH_MAX_ATTEMPTS + 1):
        test_db.execute(Job.__table__.update().values(run_after=datetime(2000, 1, 1, tzinfo=UTC)))
        test_db.commit()
        assert jobs.run_next(fetch_env.factory) is True
        test_db.expire_all()
        job = test_db.get(Job, link.job_id)
        assert job.attempts == attempt
    link = test_db.get(LabLink, link.id)
    assert (link.status, link.reason) == ("pending", "unreachable")
    assert job.status == "done" and job.error is None


def test_fetch_job_skips_links_no_longer_pending(test_db, fetch_env):
    link = _link(test_db)
    link.status = "ignored"
    test_db.commit()
    _run_all(fetch_env, test_db)
    assert fetch_env.http["seen"] == []
    assert test_db.get(LabLink, link.id).status == "ignored"


# ── API ─────────────────────────────────────────────────────────────────────


def test_list_and_ignore_require_auth(db_client, test_db, patients):
    assert db_client.get("/api/v1/lab-links").status_code == 401
    resp = db_client.post("/api/v1/lab-links/1/ignore", headers={"X-Requested-With": "fetch"})
    assert resp.status_code == 401


def test_list_pending_links(auth_client, test_db, patients):
    link = _link(test_db)
    other = _link(test_db, "https://otro.example/resultados/2")
    other.status = "ignored"
    test_db.commit()
    resp = auth_client.get("/api/v1/lab-links")
    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["id"] == link.id and item["url"] == LAB_URL
    assert item["host"] == "resultados.labprueba.example"
    assert item["sender"] == "Ana Prueba" and item["sent_at"].startswith("2026-04-01")
    assert item["status"] == "pending"
    ignored = auth_client.get("/api/v1/lab-links?status=ignored").json()
    assert [i["id"] for i in ignored] == [other.id]
    assert auth_client.get("/api/v1/lab-links?status=nada").status_code == 422


def test_ignore_link_is_audited_and_needs_csrf(auth_client, test_db, patients, member):
    link = _link(test_db)
    url = f"/api/v1/lab-links/{link.id}/ignore"
    del auth_client.headers["X-Requested-With"]
    assert auth_client.post(url).status_code == 403
    auth_client.headers["X-Requested-With"] = "fetch"
    resp = auth_client.post(url)
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    test_db.expire_all()
    assert test_db.get(LabLink, link.id).updated_by == member.id
    row = test_db.scalars(
        select(AuditLog).where(AuditLog.entity == "lab_link", AuditLog.action == "update")
    ).one()
    assert (row.user_id, row.entity_id) == (member.id, str(link.id))
    assert (row.before["status"], row.after["status"]) == ("pending", "ignored")
    assert auth_client.post(url).status_code == 200  # idempotent
    assert auth_client.post("/api/v1/lab-links/999999/ignore").status_code == 404
    assert auth_client.get("/api/v1/lab-links").json() == []


def test_upload_from_a_link_resolves_it(auth_client, test_db, fetch_env, member, named_patients):
    from app.core.config import get_settings
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: fetch_env.settings
    try:
        link = _link(test_db, "https://otro.example/resultados/9")  # not allow-listed
        pdfs = [
            store_upload(
                test_db,
                fetch_env.storage,
                data=_pdf(name),
                file_type=PDF,
                filename=f"{name}.pdf",
                user_id=member.id,
            )
            for name in ("manual", "segundo")
        ]
        test_db.commit()
        body = {"attachment_ids": [pdfs[0].id], "lab_link_id": link.id}
        resp = auth_client.post("/api/v1/lab-reports/extract", json=body)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        test_db.expire_all()
        link = test_db.get(LabLink, link.id)
        assert (link.status, link.attachment_id, link.job_id, link.updated_by) == (
            "fetched",
            pdfs[0].id,
            job_id,
            member.id,
        )
        assert test_db.get(Job, job_id).payload["lab_link_id"] == link.id
        # A link may hold several PDFs (#124): a fetched link takes another
        # file on its own request and job; the link keeps its first file/job.
        second = {"attachment_ids": [pdfs[1].id], "lab_link_id": link.id}
        resp = auth_client.post("/api/v1/lab-reports/extract", json=second)
        assert resp.status_code == 202, resp.text
        second_job_id = resp.json()["job_id"]
        assert second_job_id != job_id
        test_db.expire_all()
        assert test_db.get(Job, second_job_id).payload == {
            "attachment_ids": [pdfs[1].id],
            "patient_id": None,
            "lab_link_id": link.id,
        }
        link = test_db.get(LabLink, link.id)
        assert (link.status, link.attachment_id, link.job_id) == ("fetched", pdfs[0].id, job_id)
        # The ignore action refuses a resolved link.
        assert auth_client.post(f"/api/v1/lab-links/{link.id}/ignore").status_code == 409
        missing = {"attachment_ids": [pdfs[0].id], "lab_link_id": 999999}
        assert auth_client.post("/api/v1/lab-reports/extract", json=missing).status_code == 404

        # The refused requests ran in this shared session; a real request's
        # session ends with the request (releasing the row lock).
        test_db.rollback()
        _run_all(fetch_env, test_db)
        link = test_db.get(LabLink, link.id)
        report_ids = [
            auth_client.get(f"/api/v1/lab-reports/extract/{j}").json()["report_id"]
            for j in (job_id, second_job_id)
        ]
        reports = [test_db.get(LabReport, r) for r in report_ids]
        # One report per PDF, both from the link; the link keeps the first.
        assert len(set(report_ids)) == 2
        assert [r.source for r in reports] == ["link", "link"]
        assert link.report_id == report_ids[0]
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_upload_to_an_ignored_link_is_refused(auth_client, test_db, fetch_env, member):
    from app.core.config import get_settings
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: fetch_env.settings
    try:
        link = _link(test_db, "https://otro.example/resultados/10")
        assert auth_client.post(f"/api/v1/lab-links/{link.id}/ignore").status_code == 200
        pdf = store_upload(
            test_db,
            fetch_env.storage,
            data=_pdf("ignorado"),
            file_type=PDF,
            filename="r.pdf",
            user_id=member.id,
        )
        test_db.commit()
        body = {"attachment_ids": [pdf.id], "lab_link_id": link.id}
        assert auth_client.post("/api/v1/lab-reports/extract", json=body).status_code == 409
        test_db.rollback()
        assert test_db.scalars(select(Job).where(Job.kind == "lab_extract")).all() == []
        link = test_db.get(LabLink, link.id)
        assert (link.status, link.attachment_id, link.job_id) == ("ignored", None, None)
    finally:
        app.dependency_overrides.pop(get_settings, None)
