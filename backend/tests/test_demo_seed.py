"""``scripts.demo_seed`` / ``scripts.demo_export`` (issue #142): the local-only
guard, and a smoke run of the seed. All data is the scripts' synthetic set."""

import zipfile
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models.appointment import Appointment
from app.models.lab import LabReport, LabResult
from app.models.log_entry import LogEntry
from app.models.medication import MedListVersion
from app.services import ledd
from app.services.whatsapp.export import parse_export
from scripts import demo_export, demo_seed
from scripts import demo_seed_data as data

LOCAL = {
    "environment": "development",
    "database_url": "postgresql://n16chat:localdev@db:5432/n16chat",
    "do_spaces_endpoint": "http://minio:9000",
}


def settings(**changes) -> Settings:
    return Settings(**{**LOCAL, **changes})


def test_guard_accepts_the_compose_stack():
    demo_seed.check_local(settings())
    demo_seed.check_local(settings(database_url="postgresql+psycopg://n16chat:x@db/n16chat"))


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "production"},
        {"environment": "test"},
        {"environment": "Development"},
        {"database_url": "postgresql://n16chat:localdev@localhost:5434/n16chat"},
        {"database_url": "postgresql://doadmin:x@db-postgresql-nyc1-0.example.com:25060/n16chat"},
        {"database_url": "postgresql://n16chat:localdev@db:5432/other"},
        {"database_url": "postgresql://n16chat:localdev@db:6432/n16chat"},
        {"database_url": "not a url"},
        {"do_spaces_endpoint": "https://nyc3.digitaloceanspaces.com"},
        {"do_spaces_endpoint": ""},
    ],
)
def test_guard_refuses_anything_else(changes):
    with pytest.raises(demo_seed.NotLocal):
        demo_seed.check_local(settings(**changes))


def test_scripts_refuse_outside_the_local_stack(capsys):
    # The test process runs with ENVIRONMENT=test and a localhost database.
    assert demo_seed.main([]) == 2
    assert demo_seed.main(["--wipe"]) == 2
    assert demo_export.main(["import"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_shift_is_whole_weeks():
    assert demo_seed.shift_days(data.ANCHOR) == 0
    assert demo_seed.shift_days(data.ANCHOR + timedelta(days=6)) == 0
    assert demo_seed.shift_days(data.ANCHOR + timedelta(days=16)) == 14


def test_written_dates_follow_the_shift():
    text = "jueves 3 de diciembre a las 8:00"
    assert demo_export.shift_written_dates(text, timedelta(0), 2026) == text
    assert demo_export.shift_written_dates(text, timedelta(days=28), 2026) == (
        "jueves 31 de diciembre a las 8:00"
    )


def test_committed_export_parses():
    with zipfile.ZipFile(demo_export.committed_zip()) as archive:
        assert sorted(archive.namelist()) == sorted(
            [demo_export.CHAT_NAME, demo_export.VOICE_NAME, demo_export.PDF_NAME]
        )
    with demo_export.committed_zip().open("rb") as stream:
        report = parse_export(stream)
    kinds = sorted(m.media_kind for m in report.messages if m.media_kind)
    assert kinds == ["audio", "pdf"]
    assert report.missing_media == 0
    assert any(m.text.startswith("Bot:") for m in report.messages)


def test_seed_smoke(test_db, patients):
    today = date.today()  # entries are checked against the real date
    shift = timedelta(days=demo_seed.shift_days(today))
    for _ in range(2):  # idempotent
        demo_seed.reset(test_db)
        counts = demo_seed.seed(test_db, today=today)
    assert demo_seed.is_demo(test_db)
    assert counts["shift_days"] == shift.days
    assert test_db.scalar(select(func.count()).select_from(LogEntry)) == len(data.ENTRIES)
    assert counts["pending_review"] >= 2
    assert test_db.scalar(select(func.max(LogEntry.entry_date))) <= today
    upcoming = test_db.scalar(
        select(func.count()).select_from(Appointment).where(Appointment.status == "programada")
    )
    assert upcoming >= 5
    assert test_db.scalar(select(func.count()).select_from(LabReport)) == len(data.LAB_REPORTS)
    flagged = test_db.scalar(
        select(func.count()).select_from(LabResult).where(LabResult.flag.in_(("alto", "bajo")))
    )
    assert flagged > 5
    papa = next(p for p in patients if p.slug == "papa")
    latest = test_db.scalar(
        select(MedListVersion)
        .where(MedListVersion.patient_id == papa.id)
        .order_by(MedListVersion.version_no.desc())
    )
    assert latest.version_no == len(data.MED_VERSIONS["papa"])
    assert ledd.compute(latest.snapshot["items"])["total_mg"] > 0
    assert latest.effective_date == date(2026, 9, 10) + shift
