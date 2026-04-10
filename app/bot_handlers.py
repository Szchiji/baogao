import asyncio
import html
import json
import logging
import secrets
import time
from typing import Any

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.admin_auth import (
    _cleanup_verify_state,
    _is_rate_limited,
    _is_verify_code,
    _otp_tokens,
    _OTP_TOKEN_TTL,
    _record_verify_attempt,
    _verify_code_otps,
    _verify_codes,
)
from app.config import DEFAULT_SETTINGS, get_admin_user_ids, is_user_admin
from app.crud import (
    ban_user,
    is_user_banned,
    setting_get,
    unban_user,
    upsert_user,
)
from app.database import db_connection
from app.keyboards import (
    _make_field_prompt,
    _report_submit_keyboard,
    build_channel_link,
    get_force_sub_channels,
    get_push_channels,
    is_subscribed,
    keyboard_config,
    render_report_preview,
    report_template,
    start_inline_buttons,
    start_keyboard,
)
from app.utils import parse_json, safe_format, utc_now_iso

logger = logging.getLogger("report-bot")

_AUTO_DELETE_DELAY = 86400  # 24 hours in seconds


async def _delete_after(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    """Delete a message after *delay* seconds. Errors are silently ignored."""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.debug(
            "auto-delete skipped for chat_id=%s message_id=%s (message may already be gone)",
            chat_id, message_id,
        )


def schedule_auto_delete(bot: Bot, chat_id: int, message_id: int, delay: int = _AUTO_DELETE_DELAY) -> None:
    """Schedule a message for deletion after *delay* seconds (default 24 h)."""
    try:
        asyncio.get_running_loop().create_task(
            _delete_after(bot, chat_id, message_id, delay)
        )
    except RuntimeError:
        logger.debug(
            "schedule_auto_delete: no running event loop; skipping delete for chat_id=%s message_id=%s",
            chat_id, message_id,
        )


async def send_start_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = setting_get("start_text", DEFAULT_SETTINGS["start_text"])
    media_type = setting_get("start_media_type", "").strip().lower()
    media_url = setting_get("start_media_url", "").strip()
    user_id = update.effective_user.id if update.effective_user else None
    inline_markup = start_inline_buttons(user_id=user_id)
    keyboard = start_keyboard()
    if media_type == "photo" and media_url:
        await update.effective_chat.send_photo(
            photo=media_url,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=inline_markup,
        )
        await update.effective_chat.send_message("请选择操作：", reply_markup=keyboard)
        return
    if media_type == "video" and media_url:
        await update.effective_chat.send_video(
            video=media_url,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=inline_markup,
        )
        await update.effective_chat.send_message("请选择操作：", reply_markup=keyboard)
        return
    await update.effective_chat.send_message(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    if inline_markup:
        await update.effective_chat.send_message("快捷入口：", reply_markup=inline_markup)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    upsert_user(user_id, update.effective_user.username)
    if is_user_banned(user_id):
        await update.effective_chat.send_message("您已被限制使用此机器人。")
        return
    channels = get_force_sub_channels()
    if channels and not await is_subscribed(context.bot, user_id):
        rows: list[list[InlineKeyboardButton]] = []
        for i, ch in enumerate(channels):
            link = build_channel_link(ch)
            if link:
                label = f"先去订阅频道 {i + 1}" if len(channels) > 1 else "先去订阅"
                rows.append([InlineKeyboardButton(label, url=link)])
        rows.append([InlineKeyboardButton("我已订阅，重新检测", callback_data="retry_sub")])
        markup = InlineKeyboardMarkup(rows)
        sent = await update.effective_chat.send_message("请先订阅频道后再使用机器人。", reply_markup=markup)
        schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        return
    await send_start_content(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    base_url = (context.bot_data.get("admin_panel_url") or setting_get("admin_panel_url")).strip()
    if not base_url:
        await update.message.reply_text("未配置 ADMIN_PANEL_URL。")
        return
    _cleanup_verify_state()
    otp = secrets.token_urlsafe(16)
    _otp_tokens[otp] = time.time() + _OTP_TOKEN_TTL
    login_url = f"{base_url.rstrip('/')}/admin/otp?token={otp}"
    await update.message.reply_text(
        f"🔐 您的后台登录链接（{_OTP_TOKEN_TTL // 60} 分钟内有效）：\n{login_url}",
        disable_web_page_preview=True,
    )


def start_report_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    template = report_template()
    draft = {"template": template, "values": {}, "awaiting": ""}
    context.user_data["report_draft"] = draft
    return draft


async def write_report_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = start_report_draft(context)
    fields = draft["template"]["fields"]
    if not fields:
        await update.message.reply_text("报告模板无字段，请联系管理员配置。")
        return
    first_field = fields[0]
    draft["awaiting"] = first_field["key"]
    draft["sequential"] = True
    prompt, markup = _make_field_prompt(first_field, sequential=True)
    sent = await update.message.reply_text(
        f"📝 开始填写《{draft['template']['name']}》\n\n{prompt}",
        reply_markup=markup,
    )
    draft["prompt_msg_id"] = sent.message_id
    draft["prompt_chat_id"] = update.effective_chat.id
    schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)


async def query_reports(text: str) -> str:
    if text.startswith("@"):
        username = text[1:]
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, username, tag, data_json, created_at, channel_message_link
                FROM reports
                WHERE status = 'approved' AND username = %s
                ORDER BY id DESC LIMIT 10
                """,
                (username,),
            ).fetchall()
    elif text.startswith("#"):
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, username, tag, data_json, created_at, channel_message_link
                FROM reports
                WHERE status = 'approved' AND tag = %s
                ORDER BY id DESC LIMIT 10
                """,
                (text,),
            ).fetchall()
    else:
        return setting_get("search_help_text", DEFAULT_SETTINGS["search_help_text"])
    if not rows:
        return "未找到匹配报告。"
    link_base = setting_get("report_link_base", "").strip()
    lines = ["查询结果："]
    for row in rows:
        channel_link = row["channel_message_link"] if row["channel_message_link"] else ""
        if channel_link:
            link = channel_link
        elif link_base:
            link = f"{link_base.rstrip('/')}/reports/{row['id']}"
        else:
            link = f"报告ID: {row['id']}"
        lines.append(
            f"- #{row['id']} @{row['username'] or 'unknown'} {row['tag'] or ''}\n  {link}"
        )
    return "\n".join(lines)


async def _delete_prompt_message(context: ContextTypes.DEFAULT_TYPE, draft: dict[str, Any]) -> None:
    """Try to delete the stored prompt message from a draft."""
    msg_id = draft.pop("prompt_msg_id", None)
    chat_id = draft.pop("prompt_chat_id", None)
    if msg_id and chat_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # message may already be deleted or too old


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    has_message = bool(update.message)
    message_text = getattr(update.message, "text", None) if update.message else None
    if not has_message or not message_text:
        logger.info(
            "on_text skipped: has_message=%s message_text_present=%s update_id=%s",
            has_message,
            bool(message_text),
            update.update_id,
        )
        return
    text = message_text.strip()

    user = update.effective_user
    upsert_user(user.id, user.username)

    if is_user_banned(user.id):
        await update.message.reply_text("您已被限制使用此机器人。")
        return

    channels = get_force_sub_channels()
    if channels and not await is_subscribed(context.bot, update.effective_user.id):
        rows: list[list[InlineKeyboardButton]] = []
        for i, ch in enumerate(channels):
            link = build_channel_link(ch)
            if link:
                label = f"先去订阅频道 {i + 1}" if len(channels) > 1 else "先去订阅"
                rows.append([InlineKeyboardButton(label, url=link)])
        rows.append([InlineKeyboardButton("我已订阅，重新检测", callback_data="retry_sub")])
        markup = InlineKeyboardMarkup(rows)
        sent = await update.message.reply_text("请先订阅频道后再使用机器人。", reply_markup=markup)
        schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        return

    # Admin reject-reason flow: only when admin is NOT mid-draft to avoid ambiguity
    pending_reject_id = context.user_data.get("pending_reject_id")
    active_draft = context.user_data.get("report_draft")
    if (
        pending_reject_id is not None
        and is_user_admin(update.effective_user.id)
        and not (active_draft and active_draft.get("awaiting"))
    ):
        context.user_data.pop("pending_reject_id", None)
        reason = text
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s", (pending_reject_id,)).fetchone()
            if not report:
                await update.message.reply_text("报告不存在。")
                return
            if report["status"] != "pending":
                await update.message.reply_text(f"报告已处于 {report['status']} 状态，无法驳回。")
                return
            conn.execute(
                "UPDATE reports SET status='rejected', review_feedback=%s, reviewed_at=%s WHERE id = %s",
                (reason, utc_now_iso(), pending_reject_id),
            )
        tpl = (
            setting_get("review_rejected_template", "").strip()
            or DEFAULT_SETTINGS["review_rejected_template"]
        )
        feedback = safe_format(tpl, id=pending_reject_id, reason=reason)
        sent_feedback = await context.bot.send_message(chat_id=report["user_id"], text=feedback)
        schedule_auto_delete(context.bot, sent_feedback.chat_id, sent_feedback.message_id)
        sent_admin_reply = await update.message.reply_text(f"报告 #{pending_reject_id} 已驳回。")
        schedule_auto_delete(context.bot, sent_admin_reply.chat_id, sent_admin_reply.message_id)
        return

    draft = context.user_data.get("report_draft")
    if draft and draft.get("awaiting"):
        key = draft["awaiting"]
        field_def = next((f for f in draft["template"]["fields"] if f["key"] == key), {})
        if field_def.get("type", "text") == "photo":
            await update.message.reply_text(
                f"请发送图片（不是文字），作为「{field_def['label']}」字段。"
            )
            return
        draft["values"][key] = text
        draft["awaiting"] = ""
        sequential = draft.pop("sequential", False)

        if sequential:
            fields = draft["template"]["fields"]
            current_idx = next((i for i, f in enumerate(fields) if f["key"] == key), -1)
            next_idx = current_idx + 1
            # Delete previous prompt before sending next
            await _delete_prompt_message(context, draft)
            if next_idx < len(fields):
                next_field = fields[next_idx]
                draft["awaiting"] = next_field["key"]
                draft["sequential"] = True
                prompt, markup = _make_field_prompt(next_field, sequential=True)
                sent = await update.message.reply_text(prompt, reply_markup=markup)
                draft["prompt_msg_id"] = sent.message_id
                draft["prompt_chat_id"] = update.effective_chat.id
                schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
                return

        sent_preview = await update.message.reply_text(
            render_report_preview(draft["values"], draft["template"]),
            parse_mode=ParseMode.HTML,
            reply_markup=_report_submit_keyboard(),
        )
        schedule_auto_delete(context.bot, sent_preview.chat_id, sent_preview.message_id)
        return

    # Verify code check (only when not in a draft/admin flow)
    if _is_verify_code(text):
        _cleanup_verify_state()
        if text in _verify_codes and time.time() < _verify_codes[text]:
            if _is_rate_limited(user.id):
                await update.message.reply_text("⚠️ 验证尝试过于频繁，请稍后再试。")
                return
            _record_verify_attempt(user.id)
            if is_user_admin(user.id):
                otp = secrets.token_urlsafe(16)
                _otp_tokens[otp] = time.time() + _OTP_TOKEN_TTL
                _verify_code_otps[text] = otp
                base_url = (context.bot_data.get("admin_panel_url") or setting_get("admin_panel_url")).strip()
                if base_url:
                    await update.message.reply_text(f"✅ 身份验证成功！后台页面将自动跳转，请在 {_OTP_TOKEN_TTL // 60} 分钟内返回浏览器。")
                else:
                    await update.message.reply_text("✅ 验证成功，但未配置 ADMIN_PANEL_URL。")
            else:
                await update.message.reply_text("❌ 您不是管理员，访问请求已拒绝。")
            return

    if text.startswith("@") or text.startswith("#"):
        await update.message.reply_text(await query_reports(text))
        return

    mapping = {item["text"]: item for item in keyboard_config()}
    item = mapping.get(text)
    if not item:
        logger.info(
            "on_text unmatched keyboard action: text=%r user_id=%s chat_id=%s",
            text,
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )
        await update.message.reply_text("未识别操作，请使用底部菜单。")
        return

    action = item.get("action", "text")
    if action == "write_report":
        await write_report_flow(update, context)
    elif action == "search_help":
        await update.message.reply_text(setting_get("search_help_text"))
    elif action == "contact":
        await update.message.reply_text(setting_get("contact_text"))
    elif action == "usage":
        await update.message.reply_text(setting_get("usage_text"))
    else:
        await update.message.reply_text(item.get("value") or "已收到。")


async def submit_report(context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
    draft = context.user_data.get("report_draft")
    if not draft:
        await update.effective_chat.send_message("请先点击「写报告」。")
        return
    required_fields = [f["key"] for f in draft["template"]["fields"] if f.get("required", True)]
    missing = [k for k in required_fields if not draft["values"].get(k, "").strip()]
    if missing:
        fields_map = {f["key"]: f["label"] for f in draft["template"]["fields"]}
        missing_labels = "、".join(fields_map.get(k, k) for k in missing)
        await update.effective_chat.send_message(f"以下必填项尚未填写，请继续完善：{missing_labels}")
        return
    values = draft["values"]
    tag = values.get("tag", "")
    username = update.effective_user.username or ""
    template = draft["template"]
    try:
        with db_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO reports (user_id, username, tag, data_json, status, created_at)
                VALUES (%s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (
                    update.effective_user.id,
                    username,
                    tag,
                    json.dumps(values, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
            report_id = cur.fetchone()["id"]
    except Exception:
        logger.exception(
            "submit_report: error for user_id=%s", update.effective_user.id
        )
        await update.effective_chat.send_message("❌ 提交失败，请稍后重试。")
        return
    context.user_data.pop("report_draft", None)
    sent_confirm = await update.effective_chat.send_message(f"✅ 报告 #{report_id} 已提交，等待审核。")
    schedule_auto_delete(context.bot, sent_confirm.chat_id, sent_confirm.message_id)

    # Notify all admins with inline approve/reject buttons
    admin_ids = get_admin_user_ids()
    if admin_ids:
        preview = render_report_preview(values, template)
        submitter_id = update.effective_user.id
        notification = (
            f"📋 新报告待审核 #{report_id}\n"
            f"用户：@{html.escape(username or '未知')}（ID: {submitter_id}）\n\n"
            f"{preview}"
        )
        review_buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ 通过", callback_data=f"approve:{report_id}"),
                    InlineKeyboardButton("❌ 驳回", callback_data=f"reject:{report_id}"),
                ]
            ]
        )
        for admin_id in admin_ids:
            try:
                sent_notif = await context.bot.send_message(
                    chat_id=admin_id,
                    text=notification,
                    parse_mode=ParseMode.HTML,
                    reply_markup=review_buttons,
                )
                schedule_auto_delete(context.bot, sent_notif.chat_id, sent_notif.message_id)
                # Also send photo fields so the admin can review images
                for field in template["fields"]:
                    if field.get("type") == "photo":
                        photo_file_id = values.get(field["key"])
                        if photo_file_id:
                            sent_photo = await context.bot.send_photo(
                                chat_id=admin_id,
                                photo=photo_file_id,
                                caption=f"📷 {html.escape(field['label'])}（报告 #{report_id}）",
                            )
                            schedule_auto_delete(
                                context.bot, sent_photo.chat_id, sent_photo.message_id
                            )
            except Exception:
                logger.warning(
                    "failed to notify admin %s about report %s", admin_id, report_id, exc_info=True
                )


def _build_approval_feedback(report_id: int, channel_link: str = "") -> str:
    approved_tpl = (
        setting_get("review_approved_template", "").strip()
        or DEFAULT_SETTINGS["review_approved_template"]
    )
    # Prefer the real Telegram channel message link; fall back to report_link_base web URL
    if channel_link:
        link = channel_link
    else:
        link_base = setting_get("report_link_base", "").strip()
        link = f"{link_base.rstrip('/')}/reports/{report_id}" if link_base else ""
    return safe_format(approved_tpl, id=report_id, link=link)


def _build_channel_message_link(channel: str, message_id: int) -> str:
    """Return a t.me deep-link to a specific message posted in *channel*."""
    channel = channel.strip()
    if not channel or not message_id:
        return ""
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    # Private/supergroup numeric IDs (e.g. -1001234567890 → t.me/c/1234567890/...)
    # Telegram supergroup/channel IDs are prefixed with -100 in Bot API; the t.me/c/ path
    # uses only the remaining digits (e.g. -1001234567890 → t.me/c/1234567890/...).
    raw = channel.lstrip("-")
    if raw.isdigit():
        if raw.startswith("100"):
            raw = raw[3:]
        return f"https://t.me/c/{raw}/{message_id}"
    # Plain username without leading @
    return f"https://t.me/{channel}/{message_id}"


async def _push_report_to_channel(bot: Bot, report_id: int, report: dict) -> str:
    """Push *report* to all configured push channels.  Returns the first channel message link (or '')."""
    push_channels = get_push_channels()
    if not push_channels:
        return ""
    data_values = parse_json(report["data_json"], {})
    link_base = setting_get("report_link_base", "").strip()
    link = f"{link_base.rstrip('/')}/reports/{report_id}" if link_base else ""
    # Build per-field placeholders (field key → value, excluding photo fields)
    tpl_fields = report_template()["fields"]
    field_labels = {f["key"]: f["label"] for f in tpl_fields}
    field_types = {f["key"]: f.get("type", "text") for f in tpl_fields}
    field_placeholders: dict[str, str] = {}
    for f in tpl_fields:
        k = f["key"]
        if field_types.get(k, "text") != "photo":
            field_placeholders[k] = data_values.get(k, "")
    # Build {detail}: honour push_detail_fields_json ordering if configured
    push_detail_keys = parse_json(setting_get("push_detail_fields_json", "[]"), [])
    if isinstance(push_detail_keys, list) and push_detail_keys:
        detail_parts = []
        for k in push_detail_keys:
            if isinstance(k, str) and field_types.get(k, "text") != "photo":
                label = field_labels.get(k, k)
                detail_parts.append(f"{label}: {data_values.get(k, '')}")
    else:
        detail_parts = []
        for f in tpl_fields:
            k = f["key"]
            if field_types.get(k, "text") == "photo":
                continue
            detail_parts.append(f"{field_labels.get(k, k)}: {data_values.get(k, '')}")
    detail = "\n".join(detail_parts)
    push_tpl = setting_get("push_template", DEFAULT_SETTINGS["push_template"])
    # Merge: built-in keys always win over field-specific ones
    format_kwargs: dict[str, Any] = dict(field_placeholders)
    format_kwargs.update({
        "id": report_id,
        "username": report["username"] or "unknown",
        "detail": detail,
        "link": link,
    })
    push_text = safe_format(push_tpl, **format_kwargs)
    first_channel_link = ""
    for push_channel in push_channels:
        try:
            msg = await bot.send_message(chat_id=push_channel, text=push_text)
            channel_link = _build_channel_message_link(push_channel, msg.message_id)
            if channel_link and not first_channel_link:
                first_channel_link = channel_link
                with db_connection() as conn:
                    conn.execute(
                        "UPDATE reports SET channel_message_link=%s WHERE id=%s",
                        (channel_link, report_id),
                    )
        except Exception:
            logger.warning("failed to push report %s to channel %s", report_id, push_channel, exc_info=True)
    return first_channel_link


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at FROM reports WHERE status = 'pending' ORDER BY id DESC LIMIT 20"
        ).fetchall()
    if not rows:
        await update.message.reply_text("没有待审核报告。")
        return
    lines = ["待审核报告："]
    for row in rows:
        lines.append(f"- #{row['id']} @{row['username'] or 'unknown'} {row['created_at']}")
    lines.append("使用 /approve 报告ID 或 /reject 报告ID 原因")
    await update.message.reply_text("\n".join(lines))


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    if not context.args:
        await update.message.reply_text("用法：/approve 报告ID")
        return
    try:
        report_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("报告ID必须是数字。")
        return
    with db_connection() as conn:
        report = conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
        if not report:
            await update.message.reply_text("报告不存在。")
            return
        conn.execute(
            "UPDATE reports SET status='approved', reviewed_at=%s WHERE id = %s",
            (utc_now_iso(), report_id),
        )
    await update.message.reply_text(f"报告 #{report_id} 已通过。")

    channel_link = await _push_report_to_channel(context.bot, report_id, report)
    feedback = _build_approval_feedback(report_id, channel_link=channel_link)
    await context.bot.send_message(chat_id=report["user_id"], text=feedback)


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    if not context.args:
        await update.message.reply_text("用法：/reject 报告ID 原因")
        return
    try:
        report_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("报告ID必须是数字。")
        return
    reason = " ".join(context.args[1:]).strip() or "请联系管理员"
    with db_connection() as conn:
        report = conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
        if not report:
            await update.message.reply_text("报告不存在。")
            return
        conn.execute(
            "UPDATE reports SET status='rejected', review_feedback=%s, reviewed_at=%s WHERE id = %s",
            (reason, utc_now_iso(), report_id),
        )
    tpl = (
        setting_get("review_rejected_template", "").strip()
        or DEFAULT_SETTINGS["review_rejected_template"]
    )
    feedback = safe_format(tpl, id=report_id, reason=reason)
    await context.bot.send_message(chat_id=report["user_id"], text=feedback)
    await update.message.reply_text(f"报告 #{report_id} 已驳回。")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    if not context.args:
        await update.message.reply_text("用法：/ban 用户ID [原因]")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("用户ID必须是数字。")
        return
    reason = " ".join(context.args[1:]).strip() or "管理员限制"
    ban_user(target_id, None, reason)
    await update.message.reply_text(f"用户 {target_id} 已加入黑名单（原因：{reason}）。")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    if not context.args:
        await update.message.reply_text("用法：/unban 用户ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("用户ID必须是数字。")
        return
    unban_user(target_id)
    await update.message.reply_text(f"用户 {target_id} 已从黑名单移除。")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    logger.info(
        "on_callback received: data=%r user_id=%s chat_id=%s",
        data,
        update.effective_user.id if update.effective_user else None,
        update.effective_chat.id if update.effective_chat else None,
    )
    if data == "retry_sub":
        await query.answer()
        if await is_subscribed(context.bot, update.effective_user.id):
            sent_ok = await query.message.reply_text("订阅检测通过。")
            schedule_auto_delete(context.bot, sent_ok.chat_id, sent_ok.message_id)
            await send_start_content(update, context)
        else:
            sent_fail = await query.message.reply_text("检测失败，请确认订阅后重试。")
            schedule_auto_delete(context.bot, sent_fail.chat_id, sent_fail.message_id)
        return

    draft = context.user_data.get("report_draft")

    if data == "cancel_report":
        await query.answer()
        if draft:
            await _delete_prompt_message(context, draft)
        context.user_data.pop("report_draft", None)
        await query.message.reply_text("❌ 已取消填写报告。")
        return

    if data.startswith("fill:"):
        await query.answer()
        if not draft:
            draft = start_report_draft(context)
        key = data.split(":", 1)[1]
        fields = {f["key"]: f for f in draft["template"]["fields"]}
        field = fields.get(key)
        if not field:
            await query.message.reply_text("字段不存在。")
            return
        draft["awaiting"] = key
        draft["sequential"] = False
        prompt, markup = _make_field_prompt(field, sequential=False)
        sent = await query.message.reply_text(prompt, reply_markup=markup)
        schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        return

    if data.startswith("skip_field:"):
        await query.answer()
        key = data.split(":", 1)[1]
        if not draft or draft.get("awaiting") != key:
            return
        draft["values"][key] = ""
        draft["awaiting"] = ""
        fields = draft["template"]["fields"]
        current_idx = next((i for i, f in enumerate(fields) if f["key"] == key), -1)
        next_idx = current_idx + 1
        # Delete the previous prompt
        await _delete_prompt_message(context, draft)
        if next_idx < len(fields):
            next_field = fields[next_idx]
            draft["awaiting"] = next_field["key"]
            draft["sequential"] = True
            prompt, markup = _make_field_prompt(next_field, sequential=True)
            sent = await query.message.reply_text(prompt, reply_markup=markup)
            draft["prompt_msg_id"] = sent.message_id
            draft["prompt_chat_id"] = query.message.chat_id
            schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        else:
            sent_preview = await query.message.reply_text(
                render_report_preview(draft["values"], draft["template"]),
                parse_mode=ParseMode.HTML,
                reply_markup=_report_submit_keyboard(),
            )
            schedule_auto_delete(context.bot, sent_preview.chat_id, sent_preview.message_id)
        return

    if data == "submit_report":
        await query.answer()
        if is_user_banned(update.effective_user.id):
            await query.message.reply_text("您已被限制使用此机器人。")
            return
        if get_force_sub_channels() and not await is_subscribed(context.bot, update.effective_user.id):
            await query.message.reply_text("请先订阅频道后再提交报告。")
            return
        await submit_report(context, update)
        return

    if data.startswith("approve:"):
        if not is_user_admin(update.effective_user.id):
            await query.answer("无权限。", show_alert=True)
            return
        try:
            report_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("无效的报告ID。", show_alert=True)
            return
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
            if not report:
                await query.answer("报告不存在。", show_alert=True)
                return
            if report["status"] != "pending":
                await query.answer(f"报告已处于 {report['status']} 状态。", show_alert=True)
                return
            conn.execute(
                "UPDATE reports SET status='approved', reviewed_at=%s WHERE id = %s",
                (utc_now_iso(), report_id),
            )
        await query.answer("已通过。")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        sent_admin = await query.message.reply_text(f"✅ 报告 #{report_id} 已通过审核。")
        schedule_auto_delete(context.bot, sent_admin.chat_id, sent_admin.message_id)
        channel_link = await _push_report_to_channel(context.bot, report_id, report)
        feedback = _build_approval_feedback(report_id, channel_link=channel_link)
        sent_user = await context.bot.send_message(chat_id=report["user_id"], text=feedback)
        schedule_auto_delete(context.bot, sent_user.chat_id, sent_user.message_id)
        return

    if data.startswith("reject:"):
        if not is_user_admin(update.effective_user.id):
            await query.answer("无权限。", show_alert=True)
            return
        try:
            report_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("无效的报告ID。", show_alert=True)
            return
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
        if not report:
            await query.answer("报告不存在。", show_alert=True)
            return
        if report["status"] != "pending":
            await query.answer(f"报告已处于 {report['status']} 状态。", show_alert=True)
            return
        context.user_data["pending_reject_id"] = report_id
        await query.answer()
        sent_prompt = await query.message.reply_text(f"请输入驳回报告 #{report_id} 的原因：")
        schedule_auto_delete(context.bot, sent_prompt.chat_id, sent_prompt.message_id)
        return

    # Fallback: answer unhandled callback queries to avoid Telegram timeout errors
    await query.answer()


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    user = update.effective_user
    upsert_user(user.id, user.username)

    if is_user_banned(user.id):
        await update.message.reply_text("您已被限制使用此机器人。")
        return

    channels = get_force_sub_channels()
    if channels and not await is_subscribed(context.bot, user.id):
        rows: list[list[InlineKeyboardButton]] = []
        for i, ch in enumerate(channels):
            link = build_channel_link(ch)
            if link:
                label = f"先去订阅频道 {i + 1}" if len(channels) > 1 else "先去订阅"
                rows.append([InlineKeyboardButton(label, url=link)])
        rows.append([InlineKeyboardButton("我已订阅，重新检测", callback_data="retry_sub")])
        markup = InlineKeyboardMarkup(rows)
        sent = await update.message.reply_text("请先订阅频道后再使用机器人。", reply_markup=markup)
        schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        return

    draft = context.user_data.get("report_draft")
    if not draft or not draft.get("awaiting"):
        await update.message.reply_text("未识别操作，请使用底部菜单。")
        return

    key = draft["awaiting"]
    field_def = next((f for f in draft["template"]["fields"] if f["key"] == key), {})
    if field_def.get("type", "text") != "photo":
        await update.message.reply_text(
            f"请输入文字（不是图片），作为「{field_def.get('label', key)}」字段。"
        )
        return

    # Use the last (highest-resolution) photo variant provided by Telegram
    file_id = update.message.photo[-1].file_id
    draft["values"][key] = file_id
    draft["awaiting"] = ""
    sequential = draft.pop("sequential", False)

    if sequential:
        fields = draft["template"]["fields"]
        current_idx = next((i for i, f in enumerate(fields) if f["key"] == key), -1)
        next_idx = current_idx + 1
        # Delete previous prompt before sending next
        await _delete_prompt_message(context, draft)
        if next_idx < len(fields):
            next_field = fields[next_idx]
            draft["awaiting"] = next_field["key"]
            draft["sequential"] = True
            prompt, markup = _make_field_prompt(next_field, sequential=True)
            sent = await update.message.reply_text(prompt, reply_markup=markup)
            draft["prompt_msg_id"] = sent.message_id
            draft["prompt_chat_id"] = update.effective_chat.id
            schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
            return

    sent_preview = await update.message.reply_text(
        render_report_preview(draft["values"], draft["template"]),
        parse_mode=ParseMode.HTML,
        reply_markup=_report_submit_keyboard(),
    )
    schedule_auto_delete(context.bot, sent_preview.chat_id, sent_preview.message_id)


async def ptb_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    update_id = getattr(update, "update_id", None)
    user_id = None
    chat_id = None
    message_text = None
    callback_data = None
    if hasattr(update, "effective_user") and update.effective_user:  # type: ignore[union-attr]
        user_id = update.effective_user.id  # type: ignore[union-attr]
    if hasattr(update, "effective_chat") and update.effective_chat:  # type: ignore[union-attr]
        chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if hasattr(update, "message") and update.message:  # type: ignore[union-attr]
        message_text = getattr(update.message, "text", None)  # type: ignore[union-attr]
    if hasattr(update, "callback_query") and update.callback_query:  # type: ignore[union-attr]
        callback_data = getattr(update.callback_query, "data", None)  # type: ignore[union-attr]
    logger.error(
        "handler exception: update_id=%s user_id=%s chat_id=%s message_text=%r callback_data=%r error=%s",
        update_id,
        user_id,
        chat_id,
        message_text,
        callback_data,
        context.error,
        exc_info=context.error,
    )


def create_bot_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_error_handler(ptb_error_handler)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
