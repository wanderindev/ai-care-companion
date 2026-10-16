"""Audit helper: every write endpoint records who changed what (DESIGN §5.1).

Usage in a write path (same transaction as the change, so both commit or
neither does)::

    before = audit.snapshot(doctor)
    ...apply the change...
    db.flush()
    audit.record(db, user_id=user.id, action="update", entity="doctor",
                 entity_id=doctor.id, before=before, after=audit.snapshot(doctor))
    db.commit()

Snapshots hold PHI for some entities (patients); they are stored only in the
database and must never be logged.
"""

from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import inspect
from sqlalchemy.orm import Session as DbSession

from app.models.audit import AuditLog

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTION_LINK = "link"
ACTION_UNLINK = "unlink"
ACTION_MERGE = "merge"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"

# Bookkeeping and derived columns that would only add noise to every diff.
_DERIVED = frozenset({"search_vector"})
_SKIP = _DERIVED | {"created_at", "updated_at", "created_by", "updated_by"}


def snapshot(obj: Any, *, bookkeeping: bool = False) -> dict[str, Any]:
    """JSON-safe dict of a model's column values (dates as ISO strings).

    ``bookkeeping=True`` keeps created/updated at/by, for a delete whose audit
    row must be self-contained.
    """
    skip = _DERIVED if bookkeeping else _SKIP
    mapper = inspect(obj).mapper
    data = {attr.key: getattr(obj, attr.key) for attr in mapper.column_attrs}
    return jsonable_encoder({k: v for k, v in data.items() if k not in skip})


def record(
    db: DbSession,
    *,
    user_id: int | None,
    action: str,
    entity: str,
    entity_id: Any,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an audit row to the session (the caller commits)."""
    row = AuditLog(
        user_id=user_id,
        action=action,
        entity=entity,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )
    db.add(row)
    return row
