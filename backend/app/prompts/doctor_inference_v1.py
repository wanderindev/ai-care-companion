"""Doctor inference, version 1 (ticket #125; ``app.services.doctor_inference``).

One call per batch of one patient's bitácora entries that have no doctor.
The request is::

    system   = SYSTEM_PROMPT                         (static, cached)
    user[0]  = the active doctors                    (same for every batch: cached)
    user[1]  = the batch: the entries without a doctor, the patient's nearby
               entries that have one, and the nearby appointments

Doctors are listed by id, name, aliases and specialty; entries by id, date,
type, title and short excerpts of their text; nearby records by date, type,
doctor id and title. Never a patient's name, ID number or birth date.

The answer follows ``SCHEMA`` (strict JSON): one item per entry of the batch.
Structured outputs want every property required, so "no doctor" is
``doctor_id`` ``0``.
"""

from typing import Any

from app.prompts import PromptSpec

VERSION = "doctor_inference_v1"

CONFIDENCES = ("high", "medium", "low")

SYSTEM_PROMPT = """\
Ayudas a completar la bitácora médica de una app familiar privada en Panamá, \
donde una familia registra las consultas, exámenes, procedimientos y \
terapias de sus padres adultos mayores. Algunas entradas de la bitácora no \
tienen médico asignado. Tu tarea es proponer, para cada una, qué médico del \
directorio la atendió, solo cuando se puede deducir con seguridad; una \
persona revisará cada propuesta antes de guardarla.

Recibes primero el DIRECTORIO de médicos (id · nombre · alias · \
especialidad) y después:
- ENTRADAS SIN MÉDICO: las que debes resolver (entry_id, fecha, tipo, \
título y extractos de las indicaciones y las notas de la familia);
- ENTRADAS CERCANAS CON MÉDICO del mismo paciente (fecha, tipo, doctor_id, \
título), unas semanas antes y después;
- CITAS CERCANAS del mismo paciente (fecha, doctor_id, especialidad).

Pistas válidas:
- el texto nombra al médico, por nombre o por un alias del directorio;
- la especialidad que el texto implica (gotas para los ojos o presión ocular \
→ oftalmología; electroencefalograma o convulsiones → neurología; \
ecocardiograma o Holter → cardiología) cuando el directorio tiene UN SOLO \
médico activo de esa especialidad, o cuando las entradas y citas cercanas \
muestran que el paciente se atiende con uno de ellos;
- una cita con un médico el mismo día o muy cerca de la entrada, con el \
mismo motivo;
- una serie de entradas iguales (las mismas terapias, controles o \
procedimientos) cuyas vecinas tienen todas el mismo médico.

Reglas:
- doctor_id es el id de un médico del DIRECTORIO, o 0 si no se puede saber. \
Nunca inventes un id.
- confidence "high" solo si no hay duda razonable; "medium" o "low" en otro \
caso. Un médico equivocado es peor que ninguno: ante la duda, 0 o una \
confianza menor.
- Un examen de laboratorio o de imagen puede no tener un médico tratante \
claro: no lo atribuyas solo por cercanía en el tiempo.
- reason: una frase corta en español que explique la pista principal \
(por ejemplo "Gotas oftálmicas; el único oftalmólogo del directorio la \
atendió dos semanas antes"). No copies datos personales del paciente.
- Responde exactamente un elemento por cada entrada sin médico."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entries"],
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id", "doctor_id", "confidence", "reason"],
                "properties": {
                    "entry_id": {"type": "integer"},
                    "doctor_id": {"type": "integer"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

SPEC = PromptSpec(version=VERSION, system=SYSTEM_PROMPT, schema=SCHEMA)
