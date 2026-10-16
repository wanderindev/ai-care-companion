"""Patient edits shared by ``PATCH /patients/{id}`` and ``scripts.patients``."""

from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.models.patient import Patient
from app.services import audit

EDITABLE_FIELDS = ("display_name", "full_name", "id_number", "birth_date", "diagnoses", "notes")


def update_patient(
    db: DbSession, patient: Patient, changes: dict[str, Any], *, user_id: int | None
) -> Patient:
    """Apply ``changes`` (editable fields only) and audit-log them. Commits."""
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"not editable: {sorted(unknown)}")
    before = audit.snapshot(patient)
    for field, value in changes.items():
        setattr(patient, field, value)
    after = audit.snapshot(patient)
    if after != before:
        patient.updated_by = user_id
        audit.record(
            db,
            user_id=user_id,
            action=audit.ACTION_UPDATE,
            entity="patient",
            entity_id=patient.id,
            before=before,
            after=after,
        )
    db.commit()
    db.refresh(patient)
    return patient
