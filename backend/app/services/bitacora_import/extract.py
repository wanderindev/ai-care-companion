"""Claude structured extraction of one already-split bitácora entry.

One Messages API call per entry with a strict JSON schema
(``output_config.format``), the static Spanish system prompt cached
(``cache_control``), bounded concurrency and the SDK's retries with
exponential backoff on 429/5xx/connection errors. The model id and the API key
come from settings (``EXTRACTION_MODEL``, ``ANTHROPIC_API_KEY``).

The model only *structures* the text: the entry date always comes from the
splitter's date line, and an explicit channel suffix on the header line
("- WhatsApp") wins over the model. ``normalize`` enforces the backend's
enumerations and column lengths.

Never log the entry text or the model output: errors carry only a short code.
"""

import json
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from app.models.log_entry import CHANNELS, ENTRY_TYPES
from app.services.bitacora_import.docx_parse import HeaderHint, RawEntry

DEFAULT_CONCURRENCY = 4
MAX_RETRIES = 6  # SDK retries 408/409/429/5xx and connection errors with backoff
MAX_TOKENS = 16000
EFFORT = "medium"
MED_EVENT_KINDS = ("start", "stop", "change", "substitution")
# Server-side refusal fallback (routes a declined request to another model).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset(
    {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
)
# USD per million tokens: input, output, cache write (5 min), cache read.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}

LIMITS = {"source_label": 200, "title": 300}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "entry_date",
        "doctor",
        "source_label",
        "entry_type",
        "channel",
        "title",
        "indications",
        "family_notes",
        "next_appointment",
        "medication_events",
    ],
    "properties": {
        "entry_date": {"type": "string", "description": "YYYY-MM-DD"},
        "doctor": _nullable(_STR),
        "source_label": _nullable(_STR),
        "entry_type": {"type": "string", "enum": list(ENTRY_TYPES)},
        "channel": _nullable({"type": "string", "enum": list(CHANNELS)}),
        "title": _nullable(_STR),
        "indications": _nullable(_STR),
        "family_notes": _nullable(_STR),
        "next_appointment": _nullable(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "time", "doctor", "text"],
                "properties": {
                    "date": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
                    "time": _nullable({"type": "string", "description": "HH:MM, 24 h"}),
                    "doctor": _nullable(_STR),
                    "text": {"type": "string"},
                },
            }
        ),
        "medication_events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "medication", "detail"],
                "properties": {
                    "kind": {"type": "string", "enum": list(MED_EVENT_KINDS)},
                    "medication": {"type": "string"},
                    "detail": _nullable(_STR),
                },
            },
        },
    },
}

SYSTEM_PROMPT = """\
Eres un asistente que estructura entradas de la bitácora médica de una familia \
panameña. La bitácora la escriben los hijos sobre la salud de uno de sus padres, \
un adulto mayor. Recibes UNA entrada ya separada del documento original (en \
markdown: **negrita** y <u>subrayado</u> son el énfasis del original), con su \
fecha, y devuelves sus campos en el esquema JSON indicado.

Reglas generales:
- No inventes nada. Si un dato no está en el texto, usa null (o una lista vacía).
- Escribe en español, como en el original. No agregues interpretaciones médicas.
- indications y family_notes van en markdown: conserva la **negrita** del \
original, quita los <u></u>, y conserva los párrafos (separados por una línea \
en blanco), los saltos de línea y las viñetas del original.
- Los demás campos de texto (doctor, source_label, title, next_appointment, \
medication_events) van en texto plano, sin ** ni <u></u>.

Campos:
- entry_date: la fecha de la entrada (YYYY-MM-DD). Es la de la línea de fecha \
que te doy; no la cambies.
- doctor: nombre del médico o profesional que atendió o indicó, tal como está \
escrito, con su título ("Dra. Ana Pérez", "Dr. Ruiz"), sin el canal \
("- WhatsApp", "- correo"). null si la entrada no tiene médico (por ejemplo una \
terapia sin nombre del terapeuta, una institución o una observación de la familia).
- source_label: la fuente tal como aparece en el encabezado de la entrada \
(normalmente la primera línea), incluyendo el canal si lo trae: \
"Dr. Y - WhatsApp", "Terapia respiratoria", "FisioCentro - correo". null si la \
entrada empieza directamente con el título o el texto.
- entry_type: uno de
  consulta_control (control o cita de seguimiento), consulta_enfermedad \
(consulta por un problema agudo), visita_domiciliaria (el médico va a la casa), \
teleconsulta (consulta a distancia: WhatsApp, llamada, correo, video), terapia \
(física, respiratoria, ocupacional, de lenguaje…), vacuna, procedimiento \
(curaciones, aplicaciones, cirugías menores…), hospitalizacion (ingreso, \
urgencias con estadía, alta), examen (laboratorios, imágenes, estudios), \
observacion_familiar (notas de la familia sin profesional: síntomas observados, \
dudas, lo que se leyó en internet), otro.
- channel: presencial, domicilio, whatsapp, telefono, correo, o null si no se sabe. \
Un sufijo en el encabezado ("- WhatsApp", "- correo") indica el canal.
- title: un título breve (máximo unas 8 palabras) del motivo o tema principal. Si \
la entrada empieza con un título propio (p. ej. "**Gripe**. Se aplica…"), úsalo \
tal cual. Si no hay un tema claro, null.
- indications: lo que dijo, encontró o indicó el profesional, con sus palabras: \
diagnósticos, hallazgos, resultados, tratamientos, dosis, recomendaciones. Copia \
el texto del original; no lo resumas ni lo reescribas. Incluye la frase de \
"próxima cita" si aparece.
- family_notes: comentarios que claramente son de la familia y no del \
profesional: observaciones propias, dudas, lo que "internet recomienda", \
recordatorios entre hermanos. Copia el texto del original. null si no hay.
  En una observacion_familiar sin profesional, el texto va en family_notes.
- next_appointment: si el texto menciona la próxima cita ("Próxima cita…", \
"Próxima Cita 1400 horas Mar 13 ABR 27", "2:00 PM 18 MAY 2022", \
"1100 JUE 24 SEP 26"): date (YYYY-MM-DD), time (HH:MM en 24 h: "1400 horas" → \
14:00, "2:00 PM" → 14:00, "1100" → 11:00; null si no hay hora), doctor (con quién, \
null si no se dice; si no se dice y la entrada es de un médico, ese médico) y text \
(la frase tal cual). Años de dos dígitos son 20xx. Si falta el año, es la \
primera fecha posterior a la de la entrada. Si la fecha no se puede determinar, \
date null y conserva text. null si no hay próxima cita.
- medication_events: cada medicamento que se inicia (start), suspende (stop), \
cambia de dosis/horario/presentación (change) o se sustituye por otro \
(substitution), según el texto. medication: el nombre tal como está escrito; \
detail: dosis, horario, duración o motivo en pocas palabras (null si no hay). \
"Continuar X" sin cambios no es un evento. Lista vacía si no hay.

La "pista del encabezado" que acompaña a la entrada la calcula un programa a \
partir de la primera línea; úsala como guía, no como verdad."""


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, usage: Any) -> None:
        with self._lock:
            self.requests += 1
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
            self.cache_creation_input_tokens += (
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )
            self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "requests": self.requests,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
            }

    def cost_usd(self, model: str) -> float | None:
        prices = PRICES.get(model)
        if prices is None:
            return None
        p_in, p_out, p_write, p_read = prices
        return (
            self.input_tokens * p_in
            + self.output_tokens * p_out
            + self.cache_creation_input_tokens * p_write
            + self.cache_read_input_tokens * p_read
        ) / 1_000_000


@dataclass
class Extraction:
    entry_index: int
    data: dict[str, Any] | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def make_client(api_key: str):
    """The real Anthropic client (tests pass a fake with ``beta.messages.create``)."""
    import anthropic

    return anthropic.Anthropic(api_key=api_key, max_retries=MAX_RETRIES)


def user_message(raw: RawEntry, hint: HeaderHint) -> str:
    return (
        f"Línea de fecha: {raw.date_line} (entry_date = {raw.entry_date.isoformat()})\n"
        f"Pista del encabezado: {json.dumps(hint.as_dict(), ensure_ascii=False)}\n\n"
        f"Entrada:\n<entrada>\n{raw.markdown}\n</entrada>"
    )


def request_params(model: str, raw: RawEntry, hint: HeaderHint) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        "messages": [{"role": "user", "content": user_message(raw, hint)}],
    }
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


_UNDERLINE = re.compile(r"</?u>", re.IGNORECASE)
_BOLD = re.compile(r"\*\*")


def _markdown(value: Any) -> str | None:
    """Markdown field: bold kept, underline tags removed."""
    return _text(_UNDERLINE.sub("", value) if isinstance(value, str) else value)


def _plain(value: Any) -> Any:
    """Plain field: underline tags and bold markers removed."""
    if not isinstance(value, str):
        return value
    return _BOLD.sub("", _UNDERLINE.sub("", value))


def _text(value: Any, limit: int | None = None, warnings: list[str] | None = None, name=""):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if limit and len(value) > limit:
        if warnings is not None:
            warnings.append(f"{name}_truncated")
        value = value[: limit - 1].rstrip() + "…"
    return value


_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


def _next_appointment(value: Any, warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    appt_date = _text(value.get("date"))
    if appt_date is not None:
        try:
            appt_date = date.fromisoformat(appt_date).isoformat()
        except ValueError:
            warnings.append("next_appointment_bad_date")
            appt_date = None
    appt_time = _text(value.get("time"))
    if appt_time is not None:
        m = _TIME.match(appt_time)
        try:
            appt_time = time(int(m[1]), int(m[2])).strftime("%H:%M") if m else None
        except ValueError:
            appt_time = None
        if appt_time is None:
            warnings.append("next_appointment_bad_time")
    text = _text(value.get("text"))
    if appt_date is None and text is None:
        return None
    return {
        "date": appt_date,
        "time": appt_time,
        "doctor": _text(value.get("doctor")),
        "text": text,
    }


def _med_events(value: Any) -> list[dict[str, Any]]:
    events = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or item.get("kind") not in MED_EVENT_KINDS:
            continue
        medication = _text(item.get("medication"))
        if medication:
            events.append(
                {
                    "kind": item["kind"],
                    "medication": medication,
                    "detail": _text(item.get("detail")),
                }
            )
    return events


def normalize(data: dict[str, Any], raw: RawEntry, hint: HeaderHint) -> tuple[dict, list[str]]:
    """Clamp the model output to the backend's rules. The splitter's date and an
    explicit header channel win over the model."""
    warnings: list[str] = []
    if data.get("entry_date") != raw.entry_date.isoformat():
        warnings.append("model_date_differs")
    entry_type = data.get("entry_type")
    if entry_type not in ENTRY_TYPES:
        warnings.append("entry_type_unknown")
        entry_type = "otro"
    channel = data.get("channel")
    if channel not in CHANNELS:
        channel = None
    if hint.channel and channel != hint.channel:
        if channel is not None:
            warnings.append("channel_from_header")
        channel = hint.channel
    out = {
        "entry_date": raw.entry_date.isoformat(),
        "doctor": _text(_plain(data.get("doctor")), 200, warnings, "doctor"),
        "source_label": _text(
            _plain(data.get("source_label")), LIMITS["source_label"], warnings, "source_label"
        ),
        "entry_type": entry_type,
        "channel": channel,
        "title": _text(_plain(data.get("title")), LIMITS["title"], warnings, "title"),
        "indications": _markdown(data.get("indications")),
        "family_notes": _markdown(data.get("family_notes")),
        "next_appointment": _next_appointment(data.get("next_appointment"), warnings),
        "medication_events": _med_events(data.get("medication_events")),
    }
    if not (out["doctor"] or out["source_label"] or out["title"]):
        # Every card needs a header (log_entries.validate): fall back to the
        # first line as written.
        out["source_label"] = _text(hint.header, LIMITS["source_label"])
        if out["source_label"] is None:
            warnings.append("no_header")
    return out, warnings


def extract_entry(client, model: str, raw: RawEntry, hint: HeaderHint, usage: Usage) -> Extraction:
    import anthropic

    result = Extraction(entry_index=raw.index)
    if not raw.lines:
        result.error = "empty_entry"
        return result
    try:
        response = client.beta.messages.create(**request_params(model, raw, hint))
    except anthropic.APIStatusError as exc:
        result.error = f"api_error_{exc.status_code}"
        return result
    except anthropic.APIConnectionError:
        result.error = "api_connection_error"
        return result
    usage.add(response.usage)
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
    if not isinstance(data, dict):
        result.error = "invalid_json"
        return result
    result.data, result.warnings = normalize(data, raw, hint)
    return result


def extract_all(
    client,
    model: str,
    entries: Sequence[tuple[RawEntry, HeaderHint]],
    *,
    usage: Usage,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_result: Callable[[dict[int, Extraction]], None] | None = None,
) -> dict[int, Extraction]:
    """Extract every entry with at most ``concurrency`` requests in flight.

    ``on_result`` is called (serialized) with the results so far after each
    entry, e.g. to save a partial staging file."""
    results: dict[int, Extraction] = {}
    lock = threading.Lock()

    def run(item: tuple[RawEntry, HeaderHint]) -> None:
        raw, hint = item
        try:
            extraction = extract_entry(client, model, raw, hint, usage)
        except Exception as exc:  # one bad entry must not stop the batch
            extraction = Extraction(entry_index=raw.index, error=type(exc).__name__)
        with lock:
            results[raw.index] = extraction
            if on_result:
                on_result(results)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(run, entries))
    return results
