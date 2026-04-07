#!/usr/bin/env python
"""Seed the general_report template into the database."""
from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import AsyncSessionLocal
from app.models.template import Template
from sqlalchemy import select

GENERAL_REPORT_TEMPLATE = {
    "schema_version": 1,
    "template_key": "general_report",
    "template_name": "通用举报",
    "description": "用于举报垃圾信息、诈骗、骚扰等问题",
    "enabled": True,
    "submit_label": "提交",
    "resubmit_label": "重新提交",
    "supplement_label": "补充信息",
    "fields": [
        {
            "key": "category",
            "label": "举报类型",
            "field_type": "select",
            "required": True,
            "help_text": "请选择最符合情况的类型",
            "options": [
                {"value": "spam", "label": "垃圾信息"},
                {"value": "scam", "label": "诈骗"},
                {"value": "harassment", "label": "骚扰"},
                {"value": "other", "label": "其他"},
            ],
        },
        {
            "key": "summary",
            "label": "简要描述",
            "field_type": "text",
            "required": True,
            "help_text": "请用一句话描述问题",
            "min_length": 5,
            "max_length": 200,
        },
        {
            "key": "details",
            "label": "详细说明",
            "field_type": "textarea",
            "required": False,
            "help_text": "请详细描述发生了什么（可选）",
            "max_length": 2000,
        },
        {
            "key": "tags",
            "label": "标签",
            "field_type": "tags",
            "required": False,
            "help_text": "用逗号分隔多个标签，例如：诈骗,金融",
            "max_items": 10,
            "normalize": {
                "lowercase": True,
                "dedupe": True,
                "trim": True,
            },
        },
        {
            "key": "evidence",
            "label": "证据截图",
            "field_type": "media",
            "required": False,
            "help_text": "可上传截图或相关文件作为证据",
            "allowed_media": ["photo", "document"],
        },
    ],
}

PUBLISH_TEMPLATE = """\
📋 报告 #{{ report.report_number }}
🏷 {{ report.tags_text|default('') }}

{% for f in report.fields|default([]) %}{% if f.value_text %}- {{ f.label }}：{{ f.value_text }}
{% endif %}{% endfor %}
📎 附件数量：{{ report.media_count|default(0) }}
🔗 后台查看：{{ links.admin_report_url }}
"""


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(Template).where(Template.template_key == "general_report")
        )
        tmpl = existing.scalar_one_or_none()

        if tmpl is not None:
            print("Template 'general_report' already exists, updating…")
            tmpl.template_json = GENERAL_REPORT_TEMPLATE
            tmpl.publish_template_jinja2 = PUBLISH_TEMPLATE
            tmpl.template_name = GENERAL_REPORT_TEMPLATE["template_name"]
            tmpl.description = GENERAL_REPORT_TEMPLATE["description"]
            tmpl.enabled = True
        else:
            print("Creating template 'general_report'…")
            tmpl = Template(
                template_key="general_report",
                template_name=GENERAL_REPORT_TEMPLATE["template_name"],
                description=GENERAL_REPORT_TEMPLATE["description"],
                enabled=True,
                template_json=GENERAL_REPORT_TEMPLATE,
                publish_template_jinja2=PUBLISH_TEMPLATE,
            )
            db.add(tmpl)

        await db.commit()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
