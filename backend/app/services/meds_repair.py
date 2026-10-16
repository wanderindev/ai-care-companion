"""One-off repair of medication-list versions' «Desde» (ticket #135).

Before #135 every save from the editor (and every approved ``med_change``)
stored its version with ``effective_date = NULL``, so «Desde» disappeared.
:func:`repair_effective_dates` gives each such version the value of the
nearest earlier version of the same patient that has one, in the column and
in the snapshot's ``effective_date`` key. A snapshot whose key is missing or
null while its column has a date (a column fixed by hand) gets the column's
value. A non-null value is never overwritten, so the repair is idempotent.
Every change is audited (``med_list_version`` update, dates only). The
caller commits.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.medication import MedListVersion
from app.services import audit


@dataclass
class RepairReport:
    versions: int = 0
    filled: int = 0  # column (and snapshot) filled from an earlier version
    snapshots_synced: int = 0  # snapshot key filled from its own column
    left_null: int = 0  # no earlier version has a «Desde»


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def repair_effective_dates(db: DbSession, *, user_id: int | None) -> RepairReport:
    report = RepairReport()
    rows = db.scalars(
        select(MedListVersion)
        .order_by(MedListVersion.patient_id, MedListVersion.version_no)
        .with_for_update()
    ).all()
    carry: dict[int, date] = {}
    for version in rows:
        report.versions += 1
        snapshot_value = (version.snapshot or {}).get("effective_date")
        if version.effective_date is not None:
            carry[version.patient_id] = version.effective_date
            if not snapshot_value:
                version.snapshot = {
                    **(version.snapshot or {}),
                    "effective_date": _iso(version.effective_date),
                }
                report.snapshots_synced += 1
                _audit(db, version, user_id, before=None, synced=True)
            continue
        previous = carry.get(version.patient_id)
        if previous is None:
            report.left_null += 1
            continue
        version.effective_date = previous
        if not snapshot_value:
            version.snapshot = {**(version.snapshot or {}), "effective_date": _iso(previous)}
        report.filled += 1
        _audit(db, version, user_id, before=None, synced=False)
    db.flush()
    return report


def _audit(db: DbSession, version: MedListVersion, user_id, *, before, synced: bool) -> None:
    audit.record(
        db,
        user_id=user_id,
        action=audit.ACTION_UPDATE,
        entity="med_list_version",
        entity_id=version.id,
        before={"effective_date": before},
        after={
            "effective_date": _iso(version.effective_date),
            "repair": "snapshot_effective_date" if synced else "effective_date_from_previous",
        },
    )
