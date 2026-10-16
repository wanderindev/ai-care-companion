"""Lab reports, results and analyte series (DESIGN §5.5, §6.5).

Numbers (``value_num``, ``ref_low``, ``ref_high``) leave the API as JSON
numbers (``float``), not the strings Pydantic would make of a ``Decimal``:
the frontend plots them. ``value_text`` keeps the value as printed.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.log_entries import EntryAttachment
from app.schemas.people import _blank_to_none

ReportStatus = Literal["ok", "needs_review", "failed"]
EditableStatus = Literal["ok", "needs_review"]
Flag = Literal["alto", "bajo", "normal", "anormal"]
Comparator = Literal["<", ">", "<=", ">="]


class LabReportListItem(BaseModel):
    id: int
    patient_id: int | None
    collected_on: date | None
    reported_on: date | None
    # collected_on, else reported_on, else the day the report was stored: the
    # date the list groups and sorts by.
    report_date: date
    lab_name: str | None
    source: str
    status: ReportStatus
    patient_match: str | None
    attachment: EntryAttachment | None
    result_count: int
    out_of_range_count: int
    # Raw names of the flagged results (alto/bajo/anormal), in report order,
    # at most ``FLAGGED_NAMES_MAX`` of them, for the list card.
    flagged_names: list[str]
    panels: list[str]
    created_at: datetime
    updated_at: datetime


class LabReportPage(BaseModel):
    items: list[LabReportListItem]
    total: int
    limit: int
    offset: int


class LabResultOut(BaseModel):
    id: int
    position: int
    panel: str | None
    analyte_id: int | None
    analyte_name: str | None
    raw_name: str
    value_text: str | None
    value_num: float | None
    comparator: Comparator | None
    unit: str | None
    ref_low: float | None
    ref_high: float | None
    ref_text: str | None
    flag: Flag | None


class LabReportOut(LabReportListItem):
    results: list[LabResultOut]
    # Every file of the report: ``attachment`` first, then the other pages of
    # a photographed report (#15), in upload order.
    attachments: list[EntryAttachment]


class LabReportUpdate(BaseModel):
    """Only the fields present in the body change. ``patient_id`` cannot be
    cleared (a report goes from unidentified to a patient, or between
    patients); ``status`` ``ok`` marks the report as reviewed."""

    patient_id: int | None = None
    collected_on: date | None = None
    reported_on: date | None = None
    lab_name: str | None = Field(default=None, max_length=200)
    status: EditableStatus | None = None

    _blank = field_validator("lab_name", mode="before")(_blank_to_none)

    @field_validator("patient_id", "status", mode="before")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value


class AnalyteOut(BaseModel):
    id: int
    canonical_name: str
    aliases: list[str]
    default_unit: str | None
    category: str | None
    specimen: str
    needs_review: bool

    model_config = {"from_attributes": True}


class SeriesPoint(BaseModel):
    result_id: int
    report_id: int
    date: date
    collected_on: date | None
    reported_on: date | None
    lab_name: str | None
    report_status: ReportStatus
    raw_name: str
    value_text: str | None
    value_num: float | None
    comparator: Comparator | None
    unit: str | None
    ref_low: float | None
    ref_high: float | None
    ref_text: str | None
    flag: Flag | None


class AnalyteSeries(BaseModel):
    analyte: AnalyteOut
    patient_id: int
    points: list[SeriesPoint]


# ── Manual upload (#15) ──────────────────────────────────────────────────────


class LabUploadIn(BaseModel):
    """Files from ``POST /attachments``: one PDF, or photos of the pages of
    one report in page order. ``patient_id`` is the uploader's hint
    (``app.services.labs.matching.apply_patient_hint``)."""

    attachment_ids: list[int] = Field(min_length=1, max_length=20)
    patient_id: int | None = None
    # «Subir PDF» from a lab link (#16): the first upload marks it resolved;
    # later ones (a link with several PDFs, #124) are accepted too, one
    # report each. An ignored link is refused.
    lab_link_id: int | None = None


class LabUploadJob(BaseModel):
    job_id: int


class LabUploadOutcome(BaseModel):
    """Where an upload job ended. ``outcome`` is null while the job runs (or
    when it failed before recording anything); ``report_id`` is set for a lab
    report — also an earlier one, when the same files were already read."""

    job_id: int
    job_status: str
    outcome: Literal["lab_report", "not_lab_report", "failed"] | None
    report_id: int | None


# ── Lab portal links (#16) ───────────────────────────────────────────────────

LinkStatus = Literal["pending", "fetched", "failed", "ignored"]


class LabLinkOut(BaseModel):
    """A lab portal link from the chat. ``url`` is data to show as text or
    open in a new tab, never HTML. ``patient_id`` is only a guess from the
    message text. ``reason`` says why a pending link was not downloaded."""

    id: int
    url: str
    host: str
    status: LinkStatus
    reason: str | None
    patient_id: int | None
    sent_at: datetime | None
    sender: str | None
    report_id: int | None
    created_at: datetime


# ── Analyte review (#15) ─────────────────────────────────────────────────────

Specimen = Literal["sangre", "orina", "heces", "otro"]


class AnalyteExample(BaseModel):
    raw_name: str
    value_text: str | None
    unit: str | None


class AnalyteTriageOut(BaseModel):
    """Claude's proposal for an analyte under review (#100,
    ``app.services.labs.triage``)."""

    action: Literal["merge_into", "keep_as_new", "unsure"]
    target_id: int | None = None
    target_name: str | None = None  # null when the target no longer exists
    display_name: str | None = None
    confidence: Literal["high", "medium", "low"]
    reason_code: str | None = None
    units_match: bool | None = None
    status: Literal["pending", "applied", "accepted", "rejected"]
    error: str | None = None


class AnalyteListItem(AnalyteOut):
    result_count: int
    examples: list[AnalyteExample]
    triage: AnalyteTriageOut | None = None


class AnalyteTriageEstimate(BaseModel):
    analytes: int  # still without a proposal
    batches: int
    model: str
    cost_usd: float | None
    pending_proposals: int  # proposals waiting for accept / reject
    running_job_id: int | None


class AnalyteTriageStarted(BaseModel):
    job_id: int


class AnalyteTriageAcceptAll(BaseModel):
    accepted: int
    failed: int


class AnalyteUpdate(BaseModel):
    """Only the fields present in the body change; ``needs_review: false``
    confirms the analyte."""

    canonical_name: str | None = Field(default=None, min_length=1, max_length=200)
    default_unit: str | None = Field(default=None, max_length=50)
    category: str | None = Field(default=None, max_length=50)
    specimen: Specimen | None = None
    needs_review: bool | None = None

    _blank = field_validator("default_unit", "category", mode="before")(_blank_to_none)

    @field_validator("canonical_name", mode="before")
    @classmethod
    def _name(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("specimen", "needs_review", mode="before")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value


class AnalyteMerge(BaseModel):
    source_id: int
    allow_specimen_mismatch: bool = False
