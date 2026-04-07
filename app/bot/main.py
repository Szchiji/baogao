from __future__ import annotations

from aiogram import Bot, Dispatcher  # type: ignore
from aiogram.enums import ParseMode  # type: ignore
from aiogram.client.default import DefaultBotProperties  # type: ignore

from app.config import settings
from app.bot.handlers import start, report, admin, otp

_bot: Bot | None = None
_dp: Dispatcher | None = None


def get_bot() -> Bot | None:
    return _bot


def get_dispatcher() -> Dispatcher | None:
    return _dp


def create_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    global _bot, _dp
    _bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    _dp = Dispatcher()
    _dp.include_router(start.router)
    _dp.include_router(otp.router)
    _dp.include_router(admin.router)
    _dp.include_router(report.router)
    return _bot, _dp


async def start_polling() -> None:
    bot, dp = create_bot_and_dispatcher()
    await dp.start_polling(bot)


async def start_webhook(base_url: str, path: str = "/telegram/webhook") -> None:
    bot, dp = create_bot_and_dispatcher()
    webhook_url = f"{base_url.rstrip('/')}{path}"
    await bot.set_webhook(webhook_url)
