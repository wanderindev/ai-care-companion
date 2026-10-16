"""Lab report maintenance (operator CLI, #98).

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.lab_reports rematch --dry-run   # what would change
    python -m scripts.lab_reports rematch             # assign and audit

``rematch`` re-runs patient matching (CIP, name, name variant, then Claude
with ``CLASSIFICATION_MODEL``) for every lab report without a patient, from
the name and ID number stored at extraction — the PDF is never sent again.
Set both patients' ``full_name`` (as on the cédula) and ``id_number`` first
(``python -m scripts.patients set``). Reports a person assigned are never
touched; running it again is harmless. ``--dry-run`` still asks Claude (so
the counts are real) but saves nothing. It prints counts only, never names.
"""

import argparse
from collections import Counter

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.labs import patient_llm
from app.services.labs.rematch import RematchStats, rematch


def _counts(counter: Counter) -> str:
    return ", ".join(f"{key} {n}" for key, n in sorted(counter.items())) or "none"


def summary(stats: RematchStats, *, dry_run: bool) -> str:
    verb = "Would assign" if dry_run else "Assigned"
    ok = "would be marked reviewed (ok)" if dry_run else "marked reviewed (ok)"
    lines = [
        f"Lab reports without a patient: {stats.candidates} "
        f"(already assigned, not touched: {stats.already_assigned})",
        f"{verb}: {sum(stats.assigned.values())} ({_counts(stats.assigned)}); "
        f"{ok}: {stats.marked_ok}",
        f"Still unassigned: {sum(stats.unassigned.values())} ({_counts(stats.unassigned)})",
        f"Skipped: {sum(stats.skipped.values())} ({_counts(stats.skipped)})",
    ]
    if dry_run:
        lines.append("Dry run: nothing was saved.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.lab_reports", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("rematch", help="re-match the patient of unassigned lab reports")
    cmd.add_argument("--dry-run", action="store_true", help="show the counts, save nothing")
    args = parser.parse_args(argv)
    ask = patient_llm.Asker(get_settings())
    with SessionLocal() as db:
        stats = rematch(db, ask=ask, dry_run=args.dry_run)
    print(summary(stats, dry_run=args.dry_run))
    usage = ask.usage.as_dict()
    print(
        f"Claude calls: {usage['requests']} (input {usage['input_tokens']}, "
        f"output {usage['output_tokens']} tokens)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
