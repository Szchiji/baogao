import logging
import os
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.sql

from app.config import DEFAULT_SETTINGS

logger = logging.getLogger("report-bot")

DATABASE_URL = os.getenv("DATABASE_URL", "")


class _PGConn:
    """Thin wrapper that gives psycopg2 the same conn.execute() interface as sqlite3."""

    def __init__(self, raw_conn: Any) -> None:
        self._conn = raw_conn
        self._cur = raw_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: Any, params: tuple | None = None) -> Any:
        self._cur.execute(sql, params)
        return self._cur

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        try:
            self._cur.close()
        finally:
            self._conn.close()


@contextmanager
def db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    raw = psycopg2.connect(DATABASE_URL)
    conn = _PGConn(raw)
    try:
        yield conn
        conn.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
              id SERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL,
              username TEXT,
              tag TEXT,
              data_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              review_feedback TEXT,
              created_at TEXT NOT NULL,
              reviewed_at TEXT,
              channel_message_link TEXT
            )
            """
        )
        # Always ensure reports.id has an appropriate DEFAULT, but the mechanism
        # differs by column type:
        # • integer/bigint — use a sequence-backed DEFAULT (nextval).  Tables
        #   migrated from SQLite may have a plain INTEGER id with no sequence,
        #   causing NotNullViolation when id is omitted from INSERT statements.
        #   CREATE SEQUENCE IF NOT EXISTS, setval, and ALTER COLUMN SET DEFAULT
        #   are all idempotent so this block is safe to run on every startup.
        # • uuid — use gen_random_uuid() as the DEFAULT so that INSERT statements
        #   that omit id still get a unique identifier.  Without this, the NULL
        #   primary-key constraint is violated.  We only set the default when none
        #   is already present to avoid overwriting a deliberately chosen default.
        id_col_info = conn.execute(
            """
            SELECT data_type, column_default FROM information_schema.columns
            WHERE table_name = 'reports' AND column_name = 'id'
            """
        ).fetchone()
        if id_col_info and id_col_info["data_type"] != "uuid":
            conn.execute("CREATE SEQUENCE IF NOT EXISTS reports_id_seq")
            conn.execute(
                """
                SELECT setval(
                    'reports_id_seq',
                    COALESCE((SELECT MAX(id) FROM reports), 0) + 1,
                    false
                )
                """
            ).fetchone()
            conn.execute(
                "ALTER TABLE reports ALTER COLUMN id SET DEFAULT nextval('reports_id_seq')"
            )
        elif id_col_info and id_col_info["data_type"] == "uuid" and not id_col_info["column_default"]:
            # UUID id column with no default — set gen_random_uuid() so that
            # INSERT statements that omit id receive an auto-generated UUID.
            conn.execute(
                "ALTER TABLE reports ALTER COLUMN id SET DEFAULT gen_random_uuid()"
            )
        # Migration: rename camelCase columns to snake_case BEFORE adding new columns.
        # This must run before ADD COLUMN operations to avoid a conflict where ADD COLUMN
        # creates the snake_case column and then the subsequent RENAME fails because the
        # target already exists.
        for old_col, new_col in [
            ("userId", "user_id"),
            ("dataJson", "data_json"),
            ("reviewFeedback", "review_feedback"),
            ("channelMessageLink", "channel_message_link"),
            ("createdAt", "created_at"),
            ("reviewedAt", "reviewed_at"),
        ]:
            has_camel = conn.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'reports' AND column_name = %s
                """,
                (old_col,),
            ).fetchone()
            if has_camel:
                has_target = conn.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'reports' AND column_name = %s
                    """,
                    (new_col,),
                ).fetchone()
                if not has_target:
                    # Target column does not exist yet — safe to rename.
                    conn.execute(
                        psycopg2.sql.SQL("ALTER TABLE reports RENAME COLUMN {} TO {}").format(
                            psycopg2.sql.Identifier(old_col),
                            psycopg2.sql.Identifier(new_col),
                        )
                    )
                else:
                    # Both camelCase and snake_case columns exist (zombie state from a
                    # previously interrupted migration).  The snake_case column is already
                    # used by all queries, so drop the now-unused camelCase column to
                    # eliminate its NOT NULL constraint that would otherwise break INSERTs.
                    conn.execute(
                        psycopg2.sql.SQL("ALTER TABLE reports DROP COLUMN {}").format(
                            psycopg2.sql.Identifier(old_col),
                        )
                    )
        # Migration: add missing columns if they do not exist yet
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS user_id BIGINT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS username TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS tag TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS data_json TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS review_feedback TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS created_at TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS reviewed_at TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS channel_message_link TEXT")
        # Create index AFTER all column migrations so that the indexed column (status)
        # is guaranteed to exist even on legacy databases that lacked it initially.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, id DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id BIGINT PRIMARY KEY,
              username TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL
            )
            """
        )
        # Migration: if users table exists with a different schema (missing user_id column),
        # drop and recreate it. User rows are re-inserted automatically on next interaction.
        has_user_id_col = conn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'user_id'
            """
        ).fetchone()
        if not has_user_id_col:
            conn.execute("DROP TABLE users")
            conn.execute(
                """
                CREATE TABLE users (
                  user_id BIGINT PRIMARY KEY,
                  username TEXT,
                  first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL
                )
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
              user_id BIGINT PRIMARY KEY,
              username TEXT,
              reason TEXT,
              added_at TEXT NOT NULL
            )
            """
        )
        # Migration: if blacklist table exists with a different schema, drop and recreate.
        has_blacklist_col = conn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'blacklist' AND column_name = 'user_id'
            """
        ).fetchone()
        if not has_blacklist_col:
            conn.execute("DROP TABLE blacklist")
            conn.execute(
                """
                CREATE TABLE blacklist (
                  user_id BIGINT PRIMARY KEY,
                  username TEXT,
                  reason TEXT,
                  added_at TEXT NOT NULL
                )
                """
            )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", (key, value)
            )
