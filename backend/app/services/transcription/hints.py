"""Context sent with each voice note to improve the transcription.

Only what helps the model spell domain words: a fixed, generic Spanish
``PROMPT`` (no names, no medical facts) and ``keywords`` — the names on both
patients' current medication lists, then the active doctors' names — capped
at ``KEYWORDS_MAX_CHARS`` in total. No other data (patients' names, notes,
diagnoses, dates) goes to the provider. Neither is ever logged.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.doctor import Doctor
from app.models.patient import Patient
from app.services import meds

PROMPT = (
    "Nota de voz en español de un grupo familiar sobre la salud de Papá y Mamá: "
    "citas médicas, medicamentos, dosis y resultados de laboratorio."
)
KEYWORDS_MAX_CHARS = 400
KEYWORD_MAX_CHARS = 60


def _clean(word: str) -> str:
    return " ".join(word.replace("<", " ").replace(">", " ").split())[:KEYWORD_MAX_CHARS]


def keywords(db: DbSession) -> list[str]:
    candidates: list[str] = []
    for patient_id in db.scalars(select(Patient.id).order_by(Patient.id)):
        candidates.extend(item.name for item in meds.current_items(db, patient_id))
    candidates.extend(
        db.scalars(select(Doctor.name).where(Doctor.is_active.is_(True)).order_by(Doctor.name))
    )
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in candidates:
        word = _clean(raw or "")
        key = word.casefold()
        if not word or key in seen:
            continue
        if total + len(word) > KEYWORDS_MAX_CHARS:
            break
        seen.add(key)
        out.append(word)
        total += len(word)
    return out
