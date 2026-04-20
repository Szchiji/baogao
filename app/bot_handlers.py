import asyncio
import html
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
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
    get_user_reports,
    is_rate_limited_submission,
    is_user_banned,
    log_audit,
    setting_get,
    unban_user,
    upsert_user,
)
from app.database import db_connection
from app.keyboards import (
    _make_field_prompt,
    build_channel_link,
    get_force_sub_channels,
    get_push_channels,
    is_subscribed,
    keyboard_config,
    render_report_preview,
    report_fill_keyboard,
    report_template,
    start_inline_buttons,
    start_keyboard,
)
from app.utils import parse_json, safe_format, utc_now_iso

logger = logging.getLogger("report-bot")

_AUTO_DELETE_DELAY = 86400  # 24 hours in seconds


def _get_bot_id(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the bot_id for the current bot instance.

    The main bot uses '' (empty string); child bots use str(child_bot_db_id).
    """
    return context.bot_data.get("bot_id", "")



def _get_bot_admin_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    """Return the effective admin user IDs for the current bot instance.

    For child bots the ``bot_data["child_admin_id"]`` is the sole admin (the
    sub-admin / owner who registered this bot).  For the main bot this falls
    back to the ``ADMIN_USER_IDS`` environment variable.
    """
    child_admin_id = context.bot_data.get("child_admin_id")
    if child_admin_id is not None:
        return [int(child_admin_id)]
    return get_admin_user_ids()


def _is_bot_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Return True when *user_id* is an admin of the current bot instance."""
    return user_id in _get_bot_admin_ids(context)


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


async def _build_force_sub_prompt(bot: Bot, channels: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Build the subscription prompt message and inline keyboard for *channels*.

    For public channels a direct t.me link is used.  For private channels
    (numeric IDs) the bot fetches a Telegram invite link via the API so users
    get a real join button.  If the invite-link export fails (e.g. the bot is
    not an admin of that channel), the channel identifier is listed in the
    message text as a fallback.
    """
    rows: list[list[InlineKeyboardButton]] = []
    no_link_channels: list[str] = []
    for i, ch in enumerate(channels):
        link = build_channel_link(ch)
        if link is None:
            # Private / numeric channel — try to fetch an invite link from Telegram.
            try:
                link = await bot.export_chat_invite_link(chat_id=ch)
            except Exception:
                logger.warning(
                    "Could not export invite link for channel %s (bot may not be admin)", ch
                )
        if link:
            label = f"先去订阅频道 {i + 1}" if len(channels) > 1 else "先去订阅"
            rows.append([InlineKeyboardButton(label, url=link)])
        else:
            no_link_channels.append(ch)
    rows.append([InlineKeyboardButton("我已订阅，重新检测", callback_data="retry_sub")])
    markup = InlineKeyboardMarkup(rows)

    text = "请先订阅以下频道后再使用机器人。"
    if no_link_channels:
        ids_str = "、".join(no_link_channels)
        text += f"\n\n需订阅的频道：{ids_str}"
    return text, markup


async def send_start_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_id = _get_bot_id(context)
    text = setting_get("start_text", DEFAULT_SETTINGS["start_text"], bot_id=bot_id)
    media_type = setting_get("start_media_type", "", bot_id=bot_id).strip().lower()
    media_url = setting_get("start_media_url", "", bot_id=bot_id).strip()
    user_id = update.effective_user.id if update.effective_user else None
    # Use the per-bot admin_panel_url from bot_data (set for child bots), falling
    # back to the DB setting, then the global env var.
    bot_admin_url = (
        context.bot_data.get("admin_panel_url")
        or setting_get("admin_panel_url", bot_id=bot_id)
        or os.getenv("ADMIN_PANEL_URL", "")
    ).strip() or None
    inline_markup = start_inline_buttons(user_id=user_id, admin_panel_url=bot_admin_url, bot_id=bot_id)
    keyboard = start_keyboard(bot_id=bot_id)
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
    # Route invite-module start parameters before the normal baogao logic.
    message_text = (update.message.text or "") if update.message else ""
    start_param = message_text.split(" ", 1)[1].strip() if " " in message_text else ""
    if start_param.startswith("join_") or start_param.startswith("joinall_"):
        from app.invite.handlers import handle_invite_start
        await handle_invite_start(update, context, start_param)
        return

    user_id = update.effective_user.id
    bot_id = _get_bot_id(context)
    upsert_user(user_id, update.effective_user.username, bot_id=bot_id)
    if is_user_banned(user_id, bot_id=bot_id):
        await update.effective_chat.send_message("您已被限制使用此机器人。")
        return
    channels = get_force_sub_channels(bot_id=bot_id)
    if channels and not await is_subscribed(context.bot, user_id, bot_id=bot_id):
        text, markup = await _build_force_sub_prompt(context.bot, channels)
        sent = await update.effective_chat.send_message(text, reply_markup=markup)
        schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        return
    await send_start_content(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_bot_admin(context, update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    bot_id = _get_bot_id(context)
    base_url = (
        context.bot_data.get("admin_panel_url")
        or setting_get("admin_panel_url", bot_id=bot_id)
        or os.getenv("ADMIN_PANEL_URL", "")
    ).strip()
    if not base_url:
        await update.message.reply_text("未配置 ADMIN_PANEL_URL。")
        return
    _cleanup_verify_state()
    otp = secrets.token_urlsafe(16)
    child_admin_id = context.bot_data.get("child_admin_id")
    _otp_tokens[otp] = {
        "expiry": time.time() + _OTP_TOKEN_TTL,
        "owner_user_id": int(child_admin_id) if child_admin_id is not None else None,
        "bot_id": bot_id,
    }
    login_url = f"{base_url.rstrip('/')}/admin/otp?token={otp}"
    await update.message.reply_text(
        f"🔐 点击下方按钮登录管理后台（链接 {_OTP_TOKEN_TTL // 60} 分钟内有效）",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚀 打开管理后台", url=login_url)]]
        ),
    )


def start_report_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    template = report_template(bot_id=_get_bot_id(context))
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
    prompt, markup = _make_field_prompt(first_field, sequential=True, current_idx=0, total=len(fields))
    sent = await update.message.reply_text(
        f"📝 开始填写《{draft['template']['name']}》\n\n{prompt}",
        reply_markup=markup,
    )
    draft["prompt_msg_id"] = sent.message_id
    draft["prompt_chat_id"] = update.effective_chat.id
    schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)


_QUERY_PAGE_SIZE = 5


async def _do_query_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query_type: str,
    query_value: str,
    page: int,
    edit_message: Any = None,
) -> None:
    """Fetch and display one page of query results. *query_type* is 'a' (username) or 'h' (tag)."""
    offset = page * _QUERY_PAGE_SIZE
    limit = _QUERY_PAGE_SIZE

    if query_type == "a":
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, username, tag, created_at, channel_message_link
                FROM reports
                WHERE bot_id = %s AND status = 'approved' AND username ILIKE %s
                ORDER BY id DESC LIMIT %s OFFSET %s
                """,
                (_get_bot_id(context), query_value, limit + 1, offset),
            ).fetchall()
    else:
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, username, tag, created_at, channel_message_link
                FROM reports
                WHERE bot_id = %s AND status = 'approved' AND tag ILIKE %s
                ORDER BY id DESC LIMIT %s OFFSET %s
                """,
                (_get_bot_id(context), query_value, limit + 1, offset),
            ).fetchall()

    has_more = len(rows) > limit
    rows = list(rows)[:limit]

    if not rows:
        text = "未找到匹配报告。" if page == 0 else "没有更多结果了。"
    else:
        link_base = setting_get("report_link_base", "", bot_id=_get_bot_id(context)).strip()
        header = f"查询结果（第 {page + 1} 页）：" if page > 0 else "查询结果："
        lines = [header]
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
        text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    if page > 0:
        buttons.append([InlineKeyboardButton("← 上一页", callback_data=f"qp:{page - 1}:{query_type}:{query_value}")])
    if has_more:
        buttons.append([InlineKeyboardButton("下一页 →", callback_data=f"qp:{page + 1}:{query_type}:{query_value}")])
    markup = InlineKeyboardMarkup(buttons) if buttons else None

    if edit_message:
        try:
            await edit_message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup)


async def query_reports(text: str, bot_id: str = "") -> str:
    """Legacy single-page query; kept for plain-text fallback."""
    return setting_get("search_help_text", DEFAULT_SETTINGS["search_help_text"], bot_id=bot_id)


_MY_REPORTS_PAGE_SIZE = 5
_STATUS_LABELS = {"pending": "⏳ 待审核", "approved": "✅ 已通过", "rejected": "❌ 已驳回"}


async def my_reports_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    edit_message: Any = None,
) -> None:
    """Show the calling user's own submitted reports with pagination."""
    user_id = update.effective_user.id
    bot_id = _get_bot_id(context)
    reports = get_user_reports(user_id, offset=page * _MY_REPORTS_PAGE_SIZE, limit=_MY_REPORTS_PAGE_SIZE, bot_id=bot_id)
    has_more = len(reports) > _MY_REPORTS_PAGE_SIZE
    reports = reports[:_MY_REPORTS_PAGE_SIZE]

    if not reports and page == 0:
        text = "您还没有提交过报告。"
    else:
        lines = [f"📋 我的报告（第 {page + 1} 页）：" if page > 0 else "📋 我的报告："]
        for r in reports:
            status_label = _STATUS_LABELS.get(r["status"], r["status"])
            tag_str = f" {r['tag']}" if r.get("tag") else ""
            date_str = str(r.get("created_at", ""))[:10]
            lines.append(f"#{r['id']}{tag_str} {status_label} （{date_str}）")
            if r["status"] == "rejected" and r.get("review_feedback"):
                lines.append(f"  驳回原因：{r['review_feedback']}")
        text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    if page > 0:
        buttons.append([InlineKeyboardButton("← 上一页", callback_data=f"mrp:{page - 1}")])
    if has_more:
        buttons.append([InlineKeyboardButton("下一页 →", callback_data=f"mrp:{page + 1}")])
    markup = InlineKeyboardMarkup(buttons) if buttons else None

    if edit_message:
        try:
            await edit_message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup)


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
    bot_id = _get_bot_id(context)
    upsert_user(user.id, user.username, bot_id=bot_id)

    if is_user_banned(user.id, bot_id=bot_id):
        await update.message.reply_text("您已被限制使用此机器人。")
        return

    channels = get_force_sub_channels(bot_id=bot_id)
    if channels and not await is_subscribed(context.bot, update.effective_user.id, bot_id=bot_id):
        sub_text, markup = await _build_force_sub_prompt(context.bot, channels)
        sent = await update.message.reply_text(sub_text, reply_markup=markup)
        schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        return

    # Admin reject-reason flow: only when admin is NOT mid-draft to avoid ambiguity
    pending_reject_id = context.user_data.get("pending_reject_id")
    active_draft = context.user_data.get("report_draft")
    if (
        pending_reject_id is not None
        and _is_bot_admin(context, update.effective_user.id)
        and not (active_draft and active_draft.get("awaiting"))
    ):
        context.user_data.pop("pending_reject_id", None)
        reason = text
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s AND bot_id = %s", (pending_reject_id, bot_id)).fetchone()
            if not report:
                await update.message.reply_text("报告不存在。")
                return
            if report["status"] != "pending":
                await update.message.reply_text(f"报告已处于 {report['status']} 状态，无法驳回。")
                return
            conn.execute(
                "UPDATE reports SET status='rejected', review_feedback=%s, reviewed_at=%s WHERE id = %s AND bot_id = %s",
                (reason, utc_now_iso(), pending_reject_id, bot_id),
            )
        log_audit(update.effective_user.id, "reject", int(pending_reject_id), note=reason)
        tpl = (
            setting_get("review_rejected_template", "", bot_id=bot_id).strip()
            or DEFAULT_SETTINGS["review_rejected_template"]
        )
        feedback = safe_format(tpl, id=pending_reject_id, reason=reason)
        sent_feedback = await context.bot.send_message(
            chat_id=report["user_id"], text=feedback,
            reply_markup=_build_reject_markup(int(pending_reject_id)),
        )
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
                prev_key = key  # current field becomes the "previous" for the next prompt
                prompt, markup = _make_field_prompt(
                    next_field, sequential=True,
                    current_idx=next_idx, total=len(fields), prev_key=prev_key,
                )
                sent = await update.message.reply_text(prompt, reply_markup=markup)
                draft["prompt_msg_id"] = sent.message_id
                draft["prompt_chat_id"] = update.effective_chat.id
                schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
                return

        sent_preview = await update.message.reply_text(
            render_report_preview(draft["values"], draft["template"]),
            parse_mode=ParseMode.HTML,
            reply_markup=report_fill_keyboard(draft["values"], draft["template"]),
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
            if _is_bot_admin(context, user.id):
                otp = secrets.token_urlsafe(16)
                child_admin_id = context.bot_data.get("child_admin_id")
                _otp_tokens[otp] = {
                    "expiry": time.time() + _OTP_TOKEN_TTL,
                    "owner_user_id": int(child_admin_id) if child_admin_id is not None else None,
                    "bot_id": bot_id,
                }
                _verify_code_otps[text] = otp
                base_url = (
                    context.bot_data.get("admin_panel_url")
                    or setting_get("admin_panel_url", bot_id=bot_id)
                    or os.getenv("ADMIN_PANEL_URL", "")
                ).strip()
                if base_url:
                    await update.message.reply_text(f"✅ 身份验证成功！后台页面将自动跳转，请在 {_OTP_TOKEN_TTL // 60} 分钟内返回浏览器。")
                else:
                    await update.message.reply_text("✅ 验证成功，但未配置 ADMIN_PANEL_URL。")
            else:
                await update.message.reply_text("❌ 您不是管理员，访问请求已拒绝。")
            return

    if text.startswith("@"):
        await _do_query_page(update, context, "a", text[1:], 0)
        return

    if text.startswith("#"):
        await _do_query_page(update, context, "h", text, 0)
        return

    mapping = {item["text"]: item for item in keyboard_config(bot_id=bot_id)}
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
        await update.message.reply_text(setting_get("search_help_text", bot_id=bot_id))
    elif action == "my_reports":
        await my_reports_flow(update, context, page=0)
    elif action == "contact":
        await update.message.reply_text(setting_get("contact_text", bot_id=bot_id))
    elif action == "usage":
        await update.message.reply_text(setting_get("usage_text", bot_id=bot_id))
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

    bot_id = _get_bot_id(context)
    # Rate limiting: max 3 reports per hour
    if is_rate_limited_submission(update.effective_user.id, bot_id=bot_id):
        await update.effective_chat.send_message(
            "⚠️ 您提交报告过于频繁，请稍后再试（每小时最多 3 条）。"
        )
        return

    values = draft["values"]
    tag = values.get("tag", "")
    username = update.effective_user.username or ""
    template = draft["template"]
    try:
        with db_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO reports (bot_id, user_id, username, tag, data_json, status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (
                    bot_id,
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
    admin_ids = _get_bot_admin_ids(context)
    if admin_ids:
        preview = render_report_preview(values, template)
        submitter_id = update.effective_user.id
        link_base = setting_get("report_link_base", "", bot_id=bot_id).strip()
        detail_link = ""
        if link_base:
            detail_link = f"\n🔗 <a href='{html.escape(link_base.rstrip('/'))}/reports/{report_id}'>查看报告详情</a>"
        notification = (
            f"📋 新报告待审核 #{report_id}\n"
            f"用户：@{html.escape(username or '未知')}（ID: {submitter_id}）"
            f"{detail_link}\n\n"
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


def _build_approval_feedback(report_id: int, channel_link: str = "", bot_id: str = "") -> str:
    approved_tpl = (
        setting_get("review_approved_template", "", bot_id=bot_id).strip()
        or DEFAULT_SETTINGS["review_approved_template"]
    )
    # Prefer the real Telegram channel message link; fall back to report_link_base web URL
    if channel_link:
        link = channel_link
    else:
        link_base = setting_get("report_link_base", "", bot_id=bot_id).strip()
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


async def _push_report_to_channel(bot: Bot, report_id: int, report: dict, bot_id: str = "") -> str:
    """Push *report* to all configured push channels.  Returns the first channel message link (or '')."""
    push_channels = get_push_channels(bot_id=bot_id)
    if not push_channels:
        return ""
    data_values = parse_json(report["data_json"], {})
    link_base = setting_get("report_link_base", "", bot_id=bot_id).strip()
    link = f"{link_base.rstrip('/')}/reports/{report_id}" if link_base else ""
    # Build per-field placeholders (field key → value, excluding photo fields)
    tpl_fields = report_template(bot_id=bot_id)["fields"]
    field_labels = {f["key"]: f["label"] for f in tpl_fields}
    field_types = {f["key"]: f.get("type", "text") for f in tpl_fields}
    field_placeholders: dict[str, str] = {}
    for f in tpl_fields:
        k = f["key"]
        if field_types.get(k, "text") != "photo":
            field_placeholders[k] = data_values.get(k, "")
    # Build {detail}: honour push_detail_fields_json ordering if configured
    push_detail_keys = parse_json(setting_get("push_detail_fields_json", "[]", bot_id=bot_id), [])
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
    push_tpl = setting_get("push_template", DEFAULT_SETTINGS["push_template"], bot_id=bot_id)
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
            # Send photo fields to the channel after the text push (if enabled)
            if setting_get("push_photos_enabled", "1", bot_id=bot_id) == "1":
                for f in tpl_fields:
                    if field_types.get(f["key"], "text") == "photo":
                        photo_file_id = data_values.get(f["key"])
                        if photo_file_id:
                            try:
                                await bot.send_photo(
                                    chat_id=push_channel,
                                    photo=photo_file_id,
                                    caption=f"📷 {html.escape(f['label'])} — 报告 #{report_id}",
                                )
                            except Exception:
                                logger.warning(
                                    "failed to push photo field %s for report %s to channel %s",
                                    f["key"], report_id, push_channel, exc_info=True,
                                )
        except Exception:
            logger.warning("failed to push report %s to channel %s", report_id, push_channel, exc_info=True)
    return first_channel_link


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_bot_admin(context, update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    bot_id = _get_bot_id(context)
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at FROM reports WHERE bot_id = %s AND status = 'pending' ORDER BY id DESC LIMIT 20",
            (bot_id,),
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
    if not _is_bot_admin(context, update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    if not context.args:
        await update.message.reply_text("用法：/approve 报告ID")
        return
    report_id = context.args[0]
    bot_id = _get_bot_id(context)
    try:
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s AND bot_id = %s", (report_id, bot_id)).fetchone()
            if not report:
                await update.message.reply_text("报告不存在。")
                return
            conn.execute(
                "UPDATE reports SET status='approved', reviewed_at=%s WHERE id = %s",
                (utc_now_iso(), report_id),
            )
    except Exception:
        logger.exception("approve_cmd: invalid report_id=%s", report_id)
        await update.message.reply_text("报告ID格式无效。")
        return
    log_audit(update.effective_user.id, "approve", int(report_id))
    await update.message.reply_text(f"报告 #{report_id} 已通过。")

    channel_link = await _push_report_to_channel(context.bot, report_id, report, bot_id=bot_id)
    feedback = _build_approval_feedback(report_id, channel_link=channel_link, bot_id=bot_id)
    await context.bot.send_message(chat_id=report["user_id"], text=feedback)


def _build_reject_markup(report_id: int) -> InlineKeyboardMarkup:
    """Return an inline keyboard with a re-edit button for rejected reports."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 重新编辑", callback_data=f"reedit:{report_id}")]
    ])


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_bot_admin(context, update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    if not context.args:
        await update.message.reply_text("用法：/reject 报告ID 原因")
        return
    report_id = context.args[0]
    reason = " ".join(context.args[1:]).strip() or "请联系管理员"
    bot_id = _get_bot_id(context)
    try:
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s AND bot_id = %s", (report_id, bot_id)).fetchone()
            if not report:
                await update.message.reply_text("报告不存在。")
                return
            conn.execute(
                "UPDATE reports SET status='rejected', review_feedback=%s, reviewed_at=%s WHERE id = %s",
                (reason, utc_now_iso(), report_id),
            )
    except Exception:
        logger.exception("reject_cmd: invalid report_id=%s", report_id)
        await update.message.reply_text("报告ID格式无效。")
        return
    log_audit(update.effective_user.id, "reject", int(report_id), note=reason)
    tpl = (
        setting_get("review_rejected_template", "", bot_id=bot_id).strip()
        or DEFAULT_SETTINGS["review_rejected_template"]
    )
    feedback = safe_format(tpl, id=report_id, reason=reason)
    await context.bot.send_message(
        chat_id=report["user_id"], text=feedback,
        reply_markup=_build_reject_markup(int(report_id)),
    )
    await update.message.reply_text(f"报告 #{report_id} 已驳回。")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_bot_admin(context, update.effective_user.id):
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
    ban_user(target_id, None, reason, bot_id=_get_bot_id(context))
    await update.message.reply_text(f"用户 {target_id} 已加入黑名单（原因：{reason}）。")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_bot_admin(context, update.effective_user.id):
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
    unban_user(target_id, bot_id=_get_bot_id(context))
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
        if await is_subscribed(context.bot, update.effective_user.id, bot_id=_get_bot_id(context)):
            sent_ok = await query.message.reply_text("订阅检测通过。")
            schedule_auto_delete(context.bot, sent_ok.chat_id, sent_ok.message_id)
            await send_start_content(update, context)
        else:
            sent_fail = await query.message.reply_text("检测失败，请确认订阅后重试。")
            schedule_auto_delete(context.bot, sent_fail.chat_id, sent_fail.message_id)
        return

    # ---- My reports pagination ----
    if data.startswith("mrp:"):
        await query.answer()
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await my_reports_flow(update, context, page=page, edit_message=query.message)
        return

    # ---- Query results pagination ----
    if data.startswith("qp:"):
        await query.answer()
        # format: qp:PAGE:TYPE:VALUE
        parts = data.split(":", 3)
        if len(parts) == 4:
            try:
                page = int(parts[1])
            except ValueError:
                page = 0
            query_type = parts[2]
            query_value = parts[3]
            await _do_query_page(update, context, query_type, query_value, page, edit_message=query.message)
        return

    # ---- Re-edit rejected report ----
    if data.startswith("reedit:"):
        await query.answer()
        report_id_str = data.split(":", 1)[1]
        user_id = update.effective_user.id
        bot_id = _get_bot_id(context)
        with db_connection() as conn:
            report = conn.execute(
                "SELECT * FROM reports WHERE id = %s AND bot_id = %s AND user_id = %s AND status = 'rejected'",
                (report_id_str, bot_id, user_id),
            ).fetchone()
        if not report:
            await query.message.reply_text("报告不存在或无权重新编辑。")
            return
        draft = start_report_draft(context)
        old_values = parse_json(report["data_json"], {})
        draft["values"] = {k: v for k, v in old_values.items() if isinstance(v, str)}
        fields = draft["template"]["fields"]
        if fields:
            first_field = fields[0]
            draft["awaiting"] = first_field["key"]
            draft["sequential"] = True
            prompt, markup = _make_field_prompt(
                first_field, sequential=True, current_idx=0, total=len(fields)
            )
            sent = await query.message.reply_text(
                f"📝 重新编辑报告 #{report_id_str}（当前值已预填，直接修改或跳过）\n\n{prompt}",
                reply_markup=markup,
            )
            draft["prompt_msg_id"] = sent.message_id
            draft["prompt_chat_id"] = query.message.chat_id
            schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        return

    # ---- Back button in sequential field flow ----
    if data.startswith("back_field:"):
        await query.answer()
        target_key = data.split(":", 1)[1]
        draft = context.user_data.get("report_draft")
        if not draft:
            return
        fields = draft["template"]["fields"]
        target_idx = next((i for i, f in enumerate(fields) if f["key"] == target_key), -1)
        if target_idx < 0:
            return
        target_field = fields[target_idx]
        prev_key = fields[target_idx - 1]["key"] if target_idx > 0 else None
        draft["awaiting"] = target_key
        draft["sequential"] = True
        await _delete_prompt_message(context, draft)
        prompt, markup = _make_field_prompt(
            target_field, sequential=True,
            current_idx=target_idx, total=len(fields), prev_key=prev_key,
        )
        sent = await query.message.reply_text(prompt, reply_markup=markup)
        draft["prompt_msg_id"] = sent.message_id
        draft["prompt_chat_id"] = query.message.chat_id
        schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
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
            prev_key = key
            prompt, markup = _make_field_prompt(
                next_field, sequential=True,
                current_idx=next_idx, total=len(fields), prev_key=prev_key,
            )
            sent = await query.message.reply_text(prompt, reply_markup=markup)
            draft["prompt_msg_id"] = sent.message_id
            draft["prompt_chat_id"] = query.message.chat_id
            schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
        else:
            sent_preview = await query.message.reply_text(
                render_report_preview(draft["values"], draft["template"]),
                parse_mode=ParseMode.HTML,
                reply_markup=report_fill_keyboard(draft["values"], draft["template"]),
            )
            schedule_auto_delete(context.bot, sent_preview.chat_id, sent_preview.message_id)
        return

    if data == "submit_report":
        await query.answer()
        bot_id = _get_bot_id(context)
        if is_user_banned(update.effective_user.id, bot_id=bot_id):
            await query.message.reply_text("您已被限制使用此机器人。")
            return
        if get_force_sub_channels(bot_id=bot_id) and not await is_subscribed(context.bot, update.effective_user.id, bot_id=bot_id):
            await query.message.reply_text("请先订阅频道后再提交报告。")
            return
        await submit_report(context, update)
        return

    if data.startswith("approve:"):
        if not _is_bot_admin(context, update.effective_user.id):
            await query.answer("无权限。", show_alert=True)
            return
        report_id = data.split(":", 1)[1]
        bot_id = _get_bot_id(context)
        try:
            with db_connection() as conn:
                report = conn.execute("SELECT * FROM reports WHERE id = %s AND bot_id = %s", (report_id, bot_id)).fetchone()
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
        except Exception:
            logger.exception("approve callback: invalid report_id=%s", report_id)
            await query.answer("无效的报告ID。", show_alert=True)
            return
        log_audit(update.effective_user.id, "approve", int(report_id))
        await query.answer("已通过。")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        sent_admin = await query.message.reply_text(f"✅ 报告 #{report_id} 已通过审核。")
        schedule_auto_delete(context.bot, sent_admin.chat_id, sent_admin.message_id)
        channel_link = await _push_report_to_channel(context.bot, report_id, report, bot_id=bot_id)
        feedback = _build_approval_feedback(report_id, channel_link=channel_link, bot_id=bot_id)
        sent_user = await context.bot.send_message(chat_id=report["user_id"], text=feedback)
        schedule_auto_delete(context.bot, sent_user.chat_id, sent_user.message_id)
        return

    if data.startswith("reject:"):
        if not _is_bot_admin(context, update.effective_user.id):
            await query.answer("无权限。", show_alert=True)
            return
        report_id = data.split(":", 1)[1]
        bot_id = _get_bot_id(context)
        try:
            with db_connection() as conn:
                report = conn.execute("SELECT * FROM reports WHERE id = %s AND bot_id = %s", (report_id, bot_id)).fetchone()
        except Exception:
            logger.exception("reject callback: invalid report_id=%s", report_id)
            await query.answer("无效的报告ID。", show_alert=True)
            return
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
    bot_id = _get_bot_id(context)
    upsert_user(user.id, user.username, bot_id=bot_id)

    if is_user_banned(user.id, bot_id=bot_id):
        await update.message.reply_text("您已被限制使用此机器人。")
        return

    channels = get_force_sub_channels(bot_id=bot_id)
    if channels and not await is_subscribed(context.bot, user.id, bot_id=bot_id):
        sub_text, markup = await _build_force_sub_prompt(context.bot, channels)
        sent = await update.message.reply_text(sub_text, reply_markup=markup)
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
            prev_key = key
            prompt, markup = _make_field_prompt(
                next_field, sequential=True,
                current_idx=next_idx, total=len(fields), prev_key=prev_key,
            )
            sent = await update.message.reply_text(prompt, reply_markup=markup)
            draft["prompt_msg_id"] = sent.message_id
            draft["prompt_chat_id"] = update.effective_chat.id
            schedule_auto_delete(context.bot, sent.chat_id, sent.message_id)
            return

    sent_preview = await update.message.reply_text(
        render_report_preview(draft["values"], draft["template"]),
        parse_mode=ParseMode.HTML,
        reply_markup=report_fill_keyboard(draft["values"], draft["template"]),
    )
    schedule_auto_delete(context.bot, sent_preview.chat_id, sent_preview.message_id)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_bot_admin(context, update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    bot_id = _get_bot_id(context)
    with db_connection() as conn:
        user_count_row = conn.execute("SELECT COUNT(*) as cnt FROM users WHERE bot_id = %s", (bot_id,)).fetchone()
        report_counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM reports WHERE bot_id = %s GROUP BY status",
            (bot_id,),
        ).fetchall()
    total_users = user_count_row["cnt"] if user_count_row else 0
    counts = {r["status"]: r["cnt"] for r in report_counts}
    total_reports = sum(counts.values())
    pending = counts.get("pending", 0)
    approved = counts.get("approved", 0)
    rejected = counts.get("rejected", 0)
    await update.message.reply_text(
        f"📊 统计数据\n\n"
        f"👥 用户：{total_users} 人\n"
        f"📋 报告总数：{total_reports}\n"
        f"  ⏳ 待审核：{pending}\n"
        f"  ✅ 已通过：{approved}\n"
        f"  ❌ 已驳回：{rejected}"
    )


_PENDING_REMINDER_INTERVAL = 7200    # 2 hours
_PENDING_REMINDER_FIRST = 300         # 5 minutes after start
_AUTO_CLEANUP_INTERVAL = 86400        # 24 hours
_AUTO_CLEANUP_FIRST = 3600            # 1 hour after start


async def _pending_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: notify admins of reports pending for longer than the configured threshold."""
    bot_id = _get_bot_id(context)
    try:
        threshold_hours = int(setting_get("pending_reminder_threshold_hours", "24", bot_id=bot_id))
    except (ValueError, TypeError):
        threshold_hours = 24
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=threshold_hours)).isoformat()
    try:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT id, username FROM reports WHERE bot_id = %s AND status='pending' AND created_at < %s ORDER BY id DESC",
                (bot_id, cutoff),
            ).fetchall()
    except Exception:
        logger.warning("pending_reminder_job: DB query failed", exc_info=True)
        return
    if not rows:
        return
    count = len(rows)
    ids_str = "、".join(f"#{r['id']}" for r in rows[:5])
    if count > 5:
        ids_str += f" 等共 {count} 条"
    msg = (
        f"⏰ 提醒：有 {count} 条报告待审核超过 {threshold_hours} 小时\n"
        f"{ids_str}\n\n"
        f"请前往管理后台或使用 /pending 命令处理。"
    )
    for admin_id in _get_bot_admin_ids(context):
        try:
            await context.bot.send_message(chat_id=admin_id, text=msg)
        except Exception:
            logger.warning("pending_reminder_job: failed to notify admin %s", admin_id, exc_info=True)


async def _auto_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: delete rejected reports older than 90 days."""
    bot_id = _get_bot_id(context)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    try:
        with db_connection() as conn:
            cur = conn.execute(
                "DELETE FROM reports WHERE bot_id = %s AND status='rejected' AND created_at < %s",
                (bot_id, cutoff),
            )
            deleted = cur.rowcount
    except Exception:
        logger.warning("auto_cleanup_job: DB error", exc_info=True)
        return
    if deleted:
        logger.info("auto_cleanup_job: deleted %d old rejected reports (bot_id=%r)", deleted, bot_id)


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


def create_bot_application(
    token: str,
    owner_user_id: int | None = None,
    admin_panel_url: str = "",
    bot_id: str = "",
) -> Application:
    app = Application.builder().token(token).build()
    # Store the bot_id ('' for main bot, str(child_bot.id) for child bots) so
    # all handlers can scope their DB operations to this bot's data partition.
    if bot_id:
        app.bot_data["bot_id"] = bot_id
    # For child bots, store the sub-admin's Telegram user ID so that
    # _is_bot_admin / _get_bot_admin_ids can restrict admin commands to them only.
    if owner_user_id is not None:
        app.bot_data["child_admin_id"] = owner_user_id
    # Store the per-bot admin panel URL so that /admin and the start inline
    # button direct the sub-admin to the correct admin panel instance.
    if admin_panel_url:
        app.bot_data["admin_panel_url"] = admin_panel_url
    app.add_error_handler(ptb_error_handler)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Register invite link management handlers (uses group=-1 for higher priority)
    from app.invite.handlers import register_invite_handlers
    register_invite_handlers(app)

    if app.job_queue is not None:
        # Read the reminder check interval from DB (or env) at startup; default 2 h
        try:
            _reminder_interval_hours = int(setting_get("pending_reminder_interval_hours", str(_PENDING_REMINDER_INTERVAL // 3600), bot_id=bot_id))
        except (ValueError, TypeError):
            _reminder_interval_hours = _PENDING_REMINDER_INTERVAL // 3600
        _reminder_interval_hours = max(1, _reminder_interval_hours)
        reminder_interval_seconds = _reminder_interval_hours * 3600
        # Remind admins about pending reports (start after 5 min)
        app.job_queue.run_repeating(
            _pending_reminder_job, interval=reminder_interval_seconds, first=_PENDING_REMINDER_FIRST
        )
        # Clean up old rejected reports once a day (start after 1 hour)
        app.job_queue.run_repeating(
            _auto_cleanup_job, interval=_AUTO_CLEANUP_INTERVAL, first=_AUTO_CLEANUP_FIRST
        )
    else:
        logger.warning(
            "APScheduler not available — periodic jobs disabled. "
            "Install apscheduler to enable pending reminders and auto-cleanup."
        )

    return app
