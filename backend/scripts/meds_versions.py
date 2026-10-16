"""Maintenance of medication-list versions.

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.meds_versions repair-effective-date --by <email> [--dry-run]

``repair-effective-date`` (ticket #135): versions saved before #135 lost
their «Desde» (``effective_date`` NULL); each gets the nearest earlier
version's value, in the column and the snapshot. Never overwrites a value,
so running it again changes nothing. Audited to ``--by``. ``--dry-run``
computes everything and rolls back. Prints counts only.
"""

import argparse
import sys

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.user import User
from app.services.auth import normalize_email
from app.services.meds_repair import RepairReport, repair_effective_dates


def _print_report(report: RepairReport, *, dry_run: bool) -> None:
    prefix = "dry run, nothing written: " if dry_run else ""
    verb = "would fill" if dry_run else "filled"
    print(
        f"{prefix}{report.versions} versions checked; {verb} {report.filled} «Desde» from an "
        f"earlier version, {report.snapshots_synced} snapshot keys from their own column; "
        f"{report.left_null} left empty (no earlier «Desde»)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.meds_versions", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_rep = sub.add_parser(
        "repair-effective-date", help="restore «Desde» on versions saved without it"
    )
    p_rep.add_argument("--by", required=True, help="email of the user the changes are audited to")
    p_rep.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        email = normalize_email(args.by)
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"error: no user {email}", file=sys.stderr)
            return 1
        report = repair_effective_dates(db, user_id=user.id)
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
    _print_report(report, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
