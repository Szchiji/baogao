from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

import jinja2

from app.database import get_db
from app.models.template import Template
from app.models.report import Report
from app.schemas.template import (
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
    PreviewRequest,
    PreviewResponse,
    ValidationErrorItem,
)
from app.services.template_validator import validate_template
from app.services.jinja2_renderer import build_render_context, render_template
from app.config import settings

router = APIRouter(prefix="/templates", tags=["templates"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("/", response_model=list[TemplateOut])
async def list_templates(db: DbDep):
    result = await db.execute(select(Template).order_by(Template.template_name))
    return result.scalars().all()


@router.get("/{key}", response_model=TemplateOut)
async def get_template(key: str, db: DbDep):
    result = await db.execute(select(Template).where(Template.template_key == key))
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return tmpl


@router.post("/", response_model=TemplateOut, status_code=201)
async def create_template(body: TemplateCreate, db: DbDep):
    errors = validate_template(body.template_json)
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[{"json_path": e["json_path"], "reason": e["reason"]} for e in errors],
        )
    existing = await db.execute(
        select(Template).where(Template.template_key == body.template_key)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Template key already exists")

    tmpl = Template(**body.model_dump())
    db.add(tmpl)
    await db.flush()
    await db.refresh(tmpl)
    await db.commit()
    return tmpl


@router.put("/{key}", response_model=TemplateOut)
async def update_template(key: str, body: TemplateUpdate, db: DbDep):
    result = await db.execute(select(Template).where(Template.template_key == key))
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")

    update_data = body.model_dump(exclude_none=True)
    if "template_json" in update_data:
        errors = validate_template(update_data["template_json"])
        if errors:
            raise HTTPException(
                status_code=422,
                detail=[{"json_path": e["json_path"], "reason": e["reason"]} for e in errors],
            )

    for field, value in update_data.items():
        setattr(tmpl, field, value)

    await db.flush()
    await db.refresh(tmpl)
    await db.commit()
    return tmpl


@router.delete("/{key}", status_code=204)
async def delete_template(key: str, db: DbDep):
    result = await db.execute(select(Template).where(Template.template_key == key))
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(tmpl)
    await db.commit()


@router.post("/preview", response_model=PreviewResponse)
async def preview_template(body: PreviewRequest, db: DbDep):
    try:
        report_id = uuid.UUID(body.report_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_id")

    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    # Load the template associated with the report's content_json template_key
    template_key = (report.content_json or {}).get("_template_key")
    tmpl: Template | None = None
    if template_key:
        r = await db.execute(select(Template).where(Template.template_key == template_key))
        tmpl = r.scalar_one_or_none()

    if tmpl is None:
        # Use a minimal template object for context building
        tmpl = Template(
            template_key="__preview__",
            template_name="Preview",
            template_json={"fields": []},
            publish_template_jinja2="",
        )

    context = build_render_context(report, tmpl, settings.BASE_URL)

    try:
        rendered = render_template(body.template_text, context)
    except jinja2.TemplateSyntaxError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": exc.message, "line_number": exc.lineno},
        )

    return PreviewResponse(rendered_text=rendered)
