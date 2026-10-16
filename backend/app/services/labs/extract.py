"""Claude call for one document: is it a lab report, and if so, what does it say.

One Messages API request per document. A PDF goes as a base64 ``document``
block (PDF input: at most 32 MB per request and 600 pages, so the raw file is
capped by ``LAB_PDF_MAX_MB``, default 15 MB — base64 adds a third); photos of
a report's pages (manual upload, #15) go as base64 ``image`` blocks in page
order (JPEG, PNG or WebP; at most ``MAX_IMAGES`` pages of ``IMAGE_MAX_BYTES``
each, their total under the same ``LAB_PDF_MAX_MB`` cap; the browser already
downscales photos to 2560 px, inside the model's 2576 px long edge). The
static Spanish system prompt is cached, and a strict JSON schema
(``output_config.format``) for the answer. Streamed (``get_final_message``)
so a long report's output never hits the SDK's non-streaming time guard. The
model id comes from ``EXTRACTION_MODEL``, the key from ``ANTHROPIC_API_KEY``.

Mirrors ``app.services.bitacora_import.extract`` (fallback beta, usage and
cost accounting). Never log the PDF, the model output or anything in it:
errors are short codes.
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from app.models.lab import FLAGS, SPECIMENS
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, Usage

MAX_TOKENS = 32000
EFFORT = "medium"
# The job's lock goes stale after 15 minutes without progress
# (app.services.jobs.LOCK_TIMEOUT): two tries of at most 6 minutes each stay
# under it; anything longer is retried by the job's own backoff.
REQUEST_TIMEOUT_SECONDS = 360.0
MAX_RETRIES = 1
# Photos of one report: the API takes up to 5 MB per image; ten pages are
# plenty for a lab report and keep the request (base64) well under 32 MB.
IMAGE_MAX_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 10
IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "panel",
        "name",
        "value",
        "unit",
        "ref_low",
        "ref_high",
        "ref_text",
        "flag",
        "specimen",
    ],
    "properties": {
        "panel": _nullable(_STR),
        "name": _STR,
        "value": _STR,
        "unit": _nullable(_STR),
        "ref_low": _nullable(_STR),
        "ref_high": _nullable(_STR),
        "ref_text": _nullable(_STR),
        "flag": _nullable({"type": "string", "enum": list(FLAGS)}),
        "specimen": {"type": "string", "enum": list(SPECIMENS)},
    },
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_lab_report",
        "patient_name",
        "patient_id_number",
        "lab_name",
        "collected_on",
        "reported_on",
        "results",
    ],
    "properties": {
        "is_lab_report": {"type": "boolean"},
        "patient_name": _nullable(_STR),
        "patient_id_number": _nullable(_STR),
        "lab_name": _nullable(_STR),
        "collected_on": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
        "reported_on": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
        "results": {"type": "array", "items": RESULT_SCHEMA},
    },
}

SYSTEM_PROMPT = """\
Eres un asistente que lee documentos compartidos por la familia sobre la \
salud de dos adultos mayores en Panamá. Recibes UN documento —un PDF, o una \
o más fotos de sus páginas en orden— y devuelves sus datos en el esquema \
JSON indicado.

Primero decide is_lab_report: true solo si el documento es un informe de \
resultados de laboratorio clínico (análisis de sangre, orina, heces u otras \
muestras, con valores medidos). Recetas, facturas, órdenes de exámenes sin \
resultados, informes de imágenes (radiografías, ecografías, tomografías), \
epicrisis, referencias y cualquier otro documento → false, con los demás \
campos en null y results vacío.

Si es un informe de laboratorio:
- No inventes nada. Si un dato no aparece, usa null.
- patient_name: el nombre del paciente tal como aparece.
- patient_id_number: la cédula (CIP) o el documento de identidad del \
paciente tal como aparece; null si no hay.
- lab_name: el nombre del laboratorio o la clínica que emite el informe.
- collected_on: la fecha de toma de la muestra (YYYY-MM-DD); reported_on: la \
fecha del informe o de validación de los resultados (YYYY-MM-DD). Las fechas \
de Panamá se escriben día/mes/año.
- results: TODOS los resultados del informe, en el orden del documento, uno \
por línea de resultado:
  - panel: el título de la sección o perfil al que pertenece ("Hemograma", \
"Perfil lipídico", "Urinálisis"); null si no hay.
  - name: el nombre de la prueba tal como está escrito (sin el valor).
  - value: el valor tal como está escrito, incluidos signos como "<" o ">" \
("4.5", "4,5", "<0.01", "Negativo", "Trazas", "1+"). Sin la unidad.
  - unit: la unidad tal como está escrita; null si no hay.
  - ref_low / ref_high: los límites numéricos del rango de referencia tal \
como están escritos ("3.5", "5,0"); null el que no exista (p. ej. "< 200" \
tiene solo ref_high).
  - ref_text: el rango de referencia completo tal como está escrito \
("3.5 - 5.0", "< 200", "Negativo"); null si no hay.
  - flag: solo si el laboratorio marca el resultado: "alto" (H, ↑, Alto), \
"bajo" (L, ↓, Bajo), "anormal" (*, A, Anormal, Positivo marcado), "normal" \
(si lo marca explícitamente como normal). null si el informe no marca nada; \
no lo calcules tú.
  - specimen: la muestra: "sangre" (también suero y plasma), "orina", \
"heces" u "otro".
- No incluyas como resultados los encabezados, las notas, los métodos ni los \
datos del paciente."""

USER_TEXT = "Lee este PDF y devuelve sus datos según el esquema."
USER_TEXT_IMAGES = (
    "Estas fotos son las páginas de un mismo documento, en orden. Léelo y devuelve "
    "sus datos según el esquema."
)


@dataclass
class Extraction:
    data: dict[str, Any] | None = None
    error: str | None = None  # short code, never document text
    retryable: bool = False  # transient: let the job's backoff try again
    config_error: bool = False  # key/model problem: fail the job, record nothing
    usage: Usage = field(default_factory=Usage)


def make_client(api_key: str):
    """The real Anthropic client (tests pass a fake with ``beta.messages.stream``)."""
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
    )


def pdf_blocks(pdf: bytes | bytearray) -> list[dict[str, Any]]:
    return [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(pdf).decode("ascii"),
            },
        },
        {"type": "text", "text": USER_TEXT},
    ]


def image_blocks(images: list[tuple[str, bytes | bytearray]]) -> list[dict[str, Any]]:
    """``images``: ``(media_type, bytes)`` per page, in page order."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
        for media_type, data in images
    ]
    blocks.append({"type": "text", "text": USER_TEXT_IMAGES})
    return blocks


def request_params(model: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        "messages": [{"role": "user", "content": content}],
    }
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


# 400 (bad or unreadable PDF, too many pages) and 413 (too large) will fail the
# same way again; 401/403/404 are configuration problems (key, model): not the
# file's fault, so nothing is recorded for the file and the job fails.
PERMANENT_FILE_STATUSES = frozenset({400, 413, 422})
CONFIG_STATUSES = frozenset({401, 403, 404})


def extract_pdf(client, model: str, pdf: bytes | bytearray) -> Extraction:
    return extract_content(client, model, pdf_blocks(pdf))


def extract_images(client, model: str, images: list[tuple[str, bytes | bytearray]]) -> Extraction:
    return extract_content(client, model, image_blocks(images))


def extract_content(client, model: str, content: list[dict[str, Any]]) -> Extraction:
    import anthropic

    result = Extraction()
    params = request_params(model, content)
    try:
        with client.beta.messages.stream(**params) as stream:
            response = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        result.error = f"api_error_{exc.status_code}"
        result.config_error = exc.status_code in CONFIG_STATUSES
        result.retryable = exc.status_code not in PERMANENT_FILE_STATUSES | CONFIG_STATUSES
        return result
    except anthropic.APIConnectionError:
        result.error = "api_connection_error"
        result.retryable = True
        return result
    finally:
        del params
    result.usage.add(response.usage)
    if response.stop_reason == "refusal":
        result.error = "refusal"
        return result
    if response.stop_reason == "max_tokens":
        result.error = "truncated"
        return result
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("is_lab_report"), bool):
        result.error = "invalid_json"
        return result
    result.data = data
    return result
