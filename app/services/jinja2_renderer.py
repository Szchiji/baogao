from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from app.models.report import Report
from app.models.template import Template


@dataclass
class FieldItem:
    key: str
    label: str
    field_type: str
    value: Any
    value_text: str


def _resolve_value_text(field_def: dict, value: Any) -> str:
    field_type = field_def.get("field_type", "text")

    if value is None:
        return ""

    if field_type in ("text", "textarea"):
        return str(value)

    if field_type == "select":
        options = field_def.get("options", [])
        for opt in options:
            if opt.get("value") == value:
                return opt.get("label", str(value))
        return str(value)

    if field_type == "tags":
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)

    if field_type == "media":
        count = len(value) if isinstance(value, list) else 0
        return f"{count} 个附件"

    return str(value)


def build_render_context(report: Report, template: Template, base_url: str) -> dict:
    template_json = template.template_json or {}
    fields_defs: list[dict] = template_json.get("fields", [])
    content = report.content_json or {}

    fields: list[FieldItem] = []
    media_count = 0

    for field_def in fields_defs:
        key = field_def.get("key", "")
        label = field_def.get("label", key)
        field_type = field_def.get("field_type", "text")
        value = content.get(key)
        value_text = _resolve_value_text(field_def, value)

        if field_type == "media" and isinstance(value, list):
            media_count += len(value)

        fields.append(
            FieldItem(
                key=key,
                label=label,
                field_type=field_type,
                value=value,
                value_text=value_text,
            )
        )

    tags = report.tags or []
    tags_text = ", ".join(tags)

    admin_report_url = f"{base_url.rstrip('/')}/admin/reports/{report.id}"

    return {
        "report": {
            "id": str(report.id),
            "report_number": report.report_number,
            "status": report.status,
            "tags": tags,
            "tags_text": tags_text,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "field_type": f.field_type,
                    "value": f.value,
                    "value_text": f.value_text,
                }
                for f in fields
            ],
            "media_count": media_count,
            "submitted_by": report.submitted_by,
            "submitted_username": report.submitted_username,
        },
        "links": {
            "admin_report_url": admin_report_url,
        },
    }


def render_template(template_text: str, context: dict) -> str:
    # SandboxedEnvironment prevents access to internals (__class__, etc.)
    env = SandboxedEnvironment(undefined=jinja2.ChainableUndefined, autoescape=False)
    tmpl = env.from_string(template_text)
    return tmpl.render(**context)
