"""Telegram handlers for the invite link management module."""
import json
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import get_admin_user_ids
from app.invite.cleanup import cleanup_expired_invites, revoke_expired_invites
from app.invite.config import INVITE_COOLDOWN_HOURS, INVITE_EXPIRE_MINUTES, WELCOME_TEXT
from app.invite.redis_client import (
    ADMINS_KEY,
    INVITE_LOG_KEY,
    clear_admin_state,
    delete_pending_request,
    get_admin_state,
    get_groups,
    get_pending_request,
    is_redis_admin,
    log_invite,
    can_user_get_invite,
    record_user_invite,
    redis_client,
    remove_group,
    save_group,
    save_pending_request,
    set_admin_state,
    set_group_approval,
)

logger = logging.getLogger("report-bot")


# ── Admin check ───────────────────────────────────────────────────────────────

def is_invite_admin(user_id: int) -> bool:
    """Return True if *user_id* is an invite module admin.

    Checks both the ``ADMIN_USER_IDS`` environment variable (shared with
    baogao) and the invite-module's own Redis admin set.
    """
    if user_id in get_admin_user_ids():
        return True
    return is_redis_admin(user_id)


# ── Utility helpers ───────────────────────────────────────────────────────────

def _format_time_left(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}小时{mins}分钟" if mins else f"{hours}小时"


def _format_user_info(user_id, first_name="", last_name="", username=None) -> str:
    """Return an HTML-formatted user description with a clickable name link."""
    full_name = f"{first_name} {last_name}".strip() or str(user_id)
    name_link = f'<a href="tg://user?id={user_id}">{full_name}</a>'
    username_part = f"@{username}" if username else "无用户名"
    return f"昵称：{name_link}\n用户名：{username_part}\nID：<code>{user_id}</code>"


# ── Admin panel keyboards ─────────────────────────────────────────────────────

def _build_admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 群组管理", callback_data="adm_groups"),
            InlineKeyboardButton("📊 统计数据", callback_data="adm_stats"),
        ],
        [
            InlineKeyboardButton("🔗 分享链接", callback_data="adm_links"),
            InlineKeyboardButton("🧪 测试连接", callback_data="adm_test"),
        ],
        [
            InlineKeyboardButton("🧹 清理数据", callback_data="adm_cleanup"),
            InlineKeyboardButton("🚫 撤销链接", callback_data="adm_revoke"),
        ],
        [
            InlineKeyboardButton("👥 添加管理员", callback_data="adm_addadmin"),
        ],
    ])


def _build_admin_main_text(user_id) -> str:
    groups = get_groups(user_id)
    return (
        f"🤖 邀请管理面板\n\n"
        f"已绑定群组: {len(groups)} 个\n"
        f"邀请有效期: {INVITE_EXPIRE_MINUTES}分钟\n"
        f"邀请冷却: {INVITE_COOLDOWN_HOURS}小时"
    )


# ── Join-flow helpers ─────────────────────────────────────────────────────────

async def _request_join_approval(update, context, user, group_id, group_title, admin_id) -> bool:
    """Submit an approval request to the admin; return True on success."""
    existing = get_pending_request(user.id, group_id)
    if existing:
        await update.message.reply_text(f"⏳ 你已提交过「{group_title}」的申请，请等待管理员审核")
        return True

    user_info = {"username": user.username, "first_name": user.first_name or ""}
    save_pending_request(user.id, group_id, user_info, group_title, admin_id)

    user_detail = _format_user_info(user.id, user.first_name or "", user.last_name or "", user.username)
    keyboard = [[
        InlineKeyboardButton("✅ 同意", callback_data=f"inv_approve_{user.id}_{group_id}"),
        InlineKeyboardButton("❌ 拒绝", callback_data=f"inv_reject_{user.id}_{group_id}"),
    ]]
    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"📋 加群申请\n\n{user_detail}\n\n申请加入：{group_title}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Failed to notify admin %s: %s", admin_id, exc)
        delete_pending_request(user.id, group_id)
        await update.message.reply_text(f"❌ {group_title} 申请提交失败，请联系管理员")
        return False

    await update.message.reply_text(f"📤 已提交加入「{group_title}」的申请，请等待管理员审核")
    return True


async def _send_single_invite(update, context, user, group_id, group_title, admin_id=None) -> bool:
    """Generate and send a single invite link; handle approval flow if enabled."""
    if admin_id:
        groups = get_groups(admin_id)
        if groups.get(str(group_id), {}).get("approval_required", False):
            return await _request_join_approval(update, context, user, group_id, group_title, admin_id)

    can_get, ttl = can_user_get_invite(user.id, group_id)
    if not can_get:
        await update.message.reply_text(
            f"⏳ {group_title} 冷却中，请 {_format_time_left(ttl)} 后再试"
        )
        return False

    try:
        expire_time = int((datetime.now() + timedelta(minutes=INVITE_EXPIRE_MINUTES)).timestamp())
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=int(group_id),
            member_limit=1,
            expire_date=expire_time,
        )
        log_invite(user.id, group_id, invite_link.invite_link, group_title, admin_id)
        record_user_invite(user.id, group_id)
        keyboard = [[InlineKeyboardButton(f"👉 加入 {group_title}", url=invite_link.invite_link)]]
        await update.message.reply_text(
            f"✅ {group_title}\n⏰ {INVITE_EXPIRE_MINUTES}分钟后过期",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return True
    except Exception as exc:
        logger.error("Failed to create invite: %s", exc)
        await update.message.reply_text(f"❌ {group_title} 邀请生成失败")
        return False


async def _handle_join_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, user, admin_id) -> None:
    """Present group selection menu (or direct invite if only one group)."""
    groups = get_groups(admin_id)
    if not groups:
        await update.message.reply_text("机器人尚未配置群组，请联系管理员")
        return

    if len(groups) == 1:
        gid = list(groups.keys())[0]
        await _send_single_invite(update, context, user, gid, list(groups.values())[0]["title"], admin_id)
        return

    keyboard = []
    for gid, info in groups.items():
        can_get, ttl = can_user_get_invite(user.id, gid)
        if not can_get:
            status = " (冷却中)"
        elif info.get("approval_required", False):
            status = " (需审批)"
        else:
            status = ""
        keyboard.append([InlineKeyboardButton(
            f"{info['title']}{status}",
            callback_data=f"inv_select_{gid}_{user.id}_{admin_id}",
        )])
    keyboard.append([InlineKeyboardButton(
        "🚀 一键加入所有群组",
        callback_data=f"inv_joinall_{user.id}_{admin_id}",
    )])
    await update.message.reply_text(
        WELCOME_TEXT + f"\n\n⏰ 邀请链接有效期：{INVITE_EXPIRE_MINUTES}分钟\n"
        f"🕐 每群组每{INVITE_COOLDOWN_HOURS}小时限领一次",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_join_all(update: Update, context: ContextTypes.DEFAULT_TYPE, user, admin_id) -> None:
    """Send invite links for all available groups at once."""
    groups = get_groups(admin_id)
    if not groups:
        await update.message.reply_text("机器人尚未配置群组，请联系管理员")
        return

    if len(groups) == 1:
        gid = list(groups.keys())[0]
        await _send_single_invite(update, context, user, gid, list(groups.values())[0]["title"], admin_id)
        return

    available_groups = []
    cooling_groups = []
    for gid, info in groups.items():
        can_get, ttl = can_user_get_invite(user.id, gid)
        if can_get:
            available_groups.append((gid, info["title"]))
        else:
            cooling_groups.append((info["title"], ttl))

    if not available_groups:
        text = "⏳ 所有群组都在冷却中：\n\n"
        for title, ttl in cooling_groups:
            text += f"• {title}: {_format_time_left(ttl)}\n"
        await update.message.reply_text(text)
        return

    if len(available_groups) == 1:
        gid, title = available_groups[0]
        await _send_single_invite(update, context, user, gid, title, admin_id)
        return

    processing_msg = await update.message.reply_text("⏳ 正在处理，请稍候...")
    keyboard_buttons = []
    failed_groups = []
    approval_submitted = []
    success_count = 0

    for gid, title in available_groups:
        group_info = groups[gid]
        if group_info.get("approval_required", False):
            existing = get_pending_request(user.id, gid)
            if existing:
                approval_submitted.append(f"⏳ {title} (审核中)")
            else:
                user_info = {"username": user.username, "first_name": user.first_name or ""}
                save_pending_request(user.id, gid, user_info, title, admin_id)
                user_detail = _format_user_info(user.id, user.first_name or "", user.last_name or "", user.username)
                notify_keyboard = [[
                    InlineKeyboardButton("✅ 同意", callback_data=f"inv_approve_{user.id}_{gid}"),
                    InlineKeyboardButton("❌ 拒绝", callback_data=f"inv_reject_{user.id}_{gid}"),
                ]]
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"📋 加群申请\n\n{user_detail}\n\n申请加入：{title}",
                        reply_markup=InlineKeyboardMarkup(notify_keyboard),
                        parse_mode="HTML",
                    )
                    approval_submitted.append(f"📤 {title} (等待审核)")
                except Exception as exc:
                    logger.error("Failed to notify admin for %s: %s", gid, exc)
                    delete_pending_request(user.id, gid)
                    failed_groups.append(title)
        else:
            try:
                expire_time = int((datetime.now() + timedelta(minutes=INVITE_EXPIRE_MINUTES)).timestamp())
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=int(gid),
                    member_limit=1,
                    expire_date=expire_time,
                )
                log_invite(user.id, gid, invite_link.invite_link, title, admin_id)
                record_user_invite(user.id, gid)
                keyboard_buttons.append([InlineKeyboardButton(f"👉 加入 {title}", url=invite_link.invite_link)])
                success_count += 1
            except Exception as exc:
                logger.error("Failed to create invite for %s: %s", gid, exc)
                failed_groups.append(title)

    text = ""
    if success_count:
        text += f"✅ 已生成 {success_count} 个邀请链接\n"
    if approval_submitted:
        text += "\n".join(approval_submitted) + "\n"
    if cooling_groups:
        text += f"⏳ {len(cooling_groups)} 个群组冷却中\n"
    if failed_groups:
        text += f"❌ {len(failed_groups)} 个群组处理失败\n"
    if success_count:
        text += f"\n⏰ 链接将在 {INVITE_EXPIRE_MINUTES} 分钟后过期\n"
        text += "🔒 每个链接仅限使用一次"

    await processing_msg.edit_text(
        text or "处理完成",
        reply_markup=InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None,
    )


# ── Public entry-point called from baogao's start_cmd ────────────────────────

async def handle_invite_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    start_param: str,
) -> None:
    """Handle /start with a join_ or joinall_ parameter."""
    user = update.effective_user
    if start_param.startswith("joinall_"):
        try:
            admin_id = int(start_param[len("joinall_"):])
        except (ValueError, IndexError):
            await update.message.reply_text("链接无效，请联系管理员获取正确链接")
            return
        await _handle_join_all(update, context, user, admin_id)
    elif start_param.startswith("join_"):
        try:
            admin_id = int(start_param[len("join_"):])
        except (ValueError, IndexError):
            await update.message.reply_text("链接无效，请联系管理员获取正确链接")
            return
        await _handle_join_flow(update, context, user, admin_id)


# ── Callback handlers ─────────────────────────────────────────────────────────

async def select_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle group selection from the join menu."""
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    await query.answer()

    # format: inv_select_{group_id}_{user_id}_{admin_id}
    parts = query.data.split("_")
    if len(parts) < 5:
        await query.edit_message_text("链接已失效，请重新获取")
        raise ApplicationHandlerStop

    group_id = parts[2]
    user_id = int(parts[3])
    admin_id = int(parts[4])

    if query.from_user.id != user_id:
        await query.answer("这不是你的选择", show_alert=True)
        raise ApplicationHandlerStop

    groups = get_groups(admin_id)
    if group_id not in groups:
        await query.edit_message_text("该群组已不可用")
        raise ApplicationHandlerStop

    can_get, ttl = can_user_get_invite(user_id, group_id)
    if not can_get:
        await query.edit_message_text(
            f"⏳ 你已经在 {INVITE_COOLDOWN_HOURS} 小时内获取过该群组的邀请链接\n"
            f"请等待 {_format_time_left(ttl)} 后再试"
        )
        raise ApplicationHandlerStop

    group_title = groups[group_id]["title"]

    if groups[group_id].get("approval_required", False):
        existing = get_pending_request(user_id, group_id)
        if existing:
            await query.edit_message_text(f"⏳ 你已提交过「{group_title}」的申请，请等待管理员审核")
            raise ApplicationHandlerStop
        user_info = {"username": query.from_user.username, "first_name": query.from_user.first_name or ""}
        save_pending_request(user_id, group_id, user_info, group_title, admin_id)
        user_detail = _format_user_info(user_id, query.from_user.first_name or "", query.from_user.last_name or "", query.from_user.username)
        notify_keyboard = [[
            InlineKeyboardButton("✅ 同意", callback_data=f"inv_approve_{user_id}_{group_id}"),
            InlineKeyboardButton("❌ 拒绝", callback_data=f"inv_reject_{user_id}_{group_id}"),
        ]]
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"📋 加群申请\n\n{user_detail}\n\n申请加入：{group_title}",
                reply_markup=InlineKeyboardMarkup(notify_keyboard),
                parse_mode="HTML",
            )
            await query.edit_message_text(f"📤 已提交加入「{group_title}」的申请，请等待管理员审核")
        except Exception as exc:
            logger.error("Failed to notify admin %s: %s", admin_id, exc)
            delete_pending_request(user_id, group_id)
            await query.edit_message_text(f"❌ {group_title} 申请提交失败，请联系管理员")
        raise ApplicationHandlerStop

    try:
        expire_time = int((datetime.now() + timedelta(minutes=INVITE_EXPIRE_MINUTES)).timestamp())
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=int(group_id),
            member_limit=1,
            expire_date=expire_time,
        )
        log_invite(user_id, group_id, invite_link.invite_link, group_title, admin_id)
        record_user_invite(user_id, group_id)
        keyboard = [[InlineKeyboardButton(f"👉 点击加入 {group_title}", url=invite_link.invite_link)]]
        await query.edit_message_text(
            f"✅ {group_title}\n\n"
            f"⏰ 链接将在 {INVITE_EXPIRE_MINUTES} 分钟后过期\n"
            f"🔒 仅限你使用一次",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        logger.info("User %s got invite for group %s", user_id, group_id)
    except Exception as exc:
        logger.error("Failed to create invite: %s", exc)
        await query.edit_message_text(f"❌ {group_title} 邀请生成失败，请联系管理员")
    raise ApplicationHandlerStop


async def join_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'join all' inline button callback."""
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    await query.answer()

    # format: inv_joinall_{user_id}_{admin_id}
    parts = query.data.split("_")
    if len(parts) < 4:
        await query.edit_message_text("链接已失效，请重新获取")
        raise ApplicationHandlerStop

    user_id = int(parts[2])
    admin_id = int(parts[3])

    if query.from_user.id != user_id:
        await query.answer("这不是你的请求", show_alert=True)
        raise ApplicationHandlerStop

    groups = get_groups(admin_id)
    await query.edit_message_text("⏳ 正在处理，请稍候...")

    results = []
    success_count = 0

    for gid, info in groups.items():
        can_get, ttl = can_user_get_invite(user_id, gid)
        if not can_get:
            results.append(f"⏳ {info['title']} - 冷却中 ({_format_time_left(ttl)})")
            continue

        if info.get("approval_required", False):
            existing = get_pending_request(user_id, gid)
            if existing:
                results.append(f"⏳ {info['title']} - 审核中")
                continue
            user_info = {"username": query.from_user.username, "first_name": query.from_user.first_name or ""}
            save_pending_request(user_id, gid, user_info, info["title"], admin_id)
            user_detail = _format_user_info(user_id, query.from_user.first_name or "", query.from_user.last_name or "", query.from_user.username)
            notify_keyboard = [[
                InlineKeyboardButton("✅ 同意", callback_data=f"inv_approve_{user_id}_{gid}"),
                InlineKeyboardButton("❌ 拒绝", callback_data=f"inv_reject_{user_id}_{gid}"),
            ]]
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"📋 加群申请\n\n{user_detail}\n\n申请加入：{info['title']}",
                    reply_markup=InlineKeyboardMarkup(notify_keyboard),
                    parse_mode="HTML",
                )
                results.append(f"📤 {info['title']} - 等待审核")
            except Exception as exc:
                logger.error("Failed to notify admin for %s: %s", gid, exc)
                delete_pending_request(user_id, gid)
                results.append(f"❌ {info['title']} - 申请提交失败")
            continue

        try:
            expire_time = int((datetime.now() + timedelta(minutes=INVITE_EXPIRE_MINUTES)).timestamp())
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=int(gid),
                member_limit=1,
                expire_date=expire_time,
            )
            log_invite(user_id, gid, invite_link.invite_link, info["title"], admin_id)
            record_user_invite(user_id, gid)
            results.append(f"✅ [{info['title']}]({invite_link.invite_link})")
            success_count += 1
        except Exception as exc:
            logger.error("Failed to create invite for %s: %s", gid, exc)
            results.append(f"❌ {info['title']} - 生成失败")

    text = f"📋 处理结果（邀请 {success_count}/{len(groups)}）：\n\n"
    text += "\n".join(results)
    if success_count:
        text += f"\n\n⏰ 邀请链接 {INVITE_EXPIRE_MINUTES} 分钟后过期\n"
        text += "🔒 每个链接仅限使用一次"

    await query.edit_message_text(text, parse_mode="Markdown", disable_web_page_preview=True)
    raise ApplicationHandlerStop


async def approve_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin approves a pending join request."""
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    await query.answer()

    # format: inv_approve_{user_id}_{group_id}
    parts = query.data.split("_", 3)
    if len(parts) < 4:
        await query.edit_message_text("数据格式错误")
        raise ApplicationHandlerStop

    user_id = int(parts[2])
    group_id = parts[3]
    admin_id = query.from_user.id

    req = get_pending_request(user_id, group_id)
    if not req:
        await query.edit_message_text("❌ 申请已过期或不存在")
        raise ApplicationHandlerStop

    group_title = req["group_title"]
    try:
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=int(group_id),
            member_limit=1,
        )
        log_invite(user_id, group_id, invite_link.invite_link, group_title, admin_id)
        record_user_invite(user_id, group_id)
        delete_pending_request(user_id, group_id)

        keyboard = [[InlineKeyboardButton(f"👉 加入 {group_title}", url=invite_link.invite_link)]]
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ 你的加入「{group_title}」申请已通过！\n🔒 仅限一次使用",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as exc:
            logger.error("Failed to notify user %s: %s", user_id, exc)

        await query.edit_message_text(f"✅ 已同意用户 {user_id} 加入「{group_title}」")
        logger.info("Admin %s approved join: user %s -> group %s", admin_id, user_id, group_id)
    except Exception as exc:
        logger.error("Failed to create invite for approved request: %s", exc)
        await query.edit_message_text(f"❌ 生成邀请链接失败: {exc}")
    raise ApplicationHandlerStop


async def reject_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin rejects a pending join request."""
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    await query.answer()

    # format: inv_reject_{user_id}_{group_id}
    parts = query.data.split("_", 3)
    if len(parts) < 4:
        await query.edit_message_text("数据格式错误")
        raise ApplicationHandlerStop

    user_id = int(parts[2])
    group_id = parts[3]

    req = get_pending_request(user_id, group_id)
    group_title = req["group_title"] if req else "未知群组"
    delete_pending_request(user_id, group_id)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ 你的加入「{group_title}」申请未通过",
        )
    except Exception as exc:
        logger.error("Failed to notify user %s: %s", user_id, exc)

    await query.edit_message_text(f"❌ 已拒绝用户 {user_id} 加入「{group_title}」")
    logger.info("Admin %s rejected join: user %s -> group %s", query.from_user.id, user_id, group_id)
    raise ApplicationHandlerStop


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all adm_* callback buttons in the admin panel."""
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    user = query.from_user

    if not is_invite_admin(user.id):
        await query.answer("⛔ 你没有权限", show_alert=True)
        raise ApplicationHandlerStop

    clear_admin_state(user.id)
    data = query.data
    back_btn = [[InlineKeyboardButton("⬅️ 返回", callback_data="adm_back")]]

    if data == "adm_back":
        await query.answer()
        await query.edit_message_text(
            _build_admin_main_text(user.id),
            reply_markup=_build_admin_main_keyboard(),
        )
        raise ApplicationHandlerStop

    if data == "adm_groups":
        await query.answer()
        groups = get_groups(user.id)
        keyboard = []
        if groups:
            for gid, info in groups.items():
                icon = "🔒" if info.get("approval_required", False) else "🔓"
                keyboard.append([InlineKeyboardButton(
                    f"{icon} {info['title']}",
                    callback_data=f"adm_grp_info_{gid}",
                )])
        keyboard.append([InlineKeyboardButton("➕ 手动绑定群组", callback_data="adm_bindgroup")])
        keyboard.append([InlineKeyboardButton("⬅️ 返回", callback_data="adm_back")])
        msg = (
            f"📋 已绑定群组（{len(groups)} 个）\n🔓=直接加入  🔒=需审批"
            if groups else "暂无绑定的群组\n\n将机器人设为群管理员后即可自动绑定"
        )
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        raise ApplicationHandlerStop

    if data.startswith("adm_grp_info_"):
        await query.answer()
        gid = data[len("adm_grp_info_"):]
        groups = get_groups(user.id)
        if gid not in groups:
            await query.edit_message_text(
                "该群组不存在",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="adm_groups")]]),
            )
            raise ApplicationHandlerStop
        info = groups[gid]
        approval = "🔒 需审批" if info.get("approval_required", False) else "🔓 直接加入"
        toggle_label = "🔓 切换为直接加入" if info.get("approval_required", False) else "🔒 切换为需审批"
        keyboard = [
            [InlineKeyboardButton(toggle_label, callback_data=f"adm_grp_tog_{gid}")],
            [InlineKeyboardButton("❌ 移除群组", callback_data=f"adm_grp_del_{gid}")],
            [InlineKeyboardButton("⬅️ 返回群组列表", callback_data="adm_groups")],
        ]
        await query.edit_message_text(
            f"群组：{info['title']}\nID: `{gid}`\n审批模式: {approval}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop

    if data.startswith("adm_grp_tog_"):
        gid = data[len("adm_grp_tog_"):]
        groups = get_groups(user.id)
        if gid not in groups:
            await query.answer("群组不存在", show_alert=True)
            raise ApplicationHandlerStop
        current = groups[gid].get("approval_required", False)
        set_group_approval(gid, user.id, not current)
        status = "开启 🔒" if not current else "关闭 🔓"
        await query.answer(f"审批模式已{status}")
        groups = get_groups(user.id)
        info = groups[gid]
        approval = "🔒 需审批" if info.get("approval_required", False) else "🔓 直接加入"
        toggle_label = "🔓 切换为直接加入" if info.get("approval_required", False) else "🔒 切换为需审批"
        keyboard = [
            [InlineKeyboardButton(toggle_label, callback_data=f"adm_grp_tog_{gid}")],
            [InlineKeyboardButton("❌ 移除群组", callback_data=f"adm_grp_del_{gid}")],
            [InlineKeyboardButton("⬅️ 返回群组列表", callback_data="adm_groups")],
        ]
        await query.edit_message_text(
            f"群组：{info['title']}\nID: `{gid}`\n审批模式: {approval}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop

    if data.startswith("adm_grp_delok_"):
        await query.answer()
        gid = data[len("adm_grp_delok_"):]
        groups = get_groups(user.id)
        title = groups.get(gid, {}).get("title", gid)
        result = remove_group(gid)
        msg = f"✅ 已移除群组「{title}」" if result else "❌ 移除失败"
        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回群组列表", callback_data="adm_groups")]]),
        )
        raise ApplicationHandlerStop

    if data.startswith("adm_grp_del_"):
        await query.answer()
        gid = data[len("adm_grp_del_"):]
        groups = get_groups(user.id)
        if gid not in groups:
            await query.answer("群组不存在", show_alert=True)
            raise ApplicationHandlerStop
        info = groups[gid]
        keyboard = [[
            InlineKeyboardButton("✅ 确认移除", callback_data=f"adm_grp_delok_{gid}"),
            InlineKeyboardButton("取消", callback_data=f"adm_grp_info_{gid}"),
        ]]
        await query.edit_message_text(
            f"⚠️ 确认移除群组「{info['title']}」？",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        raise ApplicationHandlerStop

    if data == "adm_stats":
        await query.answer()
        if not redis_client:
            await query.edit_message_text("Redis 不可用", reply_markup=InlineKeyboardMarkup(back_btn))
            raise ApplicationHandlerStop
        logs = redis_client.lrange(INVITE_LOG_KEY, 0, 999)
        recent_invites = []
        revoked_count = 0
        for log in logs:
            try:
                entry = json.loads(log)
                if str(entry.get("admin_id")) != str(user.id):
                    continue
                if entry.get("revoked", False):
                    revoked_count += 1
                created = datetime.fromisoformat(entry["created_at"])
                if datetime.now() - created < timedelta(days=1):
                    recent_invites.append(entry)
            except Exception:
                continue
        total_24h = len(recent_invites)
        unique_users = len({i["user_id"] for i in recent_invites})
        text = (
            f"📊 邀请统计（最近24小时）\n\n"
            f"总邀请数: {total_24h}\n"
            f"独立用户: {unique_users}\n"
            f"已撤销链接: {revoked_count}\n\n"
            f"配置:\n"
            f"邀请有效期: {INVITE_EXPIRE_MINUTES}分钟\n"
            f"邀请冷却: {INVITE_COOLDOWN_HOURS}小时"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back_btn))
        raise ApplicationHandlerStop

    if data == "adm_links":
        await query.answer()
        bot_username = context.bot.username
        text = (
            f"🔗 分享链接（仅包含你的群组）：\n\n"
            f"• 选择加入：\nhttps://t.me/{bot_username}?start=join_{user.id}\n\n"
            f"• 一键加入全部：\nhttps://t.me/{bot_username}?start=joinall_{user.id}"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back_btn))
        raise ApplicationHandlerStop

    if data == "adm_test":
        await query.answer()
        try:
            redis_ok = bool(redis_client and redis_client.ping())
        except Exception:
            redis_ok = False
        redis_status = "连接正常" if redis_ok else "连接失败"
        groups = get_groups(user.id)
        text = (
            f"🧪 测试报告\n\n"
            f"Redis 状态: {redis_status}\n"
            f"已绑定群组: {len(groups)} 个\n"
            f"邀请有效期: {INVITE_EXPIRE_MINUTES}分钟\n"
            f"邀请冷却: {INVITE_COOLDOWN_HOURS}小时\n"
            f"当前用户ID: {user.id}\n"
            f"是否为管理员: 是"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back_btn))
        raise ApplicationHandlerStop

    if data == "adm_cleanup":
        await query.answer()
        await query.edit_message_text("🧹 正在清理过期数据...")
        removed = await cleanup_expired_invites()
        if redis_client:
            logs = redis_client.lrange(INVITE_LOG_KEY, 0, -1)
            valid = expired = revoked = 0
            for log in logs:
                try:
                    entry = json.loads(log)
                    if entry.get("revoked", False):
                        revoked += 1
                    elif datetime.fromisoformat(entry["expire_at"]) > datetime.now():
                        valid += 1
                    else:
                        expired += 1
                except Exception:
                    expired += 1
            text = (
                f"✅ 清理完成\n\n"
                f"🗑️ 已删除记录: {removed}\n"
                f"✨ 有效邀请: {valid}\n"
                f"⏰ 待撤销: {expired}\n"
                f"🚫 已撤销: {revoked}\n"
                f"📊 总计: {len(logs)}"
            )
        else:
            text = "❌ Redis 不可用"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back_btn))
        raise ApplicationHandlerStop

    if data == "adm_revoke":
        await query.answer()
        await query.edit_message_text("🚫 正在撤销失效的邀请链接...")
        revoked = await revoke_expired_invites(context.application)
        await query.edit_message_text(
            f"✅ 已撤销 {revoked} 个失效的邀请链接",
            reply_markup=InlineKeyboardMarkup(back_btn),
        )
        raise ApplicationHandlerStop

    if data == "adm_addadmin":
        await query.answer()
        set_admin_state(user.id, {"action": "add_admin"})
        await query.edit_message_text(
            "👥 请发送要添加的管理员用户 ID：\n（点击「取消」或发送 /cancel 可退出）",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="adm_back")]]),
        )
        raise ApplicationHandlerStop

    if data == "adm_bindgroup":
        await query.answer()
        set_admin_state(user.id, {"action": "bind_group_id"})
        await query.edit_message_text(
            "➕ 请发送要绑定的群组 ID：\n（例如：-1001234567890）\n（点击「取消」或发送 /cancel 可退出）",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="adm_groups")]]),
        )
        raise ApplicationHandlerStop

    # Unrecognised adm_ callback — just dismiss
    await query.answer()
    raise ApplicationHandlerStop


# ── Admin text input state machine ────────────────────────────────────────────

async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Intercept text messages when an invite admin has an active input state."""
    from telegram.ext import ApplicationHandlerStop

    if not update.message or not update.message.text:
        return

    user = update.effective_user
    if not is_invite_admin(user.id):
        return  # not an invite admin — let baogao handle

    state = get_admin_state(user.id)
    if not state:
        return  # no pending invite state — let baogao handle

    text = update.message.text.strip()
    action = state.get("action")

    if action == "add_admin":
        try:
            new_admin_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 无效的用户 ID，请发送纯数字")
            raise ApplicationHandlerStop
        if not redis_client:
            await update.message.reply_text("❌ 系统错误：Redis 不可用")
            clear_admin_state(user.id)
            raise ApplicationHandlerStop
        redis_client.sadd(ADMINS_KEY, str(new_admin_id))
        clear_admin_state(user.id)
        await update.message.reply_text(
            f"✅ 已添加管理员: {new_admin_id}",
            reply_markup=_build_admin_main_keyboard(),
        )
        raise ApplicationHandlerStop

    if action == "bind_group_id":
        try:
            group_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 无效的群组 ID，请发送数字（如 -1001234567890）")
            raise ApplicationHandlerStop
        set_admin_state(user.id, {"action": "bind_group_name", "group_id": str(group_id)})
        await update.message.reply_text(f"✅ 群组 ID: {group_id}\n\n请继续发送群组名称：")
        raise ApplicationHandlerStop

    if action == "bind_group_name":
        group_id = state.get("group_id")
        group_title = text
        clear_admin_state(user.id)
        if save_group(group_id, group_title, user.id):
            bot_username = context.bot.username
            await update.message.reply_text(
                f"✅ 已绑定群组：{group_title}\n"
                f"分享链接：https://t.me/{bot_username}?start=join_{user.id}",
                reply_markup=_build_admin_main_keyboard(),
            )
        else:
            await update.message.reply_text("❌ 绑定失败", reply_markup=_build_admin_main_keyboard())
        raise ApplicationHandlerStop

    # Unknown action — clear stale state and let baogao handle the message
    clear_admin_state(user.id)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel current input state and return to the admin panel."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        return
    clear_admin_state(user.id)
    await update.message.reply_text(
        _build_admin_main_text(user.id),
        reply_markup=_build_admin_main_keyboard(),
    )
    raise ApplicationHandlerStop


async def invpanel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the invite admin panel (/invpanel)."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("你没有权限")
        raise ApplicationHandlerStop
    clear_admin_state(user.id)
    await update.message.reply_text(
        _build_admin_main_text(user.id),
        reply_markup=_build_admin_main_keyboard(),
    )
    raise ApplicationHandlerStop


async def bind_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bindgroup [group_id] [title] — manually bind a group."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("你没有权限")
        raise ApplicationHandlerStop
    if len(context.args) < 2:
        await update.message.reply_text(
            "用法: /bindgroup [群组ID] [群组名称]\n示例: /bindgroup -1001234567890 我的群"
        )
        raise ApplicationHandlerStop
    group_id = context.args[0]
    group_title = " ".join(context.args[1:])
    if save_group(group_id, group_title, user.id):
        await update.message.reply_text(
            f"✅ 已手动绑定群组：{group_title}\n"
            f"分享链接：https://t.me/{context.bot.username}?start=join_{user.id}"
        )
    else:
        await update.message.reply_text("❌ 绑定失败")
    raise ApplicationHandlerStop


async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addadmin [user_id] — add an invite admin."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("你没有权限")
        raise ApplicationHandlerStop
    if not context.args:
        await update.message.reply_text("用法: /addadmin [用户ID]")
        raise ApplicationHandlerStop
    if not redis_client:
        await update.message.reply_text("系统错误：Redis 不可用")
        raise ApplicationHandlerStop
    redis_client.sadd(ADMINS_KEY, str(context.args[0]))
    await update.message.reply_text(f"已添加邀请管理员: {context.args[0]}")
    raise ApplicationHandlerStop


async def list_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/listgroups — list all bound groups."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("你没有权限")
        raise ApplicationHandlerStop
    groups = get_groups(user.id)
    if not groups:
        await update.message.reply_text("暂无绑定的群组")
        raise ApplicationHandlerStop
    lines = ["已绑定群组列表：\n"]
    for gid, info in groups.items():
        approval = "🔒 需审批" if info.get("approval_required", False) else "🔓 直接加入"
        lines.append(f"• {info['title']}\n  ID: `{gid}`\n  审批模式: {approval}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    raise ApplicationHandlerStop


async def remove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removegroup [group_id] — remove a bound group."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("你没有权限")
        raise ApplicationHandlerStop
    if not context.args:
        await update.message.reply_text("用法: /removegroup [群组ID]")
        raise ApplicationHandlerStop
    group_id = context.args[0]
    groups = get_groups(user.id)
    if str(group_id) not in groups:
        await update.message.reply_text("未找到该群组，或你无权移除它")
        raise ApplicationHandlerStop
    if remove_group(group_id):
        await update.message.reply_text(f"已移除群组: {group_id}")
    else:
        await update.message.reply_text("移除失败")
    raise ApplicationHandlerStop


async def set_approval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setapproval [group_id] — toggle approval mode for a group."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("你没有权限")
        raise ApplicationHandlerStop
    if not context.args:
        await update.message.reply_text(
            "用法: /setapproval [群组ID]\n每次执行会切换该群组的审批模式（开/关）"
        )
        raise ApplicationHandlerStop
    group_id = context.args[0]
    groups = get_groups(user.id)
    if str(group_id) not in groups:
        await update.message.reply_text("未找到该群组，或你无权修改它")
        raise ApplicationHandlerStop
    current = groups[str(group_id)].get("approval_required", False)
    new_value = not current
    if set_group_approval(group_id, user.id, new_value):
        status = "开启 🔒" if new_value else "关闭 🔓"
        await update.message.reply_text(
            f"✅ 群组「{groups[str(group_id)]['title']}」审批模式已{status}"
        )
    else:
        await update.message.reply_text("❌ 设置失败")
    raise ApplicationHandlerStop


async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invcleanup — manually trigger invite cleanup."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("⛔ 你没有权限")
        raise ApplicationHandlerStop
    await update.message.reply_text("🧹 开始清理过期数据...")
    removed = await cleanup_expired_invites()
    if redis_client:
        logs = redis_client.lrange(INVITE_LOG_KEY, 0, -1)
        valid = expired = revoked = 0
        for log in logs:
            try:
                entry = json.loads(log)
                if entry.get("revoked", False):
                    revoked += 1
                elif datetime.fromisoformat(entry["expire_at"]) > datetime.now():
                    valid += 1
                else:
                    expired += 1
            except Exception:
                expired += 1
        text = (
            f"✅ 清理完成\n\n"
            f"🗑️ 已删除记录: {removed}\n"
            f"✨ 有效邀请: {valid}\n"
            f"⏰ 待撤销: {expired}\n"
            f"🚫 已撤销: {revoked}\n"
            f"📊 总计: {len(logs)}"
        )
    else:
        text = "❌ Redis 不可用"
    await update.message.reply_text(text)
    raise ApplicationHandlerStop


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invrevoke — manually revoke expired invite links."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("⛔ 你没有权限")
        raise ApplicationHandlerStop
    await update.message.reply_text("🚫 正在撤销失效的邀请链接...")
    revoked = await revoke_expired_invites(context.application)
    await update.message.reply_text(f"✅ 已撤销 {revoked} 个失效的邀请链接")
    raise ApplicationHandlerStop


async def invstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invstats — show invite statistics."""
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not is_invite_admin(user.id):
        await update.message.reply_text("你没有权限")
        raise ApplicationHandlerStop
    if not redis_client:
        await update.message.reply_text("Redis 不可用")
        raise ApplicationHandlerStop
    logs = redis_client.lrange(INVITE_LOG_KEY, 0, 999)
    recent_invites = []
    revoked_count = 0
    for log in logs:
        try:
            entry = json.loads(log)
            if str(entry.get("admin_id")) != str(user.id):
                continue
            if entry.get("revoked", False):
                revoked_count += 1
            created = datetime.fromisoformat(entry["created_at"])
            if datetime.now() - created < timedelta(days=1):
                recent_invites.append(entry)
        except Exception:
            continue
    total_24h = len(recent_invites)
    unique_users = len({i["user_id"] for i in recent_invites})
    text = (
        f"📊 邀请统计（最近24小时）\n\n"
        f"总邀请数: {total_24h}\n"
        f"独立用户: {unique_users}\n"
        f"已撤销链接: {revoked_count}\n\n"
        f"配置:\n"
        f"邀请有效期: {INVITE_EXPIRE_MINUTES}分钟\n"
        f"邀请冷却: {INVITE_COOLDOWN_HOURS}小时"
    )
    await update.message.reply_text(text)
    raise ApplicationHandlerStop


# ── Chat member handlers ──────────────────────────────────────────────────────

async def bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Triggered when the bot's status changes in a group."""
    chat_member_update = update.chat_member or update.my_chat_member
    if not chat_member_update:
        return

    chat = update.effective_chat
    new_member = chat_member_update.new_chat_member
    old_member = chat_member_update.old_chat_member

    if not new_member or new_member.user.id != context.bot.id:
        return

    old_status = old_member.status if old_member else None
    new_status = new_member.status

    if new_status in ("left", "kicked"):
        return

    added_by = update.effective_user
    if not added_by or added_by.id == context.bot.id:
        return

    if not is_invite_admin(added_by.id):
        logger.warning("Non-invite-admin %s tried to add bot to group", added_by.id)
        try:
            await context.bot.send_message(chat_id=chat.id, text="只有机器人管理员才能使用此功能")
            await context.bot.leave_chat(chat.id)
        except Exception as exc:
            logger.error("Failed to leave chat: %s", exc)
        return

    if new_status == "administrator" and old_status != "administrator":
        if save_group(chat.id, chat.title, added_by.id):
            try:
                await context.bot.send_message(
                    chat_id=added_by.id,
                    text=(
                        f"✅ 机器人已成功绑定到群组「{chat.title}」\n"
                        f"群组ID: `{chat.id}`\n"
                        f"分享链接（仅包含你的群组）：\n"
                        f"• 选择加入: https://t.me/{context.bot.username}?start=join_{added_by.id}\n"
                        f"• 加入全部: https://t.me/{context.bot.username}?start=joinall_{added_by.id}"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.error("Failed to notify admin: %s", exc)
    elif new_status == "member":
        mention = f"@{added_by.username}" if added_by.username else added_by.full_name
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"{mention} 请将机器人设为管理员，否则无法使用加群功能",
            )
        except Exception as exc:
            logger.error("Failed to send message: %s", exc)


async def bot_removed_from_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clean up when the bot is removed from a group."""
    chat_member_update = update.chat_member or update.my_chat_member
    if not chat_member_update:
        return

    chat = update.effective_chat
    new_member = chat_member_update.new_chat_member
    if not new_member or new_member.user.id != context.bot.id:
        return

    if new_member.status in ("left", "kicked"):
        remove_group(chat.id)
        logger.info("Bot removed from group: %s", getattr(chat, "title", chat.id))


# ── Handler registration ──────────────────────────────────────────────────────

def register_invite_handlers(app: Application) -> None:
    """Register all invite-module handlers onto *app*.

    Invite-specific handlers are placed in group ``-1`` so they run before
    baogao's catch-all handlers (group ``0``).  Each handler raises
    ``ApplicationHandlerStop`` after processing to prevent baogao's catch-all
    from also acting on the same update.
    """
    # ── Admin message state machine (must intercept before baogao's on_text) ─
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_message_handler),
        group=-1,
    )

    # ── Invite callback buttons ────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^adm_"), group=-1)
    app.add_handler(CallbackQueryHandler(approve_request_callback, pattern="^inv_approve_"), group=-1)
    app.add_handler(CallbackQueryHandler(reject_request_callback, pattern="^inv_reject_"), group=-1)
    app.add_handler(CallbackQueryHandler(select_group_callback, pattern="^inv_select_"), group=-1)
    app.add_handler(CallbackQueryHandler(join_all_callback, pattern="^inv_joinall_"), group=-1)

    # ── Group membership events ───────────────────────────────────────────────
    app.add_handler(ChatMemberHandler(bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(bot_removed_from_group, ChatMemberHandler.MY_CHAT_MEMBER))

    # ── Invite-specific commands ───────────────────────────────────────────────
    app.add_handler(CommandHandler("invpanel", invpanel_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("bindgroup", bind_group_cmd))
    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("listgroups", list_groups_cmd))
    app.add_handler(CommandHandler("removegroup", remove_group_cmd))
    app.add_handler(CommandHandler("setapproval", set_approval_cmd))
    app.add_handler(CommandHandler("invcleanup", cleanup_cmd))
    app.add_handler(CommandHandler("invrevoke", revoke_cmd))
    app.add_handler(CommandHandler("invstats", invstats_cmd))

    logger.info("Invite module handlers registered")
