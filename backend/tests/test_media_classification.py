"""Media classification (ticket #37): request shape, the after_import hook per
mode, the classify_media job, its SHA-256 dedupe, propagation to messages and
the hand-off of lab reports to the labs pipeline.

Synthetic data only. The Anthropic API is never called: the classifier and
the lab extractor run against fake clients.
"""

import base64
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.attachment import Attachment
from app.models.job import Job
from app.models.lab import LabExtraction, LabReport
from app.models.media_classification import MediaClassification
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.attachments import HEIC, JPEG, OGG, PDF, WEBP, store_upload
from app.services.labs import extract as lab_extract
from app.services.labs import jobs as lab_jobs
from app.services.media_classification import classify
from app.services.media_classification import jobs as mjobs
from app.services.whatsapp_import import AFTER_IMPORT_HOOKS
from tests.test_labs import LAB_OUTPUT, FakeMessages, _respond

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"synthetic-photo-1" * 4
PDF_BYTES = b"%PDF-1.4\n% synthetic document 1\n%%EOF\n"


def _vp8x(width: int, height: int) -> bytes:
    """An extended-format (animated sticker) WebP header, synthetic."""
    size = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF\x30\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x02\x00\x00\x00" + size + b"\x00" * 16


def _vp8(width: int, height: int) -> bytes:
    """A lossy WebP key-frame header, synthetic."""
    return (
        b"RIFF\x30\x00\x00\x00WEBPVP8 \x20\x00\x00\x00\x10\x02\x00\x9d\x01\x2a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + b"\x00" * 16
    )


def _vp8l(width: int, height: int) -> bytes:
    """A lossless WebP header, synthetic."""
    bits = (width - 1) | ((height - 1) << 14)
    return (
        b"RIFF\x30\x00\x00\x00WEBPVP8L\x20\x00\x00\x00\x2f"
        + bits.to_bytes(4, "little")
        + b"\x00" * 16
    )


WEBP_BYTES = _vp8x(512, 512)
HEIC_BYTES = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20
DESCRIPTION = "Receta de dermatología con 2 medicamentos"
RX = {"category": "prescription", "description": DESCRIPTION, "patient_hint": "mama"}
LAB = {"category": "lab_report", "description": "Informe de laboratorio", "patient_hint": "papa"}


def _api_error(status: int):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "error", response=httpx2.Response(status, request=request), body=None
    )


class FakeClassifier:
    def __init__(self):
        self.calls: list[dict] = []
        self.output: dict | str = RX
        self.stop_reason = "end_turn"
        self.error: Exception | None = None

    def create(self, **params):
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        text = self.output if isinstance(self.output, str) else json.dumps(self.output)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=1500,
                output_tokens=60,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


@pytest.fixture
def env(monkeypatch, storage, test_engine):
    state = SimpleNamespace(
        settings=Settings(
            anthropic_api_key="test-key",
            classification_model="claude-haiku-5-5",
            extraction_model="claude-opus-5-5",
        ),
        classifier=FakeClassifier(),
        extractor=FakeMessages(_respond(LAB_OUTPUT)),
        storage=storage,
        factory=sessionmaker(bind=test_engine, autoflush=False),
        keys=[],
    )

    def make_client(key):
        state.keys.append(key)
        return SimpleNamespace(beta=SimpleNamespace(messages=state.classifier))

    monkeypatch.setattr(classify, "make_client", make_client)
    monkeypatch.setattr(
        lab_extract,
        "make_client",
        lambda key: SimpleNamespace(beta=SimpleNamespace(messages=state.extractor)),
    )
    for module in (mjobs, lab_jobs):
        monkeypatch.setattr(module, "get_settings", lambda: state.settings)
        monkeypatch.setattr(module, "get_storage", lambda: storage)
    return state


_minute = iter(range(10_000))


def _import(db, mode="daily") -> WhatsappImport:
    imp = WhatsappImport(
        mode=mode, status="done", filename="c.zip", size=1, chunk_size=1, chunk_count=1
    )
    db.add(imp)
    db.flush()
    return imp


KINDS = {"image/jpeg": "image", "image/webp": "image", "image/heic": "image"}


def _message(db, storage, imp, data=JPEG_BYTES, file_type=JPEG) -> WhatsappMessage:
    attachment = store_upload(
        db, storage, data=data, file_type=file_type, filename="IMG-0001.jpg", user_id=None
    )
    n = next(_minute)
    message = WhatsappMessage(
        import_id=imp.id,
        sent_at=datetime(2026, 5, 1, 10, 0, tzinfo=UTC).replace(minute=n % 60, second=n // 60),
        sender="Ana Prueba",
        text="",
        media_kind="pdf" if file_type is PDF else KINDS.get(file_type.mime, "audio"),
        media_filename="IMG-0001.jpg",
        attachment_id=attachment.id,
        media_import_id=imp.id,
        dedupe_hash=f"mc-hash-{n}",
    )
    db.add(message)
    db.flush()
    attachment.owner_type, attachment.owner_id = "whatsapp_message", message.id
    db.flush()
    return message


def _jobs(db, kind) -> list[Job]:
    return list(db.scalars(select(Job).where(Job.kind == kind).order_by(Job.id)))


def _hooks(db, imp) -> None:
    for hook in AFTER_IMPORT_HOOKS:
        hook(db, imp)
    db.commit()


def _run_all(env) -> None:
    while jobs.run_next(env.factory):
        pass


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# ── Request shape and output validation ─────────────────────────────────────


def test_request_shape_image_and_pdf():
    params = classify.request_params(
        "claude-haiku-5-5", classify.content_blocks("image/jpeg", JPEG_BYTES)
    )
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["effort"] == "low"
    schema = params["output_config"]["format"]["schema"]
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"category", "description", "patient_hint"}
    assert schema["properties"]["category"]["enum"] == [
        "lab_report",
        "prescription",
        "medical_document",
        "other",
    ]
    assert schema["properties"]["patient_hint"]["enum"] == ["papa", "mama", "unknown"]
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "NUNCA incluyas nombres" in params["system"][0]["text"]
    image, text = params["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image["source"]["data"]) == JPEG_BYTES
    assert text["type"] == "text"
    assert "betas" not in params and "fallbacks" not in params  # Haiku: no fallback

    pdf = classify.request_params("claude-sonnet-5-5", classify.content_blocks(PDF.mime, PDF_BYTES))
    block = pdf["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == PDF.mime
    assert pdf["fallbacks"] == "default" and pdf["betas"] == ["server-side-fallback-2026-07-01"]


def test_normalize_validates_and_trims():
    assert classify.normalize({"category": "x", "description": "a", "patient_hint": "papa"}) is None
    assert classify.normalize({"category": "other"}) is None
    assert classify.normalize([]) is None
    out = classify.normalize(
        {"category": "prescription", "description": "  Receta\n " + "x" * 300, "patient_hint": "?"}
    )
    assert out["category"] == "prescription"
    assert out["patient_hint"] == "unknown"
    assert out["description"].startswith("Receta x") and len(out["description"]) == 200


def test_settings_default_model():
    assert Settings().classification_model == "claude-haiku-5-5"


# ── Hook: enqueue rules per mode ─────────────────────────────────────────────


def test_hooks_registered_and_worker_loads_the_handler():
    assert mjobs.enqueue_classifications in AFTER_IMPORT_HOOKS
    from app.worker import HANDLER_MODULES, load_handlers

    load_handlers(HANDLER_MODULES)
    assert "app.services.media_classification.jobs" in HANDLER_MODULES
    assert mjobs.JOB_KIND in jobs.HANDLERS


@pytest.mark.parametrize("mode", ["daily", "backfill"])
def test_daily_and_backfill_classify_images_and_pdfs(test_db, env, mode):
    imp = _import(test_db, mode)
    photo = _message(test_db, env.storage, imp)
    doc = _message(test_db, env.storage, imp, PDF_BYTES, PDF)
    _message(test_db, env.storage, imp, b"OggS" + b"\x00" * 40, OGG)  # audio: not ours
    _hooks(test_db, imp)

    queued = _jobs(test_db, mjobs.JOB_KIND)
    assert [j.payload for j in queued] == [
        {"attachment_id": photo.attachment_id, "source_message_id": photo.id},
        {"attachment_id": doc.attachment_id, "source_message_id": doc.id},
    ]
    assert _jobs(test_db, "lab_extract") == []  # PDFs wait for the classifier
    assert env.classifier.calls == []  # the hook only enqueues


def test_labs_only_pdfs_go_straight_to_lab_extraction(test_db, env):
    imp = _import(test_db, "labs_only")
    doc = _message(test_db, env.storage, imp, PDF_BYTES, PDF)
    _hooks(test_db, imp)
    assert _jobs(test_db, mjobs.JOB_KIND) == []
    assert [j.payload for j in _jobs(test_db, "lab_extract")] == [
        {"attachment_id": doc.attachment_id, "source_message_id": doc.id}
    ]


# ── Job: dedupe, propagation, lab hand-off ──────────────────────────────────


def test_same_file_is_classified_once_and_propagated(test_db, env, caplog):
    imp = _import(test_db)
    first = _message(test_db, env.storage, imp)
    second = _message(test_db, env.storage, imp)  # same bytes, forwarded
    _hooks(test_db, imp)
    assert len(_jobs(test_db, mjobs.JOB_KIND)) == 1

    caplog.set_level(logging.INFO, logger="app")
    _run_all(env)
    test_db.expire_all()

    assert len(env.classifier.calls) == 1
    assert env.keys == ["test-key"]
    assert _jobs(test_db, mjobs.JOB_KIND)[0].status == "done"
    row = test_db.scalar(select(MediaClassification))
    assert (row.status, row.method, row.category) == ("done", "claude", "prescription")
    assert row.description == DESCRIPTION and row.patient_hint == "mama"
    assert row.model == "claude-haiku-5-5"
    assert row.usage["input_tokens"] == 1500
    assert row.usage["cost_usd"] == pytest.approx(0.00018)
    expected = {
        "status": "done",
        "category": "prescription",
        "description": DESCRIPTION,
        "patient_hint": "mama",
        "method": "claude",
        "sha256": row.sha256,
    }
    assert test_db.get(WhatsappMessage, first.id).classification == expected
    assert test_db.get(WhatsappMessage, second.id).classification == expected
    assert _jobs(test_db, "lab_extract") == []  # not a lab report

    # Logs: ids, category and counts — no description, hint, sender or file name.
    assert "media classification: attachment" in caplog.text
    for secret in (DESCRIPTION, "mama", "Ana Prueba", "IMG-0001"):
        assert secret not in caplog.text

    # A later import with the same file reuses the result: no job, no call.
    later = _import(test_db)
    third = _message(test_db, env.storage, later)
    _hooks(test_db, later)
    test_db.expire_all()
    assert len(_jobs(test_db, mjobs.JOB_KIND)) == 1
    assert test_db.get(WhatsappMessage, third.id).classification == expected
    assert len(env.classifier.calls) == 1


def test_duplicate_job_for_known_hash_only_copies(test_db, env):
    imp = _import(test_db)
    message = _message(test_db, env.storage, imp)
    for _ in range(2):
        mjobs.enqueue_classification(test_db, message.attachment_id, source_message_id=message.id)
    test_db.commit()
    _run_all(env)
    assert len(env.classifier.calls) == 1
    assert [j.status for j in _jobs(test_db, mjobs.JOB_KIND)] == ["done", "done"]
    assert _count(test_db, MediaClassification) == 1


def test_lab_report_pdf_is_extracted_exactly_once(test_db, env):
    env.classifier.output = LAB
    imp = _import(test_db)
    doc = _message(test_db, env.storage, imp, PDF_BYTES, PDF)
    twin = _message(test_db, env.storage, imp, PDF_BYTES, PDF)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()

    assert len(env.classifier.calls) == 1
    lab_jobs_ = _jobs(test_db, "lab_extract")
    assert [j.payload for j in lab_jobs_] == [
        {"attachment_id": doc.attachment_id, "source_message_id": doc.id}
    ]
    assert lab_jobs_[0].status == "done"
    assert len(env.extractor.calls) == 1
    assert env.extractor.calls[0]["messages"][0]["content"][0]["type"] == "document"
    report = test_db.scalar(select(LabReport))
    assert report.source == "whatsapp" and report.source_message_id == doc.id
    assert test_db.get(WhatsappMessage, twin.id).classification["category"] == "lab_report"


def test_lab_report_photo_goes_through_the_lab_pipeline(test_db, env):
    env.classifier.output = LAB
    imp = _import(test_db)
    photo = _message(test_db, env.storage, imp)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()

    lab_jobs_ = _jobs(test_db, "lab_extract")
    assert [j.payload for j in lab_jobs_] == [
        {"attachment_id": photo.attachment_id, "source_message_id": photo.id}
    ]
    assert lab_jobs_[0].status == "done"
    assert lab_jobs_[0].message == "Informe de laboratorio guardado"
    sent = env.extractor.calls[0]["messages"][0]["content"][0]
    assert sent["type"] == "image" and base64.b64decode(sent["source"]["data"]) == JPEG_BYTES
    extraction = test_db.scalar(select(LabExtraction))
    sha = test_db.get(WhatsappMessage, photo.id).classification["sha256"]
    assert extraction.sha256 == sha and extraction.outcome == "lab_report"  # key = its sha
    report = test_db.scalar(select(LabReport))
    assert report.source == "whatsapp" and report.attachment_id == photo.attachment_id


def test_lab_report_already_extracted_is_not_queued_again(test_db, env):
    env.classifier.output = LAB
    imp = _import(test_db)
    doc = _message(test_db, env.storage, imp, PDF_BYTES, PDF)
    sha = test_db.get(Attachment, doc.attachment_id).sha256
    # The extractor already read this content (and found no results table).
    test_db.add(LabExtraction(sha256=sha, outcome="not_lab_report"))
    test_db.commit()
    _hooks(test_db, imp)
    _run_all(env)
    assert len(env.classifier.calls) == 1
    assert _jobs(test_db, "lab_extract") == []


def test_known_lab_report_needs_no_call(test_db, env):
    imp = _import(test_db)
    doc = _message(test_db, env.storage, imp, PDF_BYTES, PDF)
    sha = test_db.get(Attachment, doc.attachment_id).sha256
    test_db.add(LabExtraction(sha256=sha, outcome="lab_report"))
    test_db.commit()
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert env.classifier.calls == []
    assert _jobs(test_db, "lab_extract") == []
    value = test_db.get(WhatsappMessage, doc.id).classification
    assert value["category"] == "lab_report" and value["method"] == "lab_extraction"


def test_webp_dimensions_from_the_header():
    assert mjobs.webp_dimensions(_vp8x(512, 512)) == (512, 512)
    assert mjobs.webp_dimensions(_vp8(1080, 720)) == (1080, 720)
    assert mjobs.webp_dimensions(_vp8l(300, 300)) == (300, 300)
    assert mjobs.webp_dimensions(b"RIFF\x30\x00\x00\x00WEBPVP8Z" + b"\x00" * 30) is None
    assert mjobs.webp_dimensions(JPEG_BYTES) is None
    assert mjobs.webp_dimensions(b"RIFF") is None


@pytest.mark.parametrize(
    "data", [_vp8(1080, 720), _vp8x(1024, 1024), b"RIFF\x30\x00\x00\x00WEBPVP8Z" + b"\x00" * 30]
)
def test_webp_that_is_not_sticker_shaped_is_classified(test_db, env, data):
    imp = _import(test_db)
    photo = _message(test_db, env.storage, imp, data, WEBP)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert len(env.classifier.calls) == 1
    sent = env.classifier.calls[0]["messages"][0]["content"][0]
    assert sent["source"]["media_type"] == "image/webp"
    assert test_db.get(WhatsappMessage, photo.id).classification["method"] == "claude"


def test_sticker_needs_no_call(test_db, env):
    imp = _import(test_db)
    sticker = _message(test_db, env.storage, imp, WEBP_BYTES, WEBP)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert env.classifier.calls == []
    value = test_db.get(WhatsappMessage, sticker.id).classification
    assert (value["category"], value["method"]) == ("other", "sticker")


# ── Failures ─────────────────────────────────────────────────────────────────


def test_without_key_fails_permanently_and_records_nothing(test_db, env):
    env.settings = Settings(anthropic_api_key="")
    imp = _import(test_db)
    message = _message(test_db, env.storage, imp)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    job = _jobs(test_db, mjobs.JOB_KIND)[0]
    assert job.status == "failed" and job.attempts == 1
    assert job.error == f"PermanentJobError: {mjobs.NOT_CONFIGURED}"
    assert _count(test_db, MediaClassification) == 0
    assert test_db.get(WhatsappMessage, message.id).classification is None


def test_config_error_from_api_records_nothing(test_db, env):
    env.classifier.error = _api_error(401)
    imp = _import(test_db)
    _message(test_db, env.storage, imp)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    job = _jobs(test_db, mjobs.JOB_KIND)[0]
    assert job.status == "failed" and job.attempts == 1
    assert _count(test_db, MediaClassification) == 0


def test_server_error_retries_with_backoff(test_db, env):
    env.classifier.error = _api_error(529)
    imp = _import(test_db)
    message = _message(test_db, env.storage, imp)
    _hooks(test_db, imp)
    assert jobs.run_next(env.factory) is True
    test_db.expire_all()
    job = _jobs(test_db, mjobs.JOB_KIND)[0]
    assert job.status == "queued" and job.attempts == 1
    assert job.error == f"JobError: {mjobs.RETRY_MESSAGE}"
    assert _count(test_db, MediaClassification) == 0

    env.classifier.error = None
    test_db.execute(Job.__table__.update().values(run_after=func.now()))
    test_db.commit()
    _run_all(env)
    test_db.expire_all()
    assert _jobs(test_db, mjobs.JOB_KIND)[0].status == "done"
    assert test_db.get(WhatsappMessage, message.id).classification["category"] == "prescription"


@pytest.mark.parametrize(("data", "file_type"), [(JPEG_BYTES, JPEG), (PDF_BYTES, PDF)])
@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda c: setattr(c, "error", _api_error(400)), "api_error_400"),
        (lambda c: setattr(c, "stop_reason", "refusal"), "refusal"),
        (lambda c: setattr(c, "stop_reason", "max_tokens"), "truncated"),
        (lambda c: setattr(c, "output", "no es json"), "invalid_json"),
        (lambda c: setattr(c, "output", {"category": "receta"}), "invalid_json"),
    ],
)
def test_bad_file_or_output_is_recorded_and_falls_back_to_lab_extraction(
    test_db, env, setup, code, data, file_type
):
    setup(env.classifier)
    imp = _import(test_db)
    message = _message(test_db, env.storage, imp, data, file_type)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    job = _jobs(test_db, mjobs.JOB_KIND)[0]
    assert job.status == "failed" and job.attempts == 1
    row = test_db.scalar(select(MediaClassification))
    assert row.status == "failed" and row.error == code and row.category is None
    assert test_db.get(WhatsappMessage, message.id).classification == {
        "status": "failed",
        "error": code,
        "sha256": row.sha256,
    }
    # The cheap classifier could not tell: the extractor decides (as before #37).
    lab = _jobs(test_db, "lab_extract")
    assert [j.payload for j in lab] == [
        {"attachment_id": message.attachment_id, "source_message_id": message.id}
    ]
    assert lab[0].status == "done" and len(env.extractor.calls) == 1

    later = _import(test_db)
    _message(test_db, env.storage, later, data, file_type)
    _hooks(test_db, later)
    _run_all(env)
    assert len(_jobs(test_db, mjobs.JOB_KIND)) == 1
    assert len(_jobs(test_db, "lab_extract")) == 1
    assert len(env.classifier.calls) == 1 and len(env.extractor.calls) == 1


def test_too_large_and_unsupported_are_recorded_without_a_call(test_db, env, monkeypatch):
    monkeypatch.setattr(lab_extract, "IMAGE_MAX_BYTES", 10)
    imp = _import(test_db)
    big = _message(test_db, env.storage, imp)
    heic = _message(test_db, env.storage, imp, HEIC_BYTES, HEIC)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert env.classifier.calls == []
    assert test_db.get(WhatsappMessage, big.id).classification["error"] == "too_large"
    assert test_db.get(WhatsappMessage, heic.id).classification["error"] == "unsupported"
    errors = [j.error for j in _jobs(test_db, mjobs.JOB_KIND)]
    assert mjobs.FAILED_MESSAGES["too_large"] in errors[0]
    assert mjobs.FAILED_MESSAGES["unsupported"] in errors[1]
    assert _jobs(test_db, "lab_extract") == []  # the extractor would refuse them too


def test_too_large_pdf_does_not_fall_back(test_db, env):
    env.settings = Settings(anthropic_api_key="test-key", lab_pdf_max_mb=0)
    imp = _import(test_db)
    big = _message(test_db, env.storage, imp, PDF_BYTES * 40_000, PDF)
    _hooks(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert env.classifier.calls == []
    assert test_db.get(WhatsappMessage, big.id).classification["error"] == "too_large"
    assert _jobs(test_db, "lab_extract") == []
