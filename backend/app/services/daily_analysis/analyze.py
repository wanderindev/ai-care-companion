"""One Claude call: the record context + one day's messages → proposals.

Request (see ``app.prompts.daily_analysis_v2``): the static system prompt and
the context block carry ``cache_control`` (the context is the same for every
day of a job, so the second and later days read it from the cache); the
day's messages come last. The answer is shaped by the prompt's strict JSON
schema (``output_config.format``), effort ``medium``. The server-side refusal
fallback is added for the models that take it
(``bitacora_import.extract.FALLBACK_MODELS``).

Never log the request or the answer: errors are short codes.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from app.prompts import PromptSpec
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, Usage

MAX_TOKENS = 16000
EFFORT = "medium"
# Progress (which refreshes the job's ``locked_at``) is reported only before
# each call, so one call's worst case must stay under ``jobs.LOCK_TIMEOUT``
# (15 min) or stuck-job recovery would start a second copy of the job (a
# duplicate paid call): REQUEST_TIMEOUT_SECONDS x (1 + MAX_RETRIES) + the
# SDK's backoff (a few seconds) = about 10 min. A retryable failure after
# that re-queues the job, which skips the days already done
# (``test_request_worst_case_fits_job_lock`` in test_analyte_triage checks
# the bound).
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 1

# 400/413/422 fail the same way again; 401/403/404 are configuration problems.
PERMANENT_STATUSES = frozenset({400, 413, 422})
CONFIG_STATUSES = frozenset({401, 403, 404})


@dataclass
class Analysis:
    proposals: list[dict[str, Any]] | None = None
    error: str | None = None  # short code, never model text
    retryable: bool = False
    config_error: bool = False
    usage: Usage = field(default_factory=Usage)


def make_client(api_key: str):
    """The real Anthropic client (tests pass a fake with ``beta.messages.create``)."""
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
    )


def request_params(spec: PromptSpec, model: str, context: str, messages: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": spec.system, "cache_control": {"type": "ephemeral"}}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": spec.schema},
        },
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": messages},
                ],
            }
        ],
    }
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def parse_answer(text: str | None) -> list[dict[str, Any]] | None:
    """The list of proposals, or None when the answer is not the expected JSON
    object. Items that are not objects are left for the caller to count."""
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("suggestions"), list):
        return None
    return data["suggestions"]


def analyze(client, spec: PromptSpec, model: str, context: str, messages: str) -> Analysis:
    import anthropic

    result = Analysis()
    params = request_params(spec, model, context, messages)
    try:
        response = client.beta.messages.create(**params)
    except anthropic.APIStatusError as exc:
        result.error = f"api_error_{exc.status_code}"
        result.config_error = exc.status_code in CONFIG_STATUSES
        result.retryable = exc.status_code not in PERMANENT_STATUSES | CONFIG_STATUSES
        return result
    except anthropic.APIConnectionError:
        result.error = "api_connection_error"
        result.retryable = True
        return result
    result.usage.add(response.usage)
    if response.stop_reason == "refusal":
        result.error = "refusal"
        return result
    if response.stop_reason == "max_tokens":
        result.error = "truncated"
        return result
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    result.proposals = parse_answer(text)
    if result.proposals is None:
        result.error = "invalid_json"
    return result
