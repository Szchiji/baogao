from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.api.telegram import router as telegram_router
from app.admin.router import router as admin_router
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.bot.main import start_polling, start_webhook, create_bot_and_dispatcher

    if settings.BOT_MODE == "webhook":
        await start_webhook(settings.WEBHOOK_URL)
    else:
        create_bot_and_dispatcher()
        bot_task = asyncio.create_task(_run_polling())
        app.state.bot_task = bot_task

    yield

    # Shutdown
    if hasattr(app.state, "bot_task"):
        app.state.bot_task.cancel()
        try:
            await app.state.bot_task
        except asyncio.CancelledError:
            pass

    from app.bot.main import get_bot
    bot = get_bot()
    if bot:
        await bot.session.close()


async def _run_polling() -> None:
    from app.bot.main import get_bot, get_dispatcher
    bot = get_bot()
    dp = get_dispatcher()
    if bot and dp:
        await dp.start_polling(bot)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Baogao API",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(admin_router)
    app.include_router(telegram_router)

    return app


app = create_app()
