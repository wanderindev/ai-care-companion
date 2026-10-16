"""Model registry.

Import every model module here so ``Base.metadata`` (used by Alembic's
autogenerate and by the test schema setup) always sees every table. A model
missing from this file would make ``alembic revision --autogenerate`` propose
dropping its table.
"""

from app.core.database import Base
from app.models.analysis_run import AnalysisRun
from app.models.appointment import Appointment
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.calendar_feed import CalendarFeed
from app.models.chat import ChatMessage, ChatSession
from app.models.doctor import Doctor
from app.models.extension_token import ExtensionToken
from app.models.job import Job
from app.models.lab import Analyte, LabExtraction, LabLink, LabReport, LabResult
from app.models.ledd import LeddSubstance
from app.models.log_entry import LogEntry
from app.models.media_classification import MediaClassification
from app.models.medication import (
    MedAvoid,
    MedCheckDismissal,
    MedEvent,
    MedItem,
    MedListVersion,
)
from app.models.patient import Patient
from app.models.suggestion import Suggestion
from app.models.transcription import AudioTranscript
from app.models.user import MagicLinkToken, Session, User
from app.models.whatsapp import WhatsappImport, WhatsappMessage

__all__ = [
    "AnalysisRun",
    "Appointment",
    "Attachment",
    "Analyte",
    "AudioTranscript",
    "AuditLog",
    "CalendarFeed",
    "ChatMessage",
    "ChatSession",
    "Base",
    "Doctor",
    "ExtensionToken",
    "Job",
    "LabExtraction",
    "LabLink",
    "LabReport",
    "LabResult",
    "LeddSubstance",
    "LogEntry",
    "MagicLinkToken",
    "MediaClassification",
    "MedAvoid",
    "MedCheckDismissal",
    "MedEvent",
    "MedItem",
    "MedListVersion",
    "Patient",
    "Session",
    "Suggestion",
    "User",
    "WhatsappImport",
    "WhatsappMessage",
]
