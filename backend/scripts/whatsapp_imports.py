"""WhatsApp import maintenance (operator CLI, #117).

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.whatsapp_imports fix-date-order 7 --from dmy --to mdy --dry-run
    python -m scripts.whatsapp_imports fix-date-order 7 --from dmy --to mdy

``fix-date-order`` re-dates the messages first stored by an import that was
read in the wrong day/month order (``app.services.whatsapp_import.date_order``):
a message whose recomputed hash is already stored is deleted as a duplicate,
the others get the swapped date; messages with a file, transcript,
classification or lab report/link are refused and listed by id. The affected
days leave the import's ``stats.analysis.days_done``. Audited. ``--dry-run``
saves nothing. It prints counts and message ids only, never text or names.
"""

import argparse

from app.core.database import SessionLocal
from app.services.whatsapp_import.date_order import FixError, FixReport, fix_date_order

ORDERS = ("dmy", "mdy")


def summary(report: FixReport, *, dry_run: bool) -> str:
    lines = [
        f"Messages first stored by the import: {report.candidates} "
        f"(same date in both orders, not touched: {report.unchanged})",
        f"{'Would delete' if dry_run else 'Deleted'} as duplicates: {report.deleted_duplicates}",
        f"{'Would re-date' if dry_run else 'Re-dated'}: {report.redated}",
        f"Skipped (stored hash not reproducible): {report.hash_mismatch}; "
        f"conflicts: {report.conflicts}",
        f"Analysis days {'to clear' if dry_run else 'cleared'}: {report.days_cleared}",
    ]
    if report.refused_ids:
        reasons = ", ".join(f"{k} {n}" for k, n in sorted(report.refused_reasons.items()))
        ids = ", ".join(str(i) for i in report.refused_ids)
        lines.append(f"Refused: {len(report.refused_ids)} ({reasons}); message ids: {ids}")
    else:
        lines.append("Refused: 0")
    if dry_run:
        lines.append("Dry run: nothing was saved.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.whatsapp_imports", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("fix-date-order", help="re-date an import read in the wrong order")
    cmd.add_argument("import_id", type=int)
    cmd.add_argument("--from", dest="from_order", choices=ORDERS, required=True)
    cmd.add_argument("--to", dest="to_order", choices=ORDERS, required=True)
    cmd.add_argument("--dry-run", action="store_true", help="show the counts, save nothing")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        try:
            report = fix_date_order(
                db,
                args.import_id,
                from_order=args.from_order,
                to_order=args.to_order,
                dry_run=args.dry_run,
            )
        except FixError as exc:
            print(f"Error: {exc}")
            return 1
    print(summary(report, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
