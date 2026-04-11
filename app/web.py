import asyncio
import csv
import html
import io
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application

from app.admin_auth import (
    _cleanup_verify_state,
    _otp_tokens,
    _OTP_TOKEN_TTL,
    _verify_code_otps,
    _verify_codes,
    _VERIFY_CODE_TTL,
    create_child_admin_session,
    get_child_admin_id,
    get_child_session_info,
)
from app.admin_panel import build_admin_html, report_to_html
from app.bot_handlers import _build_approval_feedback, _build_reject_markup, _push_report_to_channel
from app.config import AppConfig, DEFAULT_SETTINGS
from app.crud import ban_user, log_audit, setting_get, setting_set, unban_user, add_child_bot, remove_child_bot, list_child_bots, set_child_bot_active
from app.database import db_connection, init_bot_settings
from app.utils import parse_json, safe_format, utc_now_iso

logger = logging.getLogger("report-bot")


async def _do_broadcast(
    bot: Any,
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


def _error_page(message: str, link_text: str = "重新验证", link_href: str = "/admin/verify") -> str:
    msg_escaped = html.escape(message)
    lt_escaped = html.escape(link_text)
    lh_escaped = html.escape(link_href)
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>错误</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#070912;background-image:radial-gradient(ellipse 80% 50% at 50% 50%,rgba(244,63,94,.05) 0%,transparent 70%);display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;-webkit-font-smoothing:antialiased}}
.card{{background:rgba(255,255,255,.04);backdrop-filter:blur(22px) saturate(160%) brightness(1.03);-webkit-backdrop-filter:blur(22px) saturate(160%) brightness(1.03);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.06);padding:40px 36px;width:100%;max-width:420px;text-align:center;position:relative;overflow:hidden}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent 5%,rgba(255,255,255,.11) 40%,rgba(255,255,255,.13) 50%,rgba(255,255,255,.11) 60%,transparent 95%);pointer-events:none}}
.card::after{{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(135deg,rgba(255,255,255,.05) 0%,transparent 45%);pointer-events:none;border-radius:inherit}}
.icon{{font-size:2.8rem;margin-bottom:16px;position:relative;z-index:1}}
h2{{font-size:1.1rem;font-weight:700;color:#dde2ed;margin-bottom:8px;position:relative;z-index:1}}
p{{font-size:.85rem;color:#8b95b0;line-height:1.6;margin-bottom:24px;position:relative;z-index:1}}
a{{display:inline-flex;align-items:center;justify-content:center;padding:10px 24px;min-height:40px;background:#6366f1;color:#fff;border-radius:8px;text-decoration:none;font-size:.88rem;font-weight:600;transition:background .15s,box-shadow .15s,transform .12s;box-shadow:0 2px 8px rgba(99,102,241,.35);position:relative;z-index:1}}
a:hover{{background:#4f46e5;box-shadow:0 4px 16px rgba(99,102,241,.45);transform:translateY(-1px)}}
a:focus-visible{{outline:none;box-shadow:0 0 0 2px rgba(7,9,18,1),0 0 0 4px #6366f1}}
</style>
</head>
<body>
<div class="card">
  <div class="icon">❌</div>
  <h2>验证失败</h2>
  <p>{msg_escaped}</p>
  <a href="{lh_escaped}">{lt_escaped}</a>
</div>
</body>
</html>"""


def create_fastapi(application: Application, config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        from app import bot_manager

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
        # Start all active child bots in polling mode.
        n = await bot_manager.start_all_from_db()
        if n:
            logger.info("Started %d child bot(s)", n)
        try:
            yield
        finally:
            await bot_manager.stop_all()
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
        try:
            with db_connection() as conn:
                conn.execute("SELECT 1")
            return {"ok": True, "db": "ok"}
        except Exception:
            logger.warning("healthz: DB check failed", exc_info=True)
            return JSONResponse({"ok": False, "db": "unavailable"}, status_code=503)

    @web.get("/reports/{report_id}", response_class=HTMLResponse)
    async def report_detail(report_id: str):
        with db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE id = %s AND status = 'approved'", (report_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="report not found")
        return report_to_html(row)

    def _get_request_child_admin_id(request: Request) -> int | None:
        """Return the owner_user_id if the request carries a valid child-admin session."""
        info = _get_request_child_session_info(request)
        return info["owner_user_id"] if info else None

    def _get_request_child_session_info(request: Request) -> dict | None:
        """Return {"owner_user_id": int, "bot_id": str} for a valid child-admin session, or None."""
        session = request.cookies.get("admin_child_session", "")
        if not session:
            return None
        return get_child_session_info(session)

    def _get_request_bot_id(request: Request) -> str:
        """Return the bot_id scoped to the current request's session.

        Main admins always operate on the main bot (bot_id='').
        Child admins operate on their own child bot's partition.
        """
        info = _get_request_child_session_info(request)
        return info["bot_id"] if info else ""

    def _auth(request: Request) -> RedirectResponse | None:
        if not config.admin_panel_token:
            return None
        cookie_token = request.cookies.get("admin_token", "")
        if cookie_token == config.admin_panel_token:
            return None
        query_token = request.query_params.get("token", "")
        if query_token == config.admin_panel_token:
            return None
        # Child-bot sub-admins log in with a restricted session cookie instead.
        if _get_request_child_session_info(request) is not None:
            return None
        return RedirectResponse(url="/admin/login", status_code=303)

    def _is_main_admin(request: Request) -> bool:
        """Return True only for the full (main) admin — not child-admin sessions."""
        if not config.admin_panel_token:
            return True
        cookie_token = request.cookies.get("admin_token", "")
        if cookie_token == config.admin_panel_token:
            return True
        query_token = request.query_params.get("token", "")
        return query_token == config.admin_panel_token

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
        return HTMLResponse("""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>管理员登录</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#070912;background-image:radial-gradient(ellipse 80% 50% at 30% 40%,rgba(99,102,241,.06) 0%,transparent 70%);display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;-webkit-font-smoothing:antialiased}
.card{background:rgba(255,255,255,.04);backdrop-filter:blur(22px) saturate(160%) brightness(1.03);-webkit-backdrop-filter:blur(22px) saturate(160%) brightness(1.03);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.06);padding:40px 36px;width:100%;max-width:420px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent 5%,rgba(255,255,255,.11) 40%,rgba(255,255,255,.13) 50%,rgba(255,255,255,.11) 60%,transparent 95%);pointer-events:none}
.card::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(135deg,rgba(255,255,255,.05) 0%,transparent 45%);pointer-events:none;border-radius:inherit}
.logo{text-align:center;margin-bottom:28px;position:relative;z-index:1}
.logo-icon{font-size:2.2rem;margin-bottom:10px}
.logo h1{font-size:1.15rem;font-weight:700;color:#dde2ed;letter-spacing:-.012em}
.logo p{font-size:.83rem;color:#8b95b0;margin-top:5px}
label{display:block;font-size:.71rem;font-weight:600;color:#8b95b0;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
input[type=password]{width:100%;padding:9px 13px;min-height:40px;border:1px solid rgba(255,255,255,.1);border-radius:8px;font-size:.88rem;font-family:inherit;background:rgba(0,0,0,.3);color:#dde2ed;transition:border-color .15s,box-shadow .15s;-webkit-appearance:none}
input[type=password]::placeholder{color:#5a6480;opacity:.75}
input[type=password]:focus{outline:none;border-color:rgba(99,102,241,.55);box-shadow:0 0 0 3px rgba(99,102,241,.13)}
.field{margin-bottom:20px;position:relative;z-index:1}
.btn{display:flex;align-items:center;justify-content:center;width:100%;padding:10px;min-height:42px;background:#6366f1;color:#fff;border:none;border-radius:8px;font-size:.88rem;font-weight:600;font-family:inherit;cursor:pointer;transition:background .15s,box-shadow .15s,transform .12s;box-shadow:0 2px 8px rgba(99,102,241,.35);position:relative;z-index:1}
.btn:hover{background:#4f46e5;box-shadow:0 4px 16px rgba(99,102,241,.45);transform:translateY(-1px)}
.btn:focus-visible{outline:none;box-shadow:0 0 0 2px rgba(7,9,18,1),0 0 0 4px #6366f1}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">📋</div>
    <h1>报告机器人管理后台</h1>
    <p>请输入管理员密码以继续</p>
  </div>
  <form method="get" action="/admin">
    <div class="field">
      <label for="token">管理员密码</label>
      <input type="password" id="token" name="token" placeholder="ADMIN_PANEL_TOKEN" autofocus autocomplete="current-password">
    </div>
    <button type="submit" class="btn">🔐 登录</button>
  </form>
</div>
</body>
</html>""")

    @web.get("/admin/logout")
    async def admin_logout():
        response = HTMLResponse("已退出。")
        response.delete_cookie("admin_token")
        response.delete_cookie("admin_child_session")
        return response

    @web.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request):
        if redirect := _auth(request):
            return redirect
        child_admin_id = _get_request_child_admin_id(request)
        is_child_admin = child_admin_id is not None
        bot_id = _get_request_bot_id(request)
        should_set_cookie = _should_set_admin_cookie(request)
        saved = request.query_params.get("saved") == "1"
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings WHERE bot_id = %s", (bot_id,)).fetchall()
            pending_rows = conn.execute(
                "SELECT id, username, created_at, data_json FROM reports WHERE bot_id = %s AND status = 'pending' ORDER BY id DESC LIMIT 50",
                (bot_id,),
            ).fetchall()
            user_count_row = conn.execute("SELECT COUNT(*) as cnt FROM users WHERE bot_id = %s", (bot_id,)).fetchone()
            blacklist_rows = conn.execute(
                "SELECT user_id, username, reason, added_at FROM blacklist WHERE bot_id = %s ORDER BY added_at DESC",
                (bot_id,),
            ).fetchall()
            all_report_rows = conn.execute(
                "SELECT id, username, created_at, status, data_json, review_feedback, channel_message_link FROM reports WHERE bot_id = %s ORDER BY id DESC LIMIT 200",
                (bot_id,),
            ).fetchall()
            report_stats = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM reports WHERE bot_id = %s GROUP BY status",
                (bot_id,),
            ).fetchall()
        settings_map = {r["key"]: r["value"] for r in rows}
        pending_list = [dict(r) for r in pending_rows]
        user_count = user_count_row["cnt"] if user_count_row else 0
        blacklist_list = [dict(r) for r in blacklist_rows]
        stats = {r["status"]: r["cnt"] for r in report_stats}
        stats["total_reports"] = sum(stats.values())
        all_reports_list = [dict(r) for r in all_report_rows]
        # Child-admin default tab is the pending review queue.
        default_tab = "pending" if is_child_admin else "basic"
        response = HTMLResponse(build_admin_html(
            settings_map, pending_list, saved=saved,
            user_count=user_count, db_path="",
            blacklist=blacklist_list,
            all_reports=all_reports_list,
            stats=stats,
            initial_tab=request.query_params.get("tab", default_tab),
            is_child_admin=is_child_admin,
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
        push_photos_enabled: str = Form(""),
        pending_reminder_threshold_hours: str = Form(""),
        pending_reminder_interval_hours: str = Form(""),
        contact_text: str = Form(""),
        usage_text: str = Form(""),
        search_help_text: str = Form(""),
        report_link_base: str = Form(""),
    ):
        if redirect := _auth(request):
            return redirect
        bot_id = _get_request_bot_id(request)
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

        # Validate numeric fields
        def _safe_int(value: str, default: int, min_val: int, max_val: int) -> str:
            try:
                v = int(value.strip())
                return str(max(min_val, min(max_val, v)))
            except (ValueError, AttributeError):
                return str(default)

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
            "push_photos_enabled": "1" if push_photos_enabled.strip() == "1" else "0",
            "pending_reminder_threshold_hours": _safe_int(pending_reminder_threshold_hours, 24, 1, 720),
            "pending_reminder_interval_hours": _safe_int(pending_reminder_interval_hours, 2, 1, 168),
            "contact_text": contact_text,
            "usage_text": usage_text,
            "search_help_text": search_help_text,
            "report_link_base": report_link_base.strip(),
        }
        for key, value in updates.items():
            setting_set(key, value, bot_id=bot_id)
        return RedirectResponse(url="/admin?saved=1", status_code=303)

    @web.get("/admin/settings")
    async def admin_settings(request: Request):
        if redirect := _auth(request):
            return redirect
        bot_id = _get_request_bot_id(request)
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings WHERE bot_id = %s", (bot_id,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---- Admin verification flow ----

    @web.get("/admin/verify", response_class=HTMLResponse)
    async def admin_verify_page():
        _cleanup_verify_state()
        code = secrets.token_hex(6).upper()  # 12 uppercase hex chars (48 bits entropy)
        _verify_codes[code] = time.time() + _VERIFY_CODE_TTL
        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>管理员验证</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#070912;background-image:radial-gradient(ellipse 80% 50% at 50% 40%,rgba(99,102,241,.07) 0%,transparent 70%);display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;-webkit-font-smoothing:antialiased}}
.card{{background:rgba(255,255,255,.04);backdrop-filter:blur(22px) saturate(160%) brightness(1.03);-webkit-backdrop-filter:blur(22px) saturate(160%) brightness(1.03);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.06);padding:36px 32px;max-width:440px;width:100%;text-align:center;position:relative;overflow:hidden}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent 5%,rgba(255,255,255,.11) 40%,rgba(255,255,255,.13) 50%,rgba(255,255,255,.11) 60%,transparent 95%);pointer-events:none}}
.card::after{{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(135deg,rgba(255,255,255,.05) 0%,transparent 45%);pointer-events:none;border-radius:inherit}}
h2{{font-size:1.15rem;color:#dde2ed;margin-bottom:8px;font-weight:700;position:relative;z-index:1}}
p{{color:#8b95b0;font-size:.85rem;margin-bottom:20px;line-height:1.6;position:relative;z-index:1}}
.code-box{{background:rgba(99,102,241,.1);border:1.5px dashed rgba(99,102,241,.4);border-radius:10px;padding:20px;margin:20px 0;font-size:1.9rem;font-weight:700;letter-spacing:.3em;color:#a5b4fc;font-family:monospace;position:relative;z-index:1;cursor:pointer;transition:background .15s}}
.code-box:hover{{background:rgba(99,102,241,.15)}}
.step{{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.2);border-radius:9px;padding:12px 16px;text-align:left;font-size:.83rem;color:#93c5fd;line-height:1.9;margin-bottom:16px;position:relative;z-index:1}}
.step b{{color:#a5b4fc}}
.waiting{{color:#5a6480;font-size:.83rem;margin-top:16px;position:relative;z-index:1}}
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
  <div class="code-box" id="code-display" title="点击复制" tabindex="0" role="button" aria-label="验证码：{code}，点击复制">{code}</div>
  <p class="waiting" id="status-msg" aria-live="polite">⏳ 等待您在 Telegram 中发送验证码…</p>
</div>
<script>
(function(){{
  var code='{code}';
  // Copy on click
  var box=document.getElementById('code-display');
  if(box){{
    function copyCode(){{
      try{{navigator.clipboard.writeText(code);box.title='已复制！';setTimeout(function(){{box.title='点击复制';}},2000);}}catch(e){{}}
    }}
    box.addEventListener('click',copyCode);
    box.addEventListener('keydown',function(e){{if(e.key==='Enter'||e.key===' '){{e.preventDefault();copyCode();}}}}); 
  }}
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
            return HTMLResponse(_error_page("链接无效或已过期，请重新获取验证码。"), status_code=403)
        token_data = _otp_tokens.get(token)
        is_dict_format = isinstance(token_data, dict)
        expiry = token_data["expiry"] if is_dict_format else token_data
        if time.time() > expiry:
            _otp_tokens.pop(token, None)
            return HTMLResponse(_error_page("登录链接已过期，请重新获取验证码。"), status_code=403)
        owner_user_id: int | None = token_data.get("owner_user_id") if is_dict_format else None
        token_bot_id: str = (token_data.get("bot_id") or "") if is_dict_format else ""
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
        redirect_url = "/admin?tab=pending" if owner_user_id is not None else "/admin"
        response = HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={redirect_url}">
<script>window.location.replace('{redirect_url}');</script>
</head>
<body style="font-family:sans-serif;padding:40px">
✅ 验证成功，正在跳转到后台…<br>
如果页面没有自动跳转，请<a href="{redirect_url}">点击这里</a>。
</body></html>""")
        if owner_user_id is not None:
            # Child-bot sub-admin: issue a restricted session cookie instead of
            # the full admin_panel_token cookie so the panel can show a limited view.
            session_token = create_child_admin_session(owner_user_id, bot_id=token_bot_id)
            response.set_cookie(
                key="admin_child_session",
                value=session_token,
                httponly=True,
                samesite="lax",
                secure=_is_secure_request(request),
            )
        else:
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
        bot_id = _get_request_bot_id(request)
        ban_user(uid, None, reason.strip() or "管理员限制", bot_id=bot_id)
        return RedirectResponse(url="/admin#tab=blacklist", status_code=303)

    @web.post("/admin/blacklist/unban/{user_id}")
    async def web_blacklist_unban(user_id: int, request: Request):
        if redirect := _auth(request):
            return redirect
        bot_id = _get_request_bot_id(request)
        unban_user(user_id, bot_id=bot_id)
        return RedirectResponse(url="/admin#tab=blacklist", status_code=303)

    # ---- Settings export / import ----

    @web.get("/admin/export-settings")
    async def admin_export_settings(request: Request):
        if redirect := _auth(request):
            return redirect
        bot_id = _get_request_bot_id(request)
        with db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings WHERE bot_id = %s", (bot_id,)).fetchall()
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
        bot_id = _get_request_bot_id(request)
        # Main admin can import for main bot; child admin can only import for own bot.
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
                setting_set(key, value, bot_id=bot_id)
                imported += 1
        return RedirectResponse(url="/admin?saved=1", status_code=303)

    @web.post("/admin/approve/{report_id}")
    async def web_approve_report(report_id: str, request: Request):
        if redirect := _auth(request):
            return redirect
        from app import bot_manager
        bot_id = _get_request_bot_id(request)
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s AND bot_id = %s", (report_id, bot_id)).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="报告不存在")
            if report["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"报告已处于 {report['status']} 状态")
            conn.execute(
                "UPDATE reports SET status='approved', reviewed_at=%s WHERE id = %s",
                (utc_now_iso(), report_id),
            )
        log_audit(0, "web_approve", int(report_id))
        # Use the child bot's Telegram Bot object when approving for a child bot.
        tg_bot = bot_manager.get_bot_by_bot_id(bot_id) or web.state.tg_application.bot
        try:
            channel_link = await _push_report_to_channel(tg_bot, report_id, report, bot_id=bot_id)
        except Exception:
            logger.warning("failed to push report %s to channel", report_id, exc_info=True)
            channel_link = ""
        feedback = _build_approval_feedback(report_id, channel_link=channel_link, bot_id=bot_id)
        try:
            await tg_bot.send_message(chat_id=report["user_id"], text=feedback)
        except Exception:
            logger.warning("failed to notify user %s of approval", report["user_id"], exc_info=True)
        return RedirectResponse(url="/admin?tab=pending", status_code=303)

    @web.post("/admin/reject/{report_id}")
    async def web_reject_report(report_id: str, request: Request, reason: str = Form(default="请联系管理员")):
        if redirect := _auth(request):
            return redirect
        from app import bot_manager
        bot_id = _get_request_bot_id(request)
        with db_connection() as conn:
            report = conn.execute("SELECT * FROM reports WHERE id = %s AND bot_id = %s", (report_id, bot_id)).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="报告不存在")
            if report["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"报告已处于 {report['status']} 状态")
            conn.execute(
                "UPDATE reports SET status='rejected', review_feedback=%s, reviewed_at=%s WHERE id = %s",
                (reason.strip() or "请联系管理员", utc_now_iso(), report_id),
            )
        log_audit(0, "web_reject", int(report_id), note=reason.strip())
        tg_bot = bot_manager.get_bot_by_bot_id(bot_id) or web.state.tg_application.bot
        tpl = (
            setting_get("review_rejected_template", "", bot_id=bot_id).strip()
            or DEFAULT_SETTINGS["review_rejected_template"]
        )
        feedback = safe_format(tpl, id=report_id, reason=reason.strip() or "请联系管理员")
        try:
            await tg_bot.send_message(
                chat_id=report["user_id"], text=feedback,
                reply_markup=_build_reject_markup(int(report_id)),
            )
        except Exception:
            logger.warning("failed to notify user %s of rejection", report["user_id"], exc_info=True)
        return RedirectResponse(url="/admin?tab=pending", status_code=303)

    @web.post("/admin/batch-approve")
    async def web_batch_approve(request: Request, ids: str = Form("")):
        """Approve all specified pending report IDs in one go."""
        if redirect := _auth(request):
            return redirect
        from app import bot_manager
        bot_id = _get_request_bot_id(request)
        tg_bot = bot_manager.get_bot_by_bot_id(bot_id) or web.state.tg_application.bot
        id_list = [i.strip() for i in ids.split(",") if i.strip().isdigit()]
        for report_id in id_list:
            try:
                with db_connection() as conn:
                    report = conn.execute(
                        "SELECT * FROM reports WHERE id = %s AND bot_id = %s AND status = 'pending'", (report_id, bot_id)
                    ).fetchone()
                    if not report:
                        continue
                    conn.execute(
                        "UPDATE reports SET status='approved', reviewed_at=%s WHERE id = %s",
                        (utc_now_iso(), report_id),
                    )
                log_audit(0, "web_batch_approve", int(report_id))
                try:
                    channel_link = await _push_report_to_channel(
                        tg_bot, report_id, report, bot_id=bot_id
                    )
                except Exception:
                    logger.warning("batch approve: push failed for report %s", report_id, exc_info=True)
                    channel_link = ""
                feedback = _build_approval_feedback(report_id, channel_link=channel_link, bot_id=bot_id)
                try:
                    await tg_bot.send_message(
                        chat_id=report["user_id"], text=feedback
                    )
                except Exception:
                    logger.warning(
                        "batch approve: notify failed for user %s", report["user_id"], exc_info=True
                    )
            except Exception:
                logger.warning("batch approve: error processing report %s", report_id, exc_info=True)
        return RedirectResponse(url="/admin?tab=pending", status_code=303)

    @web.get("/admin/export-reports")
    async def export_reports(request: Request):
        """Export all reports as a CSV file."""
        if redirect := _auth(request):
            return redirect
        bot_id = _get_request_bot_id(request)
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, username, tag, data_json, status,
                       review_feedback, created_at, reviewed_at
                FROM reports WHERE bot_id = %s ORDER BY id DESC
                """,
                (bot_id,),
            ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "user_id", "username", "tag", "data_json",
                         "status", "review_feedback", "created_at", "reviewed_at"])
        for row in rows:
            writer.writerow([
                row["id"], row["user_id"], row["username"], row["tag"],
                row["data_json"], row["status"], row["review_feedback"] or "",
                row["created_at"], row["reviewed_at"] or "",
            ])
        content = output.getvalue()
        return Response(
            content=content.encode("utf-8-sig"),  # utf-8-sig for Excel compatibility
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=reports.csv"},
        )

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
        if not _is_main_admin(request):
            raise HTTPException(status_code=403, detail="子管理员无权执行广播操作")
        bot_id = _get_request_bot_id(request)
        with db_connection() as conn:
            user_rows = conn.execute("SELECT user_id FROM users WHERE bot_id = %s", (bot_id,)).fetchall()
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
        return RedirectResponse(url="/admin?tab=broadcast", status_code=303)

    # ── Child bot management ──────────────────────────────────────────────────

    @web.get("/admin/child-bots", response_class=JSONResponse)
    async def child_bots_list(request: Request):
        if redirect := _auth(request):
            return redirect
        if not _is_main_admin(request):
            raise HTTPException(status_code=403, detail="子管理员无权访问子机器人管理")
        from app import bot_manager

        bots = list_child_bots()
        result = []
        for cb in bots:
            cb_out = dict(cb)
            cb_out["running"] = bot_manager.is_running(cb["token"])
            cb_out.pop("token", None)  # never expose the token via the API
            result.append(cb_out)
        return JSONResponse({"bots": result})

    @web.post("/admin/child-bots/add")
    async def child_bot_add(request: Request):
        if redirect := _auth(request):
            return redirect
        if not _is_main_admin(request):
            raise HTTPException(status_code=403, detail="子管理员无权添加子机器人")
        from app import bot_manager

        body = await request.json()
        token = (body.get("token") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="token is required")
        # Parse sub-admin (owner) Telegram user ID.
        owner_user_id: int | None = None
        raw_owner = body.get("owner_user_id")
        if raw_owner is not None and str(raw_owner).strip():
            try:
                owner_user_id = int(str(raw_owner).strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="owner_user_id 必须是数字 Telegram 用户 ID") from None
        admin_panel_url = (body.get("admin_panel_url") or "").strip()
        # Validate token by fetching bot info from Telegram.
        bot_username = ""
        bot_name = ""
        try:
            async with Bot(token=token) as validation_bot:
                me = await validation_bot.get_me()
                bot_username = me.username or ""
                bot_name = me.full_name or ""
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"无效的 Bot Token：{exc}") from exc
        try:
            new_bot_id = add_child_bot(token, bot_username=bot_username, bot_name=bot_name, owner_user_id=owner_user_id, admin_panel_url=admin_panel_url)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"该 Token 已存在：{exc}") from exc
        # Seed default settings for this new child bot.
        child_bot_id_str = str(new_bot_id)
        init_bot_settings(child_bot_id_str)
        # Start immediately if the event loop is running.
        try:
            await bot_manager.start_child_bot(token, owner_user_id=owner_user_id, admin_panel_url=admin_panel_url, bot_id=child_bot_id_str)
        except Exception:
            logger.exception("child_bot_add: failed to start child bot @%s", bot_username)
        logger.info("admin: child bot added @%s (owner=%s)", bot_username, owner_user_id)
        return JSONResponse({"ok": True, "bot_username": bot_username, "bot_name": bot_name})

    @web.post("/admin/child-bots/remove")
    async def child_bot_remove(request: Request):
        if redirect := _auth(request):
            return redirect
        if not _is_main_admin(request):
            raise HTTPException(status_code=403, detail="子管理员无权删除子机器人")
        from app import bot_manager

        body = await request.json()
        # Accept either the numeric id or a masked token suffix; we look up by id.
        bot_id = body.get("id")
        if bot_id is None:
            raise HTTPException(status_code=400, detail="id is required")
        bots = list_child_bots()
        cb = next((b for b in bots if b["id"] == int(bot_id)), None)
        if cb is None:
            raise HTTPException(status_code=404, detail="子机器人不存在")
        token = cb["token"]
        await bot_manager.stop_child_bot(token)
        remove_child_bot(token)
        logger.info("admin: child bot removed id=%s @%s", bot_id, cb.get("bot_username"))
        return JSONResponse({"ok": True})

    @web.post("/admin/child-bots/toggle")
    async def child_bot_toggle(request: Request):
        if redirect := _auth(request):
            return redirect
        if not _is_main_admin(request):
            raise HTTPException(status_code=403, detail="子管理员无权管理子机器人")
        from app import bot_manager

        body = await request.json()
        bot_id = body.get("id")
        active = bool(body.get("active", True))
        if bot_id is None:
            raise HTTPException(status_code=400, detail="id is required")
        bots = list_child_bots()
        cb = next((b for b in bots if b["id"] == int(bot_id)), None)
        if cb is None:
            raise HTTPException(status_code=404, detail="子机器人不存在")
        token = cb["token"]
        owner_user_id = cb.get("owner_user_id")
        admin_panel_url = cb.get("admin_panel_url") or ""
        child_bot_id_str = str(cb["id"])
        set_child_bot_active(token, active)
        if active:
            await bot_manager.start_child_bot(token, owner_user_id=owner_user_id, admin_panel_url=admin_panel_url, bot_id=child_bot_id_str)
        else:
            await bot_manager.stop_child_bot(token)
        return JSONResponse({"ok": True, "active": active})

    return web
