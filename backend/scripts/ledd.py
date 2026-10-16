"""Queue the AI classification of medication names outside the LEDD table.

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.ledd queue [--dry-run]

A list save queues its unknown names by itself (``app.services.ledd_substances``);
this covers the lists saved before ticket #133 and rows left ``queued`` (no
``ANTHROPIC_API_KEY`` at the time). Every current item of every patient whose
name the catalogue does not know and that has no ``ledd_substances`` row gets
one, plus a ``classify_substance`` job; rows still ``queued`` get a new job.
A name with a row is never sent twice. Prints counts only, never names.
"""

import argparse

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.patient import Patient
from app.services import ledd_substances
from app.services import meds as meds_svc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ledd", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_queue = sub.add_parser("queue", help="queue unknown names of the current lists")
    p_queue.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        stuck = ledd_substances.requeue_stuck(db)
        new = 0
        for patient_id in db.scalars(select(Patient.id).order_by(Patient.id)):
            items = [
                meds_svc.item_snapshot(i, None) for i in meds_svc.current_items(db, patient_id)
            ]
            new += ledd_substances.queue_unknown(db, items)
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
    verb = "would queue" if args.dry_run else "queued"
    print(f"{verb} {new} new name(s) and {stuck} name(s) still waiting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
