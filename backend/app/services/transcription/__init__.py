"""Voice-note transcription (DESIGN §6.7 step 3; provider decision §11).

- ``provider``: the ``TranscriptionProvider`` interface, ``TranscriptResult``,
  ``TranscriptionError``, ``upload_format`` (stored mime → what the provider
  takes) and ``get_provider(settings)`` (``TRANSCRIPTION_PROVIDER``,
  ``TRANSCRIPTION_MODEL``, ``OPENAI_API_KEY``).
- ``openai_provider``: OpenAI ``/v1/audio/transcriptions`` (``gpt-transcribe``).
- ``hints``: the generic prompt and keyword hints (medication and doctor names).
- ``jobs``: the WhatsApp ``after_import`` hook and the ``transcribe_audio``
  job (dedupe by SHA-256 in ``audio_transcripts``).
"""

from app.services.transcription.provider import (
    TranscriptionError,
    TranscriptionNotConfigured,
    TranscriptionProvider,
    TranscriptResult,
    get_provider,
    upload_format,
)

__all__ = [
    "TranscriptResult",
    "TranscriptionError",
    "TranscriptionNotConfigured",
    "TranscriptionProvider",
    "get_provider",
    "upload_format",
]
