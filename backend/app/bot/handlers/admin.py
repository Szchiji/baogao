from __future__ import annotations

import logging

from aiogram import Router, F  # type: ignore
from aiogram.types import CallbackQuery  # type: ignore
from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.models.report import Report
from app.models.template import Template
from app.services.publish_service import push_report_to_subscribers

logger = logging.getLogger(__name__)
from app.bot.keyboards import build_admin_review_keyboard

from datetime import datetime, timezone

router = Router()


async def _get_report(db, report_id: str) -> Report | None:
    try:
        import uuid
        uid = uuid.UUID(report_id)
    except ValueError:
        return None
    result = await db.execute(select(Report).where(Report.id == uid))
    return result.scalar_one_or_none()


@router.callback_query(F.data.startswith("approve_"))
async def handle_approve(callback: CallbackQuery) -> None:
    report_id = callback.data.removeprefix("approve_")
    async with AsyncSessionLocal() as db:
        report = await _get_report(db, report_id)
        if report is None:
            await callback.answer("报告不存在", show_alert=True)
            return
        if report.status != "pending":
            await callback.answer("已被其他管理员处理", show_alert=True)
            return

        report.status = "approved"
        report.reviewed_at = datetime.now(tz=timezone.utc)
        report.reviewed_by = callback.from_user.id

        # Load template
        template_key = (report.content_json or {}).get("_template_key")
        tmpl = None
        if template_key:
            r = await db.execute(select(Template).where(Template.template_key == template_key))
            tmpl = r.scalar_one_or_none()

        await db.commit()
        await db.refresh(report)

        if tmpl and tmpl.publish_template_jinja2:
            from app.bot.main import get_bot
            bot = get_bot()
            if bot:
                await push_report_to_subscribers(bot, report, tmpl, db)

    await callback.answer("已批准 ✅")
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ <b>已批准</b>（by {callback.from_user.full_name}）",
        reply_markup=None,
    )


@router.callback_query(F.data.startswith("reject_"))
async def handle_reject(callback: CallbackQuery) -> None:
    report_id = callback.data.removeprefix("reject_")
    async with AsyncSessionLocal() as db:
        report = await _get_report(db, report_id)
        if report is None:
            await callback.answer("报告不存在", show_alert=True)
            return
        if report.status != "pending":
            await callback.answer("已被其他管理员处理", show_alert=True)
            return

        report.status = "rejected"
        report.reviewed_at = datetime.now(tz=timezone.utc)
        report.reviewed_by = callback.from_user.id
        await db.commit()

    await callback.answer("已拒绝 ❌")
    await callback.message.edit_text(
        callback.message.text + f"\n\n❌ <b>已拒绝</b>（by {callback.from_user.full_name}）",
        reply_markup=None,
    )


@router.callback_query(F.data.startswith("needmore_"))
async def handle_need_more_info(callback: CallbackQuery) -> None:
    report_id = callback.data.removeprefix("needmore_")
    async with AsyncSessionLocal() as db:
        report = await _get_report(db, report_id)
        if report is None:
            await callback.answer("报告不存在", show_alert=True)
            return
        if report.status != "pending":
            await callback.answer("已被其他管理员处理", show_alert=True)
            return

        report.status = "need_more_info"
        report.reviewed_at = datetime.now(tz=timezone.utc)
        report.reviewed_by = callback.from_user.id
        report.need_more_info_note = "管理员需要更多信息"
        await db.commit()

        # Notify submitter
        if report.submitted_by:
            from app.bot.main import get_bot
            bot = get_bot()
            if bot:
                try:
                    await bot.send_message(
                        chat_id=report.submitted_by,
                        text=(
                            f"📋 报告 <b>#{report.report_number}</b> 需要补充信息。\n"
                            f"请重新提交 /report 并补充相关内容。"
                        ),
                    )
                except Exception:
                    logger.exception("Failed to notify submitter user_id=%s for report #%s", report.submitted_by, report.report_number)

    await callback.answer("已标记需补充 ℹ️")
    await callback.message.edit_text(
        callback.message.text + f"\n\nℹ️ <b>需要补充信息</b>（by {callback.from_user.full_name}）",
        reply_markup=None,
    )
