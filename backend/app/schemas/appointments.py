"""Appointments (DESIGN §5.3, §6.3); the bitácora's "Próxima cita" lives in log_entries."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.schemas.log_entries import DoctorRef
from app.schemas.people import _blank_to_none
from app.services.appointments import to_local

Status = Literal["programada", "realizada", "cancelada"]
SortOrder = Literal["asc", "desc"]

_TEXT_FIELDS = ("location", "reason", "notes")


class AppointmentOut(BaseModel):
    """``starts_at`` is on the America/Panama clock (``-05:00``). All-day
    appointments have ``all_day = true`` and ``starts_at`` at 00:00 of the day."""

    id: int
    patient_id: int
    doctor_id: int | None
    doctor: DoctorRef | None
    starts_at: datetime
    duration_min: int
    all_day: bool
    location: str | None
    reason: str | None
    notes: str | None
    status: Status
    created_from_entry_id: int | None
    result_entry_id: int | None
    created_by: int | None
    updated_by: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("starts_at")
    def _local(self, value: datetime) -> str:
        return to_local(value).isoformat()


class AppointmentPage(BaseModel):
    items: list[AppointmentOut]
    total: int
    limit: int
    offset: int


class _AppointmentFields(BaseModel):
    patient_id: int
    doctor_id: int | None = None
    starts_at: datetime = Field(
        description="ISO 8601. Without an offset it is read as America/Panama time."
    )
    duration_min: int = Field(default=30, ge=1, le=1440)
    all_day: bool = False
    location: str | None = Field(default=None, max_length=300)
    reason: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=20_000)

    _blank = field_validator(*_TEXT_FIELDS, mode="before")(_blank_to_none)


class AppointmentCreate(_AppointmentFields):
    """A new appointment is always ``programada``; ``created_from_entry_id`` is
    set only by the bitácora's "Próxima cita" (unknown fields are ignored)."""


class AppointmentUpdate(_AppointmentFields):
    """Only the fields present in the body change. ``{"status": "cancelada"}``
    cancels; ``{"status": "realizada", "result_entry_id": …}`` records that it
    happened (the entry must be the same patient's)."""

    patient_id: int | None = None
    starts_at: datetime | None = None
    duration_min: int | None = Field(default=None, ge=1, le=1440)
    all_day: bool | None = None
    status: Status | None = None
    result_entry_id: int | None = None

    @field_validator("patient_id", "starts_at", "duration_min", "all_day", "status", mode="before")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value
