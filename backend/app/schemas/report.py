from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ReportBase(BaseModel):
    status: str = "pending"
    content_json: dict[str, Any] | None = None
    tags: list[str] | None = None
    need_more_info_note: str | None = None
    review_note: str | None = None
    submitted_by: int | None = None
    submitted_username: str | None = None


class ReportCreate(ReportBase):
    pass


class ReportUpdate(BaseModel):
    status: str | None = None
    review_note: str | None = None
    need_more_info_note: str | None = None
    reviewed_by: int | None = None
    tags: list[str] | None = None


class ReportOut(ReportBase):
    id: uuid.UUID
    report_number: int
    reviewed_at: datetime | None = None
    reviewed_by: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReportDraftOut(BaseModel):
    id: uuid.UUID
    telegram_user_id: int
    template_key: str
    draft_json: dict[str, Any]
    current_step: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
