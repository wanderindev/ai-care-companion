from datetime import datetime

from pydantic import BaseModel


class AttachmentOut(BaseModel):
    id: int
    filename: str
    mime: str
    size: int
    sha256: str
    owner_type: str | None
    owner_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AttachmentUrlOut(BaseModel):
    url: str
    expires_in: int
