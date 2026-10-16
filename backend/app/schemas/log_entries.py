"""Bitácora entries (DESIGN §5.2, §6.1)."""

import datetime as dt
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from app.schemas.people import _blank_to_none
from app.services.appointments import to_local

EntryType = Literal[
    "consulta_control",
    "consulta_enfermedad",
    "visita_domiciliaria",
    "teleconsulta",
    "terapia",
    "vacuna",
    "procedimiento",
    "hospitalizacion",
    "examen",
    "observacion_familiar",
    "otro",
]
Channel = Literal["presencial", "domicilio", "whatsapp", "telefono", "correo"]
ReviewStatus = Literal["ok", "pending_review"]
Origin = Literal["import_docx", "whatsapp", "manual", "chat"]
SortField = Literal["date", "doctor", "type"]
SortOrder = Literal["asc", "desc"]

_TEXT_FIELDS = ("source_label", "title", "indications", "family_notes")
# Same rule as app.services.log_entries.validate, checked early for a 422.
HEADER_REQUIRED = "Indica el médico, la fuente o un título."


class DoctorRef(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class EntryAttachment(BaseModel):
    """What a card or the detail view needs; the file itself comes from
    ``GET /attachments/{id}/url``."""

    id: int
    filename: str
    mime: str
    size: int

    model_config = {"from_attributes": True}


class LogEntryListItem(BaseModel):
    id: int
    patient_id: int
    entry_date: date
    entry_time: time | None
    doctor_id: int | None
    doctor: DoctorRef | None
    source_label: str | None
    entry_type: EntryType
    channel: Channel | None
    title: str | None
    indications: str | None
    family_notes: str | None
    review_status: ReviewStatus
    origin: Origin
    attachments: list[EntryAttachment]
    created_by: int | None
    updated_by: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NextAppointmentIn(BaseModel):
    """The "Próxima cita" written in an entry. Without ``time`` it is an all-day
    appointment. Leaving ``doctor_id`` out uses the entry's doctor (``null``
    means no doctor); leaving ``location`` / ``reason`` / ``notes`` out keeps
    the existing appointment's value."""

    date: dt.date
    time: dt.time | None = None
    doctor_id: int | None = None
    location: str | None = Field(default=None, max_length=300)
    reason: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=20_000)

    _blank = field_validator("location", "reason", "notes", mode="before")(_blank_to_none)


class NextAppointmentOut(BaseModel):
    """The appointment an entry created (any status; the form edits it only
    while ``programada``). ``starts_at`` is on the America/Panama clock."""

    id: int
    starts_at: datetime
    all_day: bool
    doctor_id: int | None
    doctor: DoctorRef | None
    location: str | None
    reason: str | None
    notes: str | None
    status: Literal["programada", "realizada", "cancelada"]

    model_config = {"from_attributes": True}

    @field_serializer("starts_at")
    def _local(self, value: datetime) -> str:
        return to_local(value).isoformat()


class LogEntryOut(LogEntryListItem):
    """One entry, with ``origin_ref`` (importer data: original text, extracted
    next appointment, medication events…) and ``next_appointment``, the
    appointment this entry's "Próxima cita" created (null when none)."""

    origin_ref: dict[str, Any] | None
    next_appointment: NextAppointmentOut | None = None


class LogEntryPage(BaseModel):
    items: list[LogEntryListItem]
    total: int
    limit: int
    offset: int


class _EntryFields(BaseModel):
    patient_id: int
    entry_date: date
    entry_time: time | None = None
    doctor_id: int | None = None
    source_label: str | None = Field(default=None, max_length=200)
    entry_type: EntryType
    channel: Channel | None = None
    title: str | None = Field(default=None, max_length=300)
    indications: str | None = Field(default=None, max_length=50_000)
    family_notes: str | None = Field(default=None, max_length=50_000)
    review_status: ReviewStatus = "ok"
    attachment_ids: list[int] = Field(default_factory=list, max_length=50)
    next_appointment: NextAppointmentIn | None = None

    _blank = field_validator(*_TEXT_FIELDS, mode="before")(_blank_to_none)


class LogEntryCreate(_EntryFields):
    """A manual entry. ``origin`` is always ``manual`` and ``origin_ref`` empty;
    unknown fields (e.g. a client-sent ``origin``) are ignored."""

    @model_validator(mode="after")
    def _has_header(self):
        if self.doctor_id is None and not self.source_label and not self.title:
            raise ValueError(HEADER_REQUIRED)
        return self


class LogEntryUpdate(_EntryFields):
    """Only the fields present in the body change. ``attachment_ids``, when
    present, is the entry's full set: new ids are linked, missing ones unlinked.
    Send ``{"review_status": "ok"}`` to mark an entry as reviewed.
    ``next_appointment`` creates/updates the entry's appointment; ``null``
    cancels it (only while ``programada``); leaving it out changes nothing."""

    patient_id: int | None = None
    entry_date: date | None = None
    entry_type: EntryType | None = None
    review_status: ReviewStatus | None = None
    attachment_ids: list[int] | None = Field(default=None, max_length=50)

    @field_validator(
        "patient_id", "entry_date", "entry_type", "review_status", "attachment_ids", mode="before"
    )
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value


class LogEntryFilter(BaseModel):
    """The list filters of ``GET /log-entries`` (same names and meaning), minus
    ``review_status``: the bulk review always targets ``pending_review``."""

    patient_id: list[int] | None = None
    doctor_id: list[int] | None = None
    entry_type: list[EntryType] | None = None
    channel: list[Channel] | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = Field(default=None, max_length=200)


class LogEntryBulkReview(BaseModel):
    """«Marcar todas como revisadas»: either the list's current ``filter`` or
    explicit ``ids`` (exactly one). Only ``pending_review`` entries change."""

    ids: list[int] | None = Field(default=None, min_length=1, max_length=5000)
    filter: LogEntryFilter | None = None

    @model_validator(mode="after")
    def _one_target(self):
        if (self.ids is None) == (self.filter is None):
            raise ValueError("Send either ids or filter.")
        return self


class LogEntryBulkReviewOut(BaseModel):
    updated: int
    ids: list[int]
