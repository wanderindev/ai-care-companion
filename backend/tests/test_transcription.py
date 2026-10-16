"""Voice-note transcription (ticket #36): provider request shape, the
after_import hook, the transcribe_audio job and its SHA-256 dedupe.

Synthetic data only. The OpenAI API is never called: the provider is tested
against an httpx MockTransport and the job against a fake provider.
"""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.medication import SLOTS, MedItem
from app.models.transcription import AudioTranscript
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.services import jobs
from app.services.attachments import AAC, OGG, PDF, store_upload
from app.services.transcription import hints, upload_format
from app.services.transcription import jobs as tjobs
from app.services.transcription.openai_provider import OpenAIProvider
from app.services.transcription.provider import (
    TranscriptionError,
    TranscriptionNotConfigured,
    TranscriptResult,
    get_provider,
)
from app.services.whatsapp_import import AFTER_IMPORT_HOOKS

TRANSCRIPT = "Mañana a las nueve tiene cita con la doctora Prueba, llevar los análisis."
OGG_BYTES = b"OggS" + b"\x00" * 60 + b"synthetic-voice-note-1"

# ── Provider (HTTP mocked) ───────────────────────────────────────────────────


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="sk-test", model="gpt-transcribe", transport=httpx.MockTransport(handler)
    )


def test_openai_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "text": f"  {TRANSCRIPT} ",
                "languages": [{"code": "es"}],
                "usage": {"type": "duration", "seconds": 40},
            },
        )

    result = _provider(handler).transcribe(
        OGG_BYTES,
        filename="nota-de-voz.ogg",
        mime="audio/ogg",
        language="es",
        prompt="Contexto genérico",
        keywords=["Losartán 50", "Dra. <Prueba>", "  "],
    )

    request, body = seen["request"], seen["body"]
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'name="model"\r\n\r\ngpt-transcribe' in body
    assert b'name="response_format"\r\n\r\njson' in body
    assert b'name="languages[]"\r\n\r\nes' in body
    assert b'name="language"\r\n' not in body  # gpt-transcribe: languages[] only
    assert b'name="prompt"\r\n\r\n' + "Contexto genérico".encode() in body
    assert body.count(b'name="keywords[]"') == 2
    assert "Losartán 50".encode() in body and b"Dra. Prueba" in body and b"<Prueba>" not in body
    assert b'filename="nota-de-voz.ogg"' in body
    assert b"Content-Type: audio/ogg" in body
    assert OGG_BYTES in body

    assert result.text == TRANSCRIPT
    assert result.model == "gpt-transcribe"
    assert result.duration_s == 40.0
    assert result.cost_usd == pytest.approx(0.003)
    assert result.usage == {"type": "duration", "seconds": 40}


@pytest.mark.parametrize(
    ("status", "code", "retryable", "config"),
    [
        (429, "http_429", True, False),
        (500, "http_500", True, False),
        (503, "http_503", True, False),
        (401, "http_401", False, True),
        (400, "rejected", False, False),
        (413, "rejected", False, False),
    ],
)
def test_openai_error_mapping(status, code, retryable, config):
    provider = _provider(lambda r: httpx.Response(status, json={"error": {"message": TRANSCRIPT}}))
    with pytest.raises(TranscriptionError) as info:
        provider.transcribe(OGG_BYTES, filename="a.ogg", mime="audio/ogg")
    assert info.value.code == code
    assert info.value.retryable is retryable
    assert info.value.config is config
    assert TRANSCRIPT not in str(info.value)  # provider text is never kept


def test_openai_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TranscriptionError) as info:
        _provider(handler).transcribe(OGG_BYTES, filename="a.ogg", mime="audio/ogg")
    assert info.value.retryable and info.value.code == "timeout"


def test_upload_format_mapping():
    assert upload_format("audio/ogg") == ("ogg", "audio/ogg")  # WhatsApp Ogg Opus, no ffmpeg
    assert upload_format("audio/mpeg") == ("mp3", "audio/mpeg")
    assert upload_format("audio/mp4") == ("m4a", "audio/mp4")
    assert upload_format("audio/webm; codecs=opus") == ("webm", "audio/webm")
    assert upload_format("audio/aac") is None  # raw ADTS is not on OpenAI's list
    assert upload_format("application/pdf") is None
    assert upload_format(None) is None


def test_get_provider_needs_a_key():
    with pytest.raises(TranscriptionNotConfigured):
        get_provider(Settings(openai_api_key=""))
    with pytest.raises(TranscriptionNotConfigured):
        get_provider(Settings(openai_api_key="sk-test", transcription_provider="other"))
    provider = get_provider(Settings(openai_api_key="sk-test", transcription_model="m-1"))
    assert isinstance(provider, OpenAIProvider) and provider.model == "m-1"


def test_settings_defaults():
    settings = Settings()
    assert settings.transcription_provider == "openai"
    assert settings.transcription_model == "gpt-transcribe"
    assert settings.transcription_max_mb == 25


# ── Hook and job ─────────────────────────────────────────────────────────────


class FakeProvider:
    model = "gpt-transcribe"

    def __init__(self):
        self.calls = []
        self.error: TranscriptionError | None = None
        self.text = TRANSCRIPT

    def transcribe(self, data, *, filename, mime, language="es", prompt=None, keywords=()):
        self.calls.append(
            {
                "data": data,
                "filename": filename,
                "mime": mime,
                "language": language,
                "prompt": prompt,
                "keywords": list(keywords),
            }
        )
        if self.error is not None:
            raise self.error
        return TranscriptResult(
            text=self.text,
            model=self.model,
            duration_s=12.5,
            usage={"type": "duration", "seconds": 12.5},
            cost_usd=0.000938,
        )


@pytest.fixture
def tx_env(monkeypatch, storage, test_engine):
    state = SimpleNamespace(
        settings=Settings(openai_api_key="sk-test", transcription_model="gpt-transcribe"),
        provider=FakeProvider(),
        storage=storage,
        factory=sessionmaker(bind=test_engine, autoflush=False),
    )
    monkeypatch.setattr(tjobs, "get_settings", lambda: state.settings)
    monkeypatch.setattr(tjobs, "get_storage", lambda: storage)
    monkeypatch.setattr(tjobs, "get_provider", lambda settings: state.provider)
    return state


_minute = iter(range(10_000))


def _import(db, mode="daily") -> WhatsappImport:
    imp = WhatsappImport(
        mode=mode, status="done", filename="c.zip", size=1, chunk_size=1, chunk_count=1
    )
    db.add(imp)
    db.flush()
    return imp


def _audio_message(db, storage, imp, data=OGG_BYTES, file_type=OGG) -> WhatsappMessage:
    attachment = store_upload(
        db, storage, data=data, file_type=file_type, filename="PTT-0001.opus", user_id=None
    )
    n = next(_minute)
    message = WhatsappMessage(
        import_id=imp.id,
        sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=UTC).replace(minute=n % 60, second=n // 60),
        sender="Ana Prueba",
        text="",
        media_kind="audio",
        media_filename="PTT-0001.opus",
        attachment_id=attachment.id,
        media_import_id=imp.id,
        dedupe_hash=f"hash-{n}",
    )
    db.add(message)
    db.flush()
    attachment.owner_type, attachment.owner_id = "whatsapp_message", message.id
    db.flush()
    return message


def _transcribe_jobs(db) -> list[Job]:
    return list(db.scalars(select(Job).where(Job.kind == tjobs.JOB_KIND).order_by(Job.id)))


def _run_all(env) -> None:
    while jobs.run_next(env.factory):
        pass


def test_hook_is_registered():
    assert tjobs.enqueue_transcriptions in AFTER_IMPORT_HOOKS
    from app.worker import HANDLER_MODULES

    assert "app.services.transcription.jobs" in HANDLER_MODULES
    assert tjobs.JOB_KIND in jobs.HANDLERS


def test_hook_skips_labs_only(test_db, tx_env):
    imp = _import(test_db, mode="labs_only")
    _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    assert _transcribe_jobs(test_db) == []


@pytest.mark.parametrize("mode", ["daily", "backfill"])
def test_same_audio_twice_is_sent_once(test_db, tx_env, mode, caplog):
    imp = _import(test_db, mode=mode)
    first = _audio_message(test_db, tx_env.storage, imp)
    second = _audio_message(test_db, tx_env.storage, imp)  # same bytes, forwarded
    pdf = store_upload(
        test_db, tx_env.storage, data=b"%PDF-1.4 x", file_type=PDF, filename="a.pdf", user_id=None
    )
    assert pdf.kind != "audio"
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()

    queued = _transcribe_jobs(test_db)
    assert len(queued) == 1
    assert queued[0].payload == {"attachment_id": first.attachment_id}  # ids only

    caplog.set_level(logging.INFO, logger="app")
    _run_all(tx_env)
    test_db.expire_all()

    assert len(tx_env.provider.calls) == 1
    call = tx_env.provider.calls[0]
    assert call["data"] == OGG_BYTES
    assert call["filename"] == "nota-de-voz.ogg"  # never the WhatsApp file name
    assert call["mime"] == "audio/ogg"
    assert call["language"] == "es"
    assert call["prompt"] == hints.PROMPT

    assert test_db.get(Job, queued[0].id).status == "done"
    assert test_db.get(WhatsappMessage, first.id).transcript == TRANSCRIPT
    assert test_db.get(WhatsappMessage, second.id).transcript == TRANSCRIPT
    row = test_db.scalar(select(AudioTranscript))
    assert row.status == "done" and row.text == TRANSCRIPT and row.model == "gpt-transcribe"
    assert float(row.duration_s) == 12.5
    assert row.usage["cost_usd"] == pytest.approx(0.000938)

    # No transcript text, sender or file name in the logs.
    assert "transcription: attachment" in caplog.text
    for secret in (TRANSCRIPT, "Ana Prueba", "PTT-0001"):
        assert secret not in caplog.text

    # A later import with the same file reuses the transcript, no new job/call.
    later = _import(test_db, mode="daily")
    third = _audio_message(test_db, tx_env.storage, later)
    tjobs.enqueue_transcriptions(test_db, later)
    test_db.commit()
    test_db.expire_all()
    assert len(_transcribe_jobs(test_db)) == 1
    assert test_db.get(WhatsappMessage, third.id).transcript == TRANSCRIPT
    assert len(tx_env.provider.calls) == 1


def test_duplicate_job_for_known_hash_only_copies(test_db, tx_env):
    imp = _import(test_db)
    message = _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcription(test_db, message.attachment_id)
    tjobs.enqueue_transcription(test_db, message.attachment_id)
    test_db.commit()
    _run_all(tx_env)
    test_db.expire_all()
    assert len(tx_env.provider.calls) == 1
    assert [j.status for j in _transcribe_jobs(test_db)] == ["done", "done"]
    assert test_db.scalar(select(func.count()).select_from(AudioTranscript)) == 1


def test_without_key_fails_permanently_and_records_nothing(test_db, tx_env, monkeypatch):
    monkeypatch.setattr(tjobs, "get_provider", get_provider)
    tx_env.settings = Settings(openai_api_key="")
    imp = _import(test_db)
    message = _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    _run_all(tx_env)
    test_db.expire_all()
    job = _transcribe_jobs(test_db)[0]
    assert job.status == "failed" and job.attempts == 1
    assert job.error == f"PermanentJobError: {tjobs.NOT_CONFIGURED}"
    assert test_db.scalar(select(func.count()).select_from(AudioTranscript)) == 0
    assert test_db.get(WhatsappMessage, message.id).transcript is None


def test_server_error_retries_with_backoff(test_db, tx_env):
    tx_env.provider.error = TranscriptionError("http_503", retryable=True)
    imp = _import(test_db)
    message = _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    assert jobs.run_next(tx_env.factory) is True
    test_db.expire_all()
    job = _transcribe_jobs(test_db)[0]
    assert job.status == "queued" and job.attempts == 1
    assert job.error == f"JobError: {tjobs.RETRY_MESSAGE}"
    assert test_db.scalar(select(func.count()).select_from(AudioTranscript)) == 0

    # The next attempt succeeds.
    tx_env.provider.error = None
    test_db.execute(Job.__table__.update().values(run_after=func.now()))
    test_db.commit()
    _run_all(tx_env)
    test_db.expire_all()
    assert _transcribe_jobs(test_db)[0].status == "done"
    assert test_db.get(WhatsappMessage, message.id).transcript == TRANSCRIPT


def test_rejected_file_is_recorded_and_never_resent(test_db, tx_env):
    tx_env.provider.error = TranscriptionError("rejected")
    imp = _import(test_db)
    _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    _run_all(tx_env)
    test_db.expire_all()
    assert _transcribe_jobs(test_db)[0].status == "failed"
    row = test_db.scalar(select(AudioTranscript))
    assert row.status == "failed" and row.error == "rejected" and row.text is None

    later = _import(test_db)
    _audio_message(test_db, tx_env.storage, later)
    tjobs.enqueue_transcriptions(test_db, later)
    test_db.commit()
    assert len(_transcribe_jobs(test_db)) == 1
    assert len(tx_env.provider.calls) == 1


def test_config_error_from_provider_records_nothing(test_db, tx_env):
    tx_env.provider.error = TranscriptionError("http_401", config=True)
    imp = _import(test_db)
    _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    _run_all(tx_env)
    test_db.expire_all()
    job = _transcribe_jobs(test_db)[0]
    assert job.status == "failed" and job.attempts == 1
    assert test_db.scalar(select(func.count()).select_from(AudioTranscript)) == 0


def test_too_large_is_skipped_and_recorded(test_db, tx_env):
    tx_env.settings = Settings(openai_api_key="sk-test", transcription_max_mb=0)
    imp = _import(test_db)
    _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    _run_all(tx_env)
    test_db.expire_all()
    job = _transcribe_jobs(test_db)[0]
    assert job.status == "failed" and job.attempts == 1
    assert tjobs.FAILED_MESSAGES["too_large"] in job.error
    assert tx_env.provider.calls == []
    row = test_db.scalar(select(AudioTranscript))
    assert row.status == "failed" and row.error == "too_large"


def test_unsupported_format_is_recorded(test_db, tx_env):
    imp = _import(test_db)
    _audio_message(test_db, tx_env.storage, imp, data=b"\xff\xf1" + b"\x00" * 40, file_type=AAC)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    _run_all(tx_env)
    test_db.expire_all()
    assert tx_env.provider.calls == []
    assert test_db.scalar(select(AudioTranscript)).error == "unsupported"


def test_keyword_hints_are_med_and_doctor_names_capped(test_db, tx_env, patients):
    papa, mama = patients
    test_db.add_all(
        [
            MedItem(patient_id=papa.id, name="Losartán", slot=SLOTS[0]),
            MedItem(patient_id=mama.id, name="Metformina", slot=SLOTS[0]),
            MedItem(patient_id=mama.id, name="losartán", slot=SLOTS[0]),  # duplicate
            Doctor(name="Dra. Prueba Uno"),
            Doctor(name="Dr. Retirado", is_active=False),
        ]
    )
    test_db.commit()
    words = hints.keywords(test_db)
    assert words == ["Losartán", "Metformina", "Dra. Prueba Uno"]

    test_db.add_all([Doctor(name=f"Dr. Sintético {i:03d}") for i in range(100)])
    test_db.commit()
    words = hints.keywords(test_db)
    assert sum(len(w) for w in words) <= hints.KEYWORDS_MAX_CHARS
    assert words[:2] == ["Losartán", "Metformina"]

    imp = _import(test_db)
    _audio_message(test_db, tx_env.storage, imp)
    tjobs.enqueue_transcriptions(test_db, imp)
    test_db.commit()
    _run_all(tx_env)
    assert tx_env.provider.calls[0]["keywords"] == words
