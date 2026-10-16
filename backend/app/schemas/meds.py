"""Medication lists (DESIGN §5.4, §6.4)."""

import datetime as dt
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from app.schemas.log_entries import DoctorRef
from app.schemas.people import _blank_to_none

Slot = Literal[
    "al_despertar",
    "desayuno",
    "manana",
    "almuerzo",
    "tarde",
    "cena",
    "noche",
    "madrugada",
    "segun_necesidad",
    "otros",
]

_ITEM_TEXT = ("strength", "quantity", "dose_note", "schedule_note", "instructions", "reason")


class MedItemIn(BaseModel):
    """One item of a full-list save. ``id`` is set for an item already on the
    list and omitted for a new one. ``sort_order`` is not sent: the position
    of the item among the request's items of the same slot is its order."""

    id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    strength: str | None = Field(default=None, max_length=100)
    quantity: str | None = Field(default=None, max_length=50)
    dose_note: str | None = Field(default=None, max_length=200)
    slot: Slot
    time: dt.time | None = None
    days: list[int] | None = Field(
        default=None, description="ISO weekdays (1 = lunes … 7 = domingo); null = every day."
    )
    schedule_note: str | None = Field(default=None, max_length=10000)
    instructions: str | None = Field(default=None, max_length=10000)
    prescriber_id: int | None = None
    reason: str | None = Field(default=None, max_length=300)
    start_date: date | None = None
    end_date: date | None = None
    source_entry_id: int | None = None

    _blank = field_validator(*_ITEM_TEXT, mode="before")(_blank_to_none)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("days")
    @classmethod
    def _days(cls, value: list[int] | None) -> list[int] | None:
        if not value:
            return None
        if any(d < 1 or d > 7 for d in value):
            raise ValueError("days must be ISO weekdays 1-7")
        days = sorted(set(value))
        return None if len(days) == 7 else days

    @field_validator("time")
    @classmethod
    def _minutes(cls, value: dt.time | None) -> dt.time | None:
        return value.replace(second=0, microsecond=0, tzinfo=None) if value else None

    @model_validator(mode="after")
    def _dates(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        return self


class MedAvoidIn(BaseModel):
    id: int | None = None
    substance: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)
    source_entry_id: int | None = None

    _blank = field_validator("reason", mode="before")(_blank_to_none)

    @field_validator("substance", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


LIST_NOTE_MAX = 20_000


class MedListSave(BaseModel):
    """``PUT /patients/{id}/meds``: the complete list. Current items missing
    from ``items`` are removed (``end_date`` = yesterday, Panamá); EVITAR rows
    missing from ``avoid`` are deleted. ``expected_version`` is the
    ``version_no`` the editor started from (0 when the patient has none yet):
    a different current version answers 409. ``change_summary`` replaces the
    auto-generated one when given. ``dry_run`` computes the diff and summary
    without saving (for the confirmation step).

    ``effective_date`` («Desde») and ``list_note`` (markdown, e.g. the
    regimen table) are the list-level fields: **omitted** keeps the current
    version's value, an explicit ``null`` (or a blank note) clears it."""

    items: list[MedItemIn] = Field(default_factory=list, max_length=300)
    avoid: list[MedAvoidIn] = Field(default_factory=list, max_length=100)
    expected_version: int = Field(ge=0)
    change_summary: str | None = Field(default=None, max_length=5000)
    effective_date: date | None = None
    list_note: str | None = Field(default=None, max_length=LIST_NOTE_MAX)
    dry_run: bool = False

    _blank = field_validator("change_summary", "list_note", mode="before")(_blank_to_none)


class MedItemOut(BaseModel):
    id: int
    name: str
    strength: str | None
    quantity: str | None
    dose_note: str | None
    slot: Slot
    time: dt.time | None
    days: list[int] | None
    schedule_note: str | None
    instructions: str | None
    prescriber_id: int | None
    prescriber: DoctorRef | None
    reason: str | None
    start_date: date | None
    end_date: date | None
    sort_order: int
    source_entry_id: int | None
    created_by: int | None
    updated_by: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("time")
    def _hhmm(self, value: dt.time | None) -> str | None:
        return value.strftime("%H:%M") if value else None


class MedAvoidOut(BaseModel):
    id: int
    substance: str
    reason: str | None
    source_entry_id: int | None

    model_config = {"from_attributes": True}


class MedSlotGroup(BaseModel):
    slot: Slot
    label: str
    items: list[MedItemOut]


class MedList(BaseModel):
    """The current list: every slot in printed-sheet order (empty ones too),
    the EVITAR list, and the current version (``version_no`` 0 = none yet).
    ``list_note`` (markdown, e.g. a regimen table) and ``effective_date`` (the
    sheet's "Desde"; null = the date of ``changed_at``) are the current
    version's list-level fields."""

    patient_id: int
    version_no: int
    change_summary: str | None
    changed_by: int | None
    changed_at: datetime | None
    list_note: str | None = None
    effective_date: date | None = None
    slots: list[MedSlotGroup]
    avoid: list[MedAvoidOut]
    # LEDD of the list (ticket #133, ``app.services.ledd.compute`` shape):
    # total_mg, contributions, as_needed, not_calculated, pending.
    ledd: dict[str, Any] | None = None


class MedAvoidWarning(BaseModel):
    """An item of the request that matches an EVITAR row of the request
    (``app.services.meds_checks``). ``item_index`` is its position in the
    request's ``items``; ``item_id`` is null for a new item."""

    item_index: int
    item_id: int | None
    name: str
    substance: str
    reason: str | None


class MedSaveResult(MedList):
    """``saved`` is false for a dry run or a save with no changes (then no
    version is created and ``diff`` is empty). ``auto_summary`` is the
    generated text, ``change_summary`` the stored one. ``warnings`` lists the
    request's items that match its EVITAR list (never blocking: a save with
    warnings is saved)."""

    saved: bool
    auto_summary: str
    diff: dict[str, Any]
    warnings: list[MedAvoidWarning] = Field(default_factory=list)
    # LEDD before → after the save ({before_mg, after_mg, pct}); null = unchanged.
    ledd_change: dict[str, Any] | None = None


class MedVersionOut(BaseModel):
    version_no: int
    change_summary: str
    changed_by: int | None
    changed_by_name: str | None
    changed_at: datetime
    effective_date: date | None = None
    item_count: int
    avoid_count: int
    ledd_mg: float | None = None  # the version's LEDD total (ticket #133)


class MedVersionDetail(MedVersionOut):
    """``diff`` compares this version with ``previous_version_no`` (``null``
    for version 1: everything appears as added). ``next_version_no`` is the
    following version (``null`` for the current one), for navigation."""

    snapshot: dict[str, Any]
    previous_version_no: int | None
    next_version_no: int | None = None
    diff: dict[str, Any]
    ledd: dict[str, Any] | None = None
    ledd_change: dict[str, Any] | None = None  # vs the previous version


class MedEventOut(BaseModel):
    """One ``med_events`` row: the history reconstructed from the bitácora.
    ``source_entry_date`` is the date of ``source_entry_id`` (null when the
    event has no entry or it was deleted)."""

    id: int
    event_date: date
    kind: Literal["inicio", "suspension", "cambio_dosis", "sustitucion"]
    medication: str
    detail: str | None
    source_entry_id: int | None
    source_entry_date: date | None
    review_status: Literal["ok", "pending_review"]


class MedEventsPage(BaseModel):
    """One page of ``GET …/meds/events``: ``next_cursor`` is the ``cursor`` to
    pass for the next (older) page, ``null`` when there is none."""

    items: list[MedEventOut]
    next_cursor: str | None


class MedEventUpdate(BaseModel):
    """Review of one derived event: only the fields present change. Send
    ``{"review_status": "ok"}`` to confirm it (with or without edits).
    ``detail`` may be ``null`` (or blank) to clear it; the others cannot."""

    model_config = {"extra": "forbid"}

    event_date: date | None = None
    kind: Literal["inicio", "suspension", "cambio_dosis", "sustitucion"] | None = None
    medication: str | None = Field(default=None, min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=2000)
    review_status: Literal["ok", "pending_review"] | None = None

    _blank = field_validator("detail", mode="before")(_blank_to_none)

    @field_validator("medication", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("event_date", "kind", "medication", "review_status", mode="before")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value


class MedEventsConfirm(BaseModel):
    """«Confirmar todos»: the ids of the pending events the user saw (≤ 500 per
    request, ``services.meds.CONFIRM_MAX``; the frontend sends more in chunks)."""

    ids: list[int] = Field(min_length=1, max_length=500)


class MedEventsConfirmResult(BaseModel):
    confirmed: int


class MedCheckEvent(BaseModel):
    kind: Literal["inicio", "suspension", "cambio_dosis", "sustitucion"]
    medication: str
    detail: str | None


class MedCheckEntry(BaseModel):
    """A bitácora entry newer than the list's «Desde» that mentions medication
    changes (its events), not dismissed."""

    id: int
    entry_date: date
    title: str | None
    events: list[MedCheckEvent]


class MedChecks(BaseModel):
    """``GET /patients/{id}/meds/checks``: ``reference_date`` is the current
    version's «Desde» (null: no version yet, nothing to check); ``entries``
    oldest first (at most 50), ``total`` counts them all."""

    reference_date: date | None
    total: int
    entries: list[MedCheckEntry]


class LeddSubstanceOut(BaseModel):
    """A medication name outside the LEDD table with Claude's proposal
    (``app.models.ledd``); ``needs_review`` = shown as «pendiente de confirmar»."""

    model_config = {"from_attributes": True}

    id: int
    name: str
    strength: str | None
    status: str
    is_dopaminergic: bool | None
    mechanism: str | None
    factor: float | None
    generic_name: str | None
    source: str | None
    confidence: str | None
    needs_review: bool = False


class LeddConfirm(BaseModel):
    """Confirm a proposal as dopaminergic; ``factor`` overrides the proposed one."""

    factor: float | None = Field(default=None, gt=0, le=1000)
