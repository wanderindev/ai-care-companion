"""Chat sessions and messages (DESIGN §5.7, §6.6)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChatSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title", mode="before")
    @classmethod
    def _blank(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class ChatSessionOut(BaseModel):
    id: int
    title: str | None
    created_by: int | None
    created_by_name: str | None
    created_at: datetime
    last_message_at: datetime | None
    generating: bool
    # #99: archived sessions are listed only with ``archived=true``;
    # ``can_delete``: the signed-in member may delete it (admin or creator).
    archived_at: datetime | None = None
    can_delete: bool = False


class ChatSessionUpdate(BaseModel):
    """``PATCH /chat/sessions/{id}``: archive (true) or restore (false)."""

    archived: bool


class ChatSessionPage(BaseModel):
    items: list[ChatSessionOut]
    total: int


class ChatToolCall(BaseModel):
    name: str
    status: str


class ChatAttachmentOut(BaseModel):
    """A file attached to an assistant answer (generated PDF/XLSX, #33)."""

    id: int
    filename: str
    mime: str
    size: int

    model_config = {"from_attributes": True}


class ChatSuggestionOut(BaseModel):
    """A proposal the answer made (``propose_change``, #41), shown as a card
    linking to «Pendientes». ``summary`` is one Spanish line; ``status`` is
    the suggestion's current one (``pending``, ``approved``, ``edited``,
    ``rejected``; the SSE ``suggestion`` event has no status: pending)."""

    id: int
    kind: str
    patient_id: int | None
    summary: str
    status: str


class ChatMessageOut(BaseModel):
    """One stored message, shaped for the UI.

    ``kind``: ``prompt`` (a family member's message: ``text`` is what they
    typed, ``author_*`` who) or ``assistant`` (``text`` is the answer, or
    the text written before a tool call; ``tools`` the calls it made;
    ``attachments`` the files it owns, ``owner_type='chat_message'``;
    ``suggestions`` the proposals its turn created, from
    ``meta.suggestion_ids``). Tool results and the raw content blocks are
    not exposed.
    """

    id: int
    kind: Literal["prompt", "assistant"]
    author_user_id: int | None
    author_name: str | None
    text: str
    tools: list[ChatToolCall]
    status: str
    error: str | None
    created_at: datetime
    attachments: list[ChatAttachmentOut] = []
    suggestions: list[ChatSuggestionOut] = []


class ChatSessionDetail(ChatSessionOut):
    messages: list[ChatMessageOut]


class ChatMessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=8000)

    @field_validator("text", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value
