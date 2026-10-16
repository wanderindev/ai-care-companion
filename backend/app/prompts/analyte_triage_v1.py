"""Analyte triage, version 1 (ticket #100; ``app.services.labs.triage``).

One call per batch of analytes flagged ``needs_review``. The request is::

    system   = SYSTEM_PROMPT                         (static, cached)
    user[0]  = the reviewed analyte catalogue        (same for every batch: cached)
    user[1]  = the batch to triage

Each analyte is a line with its id, name, aliases, specimen, unit(s) seen and
a few example values as printed — never a patient, a date or a lab. The
answer follows ``SCHEMA`` (strict JSON): one proposal per analyte of the
batch. Structured outputs want every property required, so "none" is ``0``
(``target_id``) or ``""`` (``display_name``).
"""

from typing import Any

from app.prompts import PromptSpec

VERSION = "analyte_triage_v1"

ACTIONS = ("merge_into", "keep_as_new", "unsure")
CONFIDENCES = ("high", "medium", "low")
REASON_CODES = (
    "same_test_spelling",  # accents, case, typos, word order
    "same_test_abbreviation",  # "HbA1c" vs "Hemoglobina glicosilada"
    "same_test_translation",  # English vs Spanish name
    "same_test_synonym",  # "Glicemia" vs "Glucosa"
    "new_test",  # a real test the catalogue does not have
    "different_specimen",  # same name, but another specimen
    "different_test",  # a similar name for a different test
    "not_a_test",  # a heading, comment or extraction artifact
    "ambiguous",
    "insufficient_data",
)

SYSTEM_PROMPT = """\
Ordenas el catálogo de exámenes de laboratorio clínico de una app familiar \
privada en Panamá. Cuando la lectura automática de un informe encuentra un \
nombre de examen que no conoce, crea un examen nuevo marcado "por revisar". \
Tu tarea es proponer, para cada examen por revisar, qué hacer con él; una \
persona revisará tus propuestas.

Recibes primero el CATÁLOGO de exámenes ya revisados y después los EXÁMENES \
POR REVISAR. Cada línea trae el id, el nombre, otros nombres (alias), la \
muestra (sangre, orina, heces, otro), las unidades vistas y algunos valores \
de ejemplo tal como se imprimieron.

Para cada examen por revisar responde exactamente una propuesta:
- "merge_into": es el MISMO examen que uno del catálogo (otra ortografía, \
abreviatura, traducción o sinónimo). target_id es el id de ese examen del \
catálogo; display_name "". Solo con la misma muestra: la glucosa en sangre y \
la glucosa en orina son exámenes distintos. Un examen relacionado no es el \
mismo: el colesterol HDL no es el colesterol total, un porcentaje no es un \
recuento absoluto (neutrófilos % frente a neutrófilos #), una razón o índice \
no es ninguno de sus componentes.
- "keep_as_new": es un examen real que el catálogo no tiene. target_id 0; \
display_name es un nombre limpio en español, como lo escribiría un médico \
(mayúscula inicial, tildes correctas, sin unidades, sin la muestra salvo que \
haga falta para distinguirlo, sin códigos del laboratorio); "" si el nombre \
actual ya está bien.
- "unsure": no se puede decidir con seguridad, o no es un examen (un título \
de sección, un comentario, una nota del laboratorio). target_id 0; \
display_name "".

Las unidades ayudan: el mismo examen suele tener la misma unidad, aunque \
algunos laboratorios usan otra (mg/dL frente a mmol/L). Unidades \
incompatibles (un porcentaje frente a un valor absoluto) indican exámenes \
distintos.

confidence: "high" solo si no hay duda razonable; "medium" o "low" en otro \
caso. Combinar dos exámenes distintos mezcla sus gráficas: ante la duda, \
"unsure" o una confianza menor.

reason_code: el motivo principal (same_test_spelling, \
same_test_abbreviation, same_test_translation, same_test_synonym, new_test, \
different_specimen, different_test, not_a_test, ambiguous, \
insufficient_data)."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "analyte_id",
                    "action",
                    "target_id",
                    "display_name",
                    "confidence",
                    "reason_code",
                ],
                "properties": {
                    "analyte_id": {"type": "integer"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "target_id": {"type": "integer"},
                    "display_name": {"type": "string"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                    "reason_code": {"type": "string", "enum": list(REASON_CODES)},
                },
            },
        }
    },
}

SPEC = PromptSpec(version=VERSION, system=SYSTEM_PROMPT, schema=SCHEMA)
