import asyncio
import html
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
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


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("report-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# In-memory state for admin panel verification (resets on restart, by design)
_verify_codes: dict[str, float] = {}    # code -> expiry_timestamp
_verify_code_otps: dict[str, str] = {}  # code -> otp_token (set after Telegram verification)
_otp_tokens: dict[str, float] = {}      # token -> expiry_timestamp
_verify_attempts: dict[int, list[float]] = {}  # user_id -> list of recent attempt timestamps

_VERIFY_CODE_TTL = 600   # 10 minutes
_OTP_TOKEN_TTL = 300     # 5 minutes
_MAX_VERIFY_ATTEMPTS = 5
_VERIFY_ATTEMPT_WINDOW = 300  # 5 minutes


def _cleanup_verify_state() -> None:
    now = time.time()
    # Use list() copy to avoid "dictionary changed size during iteration" in concurrent access
    for k in list(_verify_codes):
        if _verify_codes.get(k, now + 1) < now:
            _verify_codes.pop(k, None)
            _verify_code_otps.pop(k, None)
    for k in list(_otp_tokens):
        if _otp_tokens.get(k, now + 1) < now:
            _otp_tokens.pop(k, None)
    for uid in list(_verify_attempts):
        recent = [t for t in _verify_attempts.get(uid, []) if t > now - _VERIFY_ATTEMPT_WINDOW]
        if recent:
            _verify_attempts[uid] = recent
        else:
            _verify_attempts.pop(uid, None)


def _is_rate_limited(user_id: int) -> bool:
    now = time.time()
    recent = [t for t in _verify_attempts.get(user_id, []) if t > now - _VERIFY_ATTEMPT_WINDOW]
    return len(recent) >= _MAX_VERIFY_ATTEMPTS


def _record_verify_attempt(user_id: int) -> None:
    now = time.time()
    attempts = _verify_attempts.get(user_id, [])
    attempts = [t for t in attempts if t > now - _VERIFY_ATTEMPT_WINDOW]
    attempts.append(now)
    _verify_attempts[user_id] = attempts


DATABASE_URL = os.getenv("DATABASE_URL", "")


class _PGConn:
    """Thin wrapper that gives psycopg2 the same conn.execute() interface as sqlite3."""

    def __init__(self, raw_conn: Any) -> None:
        self._conn = raw_conn
        self._cur = raw_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params: tuple = ()) -> Any:
        self._cur.execute(sql, params)
        return self._cur

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        try:
            self._cur.close()
        finally:
            self._conn.close()

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
    "push_template": "📢 审核通过报告 #{id}\n@{username}\n{detail}",
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
    "push_detail_fields_json": "[]",
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
        mode=os.getenv("BOT_MODE", "polling").strip().lower(),
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
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    raw = psycopg2.connect(DATABASE_URL)
    conn = _PGConn(raw)
    try:
        yield conn
        conn.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
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
              id SERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL,
              username TEXT,
              tag TEXT,
              data_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              review_feedback TEXT,
              created_at TEXT NOT NULL,
              reviewed_at TEXT,
              channel_message_link TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, id DESC)"
        )
        # Migration: add missing columns if they do not exist yet
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS user_id BIGINT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS username TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS tag TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS data_json TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS review_feedback TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS created_at TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS reviewed_at TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS channel_message_link TEXT")
        # Migration: rename camelCase columns to snake_case if they exist (legacy schema)
        for old_col, new_col in [("createdAt", "created_at"), ("reviewedAt", "reviewed_at")]:
            has_camel = conn.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'reports' AND column_name = %s
                """,
                (old_col,),
            ).fetchone()
            if has_camel:
                conn.execute(f'ALTER TABLE reports RENAME COLUMN "{old_col}" TO "{new_col}"')
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id BIGINT PRIMARY KEY,
              username TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL
            )
            """
        )
        # Migration: if users table exists with a different schema (missing user_id column),
        # drop and recreate it. User rows are re-inserted automatically on next interaction.
        has_user_id_col = conn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'user_id'
            """
        ).fetchone()
        if not has_user_id_col:
            conn.execute("DROP TABLE users")
            conn.execute(
                """
                CREATE TABLE users (
                  user_id BIGINT PRIMARY KEY,
                  username TEXT,
                  first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL
                )
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
              user_id BIGINT PRIMARY KEY,
              username TEXT,
              reason TEXT,
              added_at TEXT NOT NULL
            )
            """
        )
        # Migration: if blacklist table exists with a different schema, drop and recreate.
        has_blacklist_col = conn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'blacklist' AND column_name = 'user_id'
            """
        ).fetchone()
        if not has_blacklist_col:
            conn.execute("DROP TABLE blacklist")
            conn.execute(
                """
                CREATE TABLE blacklist (
                  user_id BIGINT PRIMARY KEY,
                  username TEXT,
                  reason TEXT,
                  added_at TEXT NOT NULL
                )
                """
            )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", (key, value)
            )


def setting_get(key: str, default: str = "") -> str:
    with db_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else default


def setting_set(key: str, value: str) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def parse_json(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def safe_format(template: str, **kwargs: Any) -> str:
    """Format template with kwargs; unknown placeholders are left as-is."""
    try:
        return template.format_map(_SafeDict(**{k: str(v) for k, v in kwargs.items()}))
    except (ValueError, KeyError):
        return template


def upsert_user(user_id: int, username: str | None) -> None:
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_seen, last_seen) VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen
            """,
            (user_id, username or "", now, now),
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_user_banned(user_id: int) -> bool:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blacklist WHERE user_id = %s", (user_id,)
        ).fetchone()
        return row is not None


def ban_user(user_id: int, username: str | None, reason: str) -> None:
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO blacklist (user_id, username, reason, added_at) VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              reason=excluded.reason,
              added_at=excluded.added_at
            """,
            (user_id, username or "", reason or "管理员限制", now),
        )


def unban_user(user_id: int) -> None:
    with db_connection() as conn:
        conn.execute("DELETE FROM blacklist WHERE user_id = %s", (user_id,))

def keyboard_config() -> list[dict[str, str]]:
    items = parse_json(setting_get("keyboard_buttons_json"), [])
    normalized: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            normalized.append({"text": item, "action": "text"})
            continue
        if isinstance(item, dict) and item.get("text"):
            entry: dict[str, str] = {
                "text": str(item.get("text")),
                "action": str(item.get("action", "text")),
                "value": str(item.get("value", "")),
            }
            if item.get("row") is not None and str(item.get("row")).strip():
                entry["row"] = str(item.get("row")).strip()
            normalized.append(entry)
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
            valid_fields.append({
                "key": str(field["key"]),
                "label": str(field["label"]),
                "hint": str(field.get("hint", "")),
                "required": bool(field.get("required", True)),
                "type": str(field.get("type", "text")),
            })
    return {"name": str(data.get("name", "模板")), "fields": valid_fields}


def _make_field_prompt(field: dict[str, Any], sequential: bool = True) -> tuple[str, "InlineKeyboardMarkup"]:
    """Return (prompt_text, markup) for prompting a field value. Always includes a cancel button."""
    label = field["label"]
    hint = field.get("hint", "")
    field_type = field.get("type", "text")
    required = field.get("required", True)

    if field_type == "photo":
        prompt = f"请发送「{label}」的图片"
    else:
        prompt = f"请输入「{label}」"

    if hint:
        prompt += f"\n\n💡 {hint}"

    buttons: list[list[InlineKeyboardButton]] = []
    if not required and sequential:
        prompt += "\n\n（此项为可选，可跳过不填写）"
        buttons.append([InlineKeyboardButton("⏭ 跳过此项", callback_data=f"skip_field:{field['key']}")])
    buttons.append([InlineKeyboardButton("❌ 取消填写", callback_data="cancel_report")])
    return prompt, InlineKeyboardMarkup(buttons)


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
    items = keyboard_config()
    if not items:
        return ReplyKeyboardMarkup(
            [[KeyboardButton("写报告")], [KeyboardButton("查阅报告")]], resize_keyboard=True
        )
    rows: list[list[KeyboardButton]] = []
    current_row_key: str | None = None
    current_row: list[KeyboardButton] = []
    for item in items:
        row_val = item.get("row", "")
        btn = KeyboardButton(item["text"])
        if row_val:
            if row_val == current_row_key:
                current_row.append(btn)
            else:
                if current_row:
                    rows.append(current_row)
                current_row = [btn]
                current_row_key = row_val
        else:
            if current_row:
                rows.append(current_row)
                current_row = []
                current_row_key = None
            rows.append([btn])
    if current_row:
        rows.append(current_row)
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


def _admin_verify_url(base_url: str) -> str:
    """Return the /admin/verify URL for the Telegram entry button."""
    base = base_url.rstrip("/")
    for suffix in ("/admin/verify", "/admin/login", "/admin"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/admin/verify"


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
                if not admin_panel_url:
                    continue
                # All users see the verify page; only admins can complete verification
                url = _admin_verify_url(admin_panel_url)
            buttons.append([InlineKeyboardButton(text, url=url)])
    return InlineKeyboardMarkup(buttons) if buttons else None


def render_report_preview(values: dict[str, str], template: dict[str, Any]) -> str:
    lines = [f"📝 <b>{html.escape(str(template['name']))}</b>", ""]
    for field in template["fields"]:
        key = field["key"]
        label = html.escape(str(field["label"]))
        field_type = field.get("type", "text")
        raw_value = values.get(key, "")
        if raw_value:
            value = "📷（已上传图片）" if field_type == "photo" else html.escape(raw_value)
        else:
            value = "<i>（未填写）</i>"
        lines.append(f"<b>{label}</b>：{value}")
    return "\n".join(lines)


def report_fill_keyboard(values: dict[str, str], template: dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    for field in template["fields"]:
        key = field["key"]
        field_type = field.get("type", "text")
        has_value = bool(values.get(key, ""))
        done = "✅ " if has_value else ""
        label = field["label"]
        if field_type == "photo":
            label += " 📷"
        if not field.get("required", True):
            label += "（可选）"
        buttons.append([InlineKeyboardButton(f"{done}填写 {label}", callback_data=f"fill:{key}")])
    buttons.append([InlineKeyboardButton("提交审核", callback_data="submit_report")])
    return InlineKeyboardMarkup(buttons)


def _report_submit_keyboard() -> InlineKeyboardMarkup:
    """Return a keyboard with only Submit and Cancel buttons for the final report preview."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 提交审核", callback_data="submit_report"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_report"),
        ]
    ])


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
    """Return the /admin URL, stripping any existing admin sub-paths first."""
    base = base_url.rstrip("/")
    for suffix in ("/admin/login", "/admin"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
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
    upsert_user(user_id, update.effective_user.username)
    if is_user_banned(user_id):
        await update.effective_chat.send_message("您已被限制使用此机器人。")
        return
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

    channel = setting_get("force_sub_channel", "").strip()
    if channel and not await is_subscribed(context.bot, update.effective_user.id):
        rows = [[InlineKeyboardButton("我已订阅，重新检测", callback_data="retry_sub")]]
        channel_link = build_channel_link(channel)
        if channel_link:
            rows.insert(0, [InlineKeyboardButton("先去订阅", url=channel_link)])
        markup = InlineKeyboardMarkup(rows)
        await update.message.reply_text("请先订阅频道后再使用机器人。", reply_markup=markup)
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
        await context.bot.send_message(chat_id=report["user_id"], text=feedback)
        await update.message.reply_text(f"报告 #{pending_reject_id} 已驳回。")
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
                return

        await update.message.reply_text(
            render_report_preview(draft["values"], draft["template"]),
            parse_mode=ParseMode.HTML,
            reply_markup=_report_submit_keyboard(),
        )
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


def _is_verify_code(text: str) -> bool:
    """Return True if text looks like an admin verify code (12 uppercase hex chars)."""
    if len(text) != 12:
        return False
    return all(c in "0123456789ABCDEF" for c in text)


async def submit_report(context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
    draft = context.user_data.get("report_draft")
    if not draft:
        await update.effective_chat.send_message("请先点击“写报告”。")
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
    except (psycopg2.Error, RuntimeError):
        logger.exception(
            "submit_report: error for user_id=%s", update.effective_user.id
        )
        await update.effective_chat.send_message("❌ 提交失败，请稍后重试。")
        return
    context.user_data.pop("report_draft", None)
    await update.effective_chat.send_message(f"✅ 报告 #{report_id} 已提交，等待审核。")

    # Notify all admins with inline approve/reject buttons
    admin_ids = get_admin_user_ids()
    if admin_ids:
        preview = render_report_preview(values, template)
        notification = f"📋 新报告待审核 #{report_id}\n用户：@{html.escape(username or '未知')}\n\n{preview}"
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
                # Also send photo fields so the admin can review images
                for field in template["fields"]:
                    if field.get("type") == "photo":
                        photo_file_id = values.get(field["key"])
                        if photo_file_id:
                            await context.bot.send_photo(
                                chat_id=admin_id,
                                photo=photo_file_id,
                                caption=f"📷 {html.escape(field['label'])}（报告 #{report_id}）",
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
    """Push *report* to the configured channel.  Returns the channel message link (or '')."""
    push_channel = setting_get("push_channel", "").strip()
    if not push_channel:
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
    try:
        msg = await bot.send_message(chat_id=push_channel, text=push_text)
        channel_link = _build_channel_message_link(push_channel, msg.message_id)
        if channel_link:
            with db_connection() as conn:
                conn.execute(
                    "UPDATE reports SET channel_message_link=%s WHERE id=%s",
                    (channel_link, report_id),
                )
        return channel_link
    except Exception:
        logger.warning("failed to push report %s to channel", report_id, exc_info=True)
        return ""


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
            await query.message.reply_text("订阅检测通过。")
            await send_start_content(update, context)
        else:
            await query.message.reply_text("检测失败，请确认订阅后重试。")
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
        await query.message.reply_text(prompt, reply_markup=markup)
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
        else:
            await query.message.reply_text(
                render_report_preview(draft["values"], draft["template"]),
                parse_mode=ParseMode.HTML,
                reply_markup=_report_submit_keyboard(),
            )
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
        await query.message.reply_text(f"✅ 报告 #{report_id} 已通过审核。")
        channel_link = await _push_report_to_channel(context.bot, report_id, report)
        feedback = _build_approval_feedback(report_id, channel_link=channel_link)
        await context.bot.send_message(chat_id=report["user_id"], text=feedback)
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
        await query.message.reply_text(f"请输入驳回报告 #{report_id} 的原因：")
        return

    # Fallback: answer unhandled callback queries to avoid Telegram timeout errors
    await query.answer()


def report_to_html(report_row: dict) -> str:
    data = parse_json(report_row["data_json"], {})
    lines = [f"<h1>报告 #{report_row['id']}</h1>"]
    lines.append(f"<p>状态：{report_row['status']}</p>")
    lines.append(f"<p>用户：@{report_row['username'] or 'unknown'}</p>")
    lines.append("<ul>")
    for k, v in data.items():
        lines.append(f"<li><b>{k}</b>：{v}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


_ADMIN_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;color:#333;font-size:14px}
.container{max-width:960px;margin:0 auto;padding:20px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding:14px 20px;background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
h1{font-size:1.2rem;font-weight:700;color:#1e293b}
.logout{color:#64748b;text-decoration:none;font-size:.85rem;padding:6px 12px;border:1px solid #e2e8f0;border-radius:6px}
.logout:hover{background:#f8fafc}
.alert{padding:10px 16px;border-radius:8px;margin-bottom:16px;font-size:.9rem}
.alert-success{background:#dcfce7;color:#166534;border:1px solid #86efac}
.tabs-wrap{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden}
.tabs{display:flex;border-bottom:2px solid #e2e8f0;overflow-x:auto}
.tab-btn{padding:12px 20px;border:none;background:none;cursor:pointer;font-size:.9rem;color:#64748b;white-space:nowrap;border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s;font-family:inherit}
.tab-btn:hover{color:#2563eb;background:#f8fafc}
.tab-btn.active{color:#2563eb;font-weight:600;border-bottom-color:#2563eb}
.tab-pane{display:none;padding:24px}
.tab-pane.active{display:block}
.field{margin-bottom:18px}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px}
label{display:block;font-size:.8rem;font-weight:600;color:#475569;margin-bottom:5px;text-transform:uppercase;letter-spacing:.04em}
.hint{font-size:.78rem;color:#94a3b8;margin-top:4px}
input[type=text],textarea,select{width:100%;padding:8px 10px;border:1px solid #cbd5e1;border-radius:6px;font-size:.9rem;font-family:inherit;background:#fff;transition:border-color .15s}
input[type=text]:focus,textarea:focus,select:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.1)}
textarea{resize:vertical;min-height:70px}
.btn{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:500;transition:all .15s;font-family:inherit}
.btn-primary{background:#2563eb;color:#fff}
.btn-primary:hover{background:#1d4ed8}
.btn-danger{background:#ef4444;color:#fff}
.btn-danger:hover{background:#dc2626}
.btn-success{background:#10b981;color:#fff}
.btn-success:hover{background:#059669}
.btn-sm{padding:4px 10px;font-size:.8rem}
.btn-add{background:#eff6ff;color:#2563eb;border:1px dashed #93c5fd;padding:7px 14px;width:100%;border-radius:6px;cursor:pointer;font-size:.85rem;margin-top:6px;transition:all .15s;font-family:inherit}
.btn-add:hover{background:#dbeafe}
.editor-row{display:flex;gap:8px;align-items:center;margin-bottom:8px;padding:10px 12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px}
.editor-row input,.editor-row select{flex:1;min-width:60px}
.section-title{font-size:.95rem;font-weight:700;color:#1e293b;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid #f1f5f9}
.save-bar{background:#fff;border-top:1px solid #e2e8f0;padding:14px 24px;display:flex;justify-content:flex-end;gap:10px}
.table{width:100%;border-collapse:collapse;font-size:.9rem}
.table th,.table td{padding:10px 12px;text-align:left;border-bottom:1px solid #f1f5f9}
.table th{background:#f8fafc;font-weight:600;color:#64748b;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
.table tbody tr:hover{background:#fafafa}
.table td input{padding:5px 8px;border:1px solid #cbd5e1;border-radius:5px;font-size:.85rem;width:150px}
.muted{color:#94a3b8;font-style:italic}
.badge{display:inline-flex;align-items:center;justify-content:center;background:#ef4444;color:#fff;border-radius:10px;font-size:.7rem;font-weight:700;min-width:18px;height:18px;padding:0 5px;margin-left:4px;vertical-align:middle}
.tpl-field-card{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;margin-bottom:10px;overflow:hidden}
.tpl-field-card .editor-row{background:transparent;border:none;border-radius:0;margin-bottom:0}
@media(max-width:600px){.field-row{grid-template-columns:1fr}}
.rte-wrap{border:1px solid #cbd5e1;border-radius:6px;overflow:hidden;background:#fff}
.rte-wrap:focus-within{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.1)}
.rte-toolbar{display:flex;flex-wrap:wrap;gap:2px;padding:5px 8px;background:#f8fafc;border-bottom:1px solid #e2e8f0}
.rte-btn{padding:3px 8px;border:1px solid transparent;border-radius:4px;background:none;cursor:pointer;font-size:.85rem;font-family:inherit;color:#374151;transition:all .1s;line-height:1.4}
.rte-btn:hover{background:#e5e7eb;border-color:#d1d5db}
.rte-body{padding:8px 10px;min-height:70px;outline:none;font-size:.9rem;line-height:1.6;font-family:inherit;word-break:break-word}
.rte-body:empty:before{content:attr(data-ph);color:#94a3b8;pointer-events:none;display:block}
.rte-pills{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}
.rte-pill{padding:3px 10px;background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;border-radius:12px;cursor:pointer;font-size:.8rem;transition:all .15s;font-family:inherit}
.rte-pill:hover{background:#dbeafe;border-color:#93c5fd}
"""

_ADMIN_JS = """
(function(){
  var tabBtns=document.querySelectorAll('.tab-btn');
  var tabPanes=document.querySelectorAll('.tab-pane');
  var saveBar=document.getElementById('settings-save-bar');
  tabBtns.forEach(function(btn){
    btn.addEventListener('click',function(){
      tabBtns.forEach(function(b){b.classList.remove('active');});
      tabPanes.forEach(function(p){p.classList.remove('active');});
      btn.classList.add('active');
      document.getElementById('pane-'+btn.dataset.tab).classList.add('active');
      var noSaveTabs=['pending','blacklist','broadcast'];
      if(saveBar) saveBar.style.display=noSaveTabs.indexOf(btn.dataset.tab)>=0?'none':'';
      if(btn.dataset.tab==='review'&&_rteMap['push_template'])_rteMap['push_template'].refreshPills();
      if(btn.dataset.tab==='broadcast'&&_rteMap['broadcast_text'])_rteMap['broadcast_text'].refreshPills();
    });
  });

  // Start Buttons Editor
  var startBtnsData=__START_BUTTONS__;
  var startRows=document.getElementById('start-btn-rows');
  function makeStartRow(item){
    var row=document.createElement('div'); row.className='editor-row';
    var textIn=document.createElement('input');
    textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
    textIn.dataset.field='text'; textIn.style.flex='1';
    var urlIn=document.createElement('input');
    urlIn.type='text'; urlIn.placeholder='链接 URL（https://...）'; urlIn.value=item.url||'';
    urlIn.dataset.field='url'; urlIn.style.flex='2';
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();});
    row.appendChild(textIn); row.appendChild(urlIn); row.appendChild(rm);
    return row;
  }
  startBtnsData.forEach(function(item){startRows.appendChild(makeStartRow(item));});
  document.getElementById('start-btn-add').addEventListener('click',function(){
    startRows.appendChild(makeStartRow({text:'',url:''}));
  });
  function serializeStartBtns(){
    var result=[];
    startRows.querySelectorAll('.editor-row').forEach(function(row){
      var text=row.querySelector('[data-field=text]').value.trim();
      var url=row.querySelector('[data-field=url]').value.trim();
      if(text&&url) result.push({text:text,url:url});
    });
    document.getElementById('start_buttons_json').value=JSON.stringify(result);
  }

  // Keyboard Buttons Editor
  var kbData=__KB_BUTTONS__;
  var kbRows=document.getElementById('kb-rows');
  var KB_ACTIONS=[
    {value:'write_report',label:'写报告（内置）'},
    {value:'search_help',label:'查阅报告（内置）'},
    {value:'contact',label:'联系管理员（内置）'},
    {value:'usage',label:'操作方式（内置）'},
    {value:'text',label:'自定义回复文本'}
  ];
  function makeKbRow(item){
    var row=document.createElement('div'); row.className='editor-row';
    var textIn=document.createElement('input');
    textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
    textIn.dataset.field='text';
    var sel=document.createElement('select');
    sel.dataset.field='action'; sel.style.flex='none'; sel.style.width='180px';
    KB_ACTIONS.forEach(function(a){
      var opt=document.createElement('option');
      opt.value=a.value; opt.textContent=a.label;
      if(item.action===a.value) opt.selected=true;
      sel.appendChild(opt);
    });
    var valIn=document.createElement('input');
    valIn.type='text'; valIn.placeholder='回复内容'; valIn.value=item.value||'';
    valIn.dataset.field='value';
    valIn.style.display=(item.action==='text')?'':'none';
    sel.addEventListener('change',function(){
      valIn.style.display=sel.value==='text'?'':'none';
    });
    var rowIn=document.createElement('input');
    rowIn.type='text'; rowIn.placeholder='行号'; rowIn.value=item.row||'';
    rowIn.dataset.field='row'; rowIn.style.width='50px'; rowIn.style.flex='none';
    rowIn.title='相同行号的按钮同行显示，留空则独占一行';
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();});
    row.appendChild(textIn); row.appendChild(sel); row.appendChild(valIn); row.appendChild(rowIn); row.appendChild(rm);
    return row;
  }
  kbData.forEach(function(item){kbRows.appendChild(makeKbRow(item));});
  document.getElementById('kb-add').addEventListener('click',function(){
    kbRows.appendChild(makeKbRow({text:'',action:'write_report',value:''}));
  });
  function serializeKb(){
    var result=[];
    kbRows.querySelectorAll('.editor-row').forEach(function(row){
      var text=row.querySelector('[data-field=text]').value.trim();
      var action=row.querySelector('[data-field=action]').value;
      var value=row.querySelector('[data-field=value]').value.trim();
      var rowNum=row.querySelector('[data-field=row]').value.trim();
      if(text){
        var item={text:text,action:action};
        if(action==='text'&&value) item.value=value;
        if(rowNum) item.row=rowNum;
        result.push(item);
      }
    });
    document.getElementById('keyboard_buttons_json').value=JSON.stringify(result);
  }

  // Report Template Editor
  var tplData=__TEMPLATE__;
  var tplFieldsEl=document.getElementById('template-fields');
  var tplNameIn=document.getElementById('template-name');
  tplNameIn.value=tplData.name||'';
  function makeTplRow(field){
    var card=document.createElement('div'); card.className='tpl-field-card';
    // Row 1: key, label, type, required, remove
    var row1=document.createElement('div'); row1.className='editor-row'; row1.style.marginBottom='4px';
    var keyIn=document.createElement('input');
    keyIn.type='text'; keyIn.placeholder='英文标识（如 title）'; keyIn.value=field.key||'';
    keyIn.dataset.field='key'; keyIn.style.flex='1';
    var labelIn=document.createElement('input');
    labelIn.type='text'; labelIn.placeholder='显示名称（如 标题）'; labelIn.value=field.label||'';
    labelIn.dataset.field='label'; labelIn.style.flex='1';
    var typeSel=document.createElement('select');
    typeSel.dataset.field='type'; typeSel.style.flex='none'; typeSel.style.width='80px';
    [{value:'text',label:'文本'},{value:'photo',label:'图片'}].forEach(function(o){
      var opt=document.createElement('option');
      opt.value=o.value; opt.textContent=o.label;
      if((field.type||'text')===o.value) opt.selected=true;
      typeSel.appendChild(opt);
    });
    var reqLabel=document.createElement('label');
    reqLabel.style.cssText='display:flex;align-items:center;gap:4px;font-weight:normal;font-size:.85rem;white-space:nowrap;flex:none;text-transform:none;letter-spacing:0;color:#475569;';
    var reqCheck=document.createElement('input');
    reqCheck.type='checkbox'; reqCheck.dataset.field='required'; reqCheck.style.margin='0';
    reqCheck.checked=(field.required!==false);
    reqLabel.appendChild(reqCheck); reqLabel.appendChild(document.createTextNode('必填'));
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){card.remove();});
    row1.appendChild(keyIn); row1.appendChild(labelIn); row1.appendChild(typeSel); row1.appendChild(reqLabel); row1.appendChild(rm);
    // Row 2: hint input
    var row2=document.createElement('div'); row2.style.cssText='padding:0 12px 10px;';
    var hintIn=document.createElement('input');
    hintIn.type='text'; hintIn.placeholder='字段说明（选填）：例如"请填写今日工作摘要"，显示给用户作为填写提示';
    hintIn.value=field.hint||''; hintIn.dataset.field='hint'; hintIn.style.width='100%';
    row2.appendChild(hintIn);
    card.appendChild(row1); card.appendChild(row2);
    return card;
  }
  (tplData.fields||[]).forEach(function(f){tplFieldsEl.appendChild(makeTplRow(f));});
  document.getElementById('template-add').addEventListener('click',function(){
    tplFieldsEl.appendChild(makeTplRow({key:'',label:'',hint:'',required:true,type:'text'}));
  });
  function serializeTemplate(){
    var fields=[];
    tplFieldsEl.querySelectorAll('.tpl-field-card').forEach(function(card){
      var key=card.querySelector('[data-field=key]').value.trim();
      var label=card.querySelector('[data-field=label]').value.trim();
      var hint=card.querySelector('[data-field=hint]').value.trim();
      var type=card.querySelector('[data-field=type]').value;
      var required=card.querySelector('[data-field=required]').checked;
      if(key&&label) fields.push({key:key,label:label,hint:hint,required:required,type:type});
    });
    var tpl={name:tplNameIn.value.trim()||'模板',fields:fields};
    document.getElementById('report_template_json').value=JSON.stringify(tpl);
  }

  document.getElementById('settings-form').addEventListener('submit',function(){
    Object.keys(_rteMap).forEach(function(k){if(_rteMap[k])_rteMap[k].sync();});
    serializeStartBtns();
    serializeKb();
    serializeTemplate();
    serializePushFields();
  });

  // Push Detail Fields Editor
  var pushDetailFieldsData=__PUSH_DETAIL_FIELDS__;
  var pushFieldsList=document.getElementById('push-detail-fields-list');
  function getTplTextFields(){
    var fields=[];
    tplFieldsEl.querySelectorAll('.tpl-field-card').forEach(function(card){
      var key=card.querySelector('[data-field=key]').value.trim();
      var label=card.querySelector('[data-field=label]').value.trim();
      var type=card.querySelector('[data-field=type]').value;
      if(key&&label&&type!=='photo') fields.push({key:key,label:label});
    });
    return fields;
  }
  function makePushFieldRow(key,label){
    var row=document.createElement('div'); row.className='editor-row'; row.dataset.key=key;
    var span=document.createElement('span');
    span.textContent=(label||key)+' ('+key+')'; span.style.flex='1';
    var up=document.createElement('button');
    up.type='button'; up.textContent='↑'; up.className='btn btn-sm';
    up.style.cssText='padding:3px 8px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;cursor:pointer;flex:none;';
    up.addEventListener('click',function(){var prev=row.previousElementSibling;if(prev)pushFieldsList.insertBefore(row,prev);});
    var down=document.createElement('button');
    down.type='button'; down.textContent='↓'; down.className='btn btn-sm';
    down.style.cssText='padding:3px 8px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;cursor:pointer;flex:none;';
    down.addEventListener('click',function(){var next=row.nextElementSibling;if(next)pushFieldsList.insertBefore(next,row);});
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();renderPushFieldsAddArea();});
    row.appendChild(span); row.appendChild(up); row.appendChild(down); row.appendChild(rm);
    return row;
  }
  function renderPushFieldsAddArea(){
    var addArea=document.getElementById('push-fields-add-area');
    addArea.innerHTML='';
    var existingKeys={};
    pushFieldsList.querySelectorAll('.editor-row[data-key]').forEach(function(r){existingKeys[r.dataset.key]=true;});
    getTplTextFields().forEach(function(f){
      if(!existingKeys[f.key]){
        var btn=document.createElement('button');
        btn.type='button'; btn.textContent='＋ '+f.label+' ('+f.key+')';
        btn.className='btn-add'; btn.style.marginTop='4px';
        btn.addEventListener('click',function(){
          pushFieldsList.appendChild(makePushFieldRow(f.key,f.label));
          renderPushFieldsAddArea();
        });
        addArea.appendChild(btn);
      }
    });
  }
  function initPushFields(){
    pushFieldsList.innerHTML='';
    var tplFields=getTplTextFields();
    var labelMap={};
    tplFields.forEach(function(f){labelMap[f.key]=f.label;});
    var initKeys=pushDetailFieldsData.length>0?pushDetailFieldsData:tplFields.map(function(f){return f.key;});
    initKeys.forEach(function(k){
      if(labelMap[k]) pushFieldsList.appendChild(makePushFieldRow(k,labelMap[k]));
    });
    renderPushFieldsAddArea();
  }
  function serializePushFields(){
    var result=[];
    pushFieldsList.querySelectorAll('.editor-row[data-key]').forEach(function(row){
      result.push(row.dataset.key);
    });
    document.getElementById('push_detail_fields_json').value=JSON.stringify(result);
  }
  initPushFields();

  // Rich Text Editor
  function serializeRTENode(node){
    var out='';
    node.childNodes.forEach(function(n){
      if(n.nodeType===3){
        out+=n.textContent.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      } else if(n.nodeType===1){
        var t=n.tagName.toLowerCase();
        var inner=serializeRTENode(n);
        if(t==='b'||t==='strong') out+='<b>'+inner+'</b>';
        else if(t==='i'||t==='em') out+='<i>'+inner+'</i>';
        else if(t==='u') out+='<u>'+inner+'</u>';
        else if(t==='s'||t==='strike'||t==='del') out+='<s>'+inner+'</s>';
        else if(t==='code') out+='<code>'+inner+'</code>';
        else if(t==='a'){var href=(n.getAttribute('href')||'').replace(/"/g,'&quot;');out+='<a href="'+href+'">'+inner+'</a>';}
        else if(t==='br') out+='\\n';
        else if(t==='div'||t==='p') out+=(inner||'')+'\\n';
        else out+=inner;
      }
    });
    return out;
  }
  var _rteMap={};
  function RichTextEditor(ta,getPills){
    var self=this; self._ta=ta; self._getPills=getPills||null; self._pd=null;
    var wrap=document.createElement('div'); wrap.className='rte-wrap';
    ta.parentNode.insertBefore(wrap,ta); ta.style.display='none';
    if(getPills){var pd=document.createElement('div');pd.className='rte-pills';wrap.appendChild(pd);self._pd=pd;}
    var tb=document.createElement('div'); tb.className='rte-toolbar'; wrap.appendChild(tb);
    var body=document.createElement('div'); body.className='rte-body'; body.contentEditable='true';
    body.setAttribute('data-ph',ta.getAttribute('placeholder')||'输入内容…');
    var existing=ta.value; if(existing) body.innerHTML=existing.replace(/\\n/g,'<br>');
    wrap.appendChild(body); self._body=body;
    var tools=[
      {cmd:'bold',html:'<b>B</b>',title:'粗体'},
      {cmd:'italic',html:'<i>I</i>',title:'斜体'},
      {cmd:'underline',html:'<u>U</u>',title:'下划线'},
      {cmd:'strikeThrough',html:'<s>S</s>',title:'删除线'},
      {cmd:'code',html:'<code style="font-size:.8rem">&lt;/&gt;</code>',title:'代码'},
      {cmd:'link',html:'🔗',title:'添加链接'},
      {cmd:'unlink',html:'🔗✕',title:'移除链接'},
      {cmd:'undo',html:'↩',title:'撤销'},
      {cmd:'redo',html:'↪',title:'重做'}
    ];
    tools.forEach(function(t){
      var btn=document.createElement('button'); btn.type='button';
      btn.innerHTML=t.html; btn.title=t.title; btn.className='rte-btn';
      btn.addEventListener('mousedown',function(e){
        e.preventDefault(); body.focus();
        if(t.cmd==='code'){
          var sel=window.getSelection();
          if(sel&&sel.rangeCount>0&&!sel.isCollapsed){
            var range=sel.getRangeAt(0);
            var codeEl=document.createElement('code');
            try{range.surroundContents(codeEl);}catch(ex){var et=range.toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');document.execCommand('insertHTML',false,'<code>'+et+'</code>');}
          } else {document.execCommand('insertHTML',false,'<code></code>');}
        } else if(t.cmd==='link'){
          var sel=window.getSelection(); var st=sel?sel.toString():'';
          var url=prompt('输入链接地址（https://...）','');
          if(url){
            if(st){document.execCommand('createLink',false,url);}
            else{var su=url.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');document.execCommand('insertHTML',false,'<a href="'+su+'">'+su+'</a>');}
          }
        } else if(t.cmd==='unlink'){document.execCommand('unlink');}
        else if(t.cmd==='undo'){document.execCommand('undo');}
        else if(t.cmd==='redo'){document.execCommand('redo');}
        else{document.execCommand(t.cmd);}
      });
      tb.appendChild(btn);
    });
    self.sync=function(){var raw=serializeRTENode(body);self._ta.value=raw.replace(/\\n+$/,'');};
    self.refreshPills=function(){
      if(!self._pd||!self._getPills)return;
      var pills=self._getPills(); self._pd.innerHTML='';
      pills.forEach(function(p){
        var btn=document.createElement('button'); btn.type='button'; btn.className='rte-pill';
        btn.textContent=p.label; btn.title='插入: '+p.insert;
        btn.addEventListener('click',function(){body.focus();document.execCommand('insertText',false,p.insert);});
        self._pd.appendChild(btn);
      });
    };
  }
  function getPushTemplatePills(){
    var pills=[{label:'报告ID',insert:'{id}'},{label:'用户名',insert:'{username}'},{label:'推送详情',insert:'{detail}'},{label:'报告链接',insert:'{link}'}];
    getTplTextFields().forEach(function(f){pills.push({label:f.label,insert:'{'+f.key+'}'});});
    return pills;
  }
  function getBroadcastPills(){
    var pills=[];
    getTplTextFields().forEach(function(f){pills.push({label:f.label,insert:'{'+f.key+'}'});});
    return pills;
  }
  ['start_text','search_help_text','contact_text','usage_text'].forEach(function(name){
    var ta=document.querySelector('[name="'+name+'"]');
    if(ta) _rteMap[name]=new RichTextEditor(ta,null);
  });
  var ptTa=document.querySelector('[name="push_template"]');
  if(ptTa){_rteMap['push_template']=new RichTextEditor(ptTa,getPushTemplatePills);_rteMap['push_template'].refreshPills();}
  var btTa=document.querySelector('[name="broadcast_text"]');
  if(btTa){_rteMap['broadcast_text']=new RichTextEditor(btTa,getBroadcastPills);_rteMap['broadcast_text'].refreshPills();}

  // Broadcast Buttons Editor
  var broadcastBtnsRows=document.getElementById('broadcast-btn-rows');
  if(broadcastBtnsRows){
    function makeBroadcastRow(item){
      var row=document.createElement('div'); row.className='editor-row';
      var textIn=document.createElement('input');
      textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
      textIn.dataset.field='text'; textIn.style.flex='1';
      var urlIn=document.createElement('input');
      urlIn.type='text'; urlIn.placeholder='链接 URL（https://...）'; urlIn.value=item.url||'';
      urlIn.dataset.field='url'; urlIn.style.flex='2';
      var rm=document.createElement('button');
      rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
      rm.addEventListener('click',function(){row.remove();});
      row.appendChild(textIn); row.appendChild(urlIn); row.appendChild(rm);
      return row;
    }
    document.getElementById('broadcast-btn-add').addEventListener('click',function(){
      broadcastBtnsRows.appendChild(makeBroadcastRow({text:'',url:''}));
    });
    function serializeBroadcastBtns(){
      var result=[];
      broadcastBtnsRows.querySelectorAll('.editor-row').forEach(function(row){
        var text=row.querySelector('[data-field=text]').value.trim();
        var url=row.querySelector('[data-field=url]').value.trim();
        if(text&&url) result.push({text:text,url:url});
      });
      document.getElementById('broadcast_buttons_json').value=JSON.stringify(result);
    }
    document.getElementById('broadcast-form').addEventListener('submit',function(){
      if(_rteMap['broadcast_text'])_rteMap['broadcast_text'].sync();
      serializeBroadcastBtns();
      return confirm('确认向所有用户发送广播？');
    });
  }
})();
"""


def _render_report_content_for_admin(data_json: str, tpl_fields: list[dict[str, Any]]) -> str:
    """Return a short HTML snippet showing all field values of a report for admin review."""
    data = parse_json(data_json, {})
    if not data:
        return "<em style='color:#94a3b8'>（无内容）</em>"
    field_labels = {f["key"]: f["label"] for f in tpl_fields}
    field_types = {f["key"]: f.get("type", "text") for f in tpl_fields}
    parts = []
    for k, v in data.items():
        label = html.escape(field_labels.get(k, k))
        if field_types.get(k, "text") == "photo":
            parts.append(f"<b>{label}</b>：📷（图片，请在Telegram通知中查看）")
        else:
            display = html.escape(str(v)[:300])
            parts.append(f"<b>{label}</b>：{display}")
    return "<br>".join(parts) if parts else "<em style='color:#94a3b8'>（无内容）</em>"


def build_admin_html(settings_map: dict[str, str], pending_reports: list[dict] | None = None, saved: bool = False, user_count: int = 0, db_path: str = "", blacklist: list[dict] | None = None) -> str:
    def e(key: str) -> str:
        return html.escape(settings_map.get(key, ""))

    def safe_js(key: str, fallback: Any) -> str:
        raw = settings_map.get(key, "")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = fallback
        return (
            json.dumps(parsed, ensure_ascii=False)
            .replace("</", r"<\/")
            .replace("\u2028", r"\u2028")
            .replace("\u2029", r"\u2029")
        )

    start_buttons_js = safe_js("start_buttons_json", [])
    kb_buttons_js = safe_js("keyboard_buttons_json", [])
    template_js = safe_js("report_template_json", {"name": "", "fields": []})
    push_detail_fields_js = safe_js("push_detail_fields_json", [])

    js = (
        _ADMIN_JS
        .replace("__START_BUTTONS__", start_buttons_js)
        .replace("__KB_BUTTONS__", kb_buttons_js)
        .replace("__TEMPLATE__", template_js)
        .replace("__PUSH_DETAIL_FIELDS__", push_detail_fields_js)
    )

    pending_count = len(pending_reports) if pending_reports else 0
    pending_badge = f'<span class="badge">{pending_count}</span>' if pending_count > 0 else ""

    if pending_reports:
        tpl_fields = report_template()["fields"]
        rows_html = ""
        for r in pending_reports:
            content_html = _render_report_content_for_admin(r.get("data_json", "{}"), tpl_fields)
            rows_html += (
                "<tr>"
                f"<td>#{r['id']}</td>"
                f"<td>@{html.escape(r['username'] or 'unknown')}</td>"
                f"<td style='white-space:nowrap'>{html.escape(str(r['created_at'])[:19])}</td>"
                f"<td style='max-width:320px;word-break:break-word;font-size:.85rem;line-height:1.6'>{content_html}</td>"
                "<td style='white-space:nowrap;vertical-align:middle'>"
                f"<form method='post' action='/admin/approve/{r['id']}' style='display:block;margin-bottom:6px'>"
                "<button class='btn btn-success btn-sm' type='submit'>✅ 通过</button></form>"
                f"<form method='post' action='/admin/reject/{r['id']}' style='display:flex;gap:4px;align-items:center'>"
                "<input name='reason' placeholder='驳回原因' style='width:110px;padding:4px 6px;border:1px solid #cbd5e1;border-radius:4px;font-size:.8rem'>"
                "<button class='btn btn-danger btn-sm' type='submit'>❌ 驳回</button></form>"
                "</td></tr>"
            )
        pending_html = (
            "<div style='margin-bottom:12px'>"
            "<a href='/admin#tab=pending' onclick='location.reload();return false;' style='font-size:.85rem;color:#2563eb;text-decoration:none'>🔄 刷新列表</a>"
            "</div>"
            "<table class='table'><thead><tr>"
            "<th>ID</th><th>用户</th><th>提交时间</th><th>报告内容</th><th>操作</th>"
            "</tr></thead><tbody>" + rows_html + "</tbody></table>"
        )
    else:
        pending_html = "<p class='muted'>暂无待审核报告。</p>"

    # Build blacklist HTML
    if blacklist:
        bl_rows = ""
        for entry in blacklist:
            uid = html.escape(str(entry.get("user_id", "")))
            uname = html.escape(entry.get("username") or "")
            reason = html.escape(entry.get("reason") or "")
            added = html.escape(str(entry.get("added_at", ""))[:19])
            bl_rows += (
                "<tr>"
                f"<td>{uid}</td>"
                f"<td>{'@' + uname if uname else '<em style=\"color:#94a3b8\">未知</em>'}</td>"
                f"<td>{reason}</td>"
                f"<td style='white-space:nowrap'>{added}</td>"
                "<td>"
                f"<form method='post' action='/admin/blacklist/unban/{entry['user_id']}'>"
                "<button class='btn btn-success btn-sm' type='submit'>✅ 解除</button></form>"
                "</td></tr>"
            )
        blacklist_html = (
            "<table class='table'><thead><tr>"
            "<th>用户ID</th><th>用户名</th><th>原因</th><th>封禁时间</th><th>操作</th>"
            "</tr></thead><tbody>" + bl_rows + "</tbody></table>"
        )
    else:
        blacklist_html = "<p class='muted'>黑名单为空。</p>"

    saved_banner = "<div class='alert alert-success'>✅ 配置已保存成功！</div>" if saved else ""

    media_types = [("", "无"), ("photo", "图片"), ("video", "视频")]
    current_media_type = settings_map.get("start_media_type", "").strip().lower()
    media_type_options = "".join(
        f"<option value='{v}'{' selected' if v == current_media_type else ''}>{label}</option>"
        for v, label in media_types
    )

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>报告机器人管理后台</title>
<style>{_ADMIN_CSS}</style>
</head>
<body><div class="container">

<header>
  <h1>📋 报告机器人管理后台</h1>
  <a class="logout" href="/admin/logout">退出登录</a>
</header>

{saved_banner}

<div class="tabs-wrap">
<div class="tabs">
  <button type="button" class="tab-btn active" data-tab="basic">基本设置</button>
  <button type="button" class="tab-btn" data-tab="welcome">欢迎消息</button>
  <button type="button" class="tab-btn" data-tab="keyboard">底部菜单</button>
  <button type="button" class="tab-btn" data-tab="template">报告模板</button>
  <button type="button" class="tab-btn" data-tab="texts">文本配置</button>
  <button type="button" class="tab-btn" data-tab="review">审核设置</button>
  <button type="button" class="tab-btn" data-tab="pending">待审核{pending_badge}</button>
  <button type="button" class="tab-btn" data-tab="blacklist">黑名单</button>
  <button type="button" class="tab-btn" data-tab="broadcast">广播</button>
</div>

<form id="settings-form" method="post" action="/admin/save">

<div id="pane-basic" class="tab-pane active">
  <p class="section-title">基本设置</p>
  <div class="field-row">
    <div class="field">
      <label>强制订阅频道</label>
      <input type="text" name="force_sub_channel" value="{e('force_sub_channel')}" placeholder="@频道用户名">
      <div class="hint">填 @用户名，用户须先订阅该频道才能使用机器人（留空则不限制）</div>
    </div>
    <div class="field">
      <label>报告推送频道</label>
      <input type="text" name="push_channel" value="{e('push_channel')}" placeholder="@频道用户名">
      <div class="hint">审核通过的报告自动推送到该频道（留空则不推送）</div>
    </div>
  </div>
  <div class="field">
    <label>报告链接基地址</label>
    <input type="text" name="report_link_base" value="{e('report_link_base')}" placeholder="https://yourdomain.com">
    <div class="hint">报告查询结果显示链接的前缀，链接格式为：域名/reports/ID（留空则仅显示报告 ID）；当推送到频道时会自动使用频道消息链接，无需另行配置</div>
  </div>
  <div class="field">
    <label>数据库</label>
    <input type="text" value="PostgreSQL (DATABASE_URL)" readonly style="background:#f5f5f5;color:#888;">
    <div class="hint">数据库使用 PostgreSQL，数据持久化存储，重新部署不会丢失。请确保在平台环境变量中设置 <code>DATABASE_URL</code>。</div>
  </div>
  <div class="field">
    <label>配置导出 / 导入</label>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start">
      <a href="/admin/export-settings" class="btn btn-primary" style="text-decoration:none;display:inline-block">⬇️ 导出配置 JSON</a>
      <div style="display:flex;gap:6px;align-items:flex-start;flex-wrap:wrap">
        <textarea name="settings_json" rows="3" placeholder="粘贴之前导出的配置 JSON..." style="width:300px;min-width:200px;padding:6px 10px;border:1px solid #cbd5e1;border-radius:6px;font-size:.85rem;resize:vertical" form="import-settings-form"></textarea>
        <button type="submit" class="btn btn-success" onclick="return confirm('导入将覆盖现有配置，确认吗？')" form="import-settings-form">⬆️ 导入配置</button>
      </div>
    </div>
    <div class="hint" style="margin-top:6px">可将当前配置导出为 JSON 文件保存备份；重新部署后可导入恢复设置。</div>
  </div>
</div>

<div id="pane-welcome" class="tab-pane">
  <p class="section-title">欢迎消息（/start 命令）</p>
  <div class="field">
    <label>/start 欢迎文本</label>
    <textarea name="start_text" rows="4">{e('start_text')}</textarea>
    <div class="hint">使用工具栏进行格式化；支持 Telegram HTML：加粗、斜体、下划线、链接等</div>
  </div>
  <div class="field-row">
    <div class="field">
      <label>媒体类型</label>
      <select name="start_media_type">
        {media_type_options}
      </select>
      <div class="hint">选择后需在右侧填写对应的媒体 URL</div>
    </div>
    <div class="field">
      <label>媒体 URL</label>
      <input type="text" name="start_media_url" value="{e('start_media_url')}" placeholder="https://...">
      <div class="hint">图片或视频的直链地址</div>
    </div>
  </div>
  <div class="field">
    <label>欢迎消息内联按钮</label>
    <div class="hint" style="margin-bottom:8px">显示在欢迎文字下方的按钮，点击后跳转链接</div>
    <div id="start-btn-rows"></div>
    <button type="button" class="btn-add" id="start-btn-add">＋ 添加按钮</button>
    <input type="hidden" name="start_buttons_json" id="start_buttons_json">
  </div>
</div>

<div id="pane-keyboard" class="tab-pane">
  <p class="section-title">底部快捷键盘</p>
  <div class="hint" style="margin-bottom:14px">配置用户输入框下方的快捷按钮。可绑定内置功能，也可自定义回复内容。"行号"相同的按钮将显示在同一行（留空则独占一行）。</div>
  <div id="kb-rows"></div>
  <button type="button" class="btn-add" id="kb-add">＋ 添加按钮</button>
  <input type="hidden" name="keyboard_buttons_json" id="keyboard_buttons_json">
</div>

<div id="pane-template" class="tab-pane">
  <p class="section-title">报告填写模板</p>
  <div class="field">
    <label>模板名称</label>
    <input type="text" id="template-name" placeholder="例如：日报">
    <input type="hidden" name="report_template_json" id="report_template_json">
  </div>
  <div class="field">
    <label>模板字段</label>
    <div class="hint" style="margin-bottom:10px">每个字段可设置：英文标识（键名）、显示名称、类型（文本/图片）、是否必填、字段说明（提示用户如何填写）</div>
    <div id="template-fields"></div>
    <button type="button" class="btn-add" id="template-add">＋ 添加字段</button>
  </div>
</div>

<div id="pane-texts" class="tab-pane">
  <p class="section-title">功能文本配置</p>
  <div class="field">
    <label>查阅报告 — 帮助文本</label>
    <textarea name="search_help_text" rows="3">{e('search_help_text')}</textarea>
    <div class="hint">用户点击「查阅报告」后显示的提示，说明如何使用 @用户名 或 #标签 搜索</div>
  </div>
  <div class="field">
    <label>联系管理员 — 文本</label>
    <textarea name="contact_text" rows="3">{e('contact_text')}</textarea>
  </div>
  <div class="field">
    <label>操作方式 — 说明文本</label>
    <textarea name="usage_text" rows="5">{e('usage_text')}</textarea>
  </div>
</div>

<div id="pane-review" class="tab-pane">
  <p class="section-title">审核反馈通知</p>
  <div class="field">
    <label>审核通过 — 通知模板</label>
    <input type="text" name="review_approved_template" value="{e('review_approved_template')}">
    <div class="hint">使用 {{id}} 表示报告编号，{{link}} 表示报告链接，例如：✅ 报告 #{{id}} 审核通过。{{link}}</div>
  </div>
  <div class="field">
    <label>审核驳回 — 通知模板</label>
    <input type="text" name="review_rejected_template" value="{e('review_rejected_template')}">
    <div class="hint">使用 {{id}} 表示编号，{{reason}} 表示驳回原因，例如：❌ 报告 #{{id}} 未通过：{{reason}}</div>
  </div>
  <div class="field">
    <label>推送频道 — 推送模板</label>
    <textarea name="push_template" rows="4">{e('push_template')}</textarea>
    <div class="hint">支持占位符：{{id}} 报告编号、{{username}} 用户名、{{detail}} 报告字段内容、{{link}} 报告链接；点击上方字段按钮快速插入。<br>还可直接使用字段键名，如模板含 <code>title</code> 字段则可用 {{{{title}}}}（前后各两个大括号）。</div>
  </div>
  <div class="field">
    <label>推送详情字段 — 顺序与选择</label>
    <div class="hint" style="margin-bottom:8px">拖动排序或点击 ↑↓ 调整字段在 {{{{detail}}}} 中的显示顺序；点击 ✕ 从推送中排除该字段。留空则默认包含全部文本字段。</div>
    <div id="push-detail-fields-list"></div>
    <div id="push-fields-add-area" style="margin-top:8px"></div>
    <input type="hidden" name="push_detail_fields_json" id="push_detail_fields_json">
  </div>
</div>

<div class="save-bar" id="settings-save-bar">
  <button type="submit" class="btn btn-primary">💾 保存配置</button>
</div>

</form>
<form id="import-settings-form" method="post" action="/admin/import-settings"></form>

<div id="pane-pending" class="tab-pane">
  <p class="section-title">待审核报告（{pending_count} 条）</p>
  {pending_html}
</div>

<div id="pane-blacklist" class="tab-pane">
  <p class="section-title">黑名单管理</p>
  <div style="margin-bottom:16px">
    <form method="post" action="/admin/blacklist/ban" style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
      <div>
        <label style="font-size:.8rem;font-weight:600;color:#475569;display:block;margin-bottom:4px">用户 ID</label>
        <input type="text" name="user_id" placeholder="数字用户ID" style="width:140px;padding:6px 10px;border:1px solid #cbd5e1;border-radius:6px;font-size:.9rem">
      </div>
      <div>
        <label style="font-size:.8rem;font-weight:600;color:#475569;display:block;margin-bottom:4px">原因（可选）</label>
        <input type="text" name="reason" placeholder="限制原因" style="width:200px;padding:6px 10px;border:1px solid #cbd5e1;border-radius:6px;font-size:.9rem">
      </div>
      <button type="submit" class="btn btn-danger">🚫 加入黑名单</button>
    </form>
  </div>
  {blacklist_html}
</div>

<div id="pane-broadcast" class="tab-pane">
  <p class="section-title">广播发送（共 {user_count} 位用户曾使用机器人）</p>
  <form id="broadcast-form" method="post" action="/admin/broadcast">
    <div class="field">
      <label>广播文本</label>
      <textarea name="broadcast_text" rows="5" placeholder="使用工具栏格式化文字；点击字段按钮快速插入模板字段内容"></textarea>
    </div>
    <div class="field-row">
      <div class="field">
        <label>媒体类型</label>
        <select name="broadcast_media_type">
          <option value="">无</option>
          <option value="photo">图片</option>
          <option value="video">视频</option>
        </select>
        <div class="hint">选择后需在右侧填写对应的媒体 URL</div>
      </div>
      <div class="field">
        <label>媒体 URL</label>
        <input type="text" name="broadcast_media_url" placeholder="https://...">
        <div class="hint">图片或视频的直链地址</div>
      </div>
    </div>
    <div class="field">
      <label>内联按钮（可选）</label>
      <div class="hint" style="margin-bottom:8px">每行一个按钮，点击后跳转链接</div>
      <div id="broadcast-btn-rows"></div>
      <button type="button" class="btn-add" id="broadcast-btn-add">＋ 添加按钮</button>
      <input type="hidden" name="broadcast_buttons_json" id="broadcast_buttons_json">
    </div>
    <div style="margin-top:16px">
      <button type="submit" class="btn btn-primary">📢 发送广播</button>
    </div>
  </form>
</div>

</div>
</div>
<script>{js}</script>
</body>
</html>
"""


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    user = update.effective_user
    upsert_user(user.id, user.username)

    if is_user_banned(user.id):
        await update.message.reply_text("您已被限制使用此机器人。")
        return

    channel = setting_get("force_sub_channel", "").strip()
    if channel and not await is_subscribed(context.bot, user.id):
        rows = [[InlineKeyboardButton("我已订阅，重新检测", callback_data="retry_sub")]]
        channel_link = build_channel_link(channel)
        if channel_link:
            rows.insert(0, [InlineKeyboardButton("先去订阅", url=channel_link)])
        markup = InlineKeyboardMarkup(rows)
        await update.message.reply_text("请先订阅频道后再使用机器人。", reply_markup=markup)
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
            return

    await update.message.reply_text(
        render_report_preview(draft["values"], draft["template"]),
        parse_mode=ParseMode.HTML,
        reply_markup=_report_submit_keyboard(),
    )


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
                "SELECT * FROM reports WHERE id = %s AND status = 'approved'", (report_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="report not found")
        return report_to_html(row)

    def _auth(request: Request) -> RedirectResponse | None:
        if not config.admin_panel_token:
            return None
        cookie_token = request.cookies.get("admin_token", "")
        if cookie_token == config.admin_panel_token:
            return None
        query_token = request.query_params.get("token", "")
        if query_token == config.admin_panel_token:
            return None
        return RedirectResponse(url="/admin/login", status_code=303)

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
        if redirect := _auth(request):
            return redirect
        should_set_cookie = _should_set_admin_cookie(request)
        saved = request.query_params.get("saved") == "1"
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            pending_rows = conn.execute(
                "SELECT id, username, created_at, data_json FROM reports WHERE status = 'pending' ORDER BY id DESC LIMIT 50"
            ).fetchall()
            user_count_row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
            blacklist_rows = conn.execute(
                "SELECT user_id, username, reason, added_at FROM blacklist ORDER BY added_at DESC"
            ).fetchall()
        settings_map = {r["key"]: r["value"] for r in rows}
        pending_list = [dict(r) for r in pending_rows]
        user_count = user_count_row["cnt"] if user_count_row else 0
        blacklist_list = [dict(r) for r in blacklist_rows]
        response = HTMLResponse(build_admin_html(
            settings_map, pending_list, saved=saved,
            user_count=user_count, db_path="",
            blacklist=blacklist_list,
        ))
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
        push_template: str = Form(""),
        report_template_json: str = Form("{}"),
        push_detail_fields_json: str = Form("[]"),
        contact_text: str = Form(""),
        usage_text: str = Form(""),
        search_help_text: str = Form(""),
        report_link_base: str = Form(""),
    ):
        if redirect := _auth(request):
            return redirect
        try:
            start_buttons_obj = json.loads(start_buttons_json)
            keyboard_buttons_obj = json.loads(keyboard_buttons_json)
            report_template_obj = json.loads(report_template_json)
            push_detail_fields_obj = json.loads(push_detail_fields_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="JSON 配置格式错误")
        if not isinstance(start_buttons_obj, list):
            raise HTTPException(status_code=400, detail="start_buttons_json 必须是数组")
        if not isinstance(keyboard_buttons_obj, list):
            raise HTTPException(status_code=400, detail="keyboard_buttons_json 必须是数组")
        if not isinstance(report_template_obj, dict):
            raise HTTPException(status_code=400, detail="report_template_json 必须是对象")
        if not isinstance(push_detail_fields_obj, list):
            push_detail_fields_obj = []

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
            "push_template": push_template,
            "report_template_json": json.dumps(report_template_obj, ensure_ascii=False),
            "push_detail_fields_json": json.dumps(push_detail_fields_obj, ensure_ascii=False),
            "contact_text": contact_text,
            "usage_text": usage_text,
            "search_help_text": search_help_text,
            "report_link_base": report_link_base.strip(),
        }
        for key, value in updates.items():
            setting_set(key, value)
        return RedirectResponse(url="/admin?saved=1", status_code=303)

    @web.get("/admin/settings")
    async def admin_settings(request: Request):
        if redirect := _auth(request):
            return redirect
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---- Admin verification flow (Feature 5) ----

    @web.get("/admin/verify", response_class=HTMLResponse)
    async def admin_verify_page():
        _cleanup_verify_state()
        code = secrets.token_hex(6).upper()  # 12 uppercase hex chars (48 bits entropy)
        _verify_codes[code] = time.time() + _VERIFY_CODE_TTL
        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>管理员验证</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
.card{{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);padding:36px 32px;max-width:440px;width:100%;text-align:center}}
h2{{font-size:1.3rem;color:#1e293b;margin-bottom:8px}}
p{{color:#64748b;font-size:.9rem;margin-bottom:24px;line-height:1.6}}
.code-box{{background:#f8fafc;border:2px dashed #93c5fd;border-radius:10px;padding:20px;margin:20px 0;font-size:2rem;font-weight:700;letter-spacing:.3em;color:#2563eb;font-family:monospace}}
.step{{background:#eff6ff;border-radius:8px;padding:12px 16px;text-align:left;font-size:.85rem;color:#1d4ed8;line-height:1.8;margin-bottom:16px}}
.waiting{{color:#64748b;font-size:.85rem;margin-top:16px}}
</style>
</head>
<body>
<div class="card">
  <h2>🔐 管理员身份验证</h2>
  <p>为保障安全，请通过 Telegram 机器人完成验证。</p>
  <div class="step">
    <b>操作步骤：</b><br>
    1. 复制下方验证码<br>
    2. 打开 Telegram 与机器人对话<br>
    3. 将验证码发送给机器人<br>
    4. 机器人确认后，此页面将自动跳转到后台
  </div>
  <div class="code-box" id="code-display">{code}</div>
  <p class="waiting" id="status-msg">⏳ 等待您在 Telegram 中发送验证码…</p>
</div>
<script>
(function(){{
  var code='{code}';
  var interval=setInterval(function(){{
    fetch('/admin/verify/status?code='+encodeURIComponent(code))
      .then(function(r){{return r.json();}})
      .then(function(d){{
        if(d.status==='verified'&&d.redirect){{
          clearInterval(interval);
          document.getElementById('status-msg').textContent='✅ 验证成功，正在跳转…';
          window.location.href=d.redirect;
        }} else if(d.status==='expired'){{
          clearInterval(interval);
          document.getElementById('status-msg').textContent='❌ 验证码已过期，请刷新页面重新获取。';
        }}
      }}).catch(function(){{}});
  }},3000);
}})();
</script>
</body>
</html>
""")

    @web.get("/admin/verify/status")
    async def admin_verify_status(code: str = ""):
        _cleanup_verify_state()
        if not code or code not in _verify_codes:
            return JSONResponse({"status": "expired"})
        if time.time() > _verify_codes[code]:
            _verify_codes.pop(code, None)
            _verify_code_otps.pop(code, None)
            return JSONResponse({"status": "expired"})
        otp = _verify_code_otps.get(code)
        if otp:
            return JSONResponse({"status": "verified", "redirect": f"/admin/otp?token={otp}"})
        return JSONResponse({"status": "pending"})

    @web.get("/admin/otp", response_class=HTMLResponse)
    async def admin_otp_login(request: Request, token: str = ""):
        _cleanup_verify_state()
        if not token or token not in _otp_tokens:
            return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'>❌ 链接无效或已过期，请重新验证。<br><a href='/admin/verify'>重新验证</a></body></html>", status_code=403)
        if time.time() > _otp_tokens[token]:
            _otp_tokens.pop(token, None)
            return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'>❌ 登录链接已过期，请重新验证。<br><a href='/admin/verify'>重新验证</a></body></html>", status_code=403)
        # Consume the token
        _otp_tokens.pop(token, None)
        if not config.admin_panel_token:
            return RedirectResponse(url="/admin", status_code=303)
        # Return an HTML page that sets the cookie *before* navigating to /admin.
        # Using a plain 303 redirect with Set-Cookie can be unreliable in some
        # browsers/WebViews (e.g. Telegram's built-in browser) because the cookie
        # may not be committed to storage before the browser issues the follow-up
        # GET /admin request.  An explicit JS + meta-refresh redirect from a 200
        # response guarantees the cookie is stored first.
        response = HTMLResponse("""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=/admin">
<script>window.location.replace('/admin');</script>
</head>
<body style="font-family:sans-serif;padding:40px">
✅ 验证成功，正在跳转到后台…<br>
如果页面没有自动跳转，请<a href="/admin">点击这里</a>。
</body></html>""")
        response.set_cookie(
            key="admin_token",
            value=config.admin_panel_token,
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
        )
        return response

    # ---- Blacklist web routes ----

    @web.post("/admin/blacklist/ban")
    async def web_blacklist_ban(request: Request, user_id: str = Form(""), reason: str = Form("")):
        if redirect := _auth(request):
            return redirect
        try:
            uid = int(user_id.strip())
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="用户ID必须是数字")
        ban_user(uid, None, reason.strip() or "管理员限制")
        return RedirectResponse(url="/admin#tab=blacklist", status_code=303)

    @web.post("/admin/blacklist/unban/{user_id}")
    async def web_blacklist_unban(user_id: int, request: Request):
        if redirect := _auth(request):
            return redirect
        unban_user(user_id)
        return RedirectResponse(url="/admin#tab=blacklist", status_code=303)

    # ---- Settings export / import ----

    @web.get("/admin/export-settings")
    async def admin_export_settings(request: Request):
        if redirect := _auth(request):
            return redirect
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        data = {r["key"]: r["value"] for r in rows}
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=baogao-settings.json"},
        )

    @web.post("/admin/import-settings")
    async def admin_import_settings(request: Request, settings_json: str = Form("")):
        if redirect := _auth(request):
            return redirect
        try:
            data = json.loads(settings_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="JSON 格式错误，请检查导入内容")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="JSON 必须是对象格式")
        allowed_keys = set(DEFAULT_SETTINGS.keys())
        imported = 0
        for key, value in data.items():
            if key in allowed_keys and isinstance(value, str):
                setting_set(key, value)
                imported += 1
        safe_count = html.escape(str(imported))
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;padding:40px'>✅ 成功导入 {safe_count} 项配置。<a href='/admin?saved=1'>返回后台</a></body></html>"
        )

    @web.post("/admin/approve/{report_id}")
    async def web_approve_report(report_id: int, request: Request):
        if redirect := _auth(request):
            return redirect
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="报告不存在")
            if report["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"报告已处于 {report['status']} 状态")
            conn.execute(
                "UPDATE reports SET status='approved', reviewed_at=%s WHERE id = %s",
                (utc_now_iso(), report_id),
            )
        try:
            channel_link = await _push_report_to_channel(web.state.tg_application.bot, report_id, report)
        except Exception:
            logger.warning("failed to push report %s to channel", report_id, exc_info=True)
            channel_link = ""
        feedback = _build_approval_feedback(report_id, channel_link=channel_link)
        try:
            await web.state.tg_application.bot.send_message(chat_id=report["user_id"], text=feedback)
        except Exception:
            logger.warning("failed to notify user %s of approval", report["user_id"], exc_info=True)
        safe_id = html.escape(str(report_id))
        return HTMLResponse(f"<html><body>报告 #{safe_id} 已通过。<a href='/admin'>返回</a></body></html>")

    @web.post("/admin/reject/{report_id}")
    async def web_reject_report(report_id: int, request: Request, reason: str = Form(default="请联系管理员")):
        if redirect := _auth(request):
            return redirect
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="报告不存在")
            if report["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"报告已处于 {report['status']} 状态")
            conn.execute(
                "UPDATE reports SET status='rejected', review_feedback=%s, reviewed_at=%s WHERE id = %s",
                (reason.strip() or "请联系管理员", utc_now_iso(), report_id),
            )
        tpl = (
            setting_get("review_rejected_template", "").strip()
            or DEFAULT_SETTINGS["review_rejected_template"]
        )
        feedback = safe_format(tpl, id=report_id, reason=reason.strip() or "请联系管理员")
        try:
            await web.state.tg_application.bot.send_message(chat_id=report["user_id"], text=feedback)
        except Exception:
            logger.warning("failed to notify user %s of rejection", report["user_id"], exc_info=True)
        safe_id = html.escape(str(report_id))
        return HTMLResponse(f"<html><body>报告 #{safe_id} 已驳回。<a href='/admin'>返回</a></body></html>")

    async def _do_broadcast(
        bot: Bot,
        user_ids: list[int],
        text: str,
        media_type: str,
        media_url: str,
        markup: InlineKeyboardMarkup | None,
    ) -> None:
        # Send in batches with a short delay to stay within Telegram rate limits
        _BATCH_SIZE = 25
        _BATCH_DELAY = 1.0  # seconds between batches
        for batch_start in range(0, len(user_ids), _BATCH_SIZE):
            batch = user_ids[batch_start : batch_start + _BATCH_SIZE]
            for uid in batch:
                try:
                    if media_type == "photo" and media_url:
                        await bot.send_photo(
                            chat_id=uid,
                            photo=media_url,
                            caption=text or None,
                            parse_mode=ParseMode.HTML if text else None,
                            reply_markup=markup,
                        )
                    elif media_type == "video" and media_url:
                        await bot.send_video(
                            chat_id=uid,
                            video=media_url,
                            caption=text or None,
                            parse_mode=ParseMode.HTML if text else None,
                            reply_markup=markup,
                        )
                    elif text:
                        await bot.send_message(
                            chat_id=uid,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=markup,
                        )
                except Exception:
                    logger.warning("broadcast failed for user %s", uid, exc_info=True)
            if batch_start + _BATCH_SIZE < len(user_ids):
                await asyncio.sleep(_BATCH_DELAY)

    @web.post("/admin/broadcast")
    async def admin_broadcast(
        request: Request,
        background_tasks: BackgroundTasks,
        broadcast_text: str = Form(""),
        broadcast_media_type: str = Form(""),
        broadcast_media_url: str = Form(""),
        broadcast_buttons_json: str = Form("[]"),
    ):
        if redirect := _auth(request):
            return redirect
        with db_connection() as conn:
            user_rows = conn.execute("SELECT user_id FROM users").fetchall()
        user_ids = [r["user_id"] for r in user_rows]
        buttons_obj = parse_json(broadcast_buttons_json, [])
        markup: InlineKeyboardMarkup | None = None
        if isinstance(buttons_obj, list) and buttons_obj:
            btns: list[list[InlineKeyboardButton]] = []
            for item in buttons_obj:
                if isinstance(item, dict) and item.get("text") and item.get("url"):
                    btns.append([InlineKeyboardButton(str(item["text"]), url=str(item["url"]))])
            if btns:
                markup = InlineKeyboardMarkup(btns)
        background_tasks.add_task(
            _do_broadcast,
            web.state.tg_application.bot,
            user_ids,
            broadcast_text,
            broadcast_media_type.strip(),
            broadcast_media_url.strip(),
            markup,
        )
        safe_count = html.escape(str(len(user_ids)))
        return HTMLResponse(
            f"<html><body>广播任务已提交，将向 {safe_count} 位用户发送。<a href='/admin'>返回</a></body></html>"
        )

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
