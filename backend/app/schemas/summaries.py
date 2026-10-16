"""Emergency card screen (ticket #34): the same data as the printed card."""

from datetime import date

from pydantic import BaseModel


class CardDoctor(BaseModel):
    id: int
    name: str
    specialty: str | None
    phone: str | None
    clinic: str | None


class CardMed(BaseModel):
    name: str
    strength: str | None
    quantity: str | None
    dose_note: str | None
    slot: str
    time: str | None
    instructions: str | None


class CardMedGroup(BaseModel):
    name: str
    doses: list[str]


class CardAvoid(BaseModel):
    substance: str
    reason: str | None


class EmergencyCardOut(BaseModel):
    patient_id: int
    patient_slug: str
    display_name: str
    full_name: str | None
    id_number: str | None
    birth_date: date | None
    age: int | None
    diagnoses: str | None
    meds: list[CardMed]
    med_groups: list[CardMedGroup]
    avoid: list[CardAvoid]
    doctors: list[CardDoctor]
    updated_on: date
