"""Import the family's bitácora Word document into ``log_entries`` (one-time).

Run from ``backend/`` (procedure, costs and how to redo it: ``docs/import.md``):

    python -m scripts.import_bitacora <file.docx> --patient papa --dry-run \\
        [--staging private/staging.json] [--limit N]
    python -m scripts.import_bitacora --patient papa --from-staging private/staging.json \\
        --by <email>
    python -m scripts.import_bitacora <file.docx> --patient papa --by <email> [--staging …]

Pipeline: DOCX → markdown → split by year headers and date lines (regex) →
one Claude call per entry (``EXTRACTION_MODEL``, needs ``ANTHROPIC_API_KEY``)
→ staging JSON → ``log_entries`` rows with ``review_status=pending_review`` and
``origin=import_docx``. ``--dry-run`` stops after writing the staging file and
touches no database. ``--from-staging`` loads a (reviewed) staging file without
calling Claude. Without either, it extracts, writes the staging file and loads.

``--by`` is required whenever the run loads (audit attribution). Outside a
repository checkout (in a container) ``--staging`` is required, since the
default ``private/`` would not persist. The staging file is written before the
first Claude call and every few entries; running again with the same
``--staging`` resumes, reusing the extractions it already holds (failed ones
are retried). A dropped section/patient header line that reappears inside an
entry aborts the run before any call (PHI guard).

Re-running is idempotent: entries already imported from the same file (same
SHA-256, entry position and text hash) are skipped, and are not sent to Claude
again. Doctors are matched by name/alias; unknown ones are created (listed in
the summary, to merge later in the app); a name matching several doctors
leaves the entry without doctor and keeps the name in its source label.

The staging file contains PHI: it defaults to ``private/`` (gitignored).
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.patient import Patient
from app.models.user import User
from app.services.auth import normalize_email
from app.services.bitacora_import.docx_parse import (
    docx_to_markdown_lines,
    file_sha256,
    header_hint,
    split_entries,
)
from app.services.bitacora_import.extract import (
    DEFAULT_CONCURRENCY,
    Extraction,
    Usage,
    extract_all,
    make_client,
)
from app.services.bitacora_import.load import (
    PHI_NOTICE,
    LoadReport,
    StagingError,
    build_staging,
    check_writable,
    existing_keys,
    load_staging,
    read_staging,
    resumable,
    write_staging,
)

PATIENTS = ("papa", "mama")
SAVE_EVERY = 10  # rewrite the staging file after this many new extractions


def _err(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _default_staging(source_sha256: str) -> Path:
    return Path("private") / f"bitacora-staging-{source_sha256[:12]}.json"


def _in_checkout(cwd: Path) -> bool:
    """True inside a git checkout, where the default ``private/`` is gitignored
    and persists (not in a throwaway container)."""
    return any((d / ".git").exists() for d in (cwd, *cwd.parents))


def _section_summary(split) -> str:
    parts = [
        f"{year if year is not None else '(no year)'}: {count}"
        for year, count in split.counts_by_section()
    ]
    return ", ".join(parts) or "-"


def _print_usage(usage: Usage, model: str) -> None:
    u = usage.as_dict()
    print(
        f"claude: {u['requests']} requests, input {u['input_tokens']} tokens, "
        f"cache write {u['cache_creation_input_tokens']}, cache read "
        f"{u['cache_read_input_tokens']}, output {u['output_tokens']} (incl. thinking)"
    )
    cost = usage.cost_usd(model)
    if cost is None:
        print(f"estimated cost: unknown for model {model} (no price table)")
    else:
        print(f"estimated cost: US$ {cost:.2f} ({model})")
        if u["requests"]:
            print(f"  ≈ US$ {cost / u['requests']:.4f} per entry")


def _print_report(report: LoadReport) -> None:
    print(f"loaded: {report.new} new, {report.skipped} skipped, {report.failed} failed")
    for index, reason in report.failures:
        print(f"  failed entry #{index}: {reason}")
    if report.created_doctors:
        print(f"doctors created ({len(report.created_doctors)}), merge duplicates in the app:")
        for name in report.created_doctors:
            print(f"  {name}")
    if report.other_source_duplicates:
        indexes = ", ".join(map(str, report.other_source_duplicates))
        print(
            f"WARNING: {len(report.other_source_duplicates)} loaded entries have the same text "
            f"as entries imported from ANOTHER version of the document (likely duplicates; "
            f"entries {indexes}). See docs/import.md, 'Redo or undo'."
        )
    if report.ambiguous_doctors:
        print("ambiguous doctor names (left without doctor, name kept in source label):")
        for name in report.ambiguous_doctors:
            print(f"  {name}")


def _resolve_targets(db, slug: str, by: str | None) -> tuple[Patient | None, int | None, str]:
    patient = db.scalar(select(Patient).where(Patient.slug == slug))
    if patient is None:
        return None, None, f"no patient with slug {slug!r}"
    if by is None:
        return patient, None, ""
    user = db.scalar(select(User).where(User.email == normalize_email(by)))
    if user is None:
        return None, None, f"no user {normalize_email(by)}"
    return patient, user.id, ""


def _from_staging(args) -> int:
    try:
        staging = read_staging(Path(args.from_staging))
    except StagingError as exc:
        return _err(str(exc))
    if staging["patient"] != args.patient:
        return _err(f"staging file is for {staging['patient']!r}, not {args.patient!r}")
    if args.file and file_sha256(Path(args.file)) != staging["source_sha256"]:
        return _err("the .docx does not match the file the staging was made from")
    with SessionLocal() as db:
        patient, user_id, problem = _resolve_targets(db, args.patient, args.by)
        if patient is None:
            return _err(problem)
        report = load_staging(db, staging, patient=patient, user_id=user_id)
    _print_report(report)
    return 0 if report.failed == 0 else 2


def _extract(args) -> int:
    settings = get_settings()
    path = Path(args.file)
    if not path.is_file():
        return _err(f"no such file: {path}")
    source_sha256 = file_sha256(path)
    split = split_entries(docx_to_markdown_lines(path))
    print(
        f"parsed: {len(split.entries)} entries in {len(split.years)} sections; "
        f"{split.skipped_lines} header/preamble lines skipped"
    )
    print(f"entries per section: {_section_summary(split)}")
    if split.leaked_entries:
        # PHI guard: a dropped section/patient header line reappears inside
        # entries; it would be sent to Claude and stored. Fix the document.
        return _err(
            "a section or patient header line reappears verbatim inside entries "
            f"{', '.join(map(str, split.leaked_entries))}; remove it from the document "
            "(nothing was sent or written)"
        )

    if args.staging:
        staging_path = Path(args.staging)
    elif _in_checkout(Path.cwd()):
        staging_path = _default_staging(source_sha256)
    else:
        return _err(
            "--staging is required here: outside a repository checkout (e.g. in a "
            "container) the default private/ directory does not persist. Mount a "
            "directory and pass --staging /data/staging.json"
        )
    model = settings.extraction_model
    previous: dict | None = {}
    if staging_path.exists():
        try:
            previous = resumable(
                read_staging(staging_path),
                source_sha256=source_sha256,
                patient=args.patient,
                model=model,
            )
        except StagingError:
            return _err(f"{staging_path} exists and is not a staging file; choose another path")
        if previous is None:
            return _err(
                f"{staging_path} is a staging file for another document, patient or "
                "model; choose another --staging"
            )
    try:
        check_writable(staging_path)
    except StagingError as exc:
        return _err(str(exc))

    items = [(raw, header_hint(raw.lines)) for raw in split.entries]
    patient = user_id = None
    skipped_before = 0
    if not args.dry_run:
        with SessionLocal() as db:
            patient, user_id, problem = _resolve_targets(db, args.patient, args.by)
            if patient is None:
                return _err(problem)
            done = existing_keys(db, source_sha256)
        before = len(items)
        items = [(raw, hint) for raw, hint in items if (raw.index, raw.text_sha256) not in done]
        skipped_before = before - len(items)
        if skipped_before:
            print(f"already imported: {skipped_before} entries (not sent to Claude)")
    if args.limit is not None:
        items = items[: args.limit]
        print(f"--limit: {len(items)} entries")

    extractions: dict[int, Extraction] = {}
    for raw, _hint in items:
        staged = previous.get((raw.index, raw.text_sha256))
        if staged is not None:
            ext_warnings = [w for w in staged.get("warnings") or [] if w not in raw.warnings]
            extractions[raw.index] = Extraction(raw.index, staged["extraction"], None, ext_warnings)
    todo = [(raw, hint) for raw, hint in items if raw.index not in extractions]
    if extractions:
        print(f"resuming {staging_path}: {len(extractions)} entries already extracted")
    if todo and not settings.anthropic_api_key:
        return _err("ANTHROPIC_API_KEY is not set")

    usage = Usage()

    def save(results: dict[int, Extraction]) -> dict:
        staging = build_staging(
            source_sha256=source_sha256,
            patient_slug=args.patient,
            model=model,
            split=split,
            items=items,
            extractions={**extractions, **results},
            usage=usage,
        )
        write_staging(staging_path, staging)
        return staging

    save({})  # before any call: an interrupted run keeps what it paid for
    if todo:
        client = make_client(settings.anthropic_api_key)
        print(f"extracting {len(todo)} entries with {model} (concurrency {args.concurrency})…")

        def checkpoint(results: dict[int, Extraction]) -> None:
            if len(results) % SAVE_EVERY == 0:
                save(results)

        new = extract_all(
            client, model, todo, usage=usage, concurrency=args.concurrency, on_result=checkpoint
        )
        extractions.update(new)
    staging = save({})
    print(f"staging written: {staging_path}")
    print(f"WARNING: {PHI_NOTICE}", file=sys.stderr)

    errors = Counter(e["error"] for e in staging["entries"] if e["error"])
    warnings = Counter(w for e in staging["entries"] for w in e["warnings"])
    ok = sum(1 for e in staging["entries"] if not e["error"])
    print(f"extracted: {ok} ok, {sum(errors.values())} failed")
    for code, count in sorted(errors.items()):
        indexes = [str(e["entry_index"]) for e in staging["entries"] if e["error"] == code]
        print(f"  {code}: {count} (entries {', '.join(indexes)})")
    if warnings:
        print("warnings: " + ", ".join(f"{k} {v}" for k, v in sorted(warnings.items())))
    _print_usage(usage, model)

    if args.dry_run:
        print("dry run: database not touched. Review the staging file, then --from-staging.")
        return 0
    with SessionLocal() as db:
        report = load_staging(db, staging, patient=db.merge(patient), user_id=user_id)
    report.skipped += skipped_before
    _print_report(report)
    return 0 if report.failed == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_bitacora",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", nargs="?", help="the bitácora .docx")
    parser.add_argument("--patient", required=True, choices=PATIENTS)
    parser.add_argument(
        "--dry-run", action="store_true", help="extract and write staging only; no database"
    )
    parser.add_argument("--staging", help="staging JSON to write (default private/…)")
    parser.add_argument("--from-staging", help="load this staging JSON (no Claude calls)")
    parser.add_argument("--limit", type=int, help="extract only the first N entries (trial)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--by", help="email of the user the entries are attributed to (required to load)"
    )
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        return _err("--limit must be at least 1")
    if not args.dry_run and not args.by:
        return _err("--by <email> is required to load (the audit log records who imported)")
    if args.from_staging:
        if args.dry_run or args.limit is not None or args.staging:
            return _err("--from-staging only loads; drop --dry-run/--limit/--staging")
        return _from_staging(args)
    if not args.file:
        return _err("give the .docx file (or --from-staging)")
    return _extract(args)


if __name__ == "__main__":
    sys.exit(main())
