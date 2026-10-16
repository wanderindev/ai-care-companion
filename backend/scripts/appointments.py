"""Create appointments from the "Próxima cita" of imported bitácora entries.

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.appointments backfill --by <email> [--dry-run]

One appointment per imported entry whose ``origin_ref.next_appointment`` has a
date; past ones whose visit was recorded (a later entry of the same patient
and doctor on that day, ±1 day) become ``realizada`` with that entry as
``result_entry_id``. Idempotent: entries that already have their appointment
are skipped. ``--dry-run`` computes everything and rolls back. Rules:
``app/services/appointments_backfill.py``. Prints counts only, never text.
"""

import argparse
import sys

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.user import User
from app.services.appointments_backfill import BackfillReport, backfill
from app.services.auth import normalize_email


def _print_report(report: BackfillReport, *, dry_run: bool) -> None:
    prefix = "dry run, nothing written: " if dry_run else ""
    print(
        f"{prefix}{report.candidates} imported entries with a next appointment; "
        f"{report.existing} already have one, {report.no_date} without a date, "
        f"{report.before_entry} dated before their entry, {report.duplicate_same_day} "
        f"repeat an appointment already there (same patient, day and doctor), "
        f"{report.deleted_before} had theirs deleted in the app, {report.failed} failed."
    )
    print(
        f"{'would create' if dry_run else 'created'} {report.created}: {report.done} realizada "
        f"(visit found in the bitácora), {report.past_open} past programada (no visit "
        f"recorded), {report.upcoming} upcoming programada. {report.doctor_unmatched} "
        f"next appointments name a doctor not in the directory (left without doctor)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.appointments", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_back = sub.add_parser("backfill", help="create appointments from imported entries")
    p_back.add_argument("--by", required=True, help="email of the user the changes are audited to")
    p_back.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        email = normalize_email(args.by)
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"error: no user {email}", file=sys.stderr)
            return 1
        report = backfill(db, user_id=user.id)
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
    _print_report(report, dry_run=args.dry_run)
    return 0 if report.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
