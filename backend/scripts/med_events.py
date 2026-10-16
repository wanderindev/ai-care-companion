"""Create the derived medication history from imported bitácora entries.

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.med_events backfill --by <email> [--dry-run]

One ``med_events`` row (``pending_review``, linked to its entry) per event in
an imported entry's ``origin_ref.medication_events``. Idempotent: events
already created, and events discarded in the app, are skipped. ``--dry-run``
computes everything and rolls back. Rules:
``app/services/med_events_backfill.py``. Prints counts only, never text.
"""

import argparse
import sys

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.user import User
from app.services.auth import normalize_email
from app.services.med_events_backfill import BackfillReport, backfill


def _print_report(report: BackfillReport, *, dry_run: bool) -> None:
    prefix = "dry run, nothing written: " if dry_run else ""
    print(
        f"{prefix}{report.entries} imported entries with medication events, "
        f"{report.events} events; {report.existing} already created, "
        f"{report.deleted_before} discarded in the app, {report.unknown_kind} with an "
        f"unknown kind, {report.invalid} without a usable medication name (skipped)."
    )
    print(f"{'would create' if dry_run else 'created'} {report.created} (pending review).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.med_events", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_back = sub.add_parser("backfill", help="create med_events from imported entries")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
