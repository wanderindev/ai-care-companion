"""Patients and doctors (DESIGN §5.1)."""

from datetime import date

from pydantic import BaseModel, Field, field_validator


def _blank_to_none(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class PatientOut(BaseModel):
    id: int
    slug: str
    display_name: str
    full_name: str | None
    id_number: str | None
    birth_date: date | None
    diagnoses: str | None
    notes: str | None

    model_config = {"from_attributes": True}


class PatientUpdate(BaseModel):
    """Only the fields present in the request body are changed."""

    display_name: str | None = Field(default=None, min_length=1, max_length=50)
    full_name: str | None = Field(default=None, max_length=200)
    id_number: str | None = Field(default=None, max_length=50)
    birth_date: date | None = None
    diagnoses: str | None = Field(default=None, max_length=20_000)
    notes: str | None = Field(default=None, max_length=20_000)

    _blank = field_validator("full_name", "id_number", "diagnoses", "notes", mode="before")(
        _blank_to_none
    )

    @field_validator("display_name", mode="before")
    @classmethod
    def _display_name(cls, value):
        if value is None:
            raise ValueError("display_name cannot be null")
        return value.strip() if isinstance(value, str) else value


class DoctorOut(BaseModel):
    id: int
    name: str
    aliases: list[str]
    specialty: str | None
    phone: str | None
    email: str | None
    clinic: str | None
    address: str | None
    notes: str | None
    is_active: bool

    model_config = {"from_attributes": True}


_DOCTOR_OPTIONAL = ("specialty", "phone", "email", "clinic", "address", "notes")


class DoctorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    specialty: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=254)
    clinic: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=2_000)
    notes: str | None = Field(default=None, max_length=20_000)
    is_active: bool = True

    _blank = field_validator(*_DOCTOR_OPTIONAL, mode="before")(_blank_to_none)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value):
        if value is None:
            raise ValueError("name cannot be null")
        return value.strip() if isinstance(value, str) else value

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, value: list[str]) -> list[str]:
        if any(len(alias) > 200 for alias in value):
            raise ValueError("alias too long")
        return value


class DoctorMerge(BaseModel):
    """``POST /doctors/{target_id}/merge``: the duplicate folded into the target."""

    source_id: int = Field(gt=0, le=2**63 - 1)


class DoctorUpdate(DoctorCreate):
    """Only the fields present in the request body are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] | None = Field(default=None, max_length=50)
    is_active: bool | None = None

    @field_validator("aliases", "is_active", mode="before")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value
