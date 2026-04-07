import asyncio
import html
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
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


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("report-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


DB_PATH = Path(os.getenv("DB_PATH", "baogao.db"))

DEFAULT_SETTINGS: dict[str, str] = {
    "force_sub_channel": "",
    "push_channel": "",
    "start_text": "欢迎使用报告机器人，请通过底部菜单开始操作。",
    "start_media_type": "",
    "start_media_url": "",
    "start_buttons_json": json.dumps(
        [{"text": "管理后台", "url": "https://example.com/admin"}], ensure_ascii=False
    ),
    "keyboard_buttons_json": json.dumps(
        [
            {"text": "写报告", "action": "write_report"},
            {"text": "查阅报告", "action": "search_help"},
            {"text": "联系管理员", "action": "contact"},
            {"text": "操作方式", "action": "usage"},
        ],
        ensure_ascii=False,
    ),
    "review_approved_template": "✅ 报告 #{id} 审核通过。",
    "review_rejected_template": "❌ 报告 #{id} 审核未通过：{reason}",
    "report_template_json": json.dumps(
        {
            "name": "默认模板",
            "fields": [
                {"key": "title", "label": "标题"},
                {"key": "content", "label": "内容"},
                {"key": "tag", "label": "标签（例如 #日报）"},
            ],
        },
        ensure_ascii=False,
    ),
    "contact_text": "请联系管理员处理。",
    "usage_text": "1. 点击“写报告”填写模板\n2. 填完后提交审核\n3. 审核通过后可查阅。",
    "search_help_text": "发送 @用户名 或 #标签 查询报告。",
    "report_link_base": "",
}


@dataclass
class AppConfig:
    token: str
    mode: str
    webhook_url: str
    webhook_path: str
    host: str
    port: int
    webhook_secret: str
    admin_panel_token: str
    admin_panel_url: str


def load_config() -> AppConfig:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")
    return AppConfig(
        token=token,
        mode=os.getenv("BOT_MODE", "webhook").strip().lower(),
        webhook_url=os.getenv("WEBHOOK_URL", "").strip(),
        webhook_path=os.getenv("WEBHOOK_PATH", "/webhook").strip(),
        host=os.getenv("HOST", "0.0.0.0").strip(),
        port=int(os.getenv("PORT", "8000")),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        admin_panel_token=os.getenv("ADMIN_PANEL_TOKEN", "").strip(),
        admin_panel_url=os.getenv("ADMIN_PANEL_URL", "").strip(),
    )


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              username TEXT,
              tag TEXT,
              data_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              review_feedback TEXT,
              created_at TEXT NOT NULL,
              reviewed_at TEXT
            )
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )


def setting_get(key: str, default: str = "") -> str:
    with db_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def setting_set(key: str, value: str) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def parse_json(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def keyboard_config() -> list[dict[str, str]]:
    items = parse_json(setting_get("keyboard_buttons_json"), [])
    normalized: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            normalized.append({"text": item, "action": "text"})
            continue
        if isinstance(item, dict) and item.get("text"):
            normalized.append(
                {
                    "text": str(item.get("text")),
                    "action": str(item.get("action", "text")),
                    "value": str(item.get("value", "")),
                }
            )
    return normalized


def report_template() -> dict[str, Any]:
    data = parse_json(setting_get("report_template_json"), {})
    if not isinstance(data, dict):
        return {"name": "模板", "fields": []}
    fields = data.get("fields", [])
    if not isinstance(fields, list):
        fields = []
    valid_fields = []
    for field in fields:
        if isinstance(field, dict) and field.get("key") and field.get("label"):
            valid_fields.append({"key": str(field["key"]), "label": str(field["label"])})
    return {"name": str(data.get("name", "模板")), "fields": valid_fields}


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    channel = setting_get("force_sub_channel", "").strip()
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
    except Exception:
        logger.warning("subscription check failed for %s", user_id, exc_info=True)
        return False
    return member.status not in {"left", "kicked"}


def start_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(item["text"])] for item in keyboard_config()]
    if not rows:
        rows = [[KeyboardButton("写报告")], [KeyboardButton("查阅报告")]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _is_admin_entry_button(text: str, url: str) -> bool:
    lowered_text = text.strip().lower()
    if lowered_text in {"管理后台", "admin panel"}:
        return True
    parsed = urlparse(url.strip())
    path = parsed.path.strip().lower()
    if path == "/admin":
        return True
    return path.startswith("/admin/")


def start_inline_buttons(user_id: int | None = None) -> InlineKeyboardMarkup | None:
    raw_buttons = parse_json(setting_get("start_buttons_json"), [])
    is_admin = user_id is not None and is_user_admin(user_id)
    admin_panel_url = os.getenv("ADMIN_PANEL_URL", "").strip()
    buttons: list[list[InlineKeyboardButton]] = []
    for item in raw_buttons:
        if isinstance(item, dict) and item.get("text") and item.get("url"):
            text = str(item["text"])
            url = str(item["url"])
            if _is_admin_entry_button(text, url):
                if not is_admin or not admin_panel_url:
                    continue
                url = _normalize_admin_url(admin_panel_url)
            buttons.append([InlineKeyboardButton(text, url=url)])
    return InlineKeyboardMarkup(buttons) if buttons else None


def render_report_preview(values: dict[str, str], template: dict[str, Any]) -> str:
    lines = [f"📝 <b>{template['name']}</b>", ""]
    for field in template["fields"]:
        key = field["key"]
        label = field["label"]
        value = values.get(key, "（未填写）")
        lines.append(f"<b>{label}</b>：{value}")
    return "\n".join(lines)


def report_fill_keyboard(values: dict[str, str], template: dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    for field in template["fields"]:
        key = field["key"]
        done = "✅ " if values.get(key) else ""
        buttons.append([InlineKeyboardButton(f"{done}填写 {field['label']}", callback_data=f"fill:{key}")])
    buttons.append([InlineKeyboardButton("提交审核", callback_data="submit_report")])
    return InlineKeyboardMarkup(buttons)


def get_admin_user_ids() -> list[int]:
    raw = os.getenv("ADMIN_USER_IDS", "")
    if not raw:
        return []
    ids: list[int] = []
    for value in raw.split(","):
        item = value.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError:
            logger.warning("invalid ADMIN_USER_IDS entry ignored: %s", item)
    return ids


def is_user_admin(user_id: int) -> bool:
    return user_id in get_admin_user_ids()


def _normalize_admin_url(base_url: str) -> str:
    """Return the /admin URL, avoiding a duplicate /admin suffix."""
    base = base_url.rstrip("/")
    if base.endswith("/admin"):
        return base
    return f"{base}/admin"


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


def build_channel_link(channel: str) -> str | None:
    value = channel.strip()
    if not value:
        return None
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    if value.startswith("https://t.me/"):
        return value
    # Private/supergroup chat IDs are numeric and commonly start with -100, no public t.me link.
    if value.lstrip("-").isdigit() or value.startswith("-100"):
        return None
    return f"https://t.me/{value}"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    channel = setting_get("force_sub_channel", "").strip()
    if channel and not await is_subscribed(context.bot, user_id):
        rows = [[InlineKeyboardButton("我已订阅，重新检测", callback_data="retry_sub")]]
        channel_link = build_channel_link(channel)
        if channel_link:
            rows.insert(0, [InlineKeyboardButton("先去订阅", url=channel_link)])
        markup = InlineKeyboardMarkup(rows)
        await update.effective_chat.send_message("请先订阅频道后再使用机器人。", reply_markup=markup)
        return
    await send_start_content(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    base_url = context.bot_data.get("admin_panel_url") or setting_get("admin_panel_url")
    if not base_url:
        await update.message.reply_text("未配置 ADMIN_PANEL_URL。")
        return
    url = _normalize_admin_url(base_url)
    button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("打开管理后台", web_app=WebAppInfo(url=url))]]
    )
    await update.message.reply_text("点击进入管理后台：", reply_markup=button)


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
    await update.message.reply_text(
        f"📝 开始填写《{draft['template']['name']}》\n\n请输入{first_field['label']}："
    )


async def query_reports(text: str) -> str:
    if text.startswith("@"):
        username = text[1:]
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, username, tag, data_json, created_at
                FROM reports
                WHERE status = 'approved' AND username = ?
                ORDER BY id DESC LIMIT 10
                """,
                (username,),
            ).fetchall()
    elif text.startswith("#"):
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, username, tag, data_json, created_at
                FROM reports
                WHERE status = 'approved' AND tag = ?
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
        link = f"{link_base.rstrip('/')}/reports/{row['id']}" if link_base else f"报告ID: {row['id']}"
        lines.append(
            f"- #{row['id']} @{row['username'] or 'unknown'} {row['tag'] or ''}\n  {link}"
        )
    return "\n".join(lines)


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

    channel = setting_get("force_sub_channel", "").strip()
    if channel and not await is_subscribed(context.bot, update.effective_user.id):
        await update.message.reply_text("请先完成频道订阅后再使用。")
        return

    # Admin reject-reason flow: if an admin sent a reject reason, process it
    pending_reject_id = context.user_data.get("pending_reject_id")
    if pending_reject_id is not None and is_user_admin(update.effective_user.id):
        context.user_data.pop("pending_reject_id", None)
        reason = text
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = ?", (pending_reject_id,)).fetchone()
            if not report:
                await update.message.reply_text("报告不存在。")
                return
            if report["status"] != "pending":
                await update.message.reply_text(f"报告已处于 {report['status']} 状态，无法驳回。")
                return
            conn.execute(
                "UPDATE reports SET status='rejected', review_feedback=?, reviewed_at=? WHERE id = ?",
                (reason, utc_now_iso(), pending_reject_id),
            )
        tpl = setting_get("review_rejected_template", DEFAULT_SETTINGS["review_rejected_template"])
        feedback = tpl.format(id=pending_reject_id, reason=reason)
        await context.bot.send_message(chat_id=report["user_id"], text=feedback)
        await update.message.reply_text(f"报告 #{pending_reject_id} 已驳回。")
        return

    draft = context.user_data.get("report_draft")
    if draft and draft.get("awaiting"):
        key = draft["awaiting"]
        draft["values"][key] = text
        draft["awaiting"] = ""
        sequential = draft.pop("sequential", False)

        if sequential:
            fields = draft["template"]["fields"]
            current_idx = next((i for i, f in enumerate(fields) if f["key"] == key), -1)
            next_idx = current_idx + 1
            if next_idx < len(fields):
                next_field = fields[next_idx]
                draft["awaiting"] = next_field["key"]
                draft["sequential"] = True
                await update.message.reply_text(f"请输入{next_field['label']}：")
                return

        await update.message.reply_text(
            render_report_preview(draft["values"], draft["template"]),
            parse_mode=ParseMode.HTML,
            reply_markup=report_fill_keyboard(draft["values"], draft["template"]),
        )
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
        await update.effective_chat.send_message("请先点击“写报告”。")
        return
    required_fields = [f["key"] for f in draft["template"]["fields"]]
    missing = [k for k in required_fields if not draft["values"].get(k, "").strip()]
    if missing:
        await update.effective_chat.send_message("仍有未填写项，请继续完善。")
        return
    values = draft["values"]
    tag = values.get("tag", "")
    username = update.effective_user.username or ""
    template = draft["template"]
    with db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO reports (user_id, username, tag, data_json, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                update.effective_user.id,
                username,
                tag,
                json.dumps(values, ensure_ascii=False),
                utc_now_iso(),
            ),
        )
        report_id = cur.lastrowid
    context.user_data.pop("report_draft", None)
    await update.effective_chat.send_message(f"✅ 报告 #{report_id} 已提交，等待审核。")

    # Notify all admins with inline approve/reject buttons
    admin_ids = get_admin_user_ids()
    if admin_ids:
        preview = render_report_preview(values, template)
        notification = f"📋 新报告待审核 #{report_id}\n用户：@{username or '未知'}\n\n{preview}"
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
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=notification,
                    parse_mode=ParseMode.HTML,
                    reply_markup=review_buttons,
                )
            except Exception:
                logger.warning(
                    "failed to notify admin %s about report %s", admin_id, report_id, exc_info=True
                )


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
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            await update.message.reply_text("报告不存在。")
            return
        conn.execute(
            "UPDATE reports SET status='approved', reviewed_at=? WHERE id = ?",
            (utc_now_iso(), report_id),
        )
    await update.message.reply_text(f"报告 #{report_id} 已通过。")

    approved_tpl = setting_get("review_approved_template", DEFAULT_SETTINGS["review_approved_template"])
    feedback = approved_tpl.format(id=report_id)
    await context.bot.send_message(chat_id=report["user_id"], text=feedback)

    push_channel = setting_get("push_channel", "").strip()
    if push_channel:
        data = parse_json(report["data_json"], {})
        detail = "\n".join([f"{k}: {v}" for k, v in data.items()])
        await context.bot.send_message(
            chat_id=push_channel,
            text=f"📢 审核通过报告 #{report_id}\n@{report['username'] or 'unknown'}\n{detail}",
        )


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
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            await update.message.reply_text("报告不存在。")
            return
        conn.execute(
            "UPDATE reports SET status='rejected', review_feedback=?, reviewed_at=? WHERE id = ?",
            (reason, utc_now_iso(), report_id),
        )
    tpl = setting_get("review_rejected_template", DEFAULT_SETTINGS["review_rejected_template"])
    feedback = tpl.format(id=report_id, reason=reason)
    await context.bot.send_message(chat_id=report["user_id"], text=feedback)
    await update.message.reply_text(f"报告 #{report_id} 已驳回。")


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
            await query.message.reply_text("订阅检测通过。")
            await send_start_content(update, context)
        else:
            await query.message.reply_text("检测失败，请确认订阅后重试。")
        return

    draft = context.user_data.get("report_draft")
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
        await query.message.reply_text(f"请输入{field['label']}：")
        return

    if data == "submit_report":
        await query.answer()
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
            report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                await query.answer("报告不存在。", show_alert=True)
                return
            if report["status"] != "pending":
                await query.answer(f"报告已处于 {report['status']} 状态。", show_alert=True)
                return
            conn.execute(
                "UPDATE reports SET status='approved', reviewed_at=? WHERE id = ?",
                (utc_now_iso(), report_id),
            )
        await query.answer("已通过。")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(f"✅ 报告 #{report_id} 已通过审核。")
        approved_tpl = setting_get("review_approved_template", DEFAULT_SETTINGS["review_approved_template"])
        feedback = approved_tpl.format(id=report_id)
        await context.bot.send_message(chat_id=report["user_id"], text=feedback)
        push_channel = setting_get("push_channel", "").strip()
        if push_channel:
            data_values = parse_json(report["data_json"], {})
            detail = "\n".join([f"{k}: {v}" for k, v in data_values.items()])
            await context.bot.send_message(
                chat_id=push_channel,
                text=f"📢 审核通过报告 #{report_id}\n@{report['username'] or 'unknown'}\n{detail}",
            )
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
            report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            await query.answer("报告不存在。", show_alert=True)
            return
        if report["status"] != "pending":
            await query.answer(f"报告已处于 {report['status']} 状态。", show_alert=True)
            return
        context.user_data["pending_reject_id"] = report_id
        await query.answer()
        await query.message.reply_text(f"请输入驳回报告 #{report_id} 的原因：")
        return


def report_to_html(report_row: sqlite3.Row) -> str:
    data = parse_json(report_row["data_json"], {})
    lines = [f"<h1>报告 #{report_row['id']}</h1>"]
    lines.append(f"<p>状态：{report_row['status']}</p>")
    lines.append(f"<p>用户：@{report_row['username'] or 'unknown'}</p>")
    lines.append("<ul>")
    for k, v in data.items():
        lines.append(f"<li><b>{k}</b>：{v}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def build_admin_html(settings_map: dict[str, str], pending_reports: list[dict] | None = None) -> str:
    def e(key: str) -> str:
        return html.escape(settings_map.get(key, ""))

    if pending_reports:
        rows_html = "".join(
            f"<tr>"
            f"<td>#{r['id']}</td>"
            f"<td>@{html.escape(r['username'] or 'unknown')}</td>"
            f"<td>{html.escape(r['created_at'])}</td>"
            f"<td>"
            f"<form method='post' action='/admin/approve/{r['id']}' style='display:inline'>"
            f"<button type='submit'>✅ 通过</button></form> "
            f"<form method='post' action='/admin/reject/{r['id']}' style='display:inline'>"
            f"<input name='reason' placeholder='驳回原因' required style='width:160px'>"
            f"<button type='submit'>❌ 驳回</button></form>"
            f"</td>"
            f"</tr>"
            for r in pending_reports
        )
        pending_section = (
            "<hr><h3>待审核报告（" + str(len(pending_reports)) + "）</h3>"
            "<table border='1' cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr><th>ID</th><th>用户</th><th>提交时间</th><th>操作</th></tr>"
            + rows_html
            + "</table>"
        )
    else:
        pending_section = "<hr><p>暂无待审核报告。</p>"

    return f"""
    <html><body style="font-family: sans-serif; max-width: 900px; margin: 24px auto;">
    <h2>报告机器人管理后台</h2>
    <form method="post" action="/admin/save">
      <label>强制订阅频道（@channel）</label><br>
      <input name="force_sub_channel" value="{e('force_sub_channel')}" style="width:100%"><br><br>
      <label>报告推送频道（@channel）</label><br>
      <input name="push_channel" value="{e('push_channel')}" style="width:100%"><br><br>
      <label>/start 文本</label><br>
      <textarea name="start_text" style="width:100%;height:90px">{e('start_text')}</textarea><br><br>
      <label>/start 媒体类型（photo/video）</label><br>
      <input name="start_media_type" value="{e('start_media_type')}" style="width:100%"><br><br>
      <label>/start 媒体URL</label><br>
      <input name="start_media_url" value="{e('start_media_url')}" style="width:100%"><br><br>
      <label>/start 按钮 JSON（数组）</label><br>
      <textarea name="start_buttons_json" style="width:100%;height:120px">{e('start_buttons_json')}</textarea><br><br>
      <label>底部键盘 JSON（数组）</label><br>
      <textarea name="keyboard_buttons_json" style="width:100%;height:120px">{e('keyboard_buttons_json')}</textarea><br><br>
      <label>审核通过反馈模板</label><br>
      <input name="review_approved_template" value="{e('review_approved_template')}" style="width:100%"><br><br>
      <label>审核驳回反馈模板</label><br>
      <input name="review_rejected_template" value="{e('review_rejected_template')}" style="width:100%"><br><br>
      <label>报告模板 JSON（对象）</label><br>
      <textarea name="report_template_json" style="width:100%;height:160px">{e('report_template_json')}</textarea><br><br>
      <label>联系管理员文本</label><br>
      <textarea name="contact_text" style="width:100%;height:70px">{e('contact_text')}</textarea><br><br>
      <label>操作方式文本</label><br>
      <textarea name="usage_text" style="width:100%;height:70px">{e('usage_text')}</textarea><br><br>
      <label>查阅报告帮助文本</label><br>
      <textarea name="search_help_text" style="width:100%;height:70px">{e('search_help_text')}</textarea><br><br>
      <label>报告链接基地址（例如 https://domain.com）</label><br>
      <input name="report_link_base" value="{e('report_link_base')}" style="width:100%"><br><br>
      <button type="submit">保存配置</button>
    </form>
    {pending_section}
    </body></html>
    """


async def ptb_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "handler exception for update %r: %s",
        update,
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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def create_fastapi(application: Application, config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await application.initialize()
        await application.start()
        if config.mode == "webhook":
            if not config.webhook_url:
                raise RuntimeError("WEBHOOK_URL is required when BOT_MODE=webhook")
            webhook_target = f"{config.webhook_url.rstrip('/')}{config.webhook_path}"
            await application.bot.set_webhook(
                webhook_target,
                secret_token=config.webhook_secret or None,
            )
            logger.info("webhook set to %s", webhook_target)
        try:
            yield
        finally:
            await application.stop()
            await application.shutdown()

    web = FastAPI(title="baogao-telegram-bot", lifespan=lifespan)
    web.state.tg_application = application

    @web.post(config.webhook_path)
    async def telegram_webhook(request: Request):
        if config.webhook_secret:
            secret = request.headers.get("x-telegram-bot-api-secret-token", "")
            if secret != config.webhook_secret:
                raise HTTPException(status_code=401, detail="invalid webhook secret")
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        if not update:
            raise HTTPException(status_code=400, detail="invalid telegram update payload")
        logger.info(
            "webhook update received: update_id=%s has_message=%s has_callback=%s message_text=%r callback_data=%r user_id=%s chat_id=%s",
            update.update_id,
            bool(update.message),
            bool(update.callback_query),
            getattr(update.message, "text", None) if update.message else None,
            getattr(update.callback_query, "data", None) if update.callback_query else None,
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )
        try:
            await application.process_update(update)
        except Exception:
            logger.exception(
                "unhandled exception in process_update for update_id=%s",
                update.update_id,
            )
            raise
        return JSONResponse({"ok": True})

    @web.get("/healthz")
    async def healthz():
        return {"ok": True}

    @web.get("/reports/{report_id}", response_class=HTMLResponse)
    async def report_detail(report_id: int):
        with db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE id = ? AND status = 'approved'", (report_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="report not found")
        return report_to_html(row)

    def _auth(request: Request) -> None:
        if not config.admin_panel_token:
            return
        cookie_token = request.cookies.get("admin_token", "")
        if cookie_token == config.admin_panel_token:
            return
        query_token = request.query_params.get("token", "")
        if query_token == config.admin_panel_token:
            return
        raise HTTPException(status_code=403, detail="forbidden")

    def _should_set_admin_cookie(request: Request) -> bool:
        if not config.admin_panel_token:
            return False
        return (
            request.query_params.get("token", "") == config.admin_panel_token
            and request.cookies.get("admin_token", "") != config.admin_panel_token
        )

    def _is_secure_request(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto.lower() == "https":
            return True
        forwarded_ssl = request.headers.get("x-forwarded-ssl", "")
        return forwarded_ssl.lower() == "on"

    @web.get("/admin/login", response_class=HTMLResponse)
    async def admin_login():
        if not config.admin_panel_token:
            raise HTTPException(status_code=400, detail="admin token not set")
        return HTMLResponse(
            """
            <html><body style="font-family:sans-serif;max-width:480px;margin:40px auto;">
              <h3>管理员登录</h3>
              <form method="get" action="/admin">
                <input type="password" name="token" placeholder="ADMIN_PANEL_TOKEN" style="width:100%"><br><br>
                <button type="submit">登录</button>
              </form>
            </body></html>
            """
        )

    @web.get("/admin/logout")
    async def admin_logout():
        response = HTMLResponse("已退出。")
        response.delete_cookie("admin_token")
        return response

    @web.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request):
        _auth(request)
        should_set_cookie = _should_set_admin_cookie(request)
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            pending_rows = conn.execute(
                "SELECT id, username, created_at FROM reports WHERE status = 'pending' ORDER BY id DESC LIMIT 50"
            ).fetchall()
        settings_map = {r["key"]: r["value"] for r in rows}
        pending_list = [dict(r) for r in pending_rows]
        response = HTMLResponse(build_admin_html(settings_map, pending_list))
        if should_set_cookie:
            response.set_cookie(
                key="admin_token",
                value=config.admin_panel_token,
                httponly=True,
                samesite="lax",
                secure=_is_secure_request(request),
            )
        return response

    @web.post("/admin/save")
    async def save_admin(
        request: Request,
        force_sub_channel: str = Form(""),
        push_channel: str = Form(""),
        start_text: str = Form(""),
        start_media_type: str = Form(""),
        start_media_url: str = Form(""),
        start_buttons_json: str = Form("[]"),
        keyboard_buttons_json: str = Form("[]"),
        review_approved_template: str = Form(""),
        review_rejected_template: str = Form(""),
        report_template_json: str = Form("{}"),
        contact_text: str = Form(""),
        usage_text: str = Form(""),
        search_help_text: str = Form(""),
        report_link_base: str = Form(""),
    ):
        _auth(request)
        try:
            start_buttons_obj = json.loads(start_buttons_json)
            keyboard_buttons_obj = json.loads(keyboard_buttons_json)
            report_template_obj = json.loads(report_template_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="JSON 配置格式错误")
        if not isinstance(start_buttons_obj, list):
            raise HTTPException(status_code=400, detail="start_buttons_json 必须是数组")
        if not isinstance(keyboard_buttons_obj, list):
            raise HTTPException(status_code=400, detail="keyboard_buttons_json 必须是数组")
        if not isinstance(report_template_obj, dict):
            raise HTTPException(status_code=400, detail="report_template_json 必须是对象")

        updates = {
            "force_sub_channel": force_sub_channel.strip(),
            "push_channel": push_channel.strip(),
            "start_text": start_text,
            "start_media_type": start_media_type.strip(),
            "start_media_url": start_media_url.strip(),
            "start_buttons_json": json.dumps(start_buttons_obj, ensure_ascii=False),
            "keyboard_buttons_json": json.dumps(keyboard_buttons_obj, ensure_ascii=False),
            "review_approved_template": review_approved_template,
            "review_rejected_template": review_rejected_template,
            "report_template_json": json.dumps(report_template_obj, ensure_ascii=False),
            "contact_text": contact_text,
            "usage_text": usage_text,
            "search_help_text": search_help_text,
            "report_link_base": report_link_base.strip(),
        }
        for key, value in updates.items():
            setting_set(key, value)
        return HTMLResponse("<html><body>保存成功。<a href='/admin'>返回</a></body></html>")

    @web.get("/admin/settings")
    async def admin_settings(request: Request):
        _auth(request)
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    @web.post("/admin/approve/{report_id}")
    async def web_approve_report(report_id: int, request: Request):
        _auth(request)
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="报告不存在")
            if report["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"报告已处于 {report['status']} 状态")
            conn.execute(
                "UPDATE reports SET status='approved', reviewed_at=? WHERE id = ?",
                (utc_now_iso(), report_id),
            )
        approved_tpl = setting_get("review_approved_template", DEFAULT_SETTINGS["review_approved_template"])
        feedback = approved_tpl.format(id=report_id)
        try:
            await web.state.tg_application.bot.send_message(chat_id=report["user_id"], text=feedback)
        except Exception:
            logger.warning("failed to notify user %s of approval", report["user_id"], exc_info=True)
        push_channel = setting_get("push_channel", "").strip()
        if push_channel:
            data_values = parse_json(report["data_json"], {})
            detail = "\n".join([f"{k}: {v}" for k, v in data_values.items()])
            try:
                await web.state.tg_application.bot.send_message(
                    chat_id=push_channel,
                    text=f"📢 审核通过报告 #{report_id}\n@{report['username'] or 'unknown'}\n{detail}",
                )
            except Exception:
                logger.warning("failed to push report %s to channel", report_id, exc_info=True)
        return HTMLResponse(f"<html><body>报告 #{report_id} 已通过。<a href='/admin'>返回</a></body></html>")

    @web.post("/admin/reject/{report_id}")
    async def web_reject_report(report_id: int, request: Request, reason: str = Form("请联系管理员")):
        _auth(request)
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="报告不存在")
            if report["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"报告已处于 {report['status']} 状态")
            conn.execute(
                "UPDATE reports SET status='rejected', review_feedback=?, reviewed_at=? WHERE id = ?",
                (reason.strip() or "请联系管理员", utc_now_iso(), report_id),
            )
        tpl = setting_get("review_rejected_template", DEFAULT_SETTINGS["review_rejected_template"])
        feedback = tpl.format(id=report_id, reason=reason.strip() or "请联系管理员")
        try:
            await web.state.tg_application.bot.send_message(chat_id=report["user_id"], text=feedback)
        except Exception:
            logger.warning("failed to notify user %s of rejection", report["user_id"], exc_info=True)
        return HTMLResponse(f"<html><body>报告 #{report_id} 已驳回。<a href='/admin'>返回</a></body></html>")

    return web


def run_polling(bot_app: Application) -> None:
    bot_app.run_polling(drop_pending_updates=True)


async def run_webhook(bot_app: Application, config: AppConfig) -> None:
    api = create_fastapi(bot_app, config)
    uv_config = uvicorn.Config(api, host=config.host, port=config.port, log_level="info")
    server = uvicorn.Server(uv_config)
    await server.serve()


def main() -> None:
    init_db()
    config = load_config()
    setting_set("admin_panel_url", config.admin_panel_url)
    app = create_bot_application(config.token)
    app.bot_data["admin_panel_url"] = config.admin_panel_url
    app.bot_data["admin_panel_token"] = config.admin_panel_token

    if config.mode == "webhook":
        asyncio.run(run_webhook(app, config))
    else:
        run_polling(app)


if __name__ == "__main__":
    main()
