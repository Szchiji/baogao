from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription
from app.models.template import Template
from app.models.report import Report
from app.services.jinja2_renderer import build_render_context, render_template
from app.config import settings

logger = logging.getLogger(__name__)


async def push_report_to_subscribers(
    bot: object,
    report: Report,
    template: Template,
    db: AsyncSession,
) -> None:
    """Render publish_template_jinja2 and send to all enabled subscriptions."""
    from aiogram import Bot  # type: ignore

    assert isinstance(bot, Bot)

    context = build_render_context(report, template, settings.BASE_URL)
    text = render_template(template.publish_template_jinja2, context)

    result = await db.execute(
        select(Subscription).where(Subscription.enabled.is_(True))
    )
    subscriptions = result.scalars().all()

    for sub in subscriptions:
        try:
            await bot.send_message(chat_id=sub.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send message to subscription chat_id=%s", sub.chat_id)
