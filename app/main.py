import asyncio
import logging
import sys

import uvicorn
from telegram.ext import Application

from app.bot_handlers import create_bot_application
from app.config import AppConfig, load_config
from app.crud import setting_set
from app.database import init_db
from app.web import create_fastapi


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("report-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _run_polling_async(main_app: Application) -> None:
    """Run the main bot (and all active child bots) in polling mode."""
    from app import bot_manager

    async with main_app:
        await main_app.updater.start_polling(drop_pending_updates=True)
        await main_app.start()
        try:
            n = await bot_manager.start_all_from_db()
            if n:
                logger.info("Started %d child bot(s) in polling mode", n)
            await asyncio.Event().wait()
        finally:
            await bot_manager.stop_all()
            try:
                await main_app.updater.stop()
            except Exception:
                pass
            await main_app.stop()


def run_polling(bot_app: Application) -> None:
    asyncio.run(_run_polling_async(bot_app))


async def run_webhook(bot_app: Application, config: AppConfig) -> None:
    api = create_fastapi(bot_app, config)
    uv_config = uvicorn.Config(api, host=config.host, port=config.port, log_level="info")
    server = uvicorn.Server(uv_config)
    await server.serve()


def main() -> None:
    logger.info("Starting: initializing database…")
    init_db()
    logger.info("Database ready. Loading config…")
    config = load_config()
    logger.info("Config loaded (mode=%s). Applying settings…", config.mode)
    setting_set("admin_panel_url", config.admin_panel_url)
    app = create_bot_application(config.token)
    app.bot_data["admin_panel_url"] = config.admin_panel_url
    app.bot_data["admin_panel_token"] = config.admin_panel_token

    if config.mode == "webhook":
        logger.info("Starting webhook server on %s:%s", config.host, config.port)
        asyncio.run(run_webhook(app, config))
    else:
        logger.info("Starting polling mode")
        run_polling(app)


if __name__ == "__main__":
    main()
