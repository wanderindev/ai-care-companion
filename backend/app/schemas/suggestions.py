"""Suggestions and «Pendientes» (DESIGN §5.6, §6.7 step 5).

Each kind's ``payload`` is validated with the schema of the API it ends up
calling, so an approved suggestion is held to exactly the same rules as the
normal form:

- ``create_entry`` → ``LogEntryCreate`` (``POST /log-entries``)
- ``update_entry`` → ``{entry_id, changes: LogEntryUpdate}`` (``PATCH /log-entries/{id}``)
- ``create_appointment`` → ``AppointmentCreate`` (``POST /appointments``)
- ``med_change`` → :class:`MedChangePayload`: one item added, stopped or
  changed; applied as a full-list save computed from the **current** list
  (``PUT /patients/{id}/meds``)
- ``doctor`` → ``DoctorCreate`` (``POST /doctors``)
- ``lab_report`` → any object (reserved; no apply path in v1)

Payloads are stored as ``model_dump(mode="json", exclude_unset=True)``, so
what was left out stays left out (e.g. ``next_appointment`` in an update).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.appointments import AppointmentCreate
from app.schemas.log_entries import LogEntryCreate, LogEntryUpdate
from app.schemas.meds import MedAvoidWarning, MedItemIn, Slot
from app.schemas.people import DoctorCreate, _blank_to_none

Kind = Literal[
    "create_entry", "update_entry", "create_appointment", "med_change", "lab_report", "doctor"
]
Source = Literal["whatsapp", "chat", "ai"]
Status = Literal["pending", "approved", "edited", "rejected"]
MedAction = Literal["add", "stop", "change"]


class UpdateEntryPayload(BaseModel):
    """Changes to an existing bitácora entry: ``changes`` is a
    ``PATCH /log-entries/{entry_id}`` body (only what changes)."""

    entry_id: int = Field(gt=0)
    changes: LogEntryUpdate

    @model_validator(mode="after")
    def _something(self):
        if not self.changes.model_fields_set:
            raise ValueError("changes is empty")
        return self


class MedItemPatch(MedItemIn):
    """The fields of one medication that change; only the ones sent count."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    slot: Slot | None = None

    @field_validator("name", "slot", mode="before")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value

    @model_validator(mode="after")
    def _no_id(self):
        if "id" in self.model_fields_set:
            raise ValueError("item must not carry an id (use item_id)")
        return self


class MedChangePayload(BaseModel):
    """One change to a patient's medication list.

    - ``add``: ``item`` is a complete new medication (``name`` and ``slot``
      required);
    - ``stop``: ``item_id`` leaves the list (``end_date`` = yesterday, as when
      the editor removes it);
    - ``change``: ``item`` holds the fields of ``item_id`` that change (dose,
      time, slot…).

    ``medication`` is the name to show (the item may be gone by the time
    someone looks). ``base_version`` is the list version the proposal was
    made from: when the list has changed since, the change is still applied
    to the **current** list, and the UI says so. ``change_summary`` replaces
    the auto-generated version summary when given.
    """

    action: MedAction
    medication: str = Field(min_length=1, max_length=200)
    item_id: int | None = Field(default=None, gt=0)
    item: MedItemPatch | None = None
    base_version: int | None = Field(default=None, ge=0)
    change_summary: str | None = Field(default=None, max_length=5000)

    _blank = field_validator("change_summary", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _shape(self):
        if self.action == "add":
            if self.item is None or self.item_id is not None:
                raise ValueError("add needs item and no item_id")
            if self.item.name is None or self.item.slot is None:
                raise ValueError("a new medication needs name and slot")
        elif self.action == "stop":
            if self.item_id is None or self.item is not None:
                raise ValueError("stop needs item_id and no item")
        elif self.item_id is None or self.item is None or not self.item.model_fields_set:
            raise ValueError("change needs item_id and the changed fields in item")
        return self


class LabReportPayload(BaseModel):
    """Reserved kind: any object, never applied."""

    model_config = ConfigDict(extra="allow")


PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "create_entry": LogEntryCreate,
    "update_entry": UpdateEntryPayload,
    "create_appointment": AppointmentCreate,
    "med_change": MedChangePayload,
    "lab_report": LabReportPayload,
    "doctor": DoctorCreate,
}


class SourceMessageOut(BaseModel):
    """A WhatsApp message a suggestion is based on. The file (``attachment_id``)
    comes from ``GET /attachments/{id}/url``; ``transcript`` is the voice
    note's automatic transcript and ``classification`` the image/PDF
    classification (#36, #37)."""

    id: int
    sent_at: datetime
    sender: str | None
    text: str
    forwarded: bool
    media_kind: str | None
    attachment_id: int | None
    mime: str | None = None
    filename: str | None = None
    transcript: str | None
    classification: dict[str, Any] | None


class SuggestionOut(BaseModel):
    """``context`` is per kind, computed when listed: ``update_entry`` →
    ``{entry: {...current values} | null}``; ``med_change`` →
    ``{version_no, stale, item: {...current item} | null}``; ``doctor`` →
    ``{duplicates: [{id, name, specialty}]}`` (doctors already matching the
    proposed name or an alias). A chat suggestion (#41) has
    ``chat_session_id`` (the conversation) and ``created_by`` /
    ``created_by_name`` (who asked in the chat). ``explicit_request``: a
    WhatsApp suggestion answering a «Bot: …» message (#134)."""

    id: int
    kind: Kind
    source: Source
    patient_id: int | None
    payload: dict[str, Any]
    rationale: str | None
    explicit_request: bool = False
    source_message_ids: list[int]
    import_id: int | None
    analysis_run_id: int | None = None
    chat_session_id: int | None = None
    created_by: int | None = None
    created_by_name: str | None = None
    status: Status
    decided_by: int | None
    decided_by_name: str | None = None
    decided_at: datetime | None
    decision_note: str | None
    result_ref: dict[str, Any] | None
    created_at: datetime
    source_messages: list[SourceMessageOut] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class SuggestionPage(BaseModel):
    items: list[SuggestionOut]
    total: int
    limit: int
    offset: int


class SuggestionCount(BaseModel):
    pending: int


class SuggestionApprove(BaseModel):
    """All optional. ``payload`` replaces the proposal (same shape, validated
    the same way) → status ``edited`` when it differs. ``med_change`` only:
    ``expected_version`` is the list version the preview was computed on
    (409 when the list changed since) and ``change_summary`` the edited
    version summary. ``doctor`` only: ``doctor_id`` links the suggestion to
    an existing doctor instead of creating one."""

    payload: dict[str, Any] | None = None
    expected_version: int | None = Field(default=None, ge=0)
    change_summary: str | None = Field(default=None, max_length=5000)
    doctor_id: int | None = Field(default=None, gt=0)

    _blank = field_validator("change_summary", mode="before")(_blank_to_none)


class SuggestionReject(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)

    _blank = field_validator("reason", mode="before")(_blank_to_none)


class SuggestionPreviewIn(BaseModel):
    payload: dict[str, Any] | None = None


class MedChangePreview(BaseModel):
    """Dry run of a ``med_change``: the diff and auto summary of the full-list
    save it would make on the current list (``version_no``), its EVITAR
    warnings, and ``stale`` when the list changed since the proposal."""

    version_no: int
    auto_summary: str
    diff: dict[str, Any]
    warnings: list[MedAvoidWarning] = Field(default_factory=list)
    stale: bool
