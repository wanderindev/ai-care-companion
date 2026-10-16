"""Claude's second opinion on whose lab report it is (#98, DESIGN §6.5).

Called by ``matching.identify_patient`` only when the deterministic rules
(CIP, then name, then tolerant name variants) give no single patient and the
report's ID number does not decide it. The request carries **only** the name
and ID number printed on the report and the patients' slugs, display names
and ``full_name`` — never results, dates, the lab or the file. A strict JSON
schema (``output_config.format``) shapes the answer::

    {"patient": "papa" | "mama" | null,
     "confidence": "high" | "medium" | "low",
     "reason_code": one of REASON_CODES}

Only ``high`` assigns a patient (a wrong attribution is worse than none): the
caller treats anything else as "unassigned, needs review".

Model ``CLASSIFICATION_MODEL`` (default ``claude-haiku-5-5``), effort ``low``,
key ``ANTHROPIC_API_KEY``. Never log the names, the ID or the answer's
content: errors are short codes, logs carry ids, outcome codes and token
counts only.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.services.bitacora_import.extract import FALLBACK_BETA, FALLBACK_MODELS, Usage

MAX_TOKENS = 1000
EFFORT = "low"
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2
CONFIDENCES = ("high", "medium", "low")
REASON_CODES = (
    "same_name",
    "word_order",
    "missing_parts",
    "spelling_variant",
    "initials",
    "different_person",
    "ambiguous",
    "unreadable",
)

SYSTEM_PROMPT = """\
Decides a quién pertenece un informe de laboratorio clínico de una familia en \
Panamá. Recibes solo el nombre y el número de identificación impresos en el \
informe y el nombre completo (como en la cédula) de cada paciente posible. \
Responde en el esquema JSON indicado.

patient: el identificador ("papa" o "mama") del paciente que es, con \
seguridad, la persona del informe; null si no lo es ninguno o si dudas.

Variantes aceptables del mismo nombre: otro orden ("APELLIDO, NOMBRE"), \
mayúsculas, tildes, un segundo nombre o un segundo apellido omitido, la parte \
de casada ("de ...") omitida o abreviada, iniciales en lugar de un nombre, \
errores de tipeo menores.

No es la misma persona (patient null): un nombre de pila distinto, palabras \
que el nombre del paciente no tiene (un hijo con el mismo nombre del padre, \
"Jr.", otro apellido), o un nombre que encaja igual con más de un paciente \
(solo el apellido que comparten). El sexo o un tratamiento ("Sr.", "Sra.") \
no bastan por sí solos.

En Panamá un hijo o una hija de la pareja se llama "<nombre> <primer \
apellido del padre> <primer apellido de la madre>", y el nombre de casada \
de la madre contiene esos mismos apellidos en otro orden ("<nombre> \
<apellido de ella> ... de <apellido de él>"). Un nombre con el apellido del \
padre seguido del de la madre ("Ana Pérez López" frente a "Ana María López \
Díaz de Pérez") es de un hijo o hija de la pareja, NO de la madre: patient \
null. Un nombre parcial o con apellidos compartidos que podría ser de un \
hijo o una hija nunca tiene confidence "high". Los hijos pueden llevar los \
mismos nombres de pila que sus padres, y llevan el primer apellido del padre \
y el primer apellido de la madre. Solo distinguen al paciente de sus hijos su \
propio segundo apellido o, para la madre, "de <apellido del esposo>" con el \
"de" impreso. Un nombre sin ninguno de esos apellidos ("Juan Carlos Pérez", \
"Ana María Pérez", "Ana López") podría ser de un hijo o una hija y nunca \
tiene confidence "high".

confidence: "high" solo si no hay duda razonable; "medium" o "low" en otro \
caso. Una atribución equivocada es peor que ninguna.

reason_code: el motivo principal (same_name, word_order, missing_parts, \
spelling_variant, initials, different_person, ambiguous, unreadable)."""


@dataclass
class Answer:
    patient: str | None = None  # slug
    confidence: str | None = None
    reason_code: str | None = None
    error: str | None = None  # short code, never text from the report
    usage: Usage = field(default_factory=Usage)


def schema(slugs: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["patient", "confidence", "reason_code"],
        "properties": {
            "patient": {"anyOf": [{"type": "string", "enum": list(slugs)}, {"type": "null"}]},
            "confidence": {"type": "string", "enum": list(CONFIDENCES)},
            "reason_code": {"type": "string", "enum": list(REASON_CODES)},
        },
    }


def user_text(name: str | None, id_number: str | None, patients: Sequence[Any]) -> str:
    lines = [
        f"Nombre impreso en el informe: {name or '(ninguno)'}",
        f"Identificación impresa en el informe: {id_number or '(ninguna)'}",
        "Pacientes posibles:",
    ]
    lines += [f'- "{p.slug}" ({p.display_name}): {p.full_name}' for p in patients]
    return "\n".join(lines)


def request_params(
    model: str, name: str | None, id_number: str | None, patients: Sequence[Any]
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": schema([p.slug for p in patients])},
        },
        "messages": [{"role": "user", "content": user_text(name, id_number, patients)}],
    }
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def make_client(api_key: str):
    """The real Anthropic client (tests patch this with a fake whose
    ``beta.messages.create`` answers)."""
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
    )


def ask(
    client, model: str, name: str | None, id_number: str | None, patients: Sequence[Any]
) -> Answer:
    import anthropic

    answer = Answer()
    try:
        response = client.beta.messages.create(**request_params(model, name, id_number, patients))
    except anthropic.APIStatusError as exc:
        answer.error = f"api_error_{exc.status_code}"
        return answer
    except anthropic.APIConnectionError:
        answer.error = "api_connection_error"
        return answer
    answer.usage.add(response.usage)
    if response.stop_reason in ("refusal", "max_tokens"):
        answer.error = "refusal" if response.stop_reason == "refusal" else "truncated"
        return answer
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = None
    slugs = {p.slug for p in patients}
    if (
        not isinstance(data, dict)
        or data.get("confidence") not in CONFIDENCES
        or (data.get("patient") is not None and data.get("patient") not in slugs)
    ):
        answer.error = "invalid_json"
        return answer
    answer.patient = data["patient"]
    answer.confidence = data["confidence"]
    reason = data.get("reason_code")
    answer.reason_code = reason if reason in REASON_CODES else None
    return answer


class Asker:
    """``ask`` bound to the settings; the client is created on first use, so a
    run that never needs Claude never builds one. ``usage`` adds up every call."""

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client
        self.usage = Usage()

    @property
    def available(self) -> bool:
        return self.client is not None or bool(self.settings.anthropic_api_key)

    def __call__(self, name: str | None, id_number: str | None, patients: Sequence[Any]) -> Answer:
        if self.client is None:
            self.client = make_client(self.settings.anthropic_api_key)
        answer = ask(self.client, self.settings.classification_model, name, id_number, patients)
        if answer.usage.requests:
            self.usage.add(answer.usage)  # same attribute names as an API usage object
        return answer
