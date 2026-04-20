"""Redis operations for the invite link management module."""
import json
import logging
from datetime import datetime, timedelta

import redis

from app.invite.config import (
    INVITE_COOLDOWN_HOURS,
    INVITE_EXPIRE_MINUTES,
    REDIS_URL,
)

logger = logging.getLogger("report-bot")

# ── Redis key constants ───────────────────────────────────────────────────────
GROUPS_KEY = "tg_bot:groups"              # legacy global key (migration only)
GROUPS_PREFIX = "tg_bot:groups:"          # per-admin groups: key = groups:{admin_id}
GROUP_OWNER_PREFIX = "tg_bot:group_owner:"  # reverse lookup: group_id -> admin_id
ADMINS_KEY = "tg_bot:admins"
INVITE_LOG_KEY = "tg_bot:invite_log"
USER_INVITE_PREFIX = "tg_bot:user_invite:"
PENDING_REQUEST_PREFIX = "tg_bot:pending:"  # key = pending:{user_id}_{group_id}
ADMIN_STATE_PREFIX = "tg_bot:admin_state:"  # key = admin_state:{user_id}
ADMIN_STATE_TTL = 300  # 5-minute state expiry


def _groups_key(admin_id) -> str:
    return f"{GROUPS_PREFIX}{admin_id}"


# ── Redis connection ──────────────────────────────────────────────────────────
try:
    redis_client: redis.Redis | None = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()  # type: ignore[union-attr]
    logger.info("Invite module: Redis connected successfully")
except Exception as exc:
    logger.warning("Invite module: Redis connection failed (%s) — invite features disabled", exc)
    redis_client = None


# ── Admin helpers ─────────────────────────────────────────────────────────────

def init_admin_from_env(admin_ids: list[int]) -> None:
    """Sync admin IDs from environment into Redis."""
    if not redis_client:
        return
    for admin_id in admin_ids:
        redis_client.sadd(ADMINS_KEY, str(admin_id))


def is_redis_admin(user_id: int) -> bool:
    """Return True if *user_id* is registered as an invite admin in Redis."""
    if not redis_client:
        return False
    admins = redis_client.smembers(ADMINS_KEY)
    return str(user_id) in admins


# ── Group CRUD ────────────────────────────────────────────────────────────────

def get_groups(admin_id) -> dict:
    """Return the groups dict for *admin_id*."""
    if not redis_client:
        return {}
    try:
        data = redis_client.get(_groups_key(admin_id))
        return json.loads(data) if data else {}
    except Exception as exc:
        logger.error("get_groups failed: %s", exc)
        return {}


def save_group(group_id, title, added_by) -> bool:
    """Save a group under *added_by*'s namespace."""
    if not redis_client:
        logger.error("Redis not available — cannot save group")
        return False
    try:
        groups = get_groups(added_by)
        groups[str(group_id)] = {
            "title": title,
            "added_by": added_by,
            "invite_link": None,
        }
        redis_client.set(_groups_key(added_by), json.dumps(groups))
        redis_client.set(f"{GROUP_OWNER_PREFIX}{group_id}", str(added_by))
        logger.info("Group saved: %s (%s) for admin %s", title, group_id, added_by)
        return True
    except Exception as exc:
        logger.error("save_group failed: %s", exc)
        return False


def remove_group(group_id) -> bool:
    """Remove a group, locating its owner via the reverse-lookup key."""
    if not redis_client:
        return False
    try:
        owner_key = f"{GROUP_OWNER_PREFIX}{group_id}"
        admin_id = redis_client.get(owner_key)
        if not admin_id:
            logger.warning("No owner found for group %s", group_id)
            return False
        groups = get_groups(admin_id)
        groups.pop(str(group_id), None)
        redis_client.set(_groups_key(admin_id), json.dumps(groups))
        redis_client.delete(owner_key)
        return True
    except Exception as exc:
        logger.error("remove_group failed: %s", exc)
        return False


def set_group_approval(group_id, admin_id, required: bool) -> bool:
    """Toggle approval-required flag for a group."""
    if not redis_client:
        return False
    try:
        groups = get_groups(admin_id)
        gid = str(group_id)
        if gid not in groups:
            return False
        groups[gid]["approval_required"] = required
        redis_client.set(_groups_key(admin_id), json.dumps(groups))
        return True
    except Exception as exc:
        logger.error("set_group_approval failed: %s", exc)
        return False


# ── Pending join requests ─────────────────────────────────────────────────────

def save_pending_request(user_id, group_id, user_info: dict, group_title: str, admin_id) -> bool:
    if not redis_client:
        return False
    key = f"{PENDING_REQUEST_PREFIX}{user_id}_{group_id}"
    data = {
        "user_id": user_id,
        "username": user_info.get("username"),
        "first_name": user_info.get("first_name", ""),
        "group_id": str(group_id),
        "group_title": group_title,
        "admin_id": admin_id,
        "created_at": datetime.now().isoformat(),
    }
    redis_client.setex(key, INVITE_COOLDOWN_HOURS * 3600, json.dumps(data))
    return True


def get_pending_request(user_id, group_id):
    if not redis_client:
        return None
    key = f"{PENDING_REQUEST_PREFIX}{user_id}_{group_id}"
    data = redis_client.get(key)
    return json.loads(data) if data else None


def delete_pending_request(user_id, group_id) -> None:
    if not redis_client:
        return
    redis_client.delete(f"{PENDING_REQUEST_PREFIX}{user_id}_{group_id}")


# ── Admin input state ─────────────────────────────────────────────────────────

def get_admin_state(user_id):
    if not redis_client:
        return None
    data = redis_client.get(f"{ADMIN_STATE_PREFIX}{user_id}")
    return json.loads(data) if data else None


def set_admin_state(user_id, state: dict) -> None:
    if not redis_client:
        return
    redis_client.setex(f"{ADMIN_STATE_PREFIX}{user_id}", ADMIN_STATE_TTL, json.dumps(state))


def clear_admin_state(user_id) -> None:
    if not redis_client:
        return
    redis_client.delete(f"{ADMIN_STATE_PREFIX}{user_id}")


# ── Invite cooldown ───────────────────────────────────────────────────────────

def can_user_get_invite(user_id, group_id) -> tuple[bool, int]:
    """Return (can_get, ttl_seconds).  If cooldown active, can_get is False."""
    if not redis_client:
        return True, 0
    key = f"{USER_INVITE_PREFIX}{user_id}:{group_id}"
    if redis_client.exists(key):
        ttl = redis_client.ttl(key)
        return False, int(ttl)
    return True, 0


def record_user_invite(user_id, group_id) -> None:
    if not redis_client:
        return
    key = f"{USER_INVITE_PREFIX}{user_id}:{group_id}"
    redis_client.setex(key, INVITE_COOLDOWN_HOURS * 3600, datetime.now().isoformat())


# ── Invite log ────────────────────────────────────────────────────────────────

def log_invite(user_id, group_id, invite_link: str, group_title: str, admin_id=None) -> None:
    if not redis_client:
        return
    entry = {
        "user_id": user_id,
        "group_id": group_id,
        "group_title": group_title,
        "invite_link": invite_link,
        "admin_id": admin_id,
        "created_at": datetime.now().isoformat(),
        "expire_at": (datetime.now() + timedelta(minutes=INVITE_EXPIRE_MINUTES)).isoformat(),
        "revoked": False,
    }
    redis_client.lpush(INVITE_LOG_KEY, json.dumps(entry))
    redis_client.ltrim(INVITE_LOG_KEY, 0, 999)


# ── Data migration ────────────────────────────────────────────────────────────

def migrate_global_groups() -> None:
    """Migrate legacy single-namespace groups to per-admin namespaces."""
    if not redis_client:
        return
    try:
        data = redis_client.get(GROUPS_KEY)
        if not data:
            return
        global_groups = json.loads(data)
        if not global_groups:
            return
        logger.info("Migrating %d groups from global key to per-admin keys…", len(global_groups))
        for gid, info in global_groups.items():
            admin_id = info.get("added_by")
            if not admin_id:
                logger.warning("Group %s has no added_by — skipping migration", gid)
                continue
            save_group(gid, info["title"], admin_id)
        try:
            redis_client.rename(GROUPS_KEY, f"{GROUPS_KEY}:migrated")
        except Exception as rename_err:
            logger.warning("Could not rename old groups key: %s", rename_err)
        logger.info("Group migration complete")
    except Exception as exc:
        logger.error("Group migration failed: %s", exc)
