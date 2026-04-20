"""Background cleanup tasks for the invite link management module."""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from app.invite.redis_client import INVITE_LOG_KEY, USER_INVITE_PREFIX, redis_client

logger = logging.getLogger("report-bot")


async def cleanup_expired_invites() -> int:
    """Remove invite log entries that expired more than 1 hour ago."""
    if not redis_client:
        return 0
    logs = redis_client.lrange(INVITE_LOG_KEY, 0, -1)
    removed = 0
    for log in logs:
        try:
            entry = json.loads(log)
            expire_at = datetime.fromisoformat(entry["expire_at"])
            if datetime.now() - expire_at > timedelta(hours=1):
                redis_client.lrem(INVITE_LOG_KEY, 0, log)
                removed += 1
        except Exception:
            continue
    if removed:
        logger.info("Invite cleanup: removed %d expired log entries", removed)
    return removed


async def revoke_expired_invites(application) -> int:
    """Revoke Telegram invite links that have passed their expiry time."""
    if not redis_client:
        return 0
    logs = redis_client.lrange(INVITE_LOG_KEY, 0, -1)
    revoked_count = 0
    failed_count = 0
    for log in logs:
        try:
            entry = json.loads(log)
            if entry.get("revoked", False):
                continue
            expire_at = datetime.fromisoformat(entry["expire_at"])
            if datetime.now() < expire_at:
                continue
            group_id = entry.get("group_id")
            invite_link = entry.get("invite_link", "")
            if not group_id or not invite_link:
                continue
            try:
                await application.bot.revoke_chat_invite_link(
                    chat_id=int(group_id),
                    invite_link=invite_link,
                )
                entry["revoked"] = True
                entry["revoked_at"] = datetime.now().isoformat()
                redis_client.lrem(INVITE_LOG_KEY, 0, log)
                redis_client.lpush(INVITE_LOG_KEY, json.dumps(entry))
                revoked_count += 1
                logger.info("Revoked expired invite: %s", invite_link)
            except Exception as exc:
                failed_count += 1
                logger.error("Failed to revoke invite %s: %s", invite_link, exc)
                err_str = str(exc)
                if "INVITE_HASH_EXPIRED" in err_str or "INVITE_HASH_INVALID" in err_str:
                    entry["revoked"] = True
                    entry["revoke_failed"] = True
                    entry["revoke_error"] = err_str
                    redis_client.lrem(INVITE_LOG_KEY, 0, log)
                    redis_client.lpush(INVITE_LOG_KEY, json.dumps(entry))
        except Exception as exc:
            logger.error("Error processing log entry during revoke: %s", exc)
            continue
    if revoked_count or failed_count:
        logger.info("Revoke run: %d revoked, %d failed", revoked_count, failed_count)
    return revoked_count


async def cleanup_expired_data(application) -> None:
    """Long-running background task: clean up expired invites every hour."""
    while True:
        try:
            removed = await cleanup_expired_invites()
            revoked = await revoke_expired_invites(application)

            if redis_client:
                logs = redis_client.lrange(INVITE_LOG_KEY, 0, -1)
                active = expired = revoked_total = 0
                for log in logs:
                    try:
                        entry = json.loads(log)
                        if entry.get("revoked", False):
                            revoked_total += 1
                        elif datetime.fromisoformat(entry["expire_at"]) > datetime.now():
                            active += 1
                        else:
                            expired += 1
                    except Exception:
                        continue
                logger.info(
                    "Invite cleanup stats: %d active, %d expired, %d revoked total; "
                    "removed %d logs, revoked %d this run",
                    active, expired, revoked_total, removed, revoked,
                )
        except Exception as exc:
            logger.error("Invite cleanup error: %s", exc)
        await asyncio.sleep(3600)
