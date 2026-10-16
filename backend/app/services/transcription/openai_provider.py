"""OpenAI speech-to-text (``POST /v1/audio/transcriptions``), default model
``gpt-transcribe`` (operator decision, DESIGN §11).

Called with the ``httpx2`` client the backend already ships (used by the lab
link fetcher, and a dependency of the Anthropic SDK) instead of the ``openai``
SDK: one multipart POST does not justify another SDK and its dependency tree
in the 256 MB worker image.

Request (multipart form): ``model``, ``file`` (``<name>.<ext>`` with its
type), ``response_format=json``, ``languages[]`` (language hints;
``gpt-transcribe`` takes ``languages`` instead of the older singular
``language``), ``prompt`` (short unstructured context) and ``keywords[]``
(literal terms expected in the audio; one line each, no ``<``/``>``). The
JSON answer has ``text`` and, for this model, ``usage`` ``{"type":
"duration", "seconds": n}`` (billed per minute) or a token usage object.

Files may be up to 25 MB (the caller checks before reading them). Timeouts:
10 s to connect, ``REQUEST_TIMEOUT_SECONDS`` overall read. The client never
retries itself: the job's backoff does (``retryable`` errors).

Logging: none here beyond what the caller logs (ids, durations, cost);
never the transcript, prompt or keywords.
"""

from collections.abc import Sequence
from typing import Any

import httpx2 as httpx

from app.services.transcription.provider import TranscriptionError, TranscriptResult

API_URL = "https://api.openai.com/v1/audio/transcriptions"
REQUEST_TIMEOUT_SECONDS = 180.0
CONNECT_TIMEOUT_SECONDS = 10.0
# USD per minute of audio, for the cost estimate in the logs and ``usage``.
PRICE_PER_MINUTE_USD = {"gpt-transcribe": 0.0045}
CONFIG_STATUSES = {401, 403, 404}
FILE_STATUSES = {400, 413, 415, 422}


def _sanitize_keyword(word: str) -> str:
    return " ".join(word.replace("<", " ").replace(">", " ").split())


class OpenAIProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-transcribe",
        transport: httpx.BaseTransport | None = None,
        url: str = API_URL,
    ):
        self.api_key = api_key
        self.model = model
        self.url = url
        self._transport = transport  # tests pass an httpx MockTransport

    def request_fields(
        self, *, language: str, prompt: str | None, keywords: Sequence[str]
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"model": self.model, "response_format": "json"}
        if language:
            fields["languages[]"] = [language]
        if prompt:
            fields["prompt"] = prompt
        words = [w for w in (_sanitize_keyword(k) for k in keywords) if w]
        if words:
            fields["keywords[]"] = words
        return fields

    def transcribe(
        self,
        data: bytes,
        *,
        filename: str,
        mime: str,
        language: str = "es",
        prompt: str | None = None,
        keywords: Sequence[str] = (),
    ) -> TranscriptResult:
        fields = self.request_fields(language=language, prompt=prompt, keywords=keywords)
        timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as client:
                response = client.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=fields,
                    files={"file": (filename, data, mime)},
                )
        except httpx.TimeoutException:
            raise TranscriptionError("timeout", retryable=True) from None
        except httpx.TransportError:
            raise TranscriptionError("network", retryable=True) from None

        status = response.status_code
        if status == 429 or status >= 500:
            raise TranscriptionError(f"http_{status}", retryable=True)
        if status in CONFIG_STATUSES:
            raise TranscriptionError(f"http_{status}", config=True)
        if status in FILE_STATUSES:
            raise TranscriptionError("rejected")
        if status != 200:
            raise TranscriptionError(f"http_{status}", retryable=True)
        try:
            body = response.json()
        except ValueError:
            raise TranscriptionError("bad_response", retryable=True) from None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise TranscriptionError("bad_response", retryable=True)

        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        duration = None
        if usage.get("type") == "duration" and isinstance(usage.get("seconds"), int | float):
            duration = float(usage["seconds"])
        elif isinstance(body.get("duration"), int | float):
            duration = float(body["duration"])
        price = PRICE_PER_MINUTE_USD.get(self.model)
        cost = round(duration / 60 * price, 6) if duration is not None and price else None
        return TranscriptResult(
            text=text.strip(),
            model=self.model,
            duration_s=duration,
            usage={k: v for k, v in usage.items() if isinstance(v, int | float | str)},
            cost_usd=cost,
        )
