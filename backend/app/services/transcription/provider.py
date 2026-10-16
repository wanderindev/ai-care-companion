"""The transcription provider interface (DESIGN §6.7 step 3, §11 decision).

A provider turns one audio file into text::

    provider = get_provider(settings)          # TranscriptionNotConfigured without a key
    result = provider.transcribe(data, filename="nota.ogg", mime="audio/ogg",
                                 language="es", prompt="…", keywords=["Losartán"])
    result.text, result.duration_s, result.model, result.usage, result.cost_usd

Failures raise ``TranscriptionError`` with a short ``code`` (never provider
text, which could quote the audio) and whether retrying can help:
``retryable`` (timeouts, connection errors, 429, 5xx), ``config`` (bad or
missing key, unknown model: fix the settings, then re-run), otherwise the
file itself was refused (400/413/415: recorded, never sent again).

``upload_format(mime)`` maps a stored attachment's type (sniffed from the
bytes, ``app.services.attachments``) to the file name extension and type the
provider accepts. WhatsApp voice notes are Ogg Opus: the OpenAI API accepts
``ogg`` directly, so no ffmpeg conversion is needed. AAC in a raw ADTS stream
is not on OpenAI's list and is recorded as ``unsupported``.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import Settings

# mime (as stored on the attachment) -> (extension, mime sent to the provider)
UPLOAD_FORMATS: dict[str, tuple[str, str]] = {
    "audio/ogg": ("ogg", "audio/ogg"),
    "audio/mpeg": ("mp3", "audio/mpeg"),
    "audio/mp4": ("m4a", "audio/mp4"),
    "audio/webm": ("webm", "audio/webm"),
    "audio/wav": ("wav", "audio/wav"),
    "audio/x-wav": ("wav", "audio/wav"),
    "audio/flac": ("flac", "audio/flac"),
}


def upload_format(mime: str | None) -> tuple[str, str] | None:
    """``(extension, mime)`` to send for a stored type, or None when unsupported."""
    return UPLOAD_FORMATS.get((mime or "").split(";")[0].strip().lower())


@dataclass
class TranscriptResult:
    text: str
    model: str
    duration_s: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None


class TranscriptionError(Exception):
    """A failed transcription. ``code`` is short and safe to store and log."""

    def __init__(self, code: str, *, retryable: bool = False, config: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.config = config


class TranscriptionNotConfigured(Exception):
    """No API key (or an unknown provider) in the settings."""


class TranscriptionProvider(Protocol):
    model: str

    def transcribe(
        self,
        data: bytes,
        *,
        filename: str,
        mime: str,
        language: str = "es",
        prompt: str | None = None,
        keywords: Sequence[str] = (),
    ) -> TranscriptResult: ...


def get_provider(settings: Settings) -> TranscriptionProvider:
    """The configured provider; raises ``TranscriptionNotConfigured``."""
    name = (settings.transcription_provider or "").strip().lower()
    if name == "openai":
        if not settings.openai_api_key:
            raise TranscriptionNotConfigured("OPENAI_API_KEY is not set")
        from app.services.transcription.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=settings.openai_api_key, model=settings.transcription_model)
    raise TranscriptionNotConfigured("unknown TRANSCRIPTION_PROVIDER")
