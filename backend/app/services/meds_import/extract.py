"""Claude structured extraction of one medication list (one call per document).

Same request shape as the bitácora importer (strict JSON schema in
``output_config.format``, cached Spanish system prompt, server-side refusal
fallback, the SDK's retries; ``EXTRACTION_MODEL`` / ``ANTHROPIC_API_KEY``).

The model only *structures* the lines. Decisions that can be made in code are
made here, not by the model:

- the **slot** of each item comes from the *position*: the model returns the
  number of the section marker (``[S3]``) the item was under, and the slot
  is that section's (mapped in code by :func:`docx_parse.classify_header`).
  The header text the model also reports is only cross-checked
  (:mod:`review`). An item from the EVITAR section or from no section gets
  **no slot** (``slot: null``): the load refuses it until the operator
  decides. An item marked as rescue goes to ``segun_necesidad``; a
  clock-time section ("ENTRE 7:30 y 8:00 PM") gives the item's ``time``
  (unless the line has its own) and is prefixed to its instructions, and so
  is a qualified header's qualifier ("DESPUÉS DEL DESAYUNO");
- the list's date is the parser's "DESDE <fecha>", never the model's;
- weekdays, dates and times are validated (``normalize``). Text is **never
  truncated**: a value over its column limit gets a ``<field>_too_long``
  warning and the load's validation refuses that item.

Never log the list text or the model output: errors carry only a short code,
warnings only an item number and a code.
"""

import json
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from app.services.bitacora_import.extract import (
    EFFORT,
    FALLBACK_BETA,
    FALLBACK_MODELS,
    MAX_TOKENS,
    Usage,
    make_client,
)
from app.services.meds_import.docx_parse import AVOID, ParsedList

__all__ = ["SCHEMA", "Extraction", "Usage", "extract_document", "make_client", "normalize"]

# Column limits (app.schemas.meds.MedItemIn / MedAvoidIn).
LIMITS = {
    "name": 200,
    "strength": 100,
    "quantity": 50,
    "dose_note": 200,
    "schedule_note": 10000,
    "instructions": 10000,
    "reason": 300,
    "substance": 200,
    "avoid_reason": 2000,
}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}
_ITEM_PROPS: dict[str, Any] = {
    "section": _nullable({"type": "integer", "description": "N of the [SN] marker"}),
    "header": _nullable(_STR),
    "rescue": {"type": "boolean"},
    "name": _STR,
    "strength": _nullable(_STR),
    "quantity": _nullable(_STR),
    "dose_note": _nullable(_STR),
    "time": _nullable({"type": "string", "description": "HH:MM, 24 h"}),
    "days": _nullable({"type": "array", "items": {"type": "integer"}}),
    "schedule_note": _nullable(_STR),
    "instructions": _nullable(_STR),
    "prescriber": _nullable(_STR),
    "reason": _nullable(_STR),
    "start_date": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
    "end_date": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "avoid", "list_note"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_ITEM_PROPS),
                "properties": _ITEM_PROPS,
            },
        },
        "avoid": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["substance", "reason"],
                "properties": {"substance": _STR, "reason": _nullable(_STR)},
            },
        },
        "list_note": _nullable(_STR),
    },
}

SYSTEM_PROMPT = """\
Eres un asistente que estructura la lista de medicamentos de un adulto mayor, \
tal como la familia la tiene en un documento de Word. Recibes el documento en \
markdown (**negrita** y <u>subrayado</u> son el énfasis del original; las tablas \
van como tablas markdown), organizado en secciones por horario (AL DESPERTAR, \
DESAYUNO, MAÑANA, ALMUERZO, TARDE, CENA, NOCHE, ENTRE 7:30 y 8:00 PM, \
OTROS MEDICAMENTOS Y SUPLEMENTOS…) y un bloque EVITAR. Devuelves la lista en \
el esquema JSON indicado.

Reglas generales:
- No inventes nada. Si un dato no está, usa null (o una lista vacía).
- Copia los textos como están escritos (en español), en texto plano, sin ** ni \
<u></u>. No agregues interpretaciones médicas.
- Conserva el orden del documento.

Cada encabezado de sección lleva delante una marca [S1], [S2]… que pone el \
programa.

items: un elemento por cada medicamento o suplemento de cada sección (si el \
mismo medicamento aparece en dos secciones, son dos elementos). No omitas \
ninguno.
- section: el número N de la marca [SN] de la sección bajo la que aparece \
(el encabezado más cercano por encima). null si no está bajo ninguno.
- header: el texto de ese encabezado tal como está escrito, sin la marca \
("DESAYUNO", "ENTRE 7:30 y 8:00 PM").
- rescue: true solo si el texto lo marca como rescate o para tomar según \
necesidad ("Rescate", "si hay dolor", "SOS", "en caso de…"); si no, false.
- name: el nombre del medicamento como está escrito, sin la concentración si \
esta se puede separar ("Medicamento A", "Vitamina D 5,000 UI" se queda entero \
si así se escribe).
- strength: la concentración ("250 mg", "5 mg/ml"). null si no hay.
- quantity: cuánto se toma, tal cual: "1", "½", "¼", "2", "1 ½", \
"3 cápsulas", "10 gotas", "8.5–17 g". Conserva las fracciones como están \
(½, ¼, 1/2).
- dose_note: una aclaración de la dosis, p. ej. "(125 mg total)". null si no hay.
- time: hora exacta si la línea la da ("6:00 AM" → "06:00", "8 PM" → "20:00"). \
null si no (el horario de la sección lo pone el programa).
- days: si se toma solo ciertos días de la semana, la lista de días ISO \
(1 = lunes, 2 = martes, 3 = miércoles, 4 = jueves, 5 = viernes, 6 = sábado, \
7 = domingo): "lunes, miércoles y viernes" → [1, 3, 5]; "fines de semana" → \
[6, 7]. null si es todos los días o si no son días de la semana fijos.
- schedule_note: una pauta que no son días fijos: listas de fechas de una \
reducción o aumento progresivo ("vie 25 sep: 1; lun 28 sep: ½; …"), "cada \
15 días", "por 10 días", "días alternos". Cópiala completa. null si no hay.
- instructions: indicaciones de cómo tomarlo ("30 min antes del desayuno", \
"con la cena", "disuelto en agua"). null si no hay.
- prescriber: el médico que lo indicó si la línea lo nombra ("Dra. Ana"). null \
si no.
- reason: para qué es, si la línea lo dice ("para la presión"). null si no.
- start_date / end_date: YYYY-MM-DD si la línea dice desde / hasta cuándo \
("desde el 5 oct", "hasta el 30 nov"; también una línea "DESDE <fecha>" \
dentro de una sección, junto al medicamento al que se refiere). Sin año: el año que corresponda a la \
fecha de la lista que te doy. null si no.

avoid: cada sustancia o medicamento del bloque EVITAR (o marcado "evitar" / \
"no tomar" en otra parte): substance (como está escrito) y reason (el motivo \
si se da, si no null). Nunca pongas lo de EVITAR en items.

list_note: lo que es de toda la lista y no de un medicamento, en markdown: \
sobre todo una tabla de régimen (p. ej. el horario de un solo medicamento por \
hora), copiada como tabla markdown, y notas generales. Si una tabla es la \
propia lista de medicamentos (una fila por medicamento), sus filas son items, \
no list_note. null si no hay nada.

Los "encabezados detectados" los calcula un programa; úsalos como guía."""


@dataclass
class Extraction:
    data: dict[str, Any] | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def user_message(parsed: ParsedList) -> str:
    since = parsed.effective_date.isoformat() if parsed.effective_date else "desconocida"
    headers = ", ".join(
        f"[S{n}] {json.dumps(s.header, ensure_ascii=False)}"
        for n, s in enumerate(parsed.sections, 1)
    )
    return (
        f"Fecha de la lista (DESDE): {since}\n"
        f"Encabezados detectados, en orden: {headers or 'ninguno'}\n\n"
        f"Lista:\n<lista>\n{parsed.markdown}\n</lista>"
    )


def request_params(model: str, parsed: ParsedList) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        "messages": [{"role": "user", "content": user_message(parsed)}],
    }
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


# ── Normalization ────────────────────────────────────────────────────────────


def _text(value: Any, limit: int | None, warnings: list[str], name: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.replace("**", "").replace("<u>", "").replace("</u>", "").strip()
    if not value:
        return None
    if limit and len(value) > limit:
        warnings.append(f"{name}_too_long")  # never cut: the load refuses it
    return value


def _iso_date(value: Any, warnings: list[str], name: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        warnings.append(f"bad_{name}")
        return None


def _hhmm(value: Any, warnings: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError:
        warnings.append("bad_time")
        return None
    return parsed.strftime("%H:%M")


def _days(value: Any, warnings: list[str]) -> list[int] | None:
    if not value:
        return None
    if not isinstance(value, list) or any(
        not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 7 for d in value
    ):
        warnings.append("bad_days")
        return None
    days = sorted(set(value))
    return None if len(days) == 7 else days


def _item(raw: dict[str, Any], parsed: ParsedList, warnings: list[str]) -> dict[str, Any]:
    """One normalized item; ``slot`` is None when it needs the operator's review."""
    header = _text(raw.get("header"), None, warnings, "header")
    number = raw.get("section")
    if isinstance(number, bool) or not isinstance(number, int):
        number = None
    if number is not None and not 1 <= number <= len(parsed.sections):
        warnings.append("bad_section")
        number = None
    section = parsed.sections[number - 1] if number else None
    rescue = raw.get("rescue") is True
    if section is None or section.slot == AVOID:
        slot = None  # from no section, or from EVITAR: never an active medication silently
    elif rescue:
        slot = "segun_necesidad"
    else:
        slot = section.slot
    item_time = _hhmm(raw.get("time"), warnings)
    instructions = _text(raw.get("instructions"), None, warnings, "instructions")
    if section is not None and slot not in (None, "segun_necesidad"):
        if section.time is not None:
            item_time = item_time or section.time.strftime("%H:%M")
        # The header's qualifier ("Después del desayuno") and time window go
        # into the instructions, the window first.
        for prefix in (section.note, section.window if section.time is not None else None):
            if prefix and prefix.casefold() not in (instructions or "").casefold():
                instructions = f"{prefix}; {instructions}" if instructions else prefix
    out = {
        "section": number,
        "header": header,
        "rescue": rescue,
        "slot": slot,
        "name": _text(raw.get("name"), LIMITS["name"], warnings, "name"),
        "strength": _text(raw.get("strength"), LIMITS["strength"], warnings, "strength"),
        "quantity": _text(raw.get("quantity"), LIMITS["quantity"], warnings, "quantity"),
        "dose_note": _text(raw.get("dose_note"), LIMITS["dose_note"], warnings, "dose_note"),
        "time": item_time,
        "days": _days(raw.get("days"), warnings),
        "schedule_note": _text(
            raw.get("schedule_note"), LIMITS["schedule_note"], warnings, "schedule_note"
        ),
        "instructions": _text(instructions, LIMITS["instructions"], warnings, "instructions"),
        "prescriber": _text(raw.get("prescriber"), None, warnings, "prescriber"),
        "reason": _text(raw.get("reason"), LIMITS["reason"], warnings, "reason"),
        "start_date": _iso_date(raw.get("start_date"), warnings, "start_date"),
        "end_date": _iso_date(raw.get("end_date"), warnings, "end_date"),
    }
    if out["start_date"] and out["end_date"] and out["end_date"] < out["start_date"]:
        warnings.append("end_before_start")  # kept: the load's validation refuses it
    return out


def _avoid(raw: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    substance = _text(raw.get("substance"), LIMITS["substance"], warnings, "substance")
    if substance is None:
        return None
    return {
        "substance": substance,
        "reason": _text(raw.get("reason"), LIMITS["avoid_reason"], warnings, "reason"),
    }


def normalize(data: dict[str, Any], parsed: ParsedList) -> tuple[dict[str, Any], list[str]]:
    """The model output checked against the backend's rules, plus warnings of
    the form ``"item 3: bad_days"`` (1-based item numbers, no content). The
    section/header/completeness checks are :func:`review.review_list`."""
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    avoid: list[dict[str, Any]] = []
    raw_items = data.get("items") if isinstance(data.get("items"), list) else []
    for number, raw in enumerate(raw_items, 1):
        if not isinstance(raw, dict):
            warnings.append(f"model item {number}: not_an_object")
            continue
        item_warnings: list[str] = []
        item = _item(raw, parsed, item_warnings)
        if item["name"] is None:
            warnings.append(f"model item {number}: no_name (dropped)")
            continue
        item["warnings"] = item_warnings
        items.append(item)
        warnings.extend(f"item {len(items)}: {w}" for w in item_warnings)
    raw_avoid = data.get("avoid") if isinstance(data.get("avoid"), list) else []
    for number, raw in enumerate(raw_avoid, 1):
        row_warnings: list[str] = []
        row = _avoid(raw, row_warnings) if isinstance(raw, dict) else None
        if row is None:
            warnings.append(f"avoid {number}: no_substance (dropped)")
            continue
        avoid.append(row)
        warnings.extend(f"avoid {len(avoid)}: {w}" for w in row_warnings)
    list_note = data.get("list_note")
    list_note = list_note.strip() or None if isinstance(list_note, str) else None
    return (
        {
            "effective_date": parsed.effective_date.isoformat() if parsed.effective_date else None,
            "list_note": list_note,
            "items": items,
            "avoid": avoid,
        },
        warnings,
    )


def extract_document(client, model: str, parsed: ParsedList, usage: Usage) -> Extraction:
    import anthropic

    result = Extraction()
    try:
        response = client.beta.messages.create(**request_params(model, parsed))
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
    result.data, result.warnings = normalize(data, parsed)
    return result
