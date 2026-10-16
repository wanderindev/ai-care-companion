"""Claude call for one image or PDF: what kind of document is it.

One Messages API request per file content. A PDF goes as a base64
``document`` block (capped by ``LAB_PDF_MAX_MB`` like lab extraction), a photo
as one base64 ``image`` block (JPEG, PNG or WebP, at most
``labs.extract.IMAGE_MAX_BYTES`` = 5 MB, the API's per-image limit; WhatsApp
already compresses photos, so nothing is downscaled). The static Spanish
system prompt carries ``cache_control`` (it is short, so it may sit under the
model's minimum cacheable prefix: harmless), and a strict JSON schema
(``output_config.format``) shapes the answer::

    {"category": "lab_report" | "prescription" | "medical_document" | "other",
     "description": "≤ 200 characters, Spanish, no names or ID numbers",
     "patient_hint": "papa" | "mama" | "unknown"}

The model id comes from ``CLASSIFICATION_MODEL`` (default ``claude-haiku-5-5``:
a four-way decision and one line of text, with vision and PDF input, at a
fraction of the extraction model's price; a lab report is then read in full
by ``EXTRACTION_MODEL``), the key from ``ANTHROPIC_API_KEY``. Effort ``low``.
The server-side refusal fallback is added only for models that take it
(``bitacora_import.extract.FALLBACK_MODELS``).

The description must not identify anyone: the prompt asks for the document
type, specialty and counts ("Receta de dermatología con 2 medicamentos"),
never names, ID numbers, birth dates, phone numbers or addresses. A name that
slips through is still only stored in the database (like the file itself),
never logged. ``patient_hint`` comes only from what is visible (sex of the
patient on the document, "Sr."/"Sra."), never guessed.

Never log the file, the model output or anything in it: errors are short codes.
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from app.models.media_classification import CATEGORIES, DESCRIPTION_MAX, PATIENT_HINTS
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, Usage

MAX_TOKENS = 4000
EFFORT = "low"
REQUEST_TIMEOUT_SECONDS = 180.0
MAX_RETRIES = 1
PDF_MIME = "application/pdf"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "description", "patient_hint"],
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "description": {"type": "string"},
        "patient_hint": {"type": "string", "enum": list(PATIENT_HINTS)},
    },
}

SYSTEM_PROMPT = """\
Clasificas archivos (fotos o PDF) compartidos en el grupo de WhatsApp de una \
familia que cuida la salud de dos adultos mayores en Panamá: Papá (hombre) y \
Mamá (mujer). Recibes UN archivo y devuelves su clasificación en el esquema \
JSON indicado.

category:
- "lab_report": un informe de resultados de laboratorio clínico (sangre, \
orina, heces u otras muestras, con valores medidos), en foto o PDF. Si dudas \
entre "lab_report" y otra categoría, elige "lab_report": se revisará después.
- "prescription": una receta médica o indicación de medicamentos (con nombres \
de medicamentos, dosis o frecuencia), también la foto de una caja o frasco \
de medicamento.
- "medical_document": cualquier otro documento médico: órdenes de exámenes \
sin resultados, informes de imágenes (radiografías, ecografías, \
tomografías), epicrisis, referencias, certificados, citas, facturas o \
recibos médicos, carnés de seguro, notas de un médico.
- "other": todo lo demás (fotos de personas, lugares, comida, memes, \
stickers, capturas sin contenido médico).

description: una frase corta en español (máximo 200 caracteres) que diga qué \
es el documento: tipo, especialidad o examen, y cuántos elementos tiene \
("Receta de dermatología con 2 medicamentos", "Informe de laboratorio: \
hemograma y perfil lipídico", "Orden de ecografía abdominal", "Foto de una \
caja de medicamento"). NUNCA incluyas nombres de personas, números de cédula \
o de documento, fechas de nacimiento, teléfonos ni direcciones. Para \
"other", basta con algo genérico ("Foto sin contenido médico").

patient_hint: "papa" o "mama" solo si el documento muestra claramente que el \
paciente es un hombre o una mujer (sexo indicado, "Sr."/"Sra.", \
"paciente masculino"); si no, "unknown". No adivines."""

USER_TEXT_PDF = "Clasifica este PDF según el esquema."
USER_TEXT_IMAGE = "Clasifica esta foto según el esquema."


@dataclass
class Classification:
    data: dict[str, Any] | None = None  # {category, description, patient_hint}
    error: str | None = None  # short code, never document text
    retryable: bool = False  # transient: let the job's backoff try again
    config_error: bool = False  # key/model problem: fail the job, record nothing
    usage: Usage = field(default_factory=Usage)


def make_client(api_key: str):
    """The real Anthropic client (tests pass a fake with ``beta.messages.create``)."""
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
    )


def content_blocks(mime: str, data: bytes | bytearray) -> list[dict[str, Any]]:
    encoded = base64.b64encode(data).decode("ascii")
    if mime == PDF_MIME:
        return [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": PDF_MIME, "data": encoded},
            },
            {"type": "text", "text": USER_TEXT_PDF},
        ]
    return [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}},
        {"type": "text", "text": USER_TEXT_IMAGE},
    ]


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


# Same split as lab extraction: 400/413/422 will fail the same way again (the
# file is recorded); 401/403/404 are configuration problems (nothing recorded).
PERMANENT_FILE_STATUSES = frozenset({400, 413, 422})
CONFIG_STATUSES = frozenset({401, 403, 404})


def normalize(data: Any) -> dict[str, Any] | None:
    """The validated answer, or None when it does not match the schema."""
    if not isinstance(data, dict):
        return None
    category = data.get("category")
    description = data.get("description")
    hint = data.get("patient_hint")
    if category not in CATEGORIES or not isinstance(description, str):
        return None
    description = " ".join(description.split())[:DESCRIPTION_MAX]
    return {
        "category": category,
        "description": description or None,
        "patient_hint": hint if hint in PATIENT_HINTS else "unknown",
    }


def classify(client, model: str, mime: str, data: bytes | bytearray) -> Classification:
    import anthropic

    result = Classification()
    params = request_params(model, content_blocks(mime, data))
    try:
        response = client.beta.messages.create(**params)
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
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    result.data = normalize(parsed)
    if result.data is None:
        result.error = "invalid_json"
    return result
