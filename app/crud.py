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
