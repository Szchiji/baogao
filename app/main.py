import asyncio
import logging

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
)
logger = logging.getLogger("report-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def run_polling(bot_app: Application) -> None:
    bot_app.run_polling(drop_pending_updates=True)


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
