"""Show and fill in the two patients' private fields (operator CLI).

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.patients list
    python -m scripts.patients set <papa|mama> [--display-name ...] [--full-name ...]
        [--id-number ...] [--birth-date YYYY-MM-DD] [--diagnoses ...] [--notes ...]
        [--by <email>]

The rows themselves are created by migration 0003 with display names only.
These values are PHI: they belong in the production database, never in git.
Command-line arguments end up in shell history, so a value of ``-`` reads it
from stdin instead (e.g. ``--full-name -`` and type it, then Ctrl-D). An empty
value clears the field. ``--by`` attributes the change in the audit log to an
allowlisted user (otherwise it is recorded without a user). ``list`` shows only
which private fields are filled, not their values.
"""

import argparse
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.database import SessionLocal
from app.models.patient import Patient
from app.models.user import User
from app.services.auth import normalize_email
from app.services.patients import update_patient

_PRIVATE = ("full_name", "id_number", "birth_date", "diagnoses", "notes")


def _read(value: str) -> str:
    return sys.stdin.read().strip() if value == "-" else value.strip()


def list_patients(db: DbSession) -> list[Patient]:
    return list(db.scalars(select(Patient).order_by(Patient.id)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.patients", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list patients and which private fields are filled")
    p_set = sub.add_parser("set", help="set fields of one patient")
    p_set.add_argument("slug")
    p_set.add_argument("--display-name")
    p_set.add_argument("--full-name")
    p_set.add_argument("--id-number")
    p_set.add_argument("--birth-date", help="YYYY-MM-DD (empty to clear)")
    p_set.add_argument("--diagnoses")
    p_set.add_argument("--notes")
    p_set.add_argument("--by", help="email of the user the change is attributed to")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command == "list":
            for p in list_patients(db):
                filled = [f for f in _PRIVATE if getattr(p, f) not in (None, "")]
                print(f"{p.id}\t{p.slug}\t{p.display_name}\tfilled: {', '.join(filled) or '-'}")
            return 0

        patient = db.scalar(select(Patient).where(Patient.slug == args.slug))
        if patient is None:
            print(f"error: no patient with slug {args.slug!r}", file=sys.stderr)
            return 1
        user_id = None
        if args.by:
            user = db.scalar(select(User).where(User.email == normalize_email(args.by)))
            if user is None:
                print(f"error: no user {normalize_email(args.by)}", file=sys.stderr)
                return 1
            user_id = user.id

        changes: dict = {}
        for field in ("display_name", "full_name", "id_number", "diagnoses", "notes"):
            raw = getattr(args, field)
            if raw is not None:
                changes[field] = _read(raw) or None
        if args.birth_date is not None:
            raw = _read(args.birth_date)
            try:
                changes["birth_date"] = date.fromisoformat(raw) if raw else None
            except ValueError:
                print("error: --birth-date must be YYYY-MM-DD", file=sys.stderr)
                return 1
        if changes.get("display_name", "") is None:
            print("error: --display-name cannot be empty", file=sys.stderr)
            return 1
        if not changes:
            print("nothing to change", file=sys.stderr)
            return 1
        update_patient(db, patient, changes, user_id=user_id)
        print(f"updated: {patient.slug} ({', '.join(sorted(changes))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
