"""Doctor directory (DESIGN §5.1).

``name`` is the canonical form ("Dra. Ana Pérez"); ``aliases`` holds the other
ways the family writes it ("Anita", "Dra. Pérez"). Matching free text to a
doctor goes through ``app.services.doctors.match_doctors``.
"""

from sqlalchemy import BigInteger, Boolean, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import AuthorMixin, TimestampMixin


class Doctor(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    specialty: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(254))
    clinic: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
