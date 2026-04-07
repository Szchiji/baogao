from __future__ import annotations

import logging

from aiogram import Router, F  # type: ignore
from aiogram.filters import Command  # type: ignore
from aiogram.fsm.context import FSMContext  # type: ignore
from aiogram.fsm.state import State, StatesGroup  # type: ignore
from aiogram.types import Message, CallbackQuery  # type: ignore

from app.bot.keyboards import build_select_keyboard, build_done_keyboard
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
from app.models.template import Template
from app.models.report import Report, ReportDraft
from app.config import settings

from sqlalchemy import select

router = Router()


class ReportWizard(StatesGroup):
    collecting = State()
    waiting_media = State()


async def _get_active_template() -> Template | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Template).where(Template.enabled.is_(True)).order_by(Template.created_at)
        )
        return result.scalars().first()


async def _get_or_create_draft(db, user_id: int, template_key: str) -> ReportDraft:
    result = await db.execute(
        select(ReportDraft).where(
            ReportDraft.telegram_user_id == user_id,
            ReportDraft.template_key == template_key,
        )
    )
    draft = result.scalar_one_or_none()
    if draft is None:
        draft = ReportDraft(
            telegram_user_id=user_id,
            template_key=template_key,
            draft_json={},
            current_step=0,
        )
        db.add(draft)
        await db.flush()
    return draft


async def _ask_field(message: Message, field: dict, step: int, total: int) -> None:
    field_type = field.get("field_type", "text")
    label = field.get("label", field.get("key", "?"))
    help_text = field.get("help_text", "")
    required = field.get("required", False)
    required_mark = " <i>*必填</i>" if required else ""

    prompt = f"<b>[{step+1}/{total}] {label}</b>{required_mark}"
    if help_text:
        prompt += f"\n<i>{help_text}</i>"

    if field_type == "select":
        options = field.get("options", [])
        kb = build_select_keyboard(options, field.get("key", ""))
        await message.answer(prompt, reply_markup=kb)
    elif field_type == "media":
        allowed = field.get("allowed_media", ["photo", "document"])
        allowed_str = "、".join(allowed)
        prompt += f"\n请发送{allowed_str}，发送完毕后点击「完成」按钮。"
        kb = build_done_keyboard("media_done")
        await message.answer(prompt, reply_markup=kb)
    else:
        await message.answer(prompt)


@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext) -> None:
    tmpl = await _get_active_template()
    if tmpl is None:
        await message.answer("暂无可用报告模板，请稍后再试。")
        return

    fields = tmpl.template_json.get("fields", [])
    if not fields:
        await message.answer("模板配置错误，请联系管理员。")
        return

    await state.set_state(ReportWizard.collecting)
    await state.update_data(
        template_key=tmpl.template_key,
        current_step=0,
        answers={},
        media_buffer=[],
    )

    async with AsyncSessionLocal() as db:
        await _get_or_create_draft(db, message.from_user.id, tmpl.template_key)
        await db.commit()

    await _ask_field(message, fields[0], 0, len(fields))


@router.message(ReportWizard.collecting)
async def handle_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    template_key: str = data["template_key"]
    current_step: int = data["current_step"]
    answers: dict = data.get("answers", {})

    tmpl = await _get_active_template()
    if tmpl is None or tmpl.template_key != template_key:
        await state.clear()
        await message.answer("模板已变更，请重新开始 /report")
        return

    fields = tmpl.template_json.get("fields", [])
    field = fields[current_step]
    field_type = field.get("field_type", "text")
    key = field.get("key", "")

    if field_type in ("text", "textarea"):
        value = message.text or ""
        if field.get("required") and not value.strip():
            await message.answer("此字段为必填，请输入内容。")
            return
        answers[key] = value

    elif field_type == "tags":
        raw = message.text or ""
        raw_tags = [t.strip() for t in raw.split(",") if t.strip()]
        normalize = field.get("normalize", {})
        if normalize.get("lowercase", False):
            raw_tags = [t.lower() for t in raw_tags]
        if normalize.get("dedupe", True):
            seen: set[str] = set()
            deduped = []
            for t in raw_tags:
                if t not in seen:
                    seen.add(t)
                    deduped.append(t)
            raw_tags = deduped
        max_items = field.get("max_items")
        if max_items:
            raw_tags = raw_tags[:max_items]
        answers[key] = raw_tags

    elif field_type == "media":
        if message.text and message.text.lower() in ("done", "完成"):
            answers[key] = data.get("media_buffer", [])
        else:
            await message.answer("请发送媒体文件或点击「完成」按钮。")
            return

    async with AsyncSessionLocal() as db:
        draft = await _get_or_create_draft(db, message.from_user.id, template_key)
        draft.draft_json = answers
        draft.current_step = current_step + 1
        await db.commit()

    next_step = current_step + 1
    if next_step >= len(fields):
        await _finish_report(message, answers, tmpl, state)
        return

    await state.update_data(current_step=next_step, answers=answers)
    await _ask_field(message, fields[next_step], next_step, len(fields))


@router.callback_query(F.data.startswith("select:"))
async def handle_select_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data:
        await callback.answer("会话已过期，请重新 /report", show_alert=True)
        return

    template_key: str = data.get("template_key", "")
    current_step: int = data.get("current_step", 0)
    answers: dict = data.get("answers", {})

    parts = callback.data.split(":", 2)
    # Format: select:{field_key}:{value}
    if len(parts) < 3:
        await callback.answer()
        return

    field_key = parts[1]
    value = parts[2]
    answers[field_key] = value

    tmpl = await _get_active_template()
    if tmpl is None:
        await callback.answer("模板不可用", show_alert=True)
        return

    fields = tmpl.template_json.get("fields", [])

    async with AsyncSessionLocal() as db:
        draft = await _get_or_create_draft(db, callback.from_user.id, template_key)
        draft.draft_json = answers
        draft.current_step = current_step + 1
        await db.commit()

    next_step = current_step + 1
    await callback.message.edit_reply_markup()
    await callback.answer()

    if next_step >= len(fields):
        await _finish_report(callback.message, answers, tmpl, state)
        return

    await state.update_data(current_step=next_step, answers=answers)
    await _ask_field(callback.message, fields[next_step], next_step, len(fields))


@router.message(ReportWizard.waiting_media)
async def handle_media(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    media_buffer: list = data.get("media_buffer", [])

    if message.photo:
        file_id = message.photo[-1].file_id
        media_buffer.append({"type": "photo", "file_id": file_id})
    elif message.document:
        media_buffer.append({"type": "document", "file_id": message.document.file_id})

    await state.update_data(media_buffer=media_buffer)
    await message.answer(f"已接收 {len(media_buffer)} 个文件，继续发送或点击「完成」。")


@router.callback_query(F.data == "media_done")
async def handle_media_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    template_key: str = data.get("template_key", "")
    current_step: int = data.get("current_step", 0)
    answers: dict = data.get("answers", {})
    media_buffer: list = data.get("media_buffer", [])

    tmpl = await _get_active_template()
    if tmpl is None:
        await callback.answer("模板不可用", show_alert=True)
        return

    fields = tmpl.template_json.get("fields", [])
    field = fields[current_step]
    key = field.get("key", "")
    answers[key] = media_buffer

    async with AsyncSessionLocal() as db:
        draft = await _get_or_create_draft(db, callback.from_user.id, template_key)
        draft.draft_json = answers
        draft.current_step = current_step + 1
        await db.commit()

    next_step = current_step + 1
    await callback.message.edit_reply_markup()
    await callback.answer()

    if next_step >= len(fields):
        await _finish_report(callback.message, answers, tmpl, state)
        return

    await state.update_data(current_step=next_step, answers=answers, media_buffer=[])
    await state.set_state(ReportWizard.collecting)
    await _ask_field(callback.message, fields[next_step], next_step, len(fields))


async def _finish_report(
    message: Message,
    answers: dict,
    tmpl: Template,
    state: FSMContext | None = None,
) -> None:
    user_id = message.chat.id
    username = getattr(message.chat, "username", None)

    content = dict(answers)
    content["_template_key"] = tmpl.template_key

    # Extract top-level tags if a tags field exists
    tags: list[str] = []
    for field in tmpl.template_json.get("fields", []):
        if field.get("field_type") == "tags":
            raw = answers.get(field["key"], [])
            if isinstance(raw, list):
                tags = raw
            break

    async with AsyncSessionLocal() as db:
        report = Report(
            status="pending",
            content_json=content,
            tags=tags or None,
            submitted_by=user_id,
            submitted_username=username,
        )
        db.add(report)

        from sqlalchemy import delete as sa_delete
        await db.execute(
            sa_delete(ReportDraft).where(
                ReportDraft.telegram_user_id == user_id,
                ReportDraft.template_key == tmpl.template_key,
            )
        )
        await db.commit()
        await db.refresh(report)

    if state:
        await state.clear()

    await message.answer(
        f"✅ 报告已提交！\n报告编号：<b>#{report.report_number}</b>\n感谢您的反馈，管理员将尽快审核。"
    )

    from app.bot.main import get_bot
    bot = get_bot()
    if bot:
        for admin_id in settings.admin_ids_list:
            try:
                from app.bot.keyboards import build_admin_review_keyboard
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"📥 <b>新报告待审核</b>\n"
                        f"报告编号：<b>#{report.report_number}</b>\n"
                        f"提交人：{username or user_id}\n"
                        f"模板：{tmpl.template_key}"
                    ),
                    reply_markup=build_admin_review_keyboard(str(report.id)),
                )
            except Exception:
                logger.exception("Failed to notify admin_id=%s about new report #%s", admin_id, report.report_number)
