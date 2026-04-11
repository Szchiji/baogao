from app.database import db_connection
from app.utils import utc_now_iso


def setting_get(key: str, default: str = "") -> str:
    with db_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else default


def setting_set(key: str, value: str) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def upsert_user(user_id: int, username: str | None) -> None:
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_seen, last_seen) VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen
            """,
            (user_id, username or "", now, now),
        )


def is_user_banned(user_id: int) -> bool:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blacklist WHERE user_id = %s", (user_id,)
        ).fetchone()
        return row is not None


def ban_user(user_id: int, username: str | None, reason: str) -> None:
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO blacklist (user_id, username, reason, added_at) VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              reason=excluded.reason,
              added_at=excluded.added_at
            """,
            (user_id, username or "", reason or "管理员限制", now),
        )


def unban_user(user_id: int) -> None:
    with db_connection() as conn:
        conn.execute("DELETE FROM blacklist WHERE user_id = %s", (user_id,))


def log_audit(admin_id: int, action: str, report_id: int | None = None, note: str = "") -> None:
    """Record an admin action in the audit log."""
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, report_id, note, created_at) VALUES (%s, %s, %s, %s, %s)",
            (admin_id, action, report_id, note or None, now),
        )


def get_user_reports(user_id: int, offset: int = 0, limit: int = 5) -> list:
    """Return *limit+1* reports for *user_id* starting at *offset* (to detect has_more)."""
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, tag, status, created_at, review_feedback
            FROM reports WHERE user_id = %s
            ORDER BY id DESC LIMIT %s OFFSET %s
            """,
            (user_id, limit + 1, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def list_child_bots() -> list[dict]:
    """Return all child bots ordered by creation time."""
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, token, bot_username, bot_name, owner_user_id, created_at, active FROM child_bots ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def add_child_bot(token: str, bot_username: str = "", bot_name: str = "", owner_user_id: int | None = None) -> None:
    """Insert a new child bot record. Raises if the token already exists."""
    now = utc_now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO child_bots (token, bot_username, bot_name, owner_user_id, created_at, active)
            VALUES (%s, %s, %s, %s, %s, 1)
            """,
            (token, bot_username or "", bot_name or "", owner_user_id, now),
        )


def remove_child_bot(token: str) -> None:
    """Delete a child bot record by token."""
    with db_connection() as conn:
        conn.execute("DELETE FROM child_bots WHERE token = %s", (token,))


def set_child_bot_active(token: str, active: bool) -> None:
    """Enable or disable a child bot."""
    with db_connection() as conn:
        conn.execute(
            "UPDATE child_bots SET active = %s WHERE token = %s",
            (1 if active else 0, token),
        )


def update_child_bot_info(token: str, bot_username: str, bot_name: str) -> None:
    """Update the username/name fields (populated after connecting to Telegram)."""
    with db_connection() as conn:
        conn.execute(
            "UPDATE child_bots SET bot_username = %s, bot_name = %s WHERE token = %s",
            (bot_username or "", bot_name or "", token),
        )


def is_rate_limited_submission(user_id: int, window_seconds: int = 3600, max_count: int = 3) -> bool:
    """Return True when the user has submitted >= *max_count* reports within the last *window_seconds*."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM reports WHERE user_id = %s AND created_at > %s",
            (user_id, cutoff),
        ).fetchone()
    return (row["cnt"] if row else 0) >= max_count
