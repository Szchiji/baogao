"""Manages dynamically started child bot Application instances.

Each child bot runs in polling mode inside the same asyncio event loop as the
main application.  Child bots share the same database (and therefore settings,
users, reports, etc.) and use exactly the same handler set as the main bot.
"""
import asyncio
import logging

from telegram.ext import Application

from app.bot_handlers import create_bot_application
from app.crud import update_child_bot_info

logger = logging.getLogger("report-bot")

# token -> (Application, asyncio.Task)
_running: dict[str, tuple[Application, "asyncio.Task[None]"]] = {}


async def _poll_child(app: Application) -> None:
    """Coroutine that runs a single child bot in polling mode."""
    async with app:
        await app.updater.start_polling(drop_pending_updates=True)
        await app.start()
        try:
            # Try to fetch bot info to populate username/name in the DB.
            try:
                me = await app.bot.get_me()
                update_child_bot_info(
                    app.bot.token,
                    bot_username=me.username or "",
                    bot_name=me.full_name or "",
                )
                logger.info("Child bot started: @%s (id=%s)", me.username, me.id)
            except Exception:
                logger.warning("Child bot started but could not fetch bot info", exc_info=True)

            # Keep polling until cancelled.
            await asyncio.Event().wait()
        finally:
            try:
                await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
            except Exception:
                pass


async def start_child_bot(token: str, owner_user_id: int | None = None) -> bool:
    """Start a child bot by *token*.

    *owner_user_id* is the Telegram user ID of the sub-admin who owns this
    child bot.  When provided it is stored in ``bot_data["child_admin_id"]``
    so that admin commands are restricted to that user only.

    Returns ``True`` if the bot was started, ``False`` if it was already
    running.  Raises on configuration or network errors.
    """
    if token in _running:
        logger.debug("Child bot already running (token=…%s)", token[-8:])
        return False

    app = create_bot_application(token, owner_user_id=owner_user_id)
    task = asyncio.create_task(_poll_child(app))

    def _on_done(t: "asyncio.Task[None]") -> None:
        _running.pop(token, None)
        if not t.cancelled() and t.exception():
            logger.error(
                "Child bot task failed (token=…%s): %s", token[-8:], t.exception()
            )

    task.add_done_callback(_on_done)
    _running[token] = (app, task)
    return True


async def stop_child_bot(token: str) -> bool:
    """Stop a running child bot by *token*.

    Returns ``True`` if the bot was stopped, ``False`` if it was not running.
    """
    entry = _running.pop(token, None)
    if entry is None:
        return False
    _, task = entry
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    logger.info("Child bot stopped (token=…%s)", token[-8:])
    return True


async def start_all_from_db() -> int:
    """Load all *active* child bots from the database and start them.

    Returns the number of bots successfully started.
    """
    from app.crud import list_child_bots  # avoid circular import at module level

    bots = list_child_bots()
    started = 0
    for cb in bots:
        if not cb.get("active"):
            continue
        token = cb["token"]
        owner_user_id = cb.get("owner_user_id")
        try:
            ok = await start_child_bot(token, owner_user_id=owner_user_id)
            if ok:
                started += 1
        except Exception:
            logger.exception("Failed to start child bot (token=…%s)", token[-8:])
    return started


async def stop_all() -> None:
    """Stop every running child bot."""
    for token in list(_running.keys()):
        await stop_child_bot(token)


def is_running(token: str) -> bool:
    """Return ``True`` if the child bot for *token* is currently active."""
    entry = _running.get(token)
    if entry is None:
        return False
    _, task = entry
    return not task.done()


def list_running_tokens() -> list[str]:
    """Return the list of tokens whose child bots are currently polling."""
    return [t for t, (_, task) in _running.items() if not task.done()]
