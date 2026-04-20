import html
import logging
import os
from typing import Any
from urllib.parse import urlparse

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.crud import setting_get
from app.utils import parse_json

logger = logging.getLogger("report-bot")


def keyboard_config(bot_id: str = "") -> list[dict[str, str]]:
    items = parse_json(setting_get("keyboard_buttons_json", bot_id=bot_id), [])
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


def report_template(bot_id: str = "") -> dict[str, Any]:
    data = parse_json(setting_get("report_template_json", bot_id=bot_id), {})
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


def _make_field_prompt(
    field: dict[str, Any],
    sequential: bool = True,
    current_idx: int = 0,
    total: int = 0,
    prev_key: str | None = None,
) -> tuple[str, "InlineKeyboardMarkup"]:
    """Return (prompt_text, markup) for prompting a field value. Always includes a cancel button."""
    label = field["label"]
    hint = field.get("hint", "")
    field_type = field.get("type", "text")
    required = field.get("required", True)

    progress = f"（第 {current_idx + 1} / {total} 项）\n" if total > 0 else ""

    if field_type == "photo":
        prompt = f"{progress}请发送「{label}」的图片"
    else:
        prompt = f"{progress}请输入「{label}」"

    if hint:
        prompt += f"\n\n💡 {hint}"

    buttons: list[list[InlineKeyboardButton]] = []
    if prev_key and sequential:
        buttons.append([InlineKeyboardButton("← 返回上一项", callback_data=f"back_field:{prev_key}")])
    if not required and sequential:
        prompt += "\n\n（此项为可选，可跳过不填写）"
        buttons.append([InlineKeyboardButton("⏭ 跳过此项", callback_data=f"skip_field:{field['key']}")])
    buttons.append([InlineKeyboardButton("❌ 取消填写", callback_data="cancel_report")])
    return prompt, InlineKeyboardMarkup(buttons)


def get_force_sub_channels(bot_id: str = "") -> list[str]:
    """Return the list of configured force-subscribe channel IDs / usernames."""
    raw = setting_get("force_sub_channel", "", bot_id=bot_id).strip()
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def get_push_channels(bot_id: str = "") -> list[str]:
    """Return the list of configured push channel IDs / usernames."""
    raw = setting_get("push_channel", "", bot_id=bot_id).strip()
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


async def is_subscribed(bot: Bot, user_id: int, bot_id: str = "") -> bool:
    """Return True only when the user is subscribed to ALL force-subscribe channels."""
    channels = get_force_sub_channels(bot_id=bot_id)
    if not channels:
        return True
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in {"left", "kicked"}:
                return False
        except Exception:
            logger.warning(
                "subscription check failed for channel %s user %s", channel, user_id, exc_info=True
            )
    return True


def start_keyboard(bot_id: str = "") -> ReplyKeyboardMarkup:
    items = keyboard_config(bot_id=bot_id)
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


def _normalize_admin_url(base_url: str) -> str:
    """Return the /admin URL, stripping any existing admin sub-paths first."""
    base = base_url.rstrip("/")
    for suffix in ("/admin/login", "/admin"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/admin"


def start_inline_buttons(user_id: int | None = None, admin_panel_url: str | None = None, bot_id: str = "") -> InlineKeyboardMarkup | None:
    raw_buttons = parse_json(setting_get("start_buttons_json", bot_id=bot_id), [])
    if admin_panel_url is None:
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
    buttons.append([
        InlineKeyboardButton("✅ 提交审核", callback_data="submit_report"),
        InlineKeyboardButton("❌ 取消", callback_data="cancel_report"),
    ])
    return InlineKeyboardMarkup(buttons)


def _report_submit_keyboard() -> InlineKeyboardMarkup:
    """Return a keyboard with only Submit and Cancel buttons for the final report preview."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 提交审核", callback_data="submit_report"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_report"),
        ]
    ])


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
