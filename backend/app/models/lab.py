"""Labs (DESIGN §5.5, §6.5): analytes, lab reports and their results.

``analytes`` — the canonical tests ("Vitamina B12") with Spanish ``aliases``
("Cobalamina", "B12", "VIT B12"). ``specimen`` (``sangre``, ``orina``,
``heces``, ``otro``) separates same-named tests ("Glucosa" in blood vs. in a
urinalysis): names are matched only against analytes of the result's
specimen. Migration 0007 seeds the common ones; an unknown name found by the
extraction creates an analyte with ``needs_review = true``; ``triage`` holds
Claude's proposal for it (merge / keep as new / unsure, #100).

``lab_reports`` — one per lab PDF that Claude recognised as a lab report.
``patient_id`` stays NULL until the patient is identified; ``patient_match``
says how (``cip``, ``name``, ``name_variant``, ``llm``, ``manual``; see
``app.services.labs.matching``). ``status``: ``ok``,
``needs_review`` (patient not identified, or no results), ``failed``.
``attachment_id`` is the PDF and ``source_message_id`` the WhatsApp message
that carried it (both ``ON DELETE SET NULL``: deleting a file or message
never deletes medical results). ``raw_extraction`` is the model's output as
returned (PHI: database only, never logged).

``lab_results`` — one row per result line. ``value_text`` always keeps the
value as printed; ``value_num`` is its tolerant numeric parse (``,`` or ``.``
decimals, thousands separators) and ``comparator`` keeps a ``<``/``>``/``<=``/
``>=`` prefix (``"<5"`` → 5 with ``<``). Results go with their report
(``ON DELETE CASCADE`` from ``lab_reports`` only).

``lab_extractions`` — bookkeeping, one row per PDF content (``sha256``,
unique): the outcome of the one Claude call that file ever gets
(``lab_report``, ``not_lab_report``, ``failed`` + a short error code), the
model and token usage. A file whose SHA-256 is here is never sent again.
For a manual upload of several photos (#15) ``sha256`` holds the SHA-256 of
their ordered SHA-256s (``app.services.labs.process.extraction_key``) and
``attachment_id`` the first page.

``lab_links`` — lab-portal links shared in the chat instead of a PDF (#16,
``app.services.labs.links``): one row per normalized URL (``url_hash`` =
SHA-256 of it, unique), the message that carried it (``message_id``), a
patient guess from the message text (``patient_id``, only a hint for the UI),
``status`` (``pending`` → ``fetched`` when the worker downloaded the PDF or
someone uploaded it by hand; ``ignored`` by a person; ``failed`` is reserved —
a link that cannot be fetched stays ``pending`` for someone to act on) and a
short ``reason`` code for why it is still pending. ``attachment_id`` /
``report_id`` / ``job_id`` point at what resolved it. PHI: the URL may carry a
patient token — database only, never logged, never in a job payload.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import AuthorMixin, TimestampMixin

SPECIMENS = ("sangre", "orina", "heces", "otro")
REPORT_SOURCES = ("whatsapp", "upload", "link")
REPORT_STATUSES = ("ok", "needs_review", "failed")
PATIENT_MATCHES = ("cip", "name", "name_variant", "llm", "manual")
FLAGS = ("alto", "bajo", "normal", "anormal")
COMPARATORS = ("<", ">", "<=", ">=")
EXTRACTION_OUTCOMES = ("lab_report", "not_lab_report", "failed")
LINK_STATUSES = ("pending", "fetched", "failed", "ignored")


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    check = f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"
    return f"{column} IS NULL OR {check}" if nullable else check


class Analyte(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "analytes"
    __table_args__ = (CheckConstraint(_in("specimen", SPECIMENS), name="ck_analytes_specimen"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    default_unit: Mapped[str | None] = mapped_column(String(50))
    category: Mapped[str | None] = mapped_column(String(50))
    specimen: Mapped[str] = mapped_column(String(10), nullable=False, server_default="sangre")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Claude's review proposal (#100, ``app.services.labs.triage``), null until
    # triaged. JSON, not a FK: ``merge_analytes`` re-points every FK to analytes.
    triage: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class LabReport(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "lab_reports"
    __table_args__ = (
        CheckConstraint(_in("source", REPORT_SOURCES), name="ck_lab_reports_source"),
        CheckConstraint(_in("status", REPORT_STATUSES), name="ck_lab_reports_status"),
        CheckConstraint(
            _in("patient_match", PATIENT_MATCHES, nullable=True),
            name="ck_lab_reports_patient_match",
        ),
        Index("ix_lab_reports_patient_collected", "patient_id", "collected_on"),
        Index("ix_lab_reports_attachment_id", "attachment_id"),
        Index("ix_lab_reports_source_message_id", "source_message_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    patient_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="SET NULL")
    )
    collected_on: Mapped[date | None] = mapped_column(Date)
    reported_on: Mapped[date | None] = mapped_column(Date)
    lab_name: Mapped[str | None] = mapped_column(String(200))
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachments.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("whatsapp_messages.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="needs_review")
    patient_match: Mapped[str | None] = mapped_column(String(20))
    raw_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    results: Mapped[list["LabResult"]] = relationship(
        back_populates="report",
        order_by="LabResult.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class LabResult(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "lab_results"
    __table_args__ = (
        CheckConstraint(_in("flag", FLAGS, nullable=True), name="ck_lab_results_flag"),
        CheckConstraint(
            _in("comparator", COMPARATORS, nullable=True), name="ck_lab_results_comparator"
        ),
        Index("ix_lab_results_report_id", "report_id"),
        Index("ix_lab_results_analyte_id", "analyte_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lab_reports.id", ondelete="CASCADE"), nullable=False
    )
    analyte_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("analytes.id", ondelete="RESTRICT")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    panel: Mapped[str | None] = mapped_column(String(200))
    raw_name: Mapped[str] = mapped_column(String(300), nullable=False)
    value_text: Mapped[str | None] = mapped_column(String(300))
    value_num: Mapped[Decimal | None] = mapped_column(Numeric)
    comparator: Mapped[str | None] = mapped_column(String(2))
    unit: Mapped[str | None] = mapped_column(String(50))
    ref_low: Mapped[Decimal | None] = mapped_column(Numeric)
    ref_high: Mapped[Decimal | None] = mapped_column(Numeric)
    ref_text: Mapped[str | None] = mapped_column(String(300))
    flag: Mapped[str | None] = mapped_column(String(10))

    report: Mapped[LabReport] = relationship(back_populates="results")


class LabExtraction(TimestampMixin, Base):
    __tablename__ = "lab_extractions"
    __table_args__ = (
        CheckConstraint(_in("outcome", EXTRACTION_OUTCOMES), name="ck_lab_extractions_outcome"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachments.id", ondelete="SET NULL")
    )
    report_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("lab_reports.id", ondelete="SET NULL")
    )
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class LabLink(TimestampMixin, AuthorMixin, Base):
    __tablename__ = "lab_links"
    __table_args__ = (
        CheckConstraint(_in("status", LINK_STATUSES), name="ck_lab_links_status"),
        Index("ix_lab_links_status", "status"),
        Index("ix_lab_links_message_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("whatsapp_messages.id", ondelete="SET NULL")
    )
    patient_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(30))
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachments.id", ondelete="SET NULL")
    )
    report_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("lab_reports.id", ondelete="SET NULL")
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL")
    )
