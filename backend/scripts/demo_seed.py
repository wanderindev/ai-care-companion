"""Seed the LOCAL stack with synthetic demo data (issue #142).

For the product write-up's screenshots and the live chat demo: two patients
with invented identities, eight invented doctors, ~60 bitácora entries over
six months, appointments, medication-list versions (with a levodopa regimen,
so LEDD renders), EVITAR items and lab reports with trends. Every value is
synthetic (``scripts/demo_seed_data.py``); see ``docs/demo.md``.

    docker compose exec backend python -m scripts.demo_seed [--wipe]

**Local only, no override**: the script refuses to run unless
``ENVIRONMENT=development``, the database is the compose ``db`` service and
file storage is the compose ``minio`` service (:func:`check_local`). There is
deliberately no flag that skips this check.

Idempotent: every run first **empties** the app's data tables (bitácora,
appointments, doctors, medications, labs, WhatsApp imports, suggestions,
chat, jobs, audit log) and seeds again; users and the two patient rows stay
(the patients' private fields are rewritten). A database holding records
this script did not seed is refused unless ``--wipe`` is passed, so a local
copy of other data is never emptied by accident.

Dates follow the storyline calendar of ``demo_seed_data`` moved forward by
whole weeks (:func:`shift_days`), so the story always ends in the last week.
The writes go through the app's services (``create_entry``,
``create_appointment``, ``save_list``, ``create_event``, ``create_doctor``)
so audit rows and medication versions look like real use.
"""

import argparse
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, exists, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.models.appointment import STATUS_DONE, Appointment
from app.models.doctor import Doctor
from app.models.lab import Analyte, LabReport, LabResult
from app.models.log_entry import REVIEW_PENDING, LogEntry
from app.models.medication import MedListVersion
from app.models.patient import Patient
from app.models.user import ROLE_ADMIN, ROLE_MEMBER, User
from app.models.whatsapp import WhatsappMessage
from app.services import appointments, audit, doctors, log_entries, meds, patients
from scripts import demo_seed_data as data

LOCAL_ENVIRONMENT = "development"
COMPOSE_DB_HOST = "db"
COMPOSE_DB_PORT = 5432
COMPOSE_DB_NAME = "n16chat"
COMPOSE_STORAGE_HOST = "minio"

# Emptied on every run (one TRUNCATE, no CASCADE: a new table pointing at one
# of these makes the reset fail loudly instead of silently emptying it).
DATA_TABLES = (
    "suggestions",
    "chat_messages",
    "chat_sessions",
    "analysis_runs",
    "lab_results",
    "lab_reports",
    "lab_extractions",
    "lab_links",
    "media_classifications",
    "audio_transcripts",
    "whatsapp_messages",
    "whatsapp_imports",
    "med_check_dismissals",
    "med_events",
    "med_items",
    "med_avoid",
    "med_list_versions",
    "appointments",
    "log_entries",
    "attachments",
    "doctors",
    "ledd_substances",
    "jobs",
    "audit_log",
)


class NotLocal(RuntimeError):
    """The environment is not the local compose stack."""


def check_local(settings: Settings) -> None:
    """Refuse anything but the local compose stack. Raises :class:`NotLocal`.

    Three independent checks, all required: ``ENVIRONMENT`` is
    ``development`` (production's is ``production``), the database URL points
    at the compose service ``db:5432/n16chat`` (production uses the managed
    cluster's host name) and the file store is the compose ``minio`` service
    (production uses DO Spaces)."""
    problems = []
    if settings.environment != LOCAL_ENVIRONMENT:
        problems.append(f"ENVIRONMENT must be {LOCAL_ENVIRONMENT!r}")
    try:
        url = make_url(settings.database_url)
        host, port, name = url.host, url.port or COMPOSE_DB_PORT, url.database
    except ArgumentError:
        host, port, name = None, None, None
    if (host, port, name) != (COMPOSE_DB_HOST, COMPOSE_DB_PORT, COMPOSE_DB_NAME):
        problems.append(
            f"the database must be the compose service "
            f"{COMPOSE_DB_HOST}:{COMPOSE_DB_PORT}/{COMPOSE_DB_NAME}"
        )
    if urlsplit(settings.do_spaces_endpoint or "").hostname != COMPOSE_STORAGE_HOST:
        problems.append(f"file storage must be the compose service {COMPOSE_STORAGE_HOST!r}")
    if problems:
        raise NotLocal("; ".join(problems))


# ── Dates ────────────────────────────────────────────────────────────────────


def shift_days(today: date) -> int:
    """Whole weeks from the storyline's anchor to ``today`` (weekdays stay true)."""
    return 7 * ((today - data.ANCHOR).days // 7)


class Calendar:
    def __init__(self, today: date):
        self.today = today
        self.delta = timedelta(days=shift_days(today))

    def day(self, value: str) -> date:
        return data.d(value) + self.delta

    def at(self, value: str, hhmm: str | None) -> datetime:
        return appointments.local_datetime(
            self.day(value), time.fromisoformat(hhmm) if hhmm else None
        )


# ── Reset ────────────────────────────────────────────────────────────────────


def has_records(db: DbSession) -> bool:
    return any(
        db.scalar(select(exists().select_from(model)))
        for model in (LogEntry, Appointment, Doctor, MedListVersion, LabReport, WhatsappMessage)
    )


def is_demo(db: DbSession) -> bool:
    """True when the patients carry the demo identities (an earlier seed)."""
    rows = {p.slug: p.full_name for p in db.scalars(select(Patient))}
    return all(rows.get(slug) == p["full_name"] for slug, p in data.PATIENTS.items())


def reset(db: DbSession) -> None:
    """Empty the data tables and the analytes no migration seeded. Commits."""
    db.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY"))
    extra = [name for name, *_ in data.EXTRA_ANALYTES]
    db.execute(delete(Analyte).where(Analyte.needs_review | Analyte.canonical_name.in_(extra)))
    db.execute(update(Analyte).values(triage=None))
    db.commit()


# ── Seed ─────────────────────────────────────────────────────────────────────


def _users(db: DbSession) -> dict[str, User]:
    out = {}
    for key, (email, name, role, can_import) in data.USERS.items():
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email)
            db.add(user)
        user.display_name = name
        user.role = ROLE_ADMIN if role == "admin" else ROLE_MEMBER
        user.can_import = can_import
        user.is_active = True
        out[key] = user
    db.flush()
    return out


def _patients(db: DbSession, user_id: int) -> dict[str, Patient]:
    rows = {p.slug: p for p in db.scalars(select(Patient))}
    missing = set(data.PATIENTS) - set(rows)
    if missing:
        raise RuntimeError(f"patient rows missing (run the migrations): {sorted(missing)}")
    for slug, fields in data.PATIENTS.items():
        patients.update_patient(db, rows[slug], dict(fields), user_id=user_id)  # commits
    return rows


def _doctors(db: DbSession, user_id: int) -> dict[str, Doctor]:
    out = {}
    for key, (name, aliases, specialty, phone, email, clinic) in data.DOCTORS.items():
        out[key] = doctors.create_doctor(
            db,
            {
                "name": name,
                "aliases": aliases,
                "specialty": specialty,
                "phone": phone,
                "email": email,
                "clinic": clinic,
            },
            user_id=user_id,
        )
    return out


def _analytes(db: DbSession) -> dict[str, Analyte]:
    for name, aliases, unit, category in data.EXTRA_ANALYTES:
        if db.scalar(select(Analyte).where(Analyte.canonical_name == name)) is None:
            db.add(
                Analyte(
                    canonical_name=name,
                    aliases=aliases,
                    default_unit=unit,
                    category=category,
                    specimen="sangre",
                )
            )
    db.flush()
    needed = {r[0]: r[2] for _, _, _, results in data.LAB_REPORTS for r in results}
    rows = {a.canonical_name: a for a in db.scalars(select(Analyte))}
    for name, unit in needed.items():  # migration 0007 has them; an empty test DB does not
        if name not in rows:
            rows[name] = Analyte(canonical_name=name, default_unit=unit, specimen="sangre")
            db.add(rows[name])
    db.flush()
    return rows


def _backdate(row: Any, when: datetime) -> None:
    row.created_at = when
    row.updated_at = when


def _entries(db: DbSession, cal: Calendar, users: dict, pts: dict, docs: dict) -> list[LogEntry]:
    created = []
    for day, slug, doc, etype, channel, title, indications, notes, author, extra in data.ENTRIES:
        user_id = users[author].id
        pending = extra.get("pending", False)
        entry_date = cal.day(day)
        fields = {
            "patient_id": pts[slug].id,
            "entry_date": entry_date,
            "entry_time": time.fromisoformat(extra["time"]) if extra.get("time") else None,
            "doctor_id": docs[doc].id if doc else None,
            "source_label": extra.get("source"),
            "entry_type": etype,
            "channel": channel,
            "title": title,
            "indications": indications,
            "family_notes": notes,
        }
        if pending:
            fields["review_status"] = REVIEW_PENDING
        entry = log_entries.create_entry(
            db,
            fields,
            user_id=user_id,
            origin="import_docx" if pending else "manual",
            origin_ref={"demo_seed": True} if pending else None,
        )
        _backdate(entry, cal.at(day, extra.get("time") or "20:00") + timedelta(hours=2))
        if extra.get("appt"):
            appt = appointments.create_appointment(
                db,
                {
                    "patient_id": entry.patient_id,
                    "doctor_id": entry.doctor_id,
                    "starts_at": cal.at(day, extra.get("time")),
                    "duration_min": 60 if etype == "terapia" else 30,
                    "location": data.DOCTORS[doc][5] if doc else None,
                    "reason": title,
                },
                user_id=user_id,
                status=STATUS_DONE,
                result_entry_id=entry.id,
                notify=False,
            )
            _backdate(appt, cal.at(day, extra.get("time")) - timedelta(days=14))
        for index, (kind, medication, detail, event_pending) in enumerate(extra.get("event", [])):
            meds.create_event(
                db,
                {
                    "patient_id": entry.patient_id,
                    "event_date": entry_date,
                    "kind": kind,
                    "medication": medication,
                    "detail": detail,
                    "review_status": REVIEW_PENDING if event_pending else "ok",
                    "source_entry_id": entry.id,
                    "source_index": index,
                },
                user_id=user_id,
            )
        created.append(entry)
    db.flush()
    return created


def _upcoming(db: DbSession, cal: Calendar, users: dict, pts: dict, docs: dict) -> int:
    for day, hhmm, slug, doc, reason, location, duration, notes, status in data.UPCOMING:
        appt = appointments.create_appointment(
            db,
            {
                "patient_id": pts[slug].id,
                "doctor_id": docs[doc].id if doc else None,
                "starts_at": cal.at(day, hhmm),
                "all_day": hhmm is None,
                "duration_min": duration,
                "location": location,
                "reason": reason,
                "notes": notes,
            },
            user_id=users["marta"].id,
            notify=False,
        )
        if status != appt.status:
            appointments.update_appointment(
                db, appt, {"status": status}, user_id=users["marta"].id, notify=False
            )
    db.flush()
    return len(data.UPCOMING)


def _item(spec: dict, cal: Calendar, docs: dict) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "strength": spec.get("strength"),
        "quantity": spec.get("quantity"),
        "dose_note": None,
        "slot": spec["slot"],
        "time": time.fromisoformat(spec["time"]) if spec.get("time") else None,
        "days": None,
        "schedule_note": spec.get("schedule_note"),
        "instructions": spec.get("instructions"),
        "prescriber_id": docs[spec["prescriber"]].id if spec.get("prescriber") else None,
        "reason": spec.get("reason"),
        "start_date": cal.day(spec["start"]) if spec.get("start") else None,
        "end_date": cal.day(spec["end"]) if spec.get("end") else None,
        "source_entry_id": None,
    }


def _med_versions(db: DbSession, cal: Calendar, users: dict, pts: dict, docs: dict) -> int:
    count = 0
    for slug, versions in data.MED_VERSIONS.items():
        patient_id = pts[slug].id
        item_ids: dict[str, int] = {}
        for number, version in enumerate(versions, start=1):
            day = cal.day(version["date"])
            avoid_ids = {r.substance: r.id for r in meds.current_avoid(db, patient_id)}
            items = []
            for key, spec in version["items"].items():
                item = _item(spec, cal, docs)
                if key in item_ids:
                    item["id"] = item_ids[key]
                items.append(item)
            avoid = [
                {"substance": s, "reason": r, "source_entry_id": None}
                | ({"id": avoid_ids[s]} if s in avoid_ids else {})
                for s, r in version["avoid"]
            ]
            db.commit()  # save_list rolls back on an empty diff
            outcome = meds.save_list(
                db,
                patient_id,
                items=items,
                avoid=avoid,
                expected_version=number - 1,
                change_summary=version["summary"],
                user_id=users["marta" if slug == "mama" else "andres"].id,
                effective_date=day,
                today=day,
            )
            if not outcome.saved:
                raise RuntimeError(f"medication version {slug} #{number} made no change")
            outcome.version.changed_at = cal.at(version["date"], "21:00")
            _backdate(outcome.version, outcome.version.changed_at)
            db.flush()
            claimed = set(item_ids.values())
            current = meds.current_items(db, patient_id, day)
            for key, spec in version["items"].items():
                if key in item_ids:
                    continue
                for row in current:
                    if row.id in claimed:
                        continue
                    if (row.name, row.strength, row.slot) == (
                        spec["name"],
                        spec.get("strength"),
                        spec["slot"],
                    ):
                        item_ids[key] = row.id
                        claimed.add(row.id)
                        break
            count += 1
    db.commit()
    return count


def _flag(value: float, low: float | None, high: float | None) -> str:
    if high is not None and value > high:
        return "alto"
    if low is not None and value < low:
        return "bajo"
    return "normal"


def _ref_text(low: float | None, high: float | None) -> str:
    if low is not None and high is not None:
        return f"{low:g} - {high:g}"
    return f"< {high:g}" if high is not None else f"> {low:g}"


def _labs(db: DbSession, cal: Calendar, users: dict, pts: dict, analytes: dict) -> int:
    for collected, reported, slug, results in data.LAB_REPORTS:
        report = LabReport(
            patient_id=pts[slug].id,
            collected_on=cal.day(collected),
            reported_on=cal.day(reported),
            lab_name=data.LAB_NAME,
            source="upload",
            status="ok",
            patient_match="cip",
            created_by=users["marta"].id,
            updated_by=users["marta"].id,
        )
        db.add(report)
        db.flush()
        for position, (name, value, unit, low, high, panel) in enumerate(results):
            db.add(
                LabResult(
                    report_id=report.id,
                    analyte_id=analytes[name].id,
                    position=position,
                    panel=panel,
                    raw_name=name,
                    value_text=f"{value:g}",
                    value_num=Decimal(str(value)),
                    unit=unit,
                    ref_low=None if low is None else Decimal(str(low)),
                    ref_high=None if high is None else Decimal(str(high)),
                    ref_text=_ref_text(low, high),
                    flag=_flag(value, low, high),
                )
            )
        _backdate(report, cal.at(reported, "16:00"))
        audit.record(
            db,
            user_id=users["marta"].id,
            action=audit.ACTION_CREATE,
            entity="lab_report",
            entity_id=report.id,
            after={"patient_id": report.patient_id, "results": len(results), "demo_seed": True},
        )
    db.commit()
    return len(data.LAB_REPORTS)


def seed(db: DbSession, *, today: date | None = None) -> dict[str, int]:
    """Seed an emptied database (call :func:`reset` first). Commits."""
    cal = Calendar(today or appointments.today_local())
    users = _users(db)
    db.commit()
    pts = _patients(db, users["andres"].id)
    docs = _doctors(db, users["andres"].id)
    analytes = _analytes(db)
    entries = _entries(db, cal, users, pts, docs)
    upcoming = _upcoming(db, cal, users, pts, docs)
    db.commit()
    versions = _med_versions(db, cal, users, pts, docs)
    reports = _labs(db, cal, users, pts, analytes)
    return {
        "users": len(users),
        "doctors": len(docs),
        "entries": len(entries),
        "pending_review": sum(e.review_status == REVIEW_PENDING for e in entries),
        "appointments_upcoming": upcoming,
        "med_versions": versions,
        "lab_reports": reports,
        "shift_days": cal.delta.days,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="empty the data tables even if they hold records this script did not seed",
    )
    args = parser.parse_args(argv)
    try:
        check_local(get_settings())
    except NotLocal as exc:
        print(f"REFUSED: demo data is for the local compose stack only ({exc}).", file=sys.stderr)
        return 2

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        if has_records(db) and not is_demo(db) and not args.wipe:
            print(
                "REFUSED: the local database holds records this script did not seed. "
                "Re-run with --wipe to empty it (or `docker compose down -v`).",
                file=sys.stderr,
            )
            return 3
        reset(db)
        counts = seed(db)
    finally:
        db.close()
    print("Demo data seeded: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(
        "Sign in as demo@example.com at http://localhost:5180/entrar, then: "
        "docker compose logs backend | grep 'DEV magic link'"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
