from __future__ import annotations

from pydantic import BaseModel


class NoticeCreate(BaseModel):
    title: str
    content: str
    priority: str = 'NORMAL'
    admin_id: str | None = None
    is_pinned: bool = False


class MessageCreate(BaseModel):
    sender_id: str
    receiver_id: str
    sender_role: str
    receiver_role: str
    message: str


class MarkReadPayload(BaseModel):
    user_id: str
