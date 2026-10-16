"""Medication names outside the LEDD reference table (ticket #133, DESIGN §5.4,
§6.4), classified once by Claude and confirmed by an admin.

``ledd_substances`` — one row per normalized medication name
(``app.services.ledd.name_key``, unique) seen on a saved list that the
built-in catalogue (``app.services.ledd.CATALOGUE``) does not know. A name
with a row is **never sent again**, whatever its status:

- ``queued`` — row created after a save, ``classify_substance`` job waiting;
- ``proposed`` — Claude answered (``is_dopaminergic``, ``mechanism``,
  ``factor``, ``source``, ``confidence``, ``generic_name``). A proposal never
  counts toward the LEDD; one that is dopaminergic or not ``high`` confidence
  shows as «pendiente de confirmar» until an admin decides;
- ``confirmed`` — an admin accepted it (optionally with another factor):
  a dopaminergic one counts with ``factor``;
- ``rejected`` — an admin said it does not count (adds 0);
- ``failed`` — the answer could not be used (``error`` code); adds 0.

Only the medication name (and the strength as written) is ever sent: no
patient, no list, no dates. ``name`` / ``strength`` are the first item seen
with this key (display only).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, String
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

SUBSTANCE_STATUSES = ("queued", "proposed", "confirmed", "rejected", "failed")
MECHANISMS = (
    "levodopa",
    "levodopa_cr",
    "comt_inhibitor",
    "dopamine_agonist",
    "mao_b_inhibitor",
    "nmda_antagonist",
    "other_dopaminergic",
    "not_dopaminergic",
    "unknown",
)
CONFIDENCES = ("high", "medium", "low")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class LeddSubstance(TimestampMixin, Base):
    __tablename__ = "ledd_substances"
    __table_args__ = (
        CheckConstraint(_in("status", SUBSTANCE_STATUSES), name="ck_ledd_substances_status"),
        CheckConstraint(
            f"mechanism IS NULL OR {_in('mechanism', MECHANISMS)}",
            name="ck_ledd_substances_mechanism",
        ),
        CheckConstraint(
            f"confidence IS NULL OR {_in('confidence', CONFIDENCES)}",
            name="ck_ledd_substances_confidence",
        ),
        CheckConstraint(
            "factor IS NULL OR (factor >= 0 AND factor <= 1000)", name="ck_ledd_substances_factor"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    strength: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued"
    )
    is_dopaminergic: Mapped[bool | None] = mapped_column(Boolean)
    mechanism: Mapped[str | None] = mapped_column(String(30))
    factor: Mapped[float | None] = mapped_column(Float)
    generic_name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(300))
    confidence: Mapped[str | None] = mapped_column(String(10))
    error: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
