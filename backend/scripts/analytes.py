"""Analyte catalogue maintenance (operator CLI, #100).

Run from ``backend/`` (locally: ``docker compose exec backend ...``; in prod:
``docker compose -f docker-compose.prod.yml run --rm n16chat-backend ...``):

    python -m scripts.analytes triage --dry-run   # what Claude would propose
    python -m scripts.analytes triage             # store the proposals
    python -m scripts.analytes triage --apply     # ...and apply the safe ones

``triage`` sends every analyte flagged for review that has no proposal yet to
Claude (``EXTRACTION_MODEL``), in batches, with the reviewed catalogue, and
stores one proposal per analyte (merge into / keep as new / unsure) for an
admin to accept or reject on «Revisar exámenes» (``app.services.labs.triage``).
``--apply`` also applies, through the normal merge / confirm services
(audited), the high-confidence «keep as new» proposals and the
high-confidence merges into a target of the same specimen whose units match.
The cost estimate is printed first and the run asks for confirmation (``--yes``
skips it). ``--dry-run`` still asks Claude (so the counts are real) but saves
nothing. It prints counts only, never names or values.
"""

import argparse
import sys

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.jobs import JobError
from app.services.labs import triage


def _counts(counter) -> str:
    return ", ".join(f"{key} {n}" for key, n in sorted(counter.items())) or "none"


def summary(stats: triage.TriageStats, *, dry_run: bool, apply: bool) -> str:
    lines = [
        f"Analytes sent: {stats.candidates} in {stats.batches} batch(es)",
        f"Proposals: {sum(stats.proposals.values())} ({_counts(stats.proposals)}); "
        f"high confidence: {stats.high_confidence}",
    ]
    if apply:
        verb = "Would apply" if dry_run else "Applied"
        lines.append(
            f"{verb}: {sum(stats.auto_applied.values())} ({_counts(stats.auto_applied)}); "
            f"refused: {stats.apply_failed}"
        )
    lines.append(f"Skipped (reviewed meanwhile): {stats.skipped}")
    lines.append(f"Errors: {_counts(stats.errors)}")
    if dry_run:
        lines.append("Dry run: nothing was saved.")
    return "\n".join(lines)


def triage_command(args, *, client=None, confirm=input) -> int:
    settings = get_settings()
    with SessionLocal() as db:
        est = triage.estimate(db, settings)
        cost = f"~${est.cost_usd:.2f}" if est.cost_usd is not None else "unknown (model not priced)"
        print(
            f"To triage: {est.analytes} analytes in {est.batches} call(s) to {est.model}; "
            f"estimated tokens: input {est.input_tokens}, cached {est.cached_tokens}, "
            f"output {est.output_tokens}; estimated cost {cost}"
        )
        if est.analytes == 0:
            print("Nothing to triage.")
            return 0
        if not args.yes and confirm("Continue? [y/N] ").strip().lower() not in ("y", "yes", "s"):
            print("Cancelled.")
            return 1
        try:
            stats = triage.run_triage(
                db, settings, client=client, apply=args.apply, dry_run=args.dry_run
            )
        except JobError as exc:
            print(f"Stopped: {exc} (completed batches are saved)", file=sys.stderr)
            return 1
    print(summary(stats, dry_run=args.dry_run, apply=args.apply))
    usage = stats.usage.as_dict()
    cost = stats.usage.cost_usd(settings.extraction_model)
    print(
        f"Claude calls: {usage['requests']} (input {usage['input_tokens']}, "
        f"output {usage['output_tokens']}, cache write {usage['cache_creation_input_tokens']}, "
        f"cache read {usage['cache_read_input_tokens']} tokens)"
        + (f"; cost ${cost:.2f}" if cost is not None else "")
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.analytes", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("triage", help="ask Claude to triage the analytes under review")
    cmd.add_argument("--dry-run", action="store_true", help="show the counts, save nothing")
    cmd.add_argument(
        "--apply",
        action="store_true",
        help="also apply high-confidence keeps and same-specimen merges with matching units",
    )
    cmd.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    return triage_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
