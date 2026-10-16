from datetime import datetime

from pydantic import BaseModel


class JobOut(BaseModel):
    """A job's state for the UI's progress poll. ``payload`` is not exposed."""

    id: int
    kind: str
    status: str
    progress: int
    message: str | None
    attempts: int
    max_attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
