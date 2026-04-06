from __future__ import annotations

from aiogram import Router, F  # type: ignore
from aiogram.filters import ChatType  # type: ignore
from aiogram.types import Message  # type: ignore

from app.config import settings
from app.database import AsyncSessionLocal
from app.services.otp_service import verify_otp

router = Router()


@router.message(F.chat.type == "private", F.text.regexp(r"^\d{6}$"))
async def handle_otp(message: Message) -> None:
    user_id = message.from_user.id
    if user_id not in settings.admin_ids_list:
        return  # Silently ignore non-admins

    otp_code = message.text.strip()
    async with AsyncSessionLocal() as db:
        verified = await verify_otp(db, otp_code, user_id)
        await db.commit()

    if verified:
        await message.answer("✅ 验证成功！请返回浏览器，页面将自动跳转。")
    else:
        await message.answer("❌ 验证码无效或已过期，请重新获取。")
