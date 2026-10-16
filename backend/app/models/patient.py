"""Patients: Papá and Mamá (DESIGN §5.1).

The two rows (slugs ``papa`` / ``mama``) are seeded by migration 0003 with
display names only. ``full_name``, ``id_number`` (CIP), ``birth_date``,
``diagnoses`` and ``notes`` are PHI: the operator fills them in through
``PATCH /patients/{id}`` or ``python -m scripts.patients set``, never in git.
"""

from datetime import date

from sqlalchemy import BigInteger, Date, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import AuthorMixin, TimestampMixin


class Patient(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    id_number: Mapped[str | None] = mapped_column(String(50))
    birth_date: Mapped[date | None] = mapped_column(Date)
    diagnoses: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
