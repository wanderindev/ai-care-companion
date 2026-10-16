"""Daily WhatsApp analysis (ticket #39): the after_import hook, waiting for
transcripts/classifications (and its bound), grouping by Panamá day, the
record context, the prompt/schema, proposals → suggestions of every kind,
invalid proposals dropped, dedupe against records and pending suggestions,
irrelevant days, multi-job continuation, API failures and log privacy.

Synthetic data only: invented people, doctors, medicines and messages. The
Anthropic API is never called: the analysis runs against a fake client.
"""

import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.attachment import Attachment
from app.models.doctor import Doctor
from app.models.job import Job
from app.models.suggestion import Suggestion
from app.models.transcription import AudioTranscript
from app.models.whatsapp import WhatsappImport, WhatsappMessage
from app.prompts import daily_analysis_v1, daily_analysis_v2
from app.services import appointments, jobs, log_entries, meds
from app.services.attachments import JPEG, OGG, store_upload
from app.services.daily_analysis import analyze, pipeline
from app.services.daily_analysis import jobs as djobs
from app.services.daily_analysis.context import build_context
from app.services.whatsapp_import import AFTER_IMPORT_HOOKS

DAY = date(2026, 5, 4)
SECRET_TEXT = "Texto sintético confidencial Zeta"
SENDER = "Persona Sintética"
RATIONALE = "Razón sintética Omega (#1)"


def _usage():
    return SimpleNamespace(
        input_tokens=2000,
        output_tokens=300,
        cache_creation_input_tokens=1500,
        cache_read_input_tokens=0,
    )


class FakeMessages:
    """``beta.messages`` stand-in: answers from a queue (or a function of the
    request), records every request."""

    def __init__(self):
        self.calls: list[dict] = []
        self.answers: list = []
        self.default: dict | str = {"suggestions": []}
        self.stop_reason = "end_turn"
        self.error: Exception | None = None

    def create(self, **params):
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        answer = self.answers.pop(0) if self.answers else self.default
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(params)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
            usage=_usage(),
        )


def _api_error(status: int):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "error", response=httpx2.Response(status, request=request), body=None
    )


@pytest.fixture
def env(monkeypatch, test_engine):
    state = SimpleNamespace(
        settings=Settings(
            anthropic_api_key="test-key",
            analysis_model="claude-opus-5-5",
            analysis_max_days_per_job=14,
        ),
        fake=FakeMessages(),
        factory=sessionmaker(bind=test_engine, autoflush=False),
    )
    monkeypatch.setattr(
        analyze,
        "make_client",
        lambda key: SimpleNamespace(beta=SimpleNamespace(messages=state.fake)),
    )
    monkeypatch.setattr(djobs, "get_settings", lambda: state.settings)
    return state


@pytest.fixture
def papa(patients):
    return patients[0]


@pytest.fixture
def mama(patients):
    return patients[1]


@pytest.fixture
def doctor(test_db):
    row = Doctor(name="Dra. Ana Prueba", aliases=["Anita Prueba"], specialty="Cardiología")
    test_db.add(row)
    test_db.commit()
    return row


def _import(db, mode="daily", new=1) -> WhatsappImport:
    imp = WhatsappImport(
        mode=mode,
        status="done",
        filename="c.zip",
        size=1,
        chunk_size=1,
        chunk_count=1,
        stats={"messages": {"total": new, "new": new, "duplicate": 0, "system_skipped": 0}},
    )
    db.add(imp)
    db.commit()
    return imp


_seq = iter(range(100_000))


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """A Panamá wall-clock time as UTC (Panamá is UTC-5, no DST)."""
    return datetime.combine(day, time(hour, minute), tzinfo=UTC) + timedelta(hours=5)


def _msg(
    db,
    imp,
    when: datetime,
    text: str = "",
    *,
    media=None,
    storage=None,
    transcript=None,
    classification=None,
) -> WhatsappMessage:
    n = next(_seq)
    attachment = None
    kind = None
    if media is not None:
        data, file_type, kind = media
        attachment = store_upload(
            db,
            storage,
            data=data + str(n).encode(),
            file_type=file_type,
            filename=f"F{n}",
            user_id=None,
        )
    m = WhatsappMessage(
        import_id=imp.id,
        sent_at=when,
        sender=SENDER,
        text=text,
        media_kind=kind,
        attachment_id=attachment.id if attachment else None,
        media_import_id=imp.id if attachment else None,
        transcript=transcript,
        classification=classification,
        dedupe_hash=f"da-{n}",
    )
    db.add(m)
    db.flush()
    if attachment is not None:
        attachment.owner_type, attachment.owner_id = "whatsapp_message", m.id
    db.commit()
    return m


def _enqueue(db, imp, waits=0) -> Job:
    job = djobs.enqueue_analysis(db, imp.id, waits=waits)
    db.commit()
    return job


def _run_all(env) -> None:
    while jobs.run_next(env.factory):
        pass


def _suggestions(db) -> list[Suggestion]:
    db.expire_all()
    return list(db.scalars(select(Suggestion).order_by(Suggestion.id)))


def _stats(db, imp) -> dict:
    db.expire_all()
    return db.get(WhatsappImport, imp.id).stats["analysis"]


def _entry(papa, m, **kw):
    return {
        "kind": "create_entry",
        "patient": "papa",
        "entry_date": DAY.isoformat(),
        "entry_time": "",
        "doctor_id": 0,
        "source_label": "",
        "entry_type": "consulta_control",
        "channel": "presencial",
        "title": "Control de cardiología",
        "indications": "Seguir igual.",
        "family_notes": "",
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
        **kw,
    }


def _walk_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _walk_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _walk_objects(value)


# ── Prompt, schema, request ──────────────────────────────────────────────────


def test_schema_is_strict_and_versioned():
    spec = daily_analysis_v1.SPEC
    assert spec.version == "daily_analysis_v1"
    objects = list(_walk_objects(spec.schema))
    assert len(objects) == 6  # the root + one per kind
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    kinds = [o["properties"]["kind"]["const"] for o in objects[1:]]
    assert kinds == ["create_entry", "update_entry", "create_appointment", "med_change", "doctor"]
    text = json.dumps(spec.schema)
    for unsupported in ("minLength", "maxLength", "minimum", "maximum", '"null"'):
        assert unsupported not in text
    assert "Nunca inventes" in spec.system and "no produce nada" in spec.system


def test_request_params_cache_context_and_fallback():
    params = analyze.request_params(daily_analysis_v1.SPEC, "claude-opus-5-5", "CTX", "MSGS")
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    context, day = params["messages"][0]["content"]
    assert context == {"type": "text", "text": "CTX", "cache_control": {"type": "ephemeral"}}
    assert day == {"type": "text", "text": "MSGS"}
    assert params["output_config"]["effort"] == "medium"
    assert params["output_config"]["format"]["schema"] is daily_analysis_v1.SCHEMA
    assert params["fallbacks"] == "default"
    haiku = analyze.request_params(daily_analysis_v1.SPEC, "claude-haiku-5-5", "C", "M")
    assert "fallbacks" not in haiku


def test_parse_answer():
    assert analyze.parse_answer('{"suggestions": []}') == []
    assert analyze.parse_answer("no json") is None
    assert analyze.parse_answer('{"other": 1}') is None
    assert analyze.parse_answer(None) is None


def test_settings_defaults():
    s = Settings()
    assert s.analysis_model == "claude-opus-5-5"
    assert s.analysis_max_days_per_job == 14


# ── Hook and handler registration ────────────────────────────────────────────


def test_hook_and_handler_registered():
    assert djobs.enqueue_analysis_after_import in AFTER_IMPORT_HOOKS
    from app.worker import HANDLER_MODULES, load_handlers

    load_handlers(HANDLER_MODULES)
    assert "app.services.daily_analysis.jobs" in HANDLER_MODULES
    assert djobs.JOB_KIND in jobs.HANDLERS


@pytest.mark.parametrize(
    ("mode", "new", "queued"),
    [("daily", 3, True), ("daily", 0, False), ("backfill", 3, False), ("labs_only", 3, False)],
)
def test_hook_enqueues_only_daily_with_new_messages(test_db, mode, new, queued):
    imp = _import(test_db, mode, new=new)
    djobs.enqueue_analysis_after_import(test_db, imp)
    test_db.commit()
    rows = list(test_db.scalars(select(Job).where(Job.kind == djobs.JOB_KIND)))
    assert bool(rows) is queued
    if queued:
        job = rows[0]
        assert job.payload == {"import_id": imp.id, "waits": 0}
        assert job.run_after > test_db.scalar(select(func.now())) + timedelta(seconds=60)


# ── Waiting for transcripts / classifications ────────────────────────────────


def test_waits_for_queued_media_then_proceeds_after_the_bound(test_db, env, papa, storage):
    imp = _import(test_db)
    audio = _msg(
        test_db, imp, _at(DAY, 9), media=(b"OggS" + b"\x00" * 30, OGG, "audio"), storage=storage
    )
    _msg(test_db, imp, _at(DAY, 9, 5), "Fuimos al control con la doctora")
    # Its transcription is still running (status set by hand: no worker here).
    busy = jobs.enqueue(test_db, "transcribe_audio", {"attachment_id": audio.attachment_id})
    busy.status = "running"
    _enqueue(test_db, imp)

    assert jobs.run_next(env.factory)
    assert env.fake.calls == []
    waiting = list(test_db.scalars(select(Job).where(Job.kind == djobs.JOB_KIND).order_by(Job.id)))
    assert [j.status for j in waiting] == ["done", "queued"]
    assert waiting[1].payload == {"import_id": imp.id, "waits": 1}
    assert waiting[1].run_after > test_db.scalar(select(func.now())) + timedelta(minutes=2)
    assert _stats(test_db, imp)["status"] == "waiting"
    assert _stats(test_db, imp)["waits"] == 1

    # After MAX_WAITS waits it goes ahead with what it has.
    test_db.delete(waiting[1])
    test_db.commit()
    _enqueue(test_db, imp, waits=djobs.MAX_WAITS)
    env.fake.answers = [{"suggestions": [_entry(papa, audio)]}]
    assert jobs.run_next(env.factory)
    assert len(env.fake.calls) == 1
    day_text = env.fake.calls[0]["messages"][0]["content"][1]["text"]
    assert "1 archivo(s) de este día no se pudieron" in day_text
    assert "(nota de voz sin transcripción)" in day_text
    (row,) = _suggestions(test_db)
    assert row.rationale.endswith(pipeline.MISSING_NOTE)
    stats = _stats(test_db, imp)
    assert stats["status"] == "done" and stats["missing_media"] == 1


def test_unresolved_media_without_a_job_is_not_waited_for(test_db, env, storage):
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), media=(b"OggS" + b"\x00" * 30, OGG, "audio"), storage=storage)
    messages = pipeline.new_messages(test_db, imp.id)
    assert len(pipeline.unresolved_media(test_db, messages)) == 1
    assert pipeline.waiting_media(test_db, messages) == []


def test_recorded_failures_count_as_resolved(test_db, storage):
    imp = _import(test_db)
    audio = _msg(
        test_db, imp, _at(DAY, 9), media=(b"OggS" + b"\x00" * 30, OGG, "audio"), storage=storage
    )
    _msg(
        test_db,
        imp,
        _at(DAY, 9, 1),
        media=(b"\xff\xd8\xff\xe0" + b"x" * 30, JPEG, "image"),
        storage=storage,
        classification={"status": "failed", "error": "refusal", "sha256": "x"},
    )
    _msg(
        test_db,
        imp,
        _at(DAY, 9, 2),
        media=(b"OggS" + b"\x01" * 30, OGG, "audio"),
        storage=storage,
        transcript="",
    )
    sha = test_db.get(Attachment, audio.attachment_id).sha256
    test_db.add(AudioTranscript(sha256=sha, status="failed", error="too_large"))
    test_db.commit()
    assert pipeline.unresolved_media(test_db, pipeline.new_messages(test_db, imp.id)) == []


# ── Grouping, rendering, chunking ────────────────────────────────────────────


def test_groups_by_panama_day(test_db):
    imp = _import(test_db)
    late = _msg(test_db, imp, datetime(2026, 5, 5, 3, 30, tzinfo=UTC), "noche")  # 22:30 on 4/5
    early = _msg(test_db, imp, datetime(2026, 5, 5, 5, 30, tzinfo=UTC), "mañana")  # 00:30 on 5/5
    days = pipeline.group_by_day(pipeline.new_messages(test_db, imp.id))
    assert list(days) == [date(2026, 5, 4), date(2026, 5, 5)]
    assert [m.id for m in days[date(2026, 5, 4)]] == [late.id]
    assert [m.id for m in days[date(2026, 5, 5)]] == [early.id]
    assert pipeline.render_message(late).startswith(f"[#{late.id} 22:30] {SENDER}: noche")


def test_render_media_and_relevance(test_db, storage):
    imp = _import(test_db)
    voice = _msg(
        test_db,
        imp,
        _at(DAY, 8),
        media=(b"OggS" + b"\x00" * 30, OGG, "audio"),
        storage=storage,
        transcript="La doctora dijo que siga igual",
    )
    photo = _msg(
        test_db,
        imp,
        _at(DAY, 8, 1),
        media=(b"\xff\xd8\xff\xe0" + b"x" * 30, JPEG, "image"),
        storage=storage,
        classification={
            "status": "done",
            "category": "prescription",
            "description": "Receta con 2 medicamentos",
            "patient_hint": "mama",
        },
    )
    assert "transcripción automática: «La doctora dijo que siga igual»" in pipeline.render_message(
        voice
    )
    assert "(foto: Receta con 2 medicamentos; tipo: receta; paciente probable: Mamá)" in (
        pipeline.render_message(photo)
    )
    assert pipeline.is_relevant([photo])
    sticker = SimpleNamespace(
        text="", transcript=None, classification={"status": "done", "category": "other"}
    )
    emoji = SimpleNamespace(text="👍 🙏", transcript=None, classification=None)
    assert not pipeline.is_relevant([sticker, emoji])


def test_chunk_lines():
    lines = [(i, "x" * 40) for i in range(10)]
    chunks = pipeline.chunk_lines(lines, max_chars=100)
    assert [len(c) for c in chunks] == [2, 2, 2, 2, 2]
    assert pipeline.chunk_lines([(1, "y" * 500)], max_chars=100) == [[(1, "y" * 500)]]


def test_long_day_is_sent_in_chunks(test_db, env, monkeypatch):
    monkeypatch.setattr(pipeline, "CHUNK_MAX_CHARS", 120)
    imp = _import(test_db)
    for minute in range(4):
        _msg(test_db, imp, _at(DAY, 10, minute), "Mensaje largo de prueba " * 3)
    _enqueue(test_db, imp)
    _run_all(env)
    assert len(env.fake.calls) == 4
    assert "parte 1 de 4" in env.fake.calls[0]["messages"][0]["content"][1]["text"]


# ── Context ──────────────────────────────────────────────────────────────────


def test_context_builder(test_db, papa, mama, doctor):
    test_db.get(type(papa), papa.id).diagnoses = "Diagnóstico sintético"
    meds.save_list(
        test_db,
        papa.id,
        items=[{"name": "Medicina A", "slot": "desayuno", "strength": "50 mg", "quantity": "1"}],
        avoid=[],
        expected_version=0,
        user_id=None,
    )
    entry = log_entries.create_entry(
        test_db,
        {
            "patient_id": papa.id,
            "entry_date": DAY - timedelta(days=3),
            "entry_type": "consulta_control",
            "doctor_id": doctor.id,
            "title": "Control previo",
            "indications": "Indicación sintética " * 20,
        },
        user_id=None,
    )
    appointments.create_appointment(
        test_db,
        {
            "patient_id": mama.id,
            "doctor_id": doctor.id,
            "starts_at": datetime(2026, 5, 20, 10, 0),
            "reason": "Control",
        },
        user_id=None,
        notify=False,
    )
    inactive = Doctor(name="Dr. Retirado Prueba", is_active=False)
    test_db.add(inactive)
    test_db.commit()

    ctx = build_context(test_db, [DAY])
    item = meds.current_items(test_db, papa.id)[0]
    assert ctx.patients == {"papa": papa.id, "mama": mama.id}
    assert ctx.doctor_ids == {doctor.id}
    assert set(ctx.med_items) == {item.id}
    assert ctx.med_versions == {papa.id: 1, mama.id: 0}
    text = ctx.text
    assert "- papa: Papá. Diagnósticos: Diagnóstico sintético" in text
    assert f"- {doctor.id} · Dra. Ana Prueba · Anita Prueba · Cardiología" in text
    assert "Retirado" not in text
    assert f"- {item.id} · Medicina A · 50 mg · 1 · desayuno · -" in text
    assert f"- {entry.id} · {(DAY - timedelta(days=3)).isoformat()} · Dra. Ana Prueba" in text
    assert "…" in text  # long summaries are cut
    assert "- mama · 2026-05-20 10:00 · Dra. Ana Prueba" in text


# ── Proposals → suggestions ──────────────────────────────────────────────────


def test_every_kind_becomes_a_suggestion(test_db, env, papa, mama, doctor):
    meds.save_list(
        test_db,
        papa.id,
        items=[
            {"name": "Medicina A", "slot": "desayuno", "strength": "50 mg"},
            {"name": "Medicina B", "slot": "cena"},
        ],
        avoid=[],
        expected_version=0,
        user_id=None,
    )
    entry = log_entries.create_entry(
        test_db,
        {
            "patient_id": mama.id,
            "entry_date": DAY - timedelta(days=1),
            "entry_type": "consulta_control",
            "title": "Control de ayer",
        },
        user_id=None,
    )
    test_db.commit()
    items = {i.name: i for i in meds.current_items(test_db, papa.id)}
    imp = _import(test_db)
    m1 = _msg(test_db, imp, _at(DAY, 9), "La Dra. Prueba dijo que suspenda la Medicina A")
    m2 = _msg(test_db, imp, _at(DAY, 9, 10), "Próxima cita el 20 a las 3 pm")
    blank = {k: "" for k in ("strength", "quantity", "dose_note", "schedule_note", "instructions")}
    proposals = [
        _entry(papa, m1, doctor_id=doctor.id, entry_time="08:30"),
        {
            "kind": "update_entry",
            "patient": "mama",
            "entry_id": entry.id,
            "title": "",
            "add_indications": "",
            "add_family_notes": "Se sintió bien después.",
            "rationale": RATIONALE,
            "source_message_ids": [m2.id],
        },
        {
            "kind": "create_appointment",
            "patient": "papa",
            "date": "2026-05-20",
            "time": "15:00",
            "doctor_id": doctor.id,
            "location": "",
            "reason": "Control",
            "notes": "",
            "rationale": RATIONALE,
            "source_message_ids": [m2.id],
        },
        {
            "kind": "med_change",
            "patient": "papa",
            "action": "stop",
            "medication": "Medicina A",
            "item_id": items["Medicina A"].id,
            "name": "",
            "slot": "",
            "reason": "",
            "prescriber_id": 0,
            **blank,
            "rationale": RATIONALE,
            "source_message_ids": [m1.id],
        },
        {
            "kind": "med_change",
            "patient": "papa",
            "action": "change",
            "medication": "Medicina B",
            "item_id": items["Medicina B"].id,
            "name": "Otro nombre",
            "slot": "noche",
            "reason": "",
            "prescriber_id": doctor.id,
            **blank,
            "rationale": RATIONALE,
            "source_message_ids": [m1.id],
        },
        {
            "kind": "med_change",
            "patient": "papa",
            "action": "add",
            "medication": "Medicina C",
            "item_id": 0,
            "name": "Medicina C",
            "slot": "almuerzo",
            "reason": "",
            "prescriber_id": 0,
            **{**blank, "quantity": "1"},
            "rationale": RATIONALE,
            "source_message_ids": [m1.id],
        },
        {
            "kind": "doctor",
            "name": "Dr. Nuevo Prueba",
            "aliases": ["Nuevo"],
            "specialty": "Neurología",
            "clinic": "",
            "phone": "",
            "rationale": RATIONALE,
            "source_message_ids": [m1.id, m2.id],
        },
    ]
    env.fake.answers = [{"suggestions": proposals}]
    _enqueue(test_db, imp)
    _run_all(env)

    rows = _suggestions(test_db)
    assert [r.kind for r in rows] == [
        "create_entry",
        "update_entry",
        "create_appointment",
        "med_change",
        "med_change",
        "med_change",
        "doctor",
    ]
    entry_s, update_s, appt_s, stop_s, change_s, add_s, doctor_s = rows
    assert all(r.import_id == imp.id and r.source == "whatsapp" for r in rows)
    assert all(r.status == "pending" and r.rationale == RATIONALE for r in rows)
    assert entry_s.payload == {
        "patient_id": papa.id,
        "entry_date": DAY.isoformat(),
        "entry_time": "08:30:00",
        "doctor_id": doctor.id,
        "entry_type": "consulta_control",
        "channel": "presencial",
        "title": "Control de cardiología",
        "indications": "Seguir igual.",
    }
    assert entry_s.patient_id == papa.id and entry_s.source_message_ids == [m1.id]
    assert update_s.patient_id == mama.id
    assert update_s.payload == {
        "entry_id": entry.id,
        "changes": {"family_notes": "Se sintió bien después."},
    }
    assert appt_s.payload["starts_at"] == "2026-05-20T15:00:00"
    assert appt_s.payload["all_day"] is False and appt_s.payload["doctor_id"] == doctor.id
    assert stop_s.payload == {
        "action": "stop",
        "medication": "Medicina A",
        "item_id": items["Medicina A"].id,
        "base_version": 1,
    }
    assert change_s.payload["item"] == {"slot": "noche", "prescriber_id": doctor.id}
    assert add_s.payload["item"] == {"name": "Medicina C", "slot": "almuerzo", "quantity": "1"}
    assert doctor_s.patient_id is None
    assert doctor_s.payload["name"] == "Dr. Nuevo Prueba"
    assert doctor_s.source_message_ids == [m1.id, m2.id]
    for r in rows:
        assert "attachment_ids" not in r.payload
        assert "attachment_ids" not in r.payload.get("changes", {})

    stats = _stats(test_db, imp)
    assert stats["suggestions_created"] == 7
    assert stats["dropped_invalid"] == 0 and stats["skipped_duplicate"] == 0
    assert stats["prompt_version"] == "daily_analysis_v2"
    assert stats["model"] == "claude-opus-5-5"
    assert stats["days"] == 1 and stats["days_done"] == [DAY.isoformat()]
    assert stats["usage"]["requests"] == 1 and stats["cost_usd"] > 0

    request = env.fake.calls[0]
    context = request["messages"][0]["content"][0]["text"]
    assert "Medicina A" in context and "Dra. Ana Prueba" in context
    day_text = request["messages"][0]["content"][1]["text"]
    assert f"[#{m1.id} 09:00]" in day_text and "lunes 2026-05-04" in day_text


def test_invalid_proposals_are_dropped_and_counted(test_db, env, papa, mama, doctor):
    meds.save_list(
        test_db,
        mama.id,
        items=[{"name": "Medicina M", "slot": "desayuno"}],
        avoid=[],
        expected_version=0,
        user_id=None,
    )
    test_db.commit()
    mama_item = meds.current_items(test_db, mama.id)[0]
    imp = _import(test_db)
    m = _msg(test_db, imp, _at(DAY, 9), "Algo médico pasó hoy")
    other = _import(test_db)
    elsewhere = _msg(test_db, other, _at(DAY, 9, 1), "Otro día")
    stop = {
        "kind": "med_change",
        "patient": "papa",
        "action": "stop",
        "medication": "Medicina M",
        "item_id": mama_item.id,  # Mamá's item for Papá
        "name": "",
        "slot": "",
        "strength": "",
        "quantity": "",
        "dose_note": "",
        "schedule_note": "",
        "instructions": "",
        "reason": "",
        "prescriber_id": 0,
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
    }
    proposals = [
        _entry(papa, m, doctor_id=99_999),  # unknown doctor
        _entry(papa, m, patient="nadie"),  # unknown patient
        _entry(papa, m, source_message_ids=[elsewhere.id]),  # not this day's message
        _entry(papa, m, source_message_ids=[]),
        _entry(papa, m, entry_date="mañana"),  # schema rejects the date
        _entry(papa, m, entry_time="25:99"),
        stop,
        {**stop, "patient": "mama", "medication": "Medicina Distinta", "item_id": mama_item.id},
        {**stop, "action": "add", "item_id": 0, "name": "Medicina N", "slot": ""},  # no slot
        {
            "kind": "update_entry",
            "patient": "papa",
            "entry_id": 123_456,
            "title": "x",
            "add_indications": "",
            "add_family_notes": "",
            "rationale": RATIONALE,
            "source_message_ids": [m.id],
        },
        {"kind": "lab_report", "patient": "papa", "source_message_ids": [m.id]},
        "no es un objeto",
        _entry(papa, m),  # the one valid proposal
    ]
    env.fake.answers = [{"suggestions": proposals}]
    _enqueue(test_db, imp)
    _run_all(env)
    rows = _suggestions(test_db)
    assert len(rows) == 1 and rows[0].kind == "create_entry"
    stats = _stats(test_db, imp)
    assert stats["dropped_invalid"] == len(proposals) - 1
    assert stats["suggestions_created"] == 1


# ── Duplicates ───────────────────────────────────────────────────────────────


def test_dedupe_against_records_and_pending_suggestions(test_db, env, papa, doctor):
    meds.save_list(
        test_db,
        papa.id,
        items=[{"name": "Medicina A", "slot": "desayuno", "strength": "50 mg"}],
        avoid=[],
        expected_version=0,
        user_id=None,
    )
    log_entries.create_entry(
        test_db,
        {
            "patient_id": papa.id,
            "entry_date": DAY,
            "entry_type": "consulta_control",
            "doctor_id": doctor.id,
            "title": "Control cardiología",
        },
        user_id=None,
    )
    appointments.create_appointment(
        test_db,
        {"patient_id": papa.id, "doctor_id": doctor.id, "starts_at": datetime(2026, 5, 20, 15, 30)},
        user_id=None,
        notify=False,
    )
    test_db.commit()
    imp = _import(test_db)
    m = _msg(test_db, imp, _at(DAY, 9), "Fuimos al control")

    def appt(day, at, doctor_id):
        return {
            "kind": "create_appointment",
            "patient": "papa",
            "date": day,
            "time": at,
            "doctor_id": doctor_id,
            "location": "",
            "reason": "",
            "notes": "",
            "rationale": RATIONALE,
            "source_message_ids": [m.id],
        }

    add = {
        "kind": "med_change",
        "patient": "papa",
        "action": "add",
        "medication": "Medicina A 100 mg",
        "item_id": 0,
        "name": "Medicina A",
        "strength": "100 mg",
        "quantity": "",
        "dose_note": "",
        "slot": "noche",
        "schedule_note": "",
        "instructions": "",
        "reason": "",
        "prescriber_id": 0,
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
    }
    new_doctor = {
        "kind": "doctor",
        "name": "Anita Prueba",  # an alias of the existing doctor
        "aliases": [],
        "specialty": "",
        "clinic": "",
        "phone": "",
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
    }
    env.fake.answers = [
        {
            "suggestions": [
                _entry(papa, m, doctor_id=doctor.id, title="Control de cardiología"),  # dup
                _entry(papa, m, doctor_id=doctor.id, title="Vacuna de la influenza"),  # new
                appt("2026-05-20", "15:00", doctor.id),  # within 1 h: dup
                appt("2026-05-20", "18:00", doctor.id),  # new
                appt("2026-05-20", "", 0),  # all-day, same day, any doctor: dup
                add,  # already on the list: dup
                new_doctor,  # dup
                _entry(papa, m, doctor_id=doctor.id, title="Vacuna influenza"),  # pending dup
            ]
        }
    ]
    _enqueue(test_db, imp)
    _run_all(env)
    rows = _suggestions(test_db)
    assert [(r.kind, r.payload.get("title") or r.payload.get("starts_at")) for r in rows] == [
        ("create_entry", "Vacuna de la influenza"),
        ("create_appointment", "2026-05-20T18:00:00"),
    ]
    stats = _stats(test_db, imp)
    assert stats["skipped_duplicate"] == 6 and stats["suggestions_created"] == 2

    # A later import repeating the same facts adds nothing new.
    imp2 = _import(test_db)
    m2 = _msg(test_db, imp2, _at(DAY, 20), "Repito lo de hoy")
    env.fake.answers = [
        {
            "suggestions": [
                _entry(papa, m2, doctor_id=doctor.id, title="Vacuna de la influenza"),
                {**appt("2026-05-20", "18:30", doctor.id), "source_message_ids": [m2.id]},
            ]
        }
    ]
    _enqueue(test_db, imp2)
    _run_all(env)
    assert len(_suggestions(test_db)) == 2
    assert _stats(test_db, imp2)["skipped_duplicate"] == 2


def test_same_entry_rules():
    ctl = "consulta_control"
    obs = "observacion_familiar"
    # With a doctor: alike titles (or an empty one) of the same type.
    assert pipeline.same_entry(7, ctl, "Control de cardiología", ctl, "control cardiologia")
    assert pipeline.same_entry(7, ctl, None, ctl, "Algo")
    assert not pipeline.same_entry(7, ctl, "Control de cardiología", ctl, "Vacuna influenza")
    assert not pipeline.same_entry(7, ctl, "Control", "examen", "Control")
    # Without a doctor: stricter, after the common leading words.
    assert not pipeline.same_entry(None, obs, "Observación: caída", obs, "Observación: fiebre")
    assert pipeline.same_entry(None, obs, "Observación: caída", obs, "observacion caida")
    assert pipeline.same_entry(None, obs, "Caída en el baño", obs, "Caida en el baño.")
    assert not pipeline.same_entry(None, obs, None, obs, "Observación: fiebre")
    assert not pipeline.same_entry(None, obs, "Observación", obs, "Observación: fiebre")


def test_entry_without_doctor_is_skipped_only_when_really_the_same(test_db, env, papa):
    log_entries.create_entry(
        test_db,
        {
            "patient_id": papa.id,
            "entry_date": DAY,
            "entry_type": "observacion_familiar",
            "title": "Observación: caída",
        },
        user_id=None,
    )
    test_db.commit()
    imp = _import(test_db)
    m = _msg(test_db, imp, _at(DAY, 9), "Tiene fiebre desde la tarde")
    obs = {"entry_type": "observacion_familiar", "channel": "", "indications": ""}
    env.fake.answers = [
        {
            "suggestions": [
                _entry(papa, m, title="Observación: fiebre", **obs),  # new event
                _entry(papa, m, title="Observación: caída", **obs),  # the same: skipped
                _entry(papa, m, title="Observación: caída", entry_type="consulta_enfermedad"),
            ]
        }
    ]
    _enqueue(test_db, imp)
    _run_all(env)
    rows = _suggestions(test_db)
    assert [(r.payload["title"], r.payload["entry_type"]) for r in rows] == [
        ("Observación: fiebre", "observacion_familiar"),
        ("Observación: caída", "consulta_enfermedad"),
    ]
    assert _stats(test_db, imp)["skipped_duplicate"] == 1


# ── update_entry is append-only ──────────────────────────────────────────────


def _update(m, entry_id, **kw):
    return {
        "kind": "update_entry",
        "patient": "papa",
        "entry_id": entry_id,
        "title": "",
        "add_indications": "",
        "add_family_notes": "",
        "rationale": RATIONALE,
        "source_message_ids": [m.id],
        **kw,
    }


def test_update_entry_appends_to_the_full_current_text(test_db, env, papa):
    long_text = "Indicación sintética larga número uno. " * 30  # far over the summary
    entry = log_entries.create_entry(
        test_db,
        {
            "patient_id": papa.id,
            "entry_date": DAY,
            "entry_type": "consulta_control",
            "title": "Control",
            "indications": long_text,
        },
        user_id=None,
    )
    bare = log_entries.create_entry(
        test_db,
        {"patient_id": papa.id, "entry_date": DAY, "entry_type": "otro", "source_label": "Nota"},
        user_id=None,
    )
    test_db.commit()
    imp = _import(test_db)
    m = _msg(test_db, imp, _at(DAY, 9), "La doctora agregó reposo")
    env.fake.answers = [
        {
            "suggestions": [
                _update(
                    m,
                    entry.id,
                    title="Título nuevo",  # the entry has one: ignored
                    add_indications="Reposo tres días.",
                    add_family_notes="Lo contó Ana.",
                ),
                _update(m, bare.id, title="Nota de la familia"),  # fills the empty title
                _update(m, entry.id),  # nothing to add
                _update(m, entry.id, add_indications="indicación sintética larga número uno"),
            ]
        }
    ]
    _enqueue(test_db, imp)
    _run_all(env)
    rows = _suggestions(test_db)
    assert len(rows) == 2
    changes = rows[0].payload["changes"]
    assert changes == {
        "indications": long_text.rstrip() + "\n\nReposo tres días.",
        "family_notes": "Lo contó Ana.",
    }
    assert rows[1].payload["changes"] == {"title": "Nota de la familia"}
    assert _stats(test_db, imp)["dropped_invalid"] == 2  # empty / already there


# ── Concurrency, progress, broken requests ───────────────────────────────────


def test_second_job_for_the_same_import_requeues_itself(test_db, env):
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), "Algo médico")
    job = _enqueue(test_db, imp, waits=djobs.MAX_WAITS)
    holder = env.factory()
    try:
        assert pipeline.lock_import(holder, imp.id)  # "another job" holds the import
        assert jobs.run_next(env.factory)
    finally:
        holder.rollback()
        holder.close()
    assert env.fake.calls == []
    test_db.expire_all()
    assert test_db.get(Job, job.id).status == "done"
    again = test_db.scalars(
        select(Job).where(Job.kind == djobs.JOB_KIND, Job.status == "queued")
    ).one()
    assert again.payload == {"import_id": imp.id, "waits": djobs.MAX_WAITS}
    assert again.run_after > test_db.scalar(select(func.now())) + timedelta(minutes=2)
    assert _stats_or_none(test_db, imp) is None  # nothing analyzed


def test_progress_before_every_call(test_db, env, monkeypatch):
    monkeypatch.setattr(pipeline, "CHUNK_MAX_CHARS", 120)
    imp = _import(test_db)
    for minute in range(3):
        _msg(test_db, imp, _at(DAY, 10, minute), "Mensaje largo de prueba " * 3)
    _msg(test_db, imp, _at(DAY + timedelta(days=1), 10), "Otro día")
    seen = []
    pipeline.run_analysis(
        test_db,
        test_db.get(WhatsappImport, imp.id),
        pipeline.new_messages(test_db, imp.id),
        SimpleNamespace(beta=SimpleNamespace(messages=env.fake)),
        djobs.options(),
        progress=lambda pct, msg: seen.append((len(env.fake.calls), msg)),
    )
    assert len(env.fake.calls) == 4
    assert [n for n, _ in seen] == [0, 1, 2, 3]  # one refresh before each call
    assert seen[-1][1] == "Analizando día 2 de 2"


def test_first_call_rejected_fails_the_job(test_db, env):
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    _msg(test_db, imp, _at(DAY + timedelta(days=1), 9), "Algo más")
    env.fake.error = _api_error(400)
    job = _enqueue(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert test_db.get(Job, job.id).status == "failed"
    assert djobs.REQUEST_REFUSED in test_db.get(Job, job.id).error
    assert len(env.fake.calls) == 1
    assert _stats_or_none(test_db, imp) is None  # no day marked done


def test_rejected_call_after_a_success_fails_only_that_day(test_db, env):
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    _msg(test_db, imp, _at(DAY + timedelta(days=1), 9), "Algo más")
    env.fake.answers = [{"suggestions": []}, _api_error(400)]
    job = _enqueue(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert test_db.get(Job, job.id).status == "done"
    stats = _stats(test_db, imp)
    assert stats["days_failed"] == 1 and len(stats["days_done"]) == 2


# ── Irrelevant days, continuation, failures, logs ────────────────────────────


def test_irrelevant_day_makes_no_call(test_db, env, storage):
    imp = _import(test_db)
    _msg(
        test_db,
        imp,
        _at(DAY, 9),
        media=(b"\xff\xd8\xff\xe0" + b"s" * 30, JPEG, "image"),
        storage=storage,
        classification={"status": "done", "category": "other", "description": "Sticker"},
    )
    _msg(test_db, imp, _at(DAY, 9, 1), "👍")
    _enqueue(test_db, imp)
    _run_all(env)
    assert env.fake.calls == []
    stats = _stats(test_db, imp)
    assert stats["days_skipped"] == 1 and stats["days"] == 0
    assert stats["status"] == "done" and stats["days_done"] == [DAY.isoformat()]


def test_chatter_day_answers_nothing(test_db, env):
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), "¿Quién trae el pan mañana?")
    _enqueue(test_db, imp)
    _run_all(env)
    assert len(env.fake.calls) == 1
    assert _suggestions(test_db) == []
    assert _stats(test_db, imp)["suggestions_created"] == 0


def test_max_days_per_job_continues_in_another_job(test_db, env):
    env.settings = Settings(anthropic_api_key="k", analysis_max_days_per_job=2)
    imp = _import(test_db)
    for offset in range(3):
        _msg(test_db, imp, _at(DAY + timedelta(days=offset), 9), f"Mensaje del día {offset}")
    _enqueue(test_db, imp)
    assert jobs.run_next(env.factory)
    assert len(env.fake.calls) == 2
    stats = _stats(test_db, imp)
    assert stats["status"] == "running" and len(stats["days_done"]) == 2
    follow = test_db.scalars(
        select(Job).where(Job.kind == djobs.JOB_KIND, Job.status == "queued")
    ).one()
    assert follow.payload == {"import_id": imp.id, "waits": djobs.MAX_WAITS}
    _run_all(env)
    assert len(env.fake.calls) == 3
    assert _stats(test_db, imp)["status"] == "done"
    assert len(_stats(test_db, imp)["days_done"]) == 3
    # Re-running never analyzes a day twice.
    _enqueue(test_db, imp, waits=djobs.MAX_WAITS)
    _run_all(env)
    assert len(env.fake.calls) == 3


def test_missing_key_fails_the_job(test_db, env):
    env.settings = Settings(anthropic_api_key="")
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    job = _enqueue(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert test_db.get(Job, job.id).status == "failed"
    assert djobs.NOT_CONFIGURED in test_db.get(Job, job.id).error


@pytest.mark.parametrize(("status", "outcome"), [(401, "failed"), (529, "queued")])
def test_api_errors(test_db, env, status, outcome):
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    env.fake.error = _api_error(status)
    job = _enqueue(test_db, imp)
    assert jobs.run_next(env.factory)
    test_db.expire_all()
    assert test_db.get(Job, job.id).status == outcome
    assert _stats_or_none(test_db, imp) in (None, "pending", "running")


def _stats_or_none(db, imp):
    db.expire_all()
    analysis = db.get(WhatsappImport, imp.id).stats.get("analysis")
    return analysis.get("status") if analysis else None


def test_refusal_counts_the_day_as_failed_and_finishes(test_db, env):
    imp = _import(test_db)
    _msg(test_db, imp, _at(DAY, 9), "Algo")
    env.fake.stop_reason = "refusal"
    job = _enqueue(test_db, imp)
    _run_all(env)
    test_db.expire_all()
    assert test_db.get(Job, job.id).status == "done"
    stats = _stats(test_db, imp)
    assert stats["days_failed"] == 1 and stats["days_done"] == [DAY.isoformat()]


def test_logs_carry_no_message_content(test_db, env, papa, caplog):
    imp = _import(test_db)
    m = _msg(test_db, imp, _at(DAY, 9), SECRET_TEXT)
    env.fake.answers = [
        {
            "suggestions": [
                _entry(papa, m, title=SECRET_TEXT),
                _entry(papa, m, doctor_id=424_242, title=SECRET_TEXT),
            ]
        }
    ]
    _enqueue(test_db, imp)
    with caplog.at_level(logging.DEBUG):
        _run_all(env)
    assert len(_suggestions(test_db)) == 1
    logged = caplog.text
    assert "daily analysis: import" in logged
    for secret in (SECRET_TEXT, SENDER, RATIONALE, "Zeta", "Omega"):
        assert secret not in logged


# ── Explicit «Bot: …» requests (#134) ────────────────────────────────────────

BOT_CORRECTION = (
    "Bot corrección MAMÁ LISTA DE MEDICAMENTOS: Suplemento Combinado se reemplaza "
    "con Suplemento Simple 1/d"
)


@pytest.mark.parametrize(
    "text",
    [
        "Bot: reflejar en la lista de medicamentos y bitácora",
        BOT_CORRECTION,
        "bot corrección: algo",
        "BOT, agregar algo",
        "  Bot agregar algo",
        '"Bot: algo"',
        "«Bot» corrección algo",
        "*Bot*: algo",
        "_Bot:_ algo",
        "Bót: algo",
        "Bot",
        "Bot\nsegunda línea",
    ],
)
def test_is_bot_request_accepts(text):
    assert pipeline.is_bot_request(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Bote de basura",
        "Bots",
        "bot2 algo",
        "robot: algo",
        "Robótica",
        "El bot dijo",
        "a Bot: algo",
    ],
)
def test_is_bot_request_rejects(text):
    assert not pipeline.is_bot_request(text)


def test_v2_prompt_adds_explicit_requests_and_keeps_v1():
    spec = daily_analysis_v2.SPEC
    assert spec.version == "daily_analysis_v2"
    assert spec.schema is daily_analysis_v1.SCHEMA
    assert pipeline.EXPLICIT_TAG in spec.system
    rules = daily_analysis_v2.EXPLICIT_RULES
    assert '"stop"' in rules and '"add"' in rules and rules in spec.system
    assert "Nunca cambies de paciente" in spec.system
    assert "Nunca inventes" in spec.system and "no produce nada" in spec.system
    assert daily_analysis_v1.SPEC.version == "daily_analysis_v1"  # v1 untouched
    assert "PEDIDO EXPLÍCITO" not in daily_analysis_v1.SYSTEM_PROMPT


def test_daily_job_uses_v2(env):
    assert djobs.options().spec is daily_analysis_v2.SPEC


_MED_BLANK = (
    "strength",
    "quantity",
    "dose_note",
    "schedule_note",
    "instructions",
    "name",
    "slot",
    "reason",
)


def _med(**kw):
    return {
        "kind": "med_change",
        "patient": "mama",
        "item_id": 0,
        "prescriber_id": 0,
        **{k: "" for k in _MED_BLANK},
        "rationale": RATIONALE,
        **kw,
    }


def _mama_list(db, mama):
    meds.save_list(
        db,
        mama.id,
        items=[
            {"name": "Suplemento Combinado", "slot": "desayuno"},
            {"name": "Medicina Fija", "slot": "cena"},
        ],
        avoid=[],
        expected_version=0,
        user_id=None,
    )
    db.commit()
    return {i.name: i for i in meds.current_items(db, mama.id)}


def test_bot_message_is_tagged_in_the_request(test_db, env, mama):
    imp = _import(test_db, new=2)
    plain = _msg(test_db, imp, _at(DAY, 9), "Mamá durmió bien")
    bot = _msg(test_db, imp, _at(DAY, 9, 5), BOT_CORRECTION)
    _enqueue(test_db, imp)
    _run_all(env)

    request = env.fake.calls[0]
    assert request["system"][0]["text"] == daily_analysis_v2.SYSTEM_PROMPT
    day_text = request["messages"][0]["content"][1]["text"]
    assert f"[#{bot.id} 09:05] {SENDER} {pipeline.EXPLICIT_TAG}: {BOT_CORRECTION}" in day_text
    assert f"[#{plain.id} 09:00] {SENDER}: Mamá durmió bien" in day_text
    assert day_text.count(pipeline.EXPLICIT_TAG) == 1


def test_bot_med_correction_yields_a_tagged_med_change(test_db, env, mama, auth_client):
    items = _mama_list(test_db, mama)
    imp = _import(test_db, new=2)
    plain = _msg(test_db, imp, _at(DAY, 8), "Mamá amaneció con algo de tos")
    bot = _msg(test_db, imp, _at(DAY, 9), BOT_CORRECTION)
    env.fake.answers = [
        {
            "suggestions": [
                _entry(
                    None,
                    plain,
                    patient="mama",
                    entry_type="observacion_familiar",
                    channel="",
                    title="Tos",
                ),
                _med(
                    action="stop",
                    medication="Suplemento Combinado",
                    item_id=items["Suplemento Combinado"].id,
                    rationale="Pedido explícito: reemplazo pedido a las 09:00 (#1).",
                    source_message_ids=[bot.id],
                ),
                _med(
                    action="add",
                    medication="Suplemento Simple",
                    name="Suplemento Simple",
                    slot="desayuno",
                    quantity="1",
                    rationale="Reemplazo pedido a las 09:00 (#1).",
                    source_message_ids=[bot.id],
                ),
            ]
        }
    ]
    _enqueue(test_db, imp)
    _run_all(env)

    rows = _suggestions(test_db)
    assert [r.kind for r in rows] == ["create_entry", "med_change", "med_change"]
    entry_s, stop_s, add_s = rows
    assert entry_s.rationale == RATIONALE  # not explicit: untouched
    assert stop_s.rationale == "Pedido explícito: reemplazo pedido a las 09:00 (#1)."
    assert add_s.rationale == "Pedido explícito: Reemplazo pedido a las 09:00 (#1)."
    for r in (stop_s, add_s):
        assert r.patient_id == mama.id and r.source_message_ids == [bot.id]
    assert stop_s.payload["action"] == "stop"
    assert stop_s.payload["item_id"] == items["Suplemento Combinado"].id
    assert add_s.payload["item"] == {
        "name": "Suplemento Simple",
        "slot": "desayuno",
        "quantity": "1",
    }
    stats = _stats(test_db, imp)
    assert stats["prompt_version"] == "daily_analysis_v2"
    assert stats["explicit_requests"] == 1 and stats["explicit_created"] == 2
    assert stats["explicit_skipped"] == 0 and stats["explicit_unanswered"] == 0

    listed = auth_client.get("/api/v1/suggestions").json()["items"]
    # Explicit requests first (newest first within each group).
    assert [s["id"] for s in listed] == [add_s.id, stop_s.id, entry_s.id]
    assert [s["explicit_request"] for s in listed] == [True, True, False]


def test_explicit_first_only_in_the_pending_inbox(test_db, mama, auth_client):
    from app.services import suggestions as svc

    def add(rationale, source_message_ids=()):
        row = svc.create_suggestion(
            test_db,
            kind="doctor",
            payload={"name": f"Dr. Sintético {rationale[:12]}"},
            rationale=rationale,
            source_message_ids=list(source_message_ids),
        )
        test_db.commit()
        return row

    explicit = add(svc.explicit_rationale("Uno", True))
    plain = add("Dos")
    listed = auth_client.get("/api/v1/suggestions").json()["items"]
    assert [s["id"] for s in listed] == [explicit.id, plain.id]
    history = auth_client.get(
        "/api/v1/suggestions", params={"status": ["pending", "rejected"]}
    ).json()["items"]
    assert [s["id"] for s in history] == [plain.id, explicit.id]


def test_non_bot_day_is_read_exactly_as_in_v1(test_db, env, papa):
    imp = _import(test_db, new=2)
    m1 = _msg(test_db, imp, _at(DAY, 9), "El robot de cocina se dañó")
    m2 = _msg(test_db, imp, _at(DAY, 9, 30), "La Dra. dijo que siga igual")
    env.fake.answers = [{"suggestions": [_entry(papa, m2)]}]
    _enqueue(test_db, imp)
    _run_all(env)

    request = env.fake.calls[0]
    context, day = (b["text"] for b in request["messages"][0]["content"])
    assert day == (
        "MENSAJES NUEVOS del lunes 2026-05-04 (hora de Panamá)\n\n"
        f"[#{m1.id} 09:00] {SENDER}: El robot de cocina se dañó\n"
        f"[#{m2.id} 09:30] {SENDER}: La Dra. dijo que siga igual"
    )
    v1 = analyze.request_params(daily_analysis_v1.SPEC, "claude-opus-5-5", context, day)
    assert request["system"][0]["text"] == daily_analysis_v2.SYSTEM_PROMPT
    assert {k: v for k, v in request.items() if k != "system"} == {
        k: v for k, v in v1.items() if k != "system"
    }
    rows = _suggestions(test_db)
    assert len(rows) == 1 and rows[0].rationale == RATIONALE
    stats = _stats(test_db, imp)
    assert stats["explicit_requests"] == 0 and stats["explicit_created"] == 0


def test_explicit_request_skipped_or_unanswered_is_counted(test_db, env, mama, caplog):
    items = _mama_list(test_db, mama)
    imp = _import(test_db, new=2)
    bot1 = _msg(test_db, imp, _at(DAY, 9), "Bot: " + SECRET_TEXT)
    _msg(test_db, imp, _at(DAY, 10), "bot corrección " + SECRET_TEXT)
    env.fake.answers = [
        {
            "suggestions": [
                # Already on the list: a duplicate, skipped but counted.
                _med(
                    action="add",
                    medication="Medicina Fija",
                    name="Medicina Fija",
                    slot="cena",
                    source_message_ids=[bot1.id],
                ),
                # Not a current item: invalid, skipped but counted.
                _med(
                    action="stop",
                    medication="Otra",
                    item_id=items["Medicina Fija"].id + 999,
                    source_message_ids=[bot1.id],
                ),
            ]
        }
    ]
    _enqueue(test_db, imp)
    with caplog.at_level(logging.INFO, logger="app.daily_analysis"):
        _run_all(env)

    assert _suggestions(test_db) == []
    stats = _stats(test_db, imp)
    assert stats["explicit_requests"] == 2 and stats["explicit_created"] == 0
    assert stats["explicit_skipped"] == 2 and stats["explicit_unanswered"] == 2
    assert stats["skipped_duplicate"] == 1 and stats["dropped_invalid"] == 1
    assert "2 explicit request(s), 2 without a suggestion" in caplog.text
    for secret in (SECRET_TEXT, SENDER, "Zeta"):
        assert secret not in caplog.text


def test_explicit_rationale():
    from app.services import suggestions as svc

    assert svc.explicit_rationale("Razón", True) == "Pedido explícito: Razón"
    assert svc.explicit_rationale("pedido explicito - Razón", True) == "Pedido explícito: Razón"
    assert svc.explicit_rationale(None, True) == "Pedido explícito."
    assert svc.explicit_rationale("Pedido explícito: Razón", False) == "Razón"
    assert svc.explicit_rationale(" Razón ", False) == "Razón"
