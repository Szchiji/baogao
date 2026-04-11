import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("report-bot")

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
            {"text": "我的报告", "action": "my_reports"},
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
    "usage_text": "1. 点击「写报告」填写模板\n2. 填完后提交审核\n3. 审核通过后可查阅。",
    "search_help_text": "发送 @用户名 或 #标签 查询报告。",
    "report_link_base": "",
    "push_detail_fields_json": "[]",
    "push_photos_enabled": "1",
    "pending_reminder_threshold_hours": "24",
    "pending_reminder_interval_hours": "2",
    "clone_mode_enabled": "0",
    "clone_botfather_link": "",
    "clone_text": "🤖 点击下方按钮即可一键克隆此机器人，无需手动操作 BotFather！",
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
