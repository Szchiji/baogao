import asyncio
import html
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application

from app.admin_auth import (
    _cleanup_verify_state,
    _otp_tokens,
    _OTP_TOKEN_TTL,
    _verify_code_otps,
    _verify_codes,
    _VERIFY_CODE_TTL,
)
from app.admin_panel import build_admin_html, report_to_html
from app.bot_handlers import _build_approval_feedback, _push_report_to_channel
from app.config import AppConfig, DEFAULT_SETTINGS
from app.crud import ban_user, setting_get, setting_set, unban_user
from app.database import db_connection
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>错误</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f9fafb;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
.card{{background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.1);padding:40px 36px;width:100%;max-width:420px;text-align:center}}
.icon{{font-size:3rem;margin-bottom:16px}}
h2{{font-size:1.15rem;font-weight:700;color:#111827;margin-bottom:8px}}
p{{font-size:.88rem;color:#6b7280;line-height:1.6;margin-bottom:24px}}
a{{display:inline-block;padding:10px 24px;background:#4f46e5;color:#fff;border-radius:8px;text-decoration:none;font-size:.9rem;font-weight:600;transition:background .15s}}
a:hover{{background:#4338ca}}
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
    async def report_detail(report_id: str):
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
        return HTMLResponse("""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>管理员登录</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f9fafb;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.card{background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.1);padding:40px 36px;width:100%;max-width:420px}
.logo{text-align:center;margin-bottom:28px}
.logo h1{font-size:1.3rem;font-weight:700;color:#111827;margin-top:10px}
.logo p{font-size:.85rem;color:#6b7280;margin-top:4px}
label{display:block;font-size:.78rem;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
input[type=password]{width:100%;padding:10px 13px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:.9rem;font-family:inherit;transition:border-color .15s,box-shadow .15s}
input[type=password]:focus{outline:none;border-color:#4f46e5;box-shadow:0 0 0 3px rgba(79,70,229,.12)}
.field{margin-bottom:20px}
.btn{display:block;width:100%;padding:11px;background:#4f46e5;color:#fff;border:none;border-radius:8px;font-size:.9rem;font-weight:600;font-family:inherit;cursor:pointer;transition:background .15s;text-align:center}
.btn:hover{background:#4338ca}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div style="font-size:2.5rem">📋</div>
    <h1>报告机器人管理后台</h1>
    <p>请输入管理员密码以继续</p>
  </div>
  <form method="get" action="/admin">
    <div class="field">
      <label>管理员密码</label>
      <input type="password" name="token" placeholder="ADMIN_PANEL_TOKEN" autofocus>
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
            all_report_rows = conn.execute(
                "SELECT id, username, created_at, status, data_json, review_feedback, channel_message_link FROM reports ORDER BY id DESC LIMIT 200"
            ).fetchall()
            report_stats = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM reports GROUP BY status"
            ).fetchall()
        settings_map = {r["key"]: r["value"] for r in rows}
        pending_list = [dict(r) for r in pending_rows]
        user_count = user_count_row["cnt"] if user_count_row else 0
        blacklist_list = [dict(r) for r in blacklist_rows]
        stats = {r["status"]: r["cnt"] for r in report_stats}
        stats["total_reports"] = sum(stats.values())
        all_reports_list = [dict(r) for r in all_report_rows]
        response = HTMLResponse(build_admin_html(
            settings_map, pending_list, saved=saved,
            user_count=user_count, db_path="",
            blacklist=blacklist_list,
            all_reports=all_reports_list,
            stats=stats,
            initial_tab=request.query_params.get("tab", "basic"),
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
            return HTMLResponse(_error_page("链接无效或已过期，请重新获取验证码。"), status_code=403)
        if time.time() > _otp_tokens[token]:
            _otp_tokens.pop(token, None)
            return HTMLResponse(_error_page("登录链接已过期，请重新获取验证码。"), status_code=403)
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
        return RedirectResponse(url="/admin?saved=1", status_code=303)

    @web.post("/admin/approve/{report_id}")
    async def web_approve_report(report_id: str, request: Request):
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
        return RedirectResponse(url="/admin?tab=pending", status_code=303)

    @web.post("/admin/reject/{report_id}")
    async def web_reject_report(report_id: str, request: Request, reason: str = Form(default="请联系管理员")):
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
        return RedirectResponse(url="/admin?tab=pending", status_code=303)

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
        return RedirectResponse(url="/admin?tab=broadcast", status_code=303)

    return web
