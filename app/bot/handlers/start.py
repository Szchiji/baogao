from __future__ import annotations

from aiogram import Router  # type: ignore
from aiogram.filters import CommandStart  # type: ignore
from aiogram.types import Message  # type: ignore

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 欢迎使用报告系统！\n\n"
        "• 发送 /report 提交一份新报告\n"
        "• 如需帮助请联系管理员"
    )
