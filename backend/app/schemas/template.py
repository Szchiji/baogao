from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TemplateBase(BaseModel):
    template_key: str
    template_name: str
    description: str = ""
    enabled: bool = True
    template_json: dict[str, Any]
    publish_template_jinja2: str = ""


class TemplateCreate(TemplateBase):
    pass


class TemplateUpdate(BaseModel):
    template_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    template_json: dict[str, Any] | None = None
    publish_template_jinja2: str | None = None


class TemplateOut(TemplateBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PreviewRequest(BaseModel):
    report_id: str
    template_text: str


class PreviewResponse(BaseModel):
    rendered_text: str


class ValidationErrorItem(BaseModel):
    json_path: str
    reason: str
