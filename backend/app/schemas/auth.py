from pydantic import BaseModel, Field


class RequestLinkIn(BaseModel):
    # Deliberately a plain string (no EmailStr dependency); anything that is not
    # on the allowlist simply never gets an email.
    email: str = Field(min_length=3, max_length=254)


class RequestLinkOut(BaseModel):
    message: str


class VerifyIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: str
    is_admin: bool
    # Effective import permission (admin + ``users.can_import``): the UI shows
    # «Importar WhatsApp» only when it is true.
    can_import: bool = Field(validation_alias="is_import_admin")

    model_config = {"from_attributes": True}
