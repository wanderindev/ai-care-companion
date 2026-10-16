"""Seed a patient's medication list from the family's Word document (version 1).

Run from ``backend/`` (procedure: ``docs/import.md``, "Medication lists"):

    python -m scripts.import_meds <file.docx> --patient papa --dry-run [--staging …]
    python -m scripts.import_meds --patient papa --from-staging <staging.json> \\
        --by <email> [--dry-run]
    python -m scripts.import_meds <file.docx> --patient papa --by <email> [--staging …]

Pipeline: DOCX → markdown → section headers mapped to slots and the "DESDE
<fecha>" line read in code; the preamble (title, patient name and ID number)
is dropped → one Claude call for the whole document (``EXTRACTION_MODEL``,
needs ``ANTHROPIC_API_KEY``) → staging JSON → version 1 through
``app.services.meds.save_list``.

- ``<file.docx> --dry-run``: extract and write the staging file only (no
  database).
- ``--from-staging``: load a (reviewed) staging file, no Claude call; with
  ``--dry-run`` it validates and runs the save, then rolls it back (preview).
- Neither: extract, write the staging file and load in one go.

``--by`` is required whenever the run saves (the version's ``changed_by`` and
the audit log). The load is refused (exit 1) when the patient already has a
list version: the import only seeds version 1; later changes go through the
app. A staging file that already holds a successful extraction of the same
document, patient and model is reused instead of calling Claude again (delete
it to extract again). The default staging file is ``private/meds-<patient>.json``
inside a repository checkout; elsewhere (a container) ``--staging`` is
required. It contains PHI (mode 600, gitignored directory).

Review (``app.services.meds_import.review``, printed as ``REVISAR``): an item
taken from EVITAR or from no section has no slot and always blocks the load
until it is fixed in the staging file; possible missing items (a section with
more text lines than items) and header disagreements block it unless
``--accept-warnings`` is passed after checking them against the document.

Output is counts, item numbers and codes only, never medication names. A body
line naming an ID document (CIP, cédula) aborts before any call (PHI guard),
and so does a line before the first section header that is not the title,
the patient's name / ID line or DESDE (``preamble_unrecognized``: it would be
dropped, e.g. a medication or an unrecognized header; ``--accept-warnings``
does not apply).
Exit status: 0 ok, 1 usage/setup error or refused, 2 extraction failed or the
staging file does not validate or pass the review (nothing saved).
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.medication import SLOTS
from app.models.patient import Patient
from app.models.user import User
from app.services import meds
from app.services.appointments import today_local
from app.services.auth import normalize_email
from app.services.bitacora_import.docx_parse import file_sha256
from app.services.meds_import.docx_parse import docx_to_markdown_lines, split_document
from app.services.meds_import.extract import Usage, extract_document, make_client
from app.services.meds_import.load import (
    PHI_NOTICE,
    AlreadyImported,
    LoadReport,
    StagingError,
    build_staging,
    check_writable,
    load_staging,
    read_staging,
    reusable,
    write_staging,
)
from app.services.meds_import.review import Review, review_list

PATIENTS = ("papa", "mama")


def _err(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _in_checkout(cwd: Path) -> bool:
    return any((d / ".git").exists() for d in (cwd, *cwd.parents))


def _slot_counts(counts: dict[str, int]) -> str:
    order = [*SLOTS, "evitar"]
    parts = [f"{slot} {counts[slot]}" for slot in order if counts.get(slot)]
    if counts.get(None):
        parts.append(f"NO SLOT {counts[None]}")
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


def _print_staging_summary(staging: dict) -> None:
    data = staging.get("list") or {}
    items = data.get("items") or []
    print(
        f"extracted: {len(items)} items ({_slot_counts(Counter(i.get('slot') for i in items))}), "
        f"{len(data.get('avoid') or [])} EVITAR, "
        f"list note: {'yes' if data.get('list_note') else 'no'}, "
        f"desde: {'yes' if data.get('effective_date') else 'no'}"
    )
    for warning in staging.get("warnings") or []:
        print(f"  warning: {warning}")
    _print_review(review_list(staging.get("sections") or [], data, today_local()))


def _print_review(review: Review, accepted: bool = False) -> None:
    if review.blocking or review.warnings:
        label = "accepted (--accept-warnings)" if accepted else "the load refuses until fixed"
        print(f"REVISAR ({len(review.blocking) + len(review.warnings)}; {label}):")
        for problem in review.blocking:
            print(f"  REVISAR {problem} [blocking]")
        for warning in review.warnings:
            print(f"  REVISAR {warning}")
    for note in review.notes:
        print(f"  note: {note}")


def _print_report(report: LoadReport, dry_run: bool) -> None:
    if report.failures:
        print(f"staging does not validate ({len(report.failures)} problems), nothing saved:")
        for failure in report.failures:
            print(f"  {failure}")
        if any(f.startswith("REVISAR") for f in report.failures):
            print(
                "  (check those against the document; fix the staging file, or pass "
                "--accept-warnings if the list is complete)"
            )
        return
    if report.warnings_accepted:
        _print_review(Review(warnings=report.review.warnings), accepted=True)
    for note in report.review.notes:
        print(f"note: {note}")
    what = f"{report.items} items ({_slot_counts(report.by_slot)}), {report.avoid} EVITAR"
    if dry_run:
        print(f"dry run: would create version 1 with {what}; rolled back, nothing saved")
    elif report.saved:
        print(f"saved: version {report.version_no} with {what}")
    else:
        print("nothing saved (the list is empty)")
    if report.unmatched_prescribers:
        print(
            f"prescribers not matched to one doctor: {report.unmatched_prescribers} "
            "(kept in the item's instructions as 'Indicado por: …'; set them in the app)"
        )


def _resolve(db, slug: str, by: str | None) -> tuple[Patient | None, int | None, str]:
    patient = db.scalar(select(Patient).where(Patient.slug == slug))
    if patient is None:
        return None, None, f"no patient with slug {slug!r}"
    if by is None:
        return patient, None, ""
    user = db.scalar(select(User).where(User.email == normalize_email(by)))
    if user is None:
        return None, None, f"no user {normalize_email(by)}"
    return patient, user.id, ""


def _load(staging: dict, args, dry_run: bool) -> int:
    with SessionLocal() as db:
        patient, user_id, problem = _resolve(db, args.patient, args.by)
        if patient is None:
            return _err(problem)
        try:
            report = load_staging(
                db,
                staging,
                patient=patient,
                user_id=user_id,
                dry_run=dry_run,
                accept_warnings=args.accept_warnings,
            )
        except AlreadyImported as exc:
            return _err(
                f"{args.patient} already has a medication list (version {exc.version_no}); "
                "the import only creates version 1. Change the list in the app."
            )
    _print_report(report, dry_run)
    return 2 if report.failures else 0


def _from_staging(args) -> int:
    try:
        staging = read_staging(Path(args.from_staging))
    except StagingError as exc:
        return _err(str(exc))
    if staging["patient"] != args.patient:
        return _err(f"staging file is for {staging['patient']!r}, not {args.patient!r}")
    if args.file and file_sha256(Path(args.file)) != staging["source_sha256"]:
        return _err("the .docx does not match the file the staging was made from")
    return _load(staging, args, dry_run=args.dry_run)


def _already_has_version(slug: str) -> int | None:
    with SessionLocal() as db:
        patient = db.scalar(select(Patient).where(Patient.slug == slug))
        if patient is None:
            return None
        latest = meds.latest_version(db, patient.id)
        return latest.version_no if latest else None


def _extract(args) -> int:
    settings = get_settings()
    path = Path(args.file)
    if not path.is_file():
        return _err(f"no such file: {path}")
    source_sha256 = file_sha256(path)
    parsed = split_document(docx_to_markdown_lines(path))
    print(
        f"parsed: {len(parsed.sections)} sections ({_slot_counts(parsed.section_counts())}); "
        f"{parsed.skipped_lines} header/preamble lines skipped, "
        f"{parsed.repeated_header_lines} repeated header lines dropped; "
        f"desde: {'found' if parsed.effective_date else 'not found'}"
    )
    empty = [str(n) for n, s in enumerate(parsed.sections, 1) if not s.lines]
    if empty:
        print(f"sections with no lines (by number): {', '.join(empty)}")
    if not parsed.sections:
        return _err("no section header found (AL DESPERTAR, DESAYUNO, …); nothing was sent")
    if parsed.preamble_unrecognized:
        for number in parsed.preamble_unrecognized:
            print(f"  REVISAR preamble_unrecognized line={number} [blocking]")
        return _err(
            "lines before the first section header are not the title, the patient's name/ID "
            "or DESDE, and would be dropped (a medication or an unrecognized header?): "
            f"non-empty lines {', '.join(map(str, parsed.preamble_unrecognized))} of the "
            "document. Put them under a recognized section header (AL DESPERTAR, DESAYUNO, …) "
            "or remove them (nothing was sent or written; --accept-warnings does not apply)"
        )
    if parsed.id_lines:
        return _err(
            "lines after the first section header name an ID document (CIP/cédula): lines "
            f"{', '.join(map(str, parsed.id_lines))} of the list body; remove them from the "
            "document (nothing was sent or written)"
        )

    if args.staging:
        staging_path = Path(args.staging)
    elif _in_checkout(Path.cwd()):
        staging_path = Path("private") / f"meds-{args.patient}.json"
    else:
        return _err(
            "--staging is required here: outside a repository checkout (e.g. in a "
            "container) the default private/ directory does not persist"
        )
    model = settings.extraction_model
    staging = None
    if staging_path.exists():
        try:
            previous = read_staging(staging_path)
        except StagingError:
            return _err(
                f"{staging_path} exists and is not a meds staging file; choose another path"
            )
        if not reusable(previous, source_sha256=source_sha256, patient=args.patient, model=model):
            return _err(
                f"{staging_path} exists (another document, patient or model, or a failed "
                "extraction); delete it or choose another --staging"
            )
        staging = previous
        print(f"reusing the extraction in {staging_path} (delete it to extract again)")
    try:
        check_writable(staging_path)
    except StagingError as exc:
        return _err(str(exc))

    if not args.dry_run:
        version_no = _already_has_version(args.patient)
        if version_no is not None:
            return _err(
                f"{args.patient} already has a medication list (version {version_no}); "
                "nothing was sent. The import only creates version 1."
            )

    if staging is None:
        if not settings.anthropic_api_key:
            return _err("ANTHROPIC_API_KEY is not set")
        usage = Usage()
        print(f"extracting with {model}…")
        extraction = extract_document(make_client(settings.anthropic_api_key), model, parsed, usage)
        staging = build_staging(
            source_sha256=source_sha256,
            patient_slug=args.patient,
            model=model,
            parsed=parsed,
            extraction=extraction,
            usage=usage,
        )
        write_staging(staging_path, staging)
        print(f"staging written: {staging_path}")
        print(f"WARNING: {PHI_NOTICE}", file=sys.stderr)
        _print_usage(usage, model)
        if extraction.error:
            print(f"extraction failed: {extraction.error}")
            return 2
    _print_staging_summary(staging)

    if args.dry_run:
        print("dry run: database not touched. Review the staging file, then --from-staging.")
        return 0
    return _load(staging, args, dry_run=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_meds",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", nargs="?", help="the medication list .docx")
    parser.add_argument("--patient", required=True, choices=PATIENTS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with a .docx: extract and write staging only; with --from-staging: preview",
    )
    parser.add_argument(
        "--staging", help="staging JSON to write (default private/meds-<patient>.json)"
    )
    parser.add_argument("--from-staging", help="load this staging JSON (no Claude call)")
    parser.add_argument("--by", help="email of the user the version is attributed to")
    parser.add_argument(
        "--accept-warnings",
        action="store_true",
        help="load despite REVISAR review warnings (checked against the document)",
    )
    args = parser.parse_args(argv)

    if not args.dry_run and not args.by:
        return _err("--by <email> is required to save (the version records who imported)")
    if args.from_staging:
        if args.staging:
            return _err("--from-staging only loads; drop --staging")
        return _from_staging(args)
    if not args.file:
        return _err("give the .docx file (or --from-staging)")
    return _extract(args)


if __name__ == "__main__":
    sys.exit(main())
