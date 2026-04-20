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


def _start_invite_cleanup(app: Application) -> None:
    """Schedule the invite-module background cleanup task if Redis is available."""
    try:
        from app.invite.cleanup import cleanup_expired_data
        from app.invite.redis_client import redis_client as _redis

        if _redis is not None:
            asyncio.create_task(cleanup_expired_data(app))
            logger.info("Invite module: background cleanup task started")
        else:
            logger.info("Invite module: Redis unavailable — cleanup task skipped")
    except Exception as exc:  # pragma: no cover
        logger.warning("Invite module: failed to start cleanup task: %s", exc)


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
            _start_invite_cleanup(main_app)
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
    # The server runs in the current event loop; start the cleanup task first.
    asyncio.create_task(_deferred_cleanup(bot_app))
    await server.serve()


async def _deferred_cleanup(app: Application) -> None:
    """Wait briefly for the event loop to be ready, then launch invite cleanup."""
    await asyncio.sleep(1)
    _start_invite_cleanup(app)


def main() -> None:
    logger.info("Starting: initializing database…")
    init_db()
    logger.info("Database ready. Loading config…")
    config = load_config()
    logger.info("Config loaded (mode=%s). Applying settings…", config.mode)
    setting_set("admin_panel_url", config.admin_panel_url)

    # Initialise invite-module Redis data on startup
    try:
        from app.config import get_admin_user_ids
        from app.invite.redis_client import init_admin_from_env, migrate_global_groups, redis_client as _redis

        if _redis is not None:
            init_admin_from_env(get_admin_user_ids())
            migrate_global_groups()
    except Exception as exc:
        logger.warning("Invite module init skipped: %s", exc)

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
