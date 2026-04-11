import logging
import os
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras

from app.config import DEFAULT_SETTINGS

__all__ = ["db_connection", "init_db", "init_bot_settings"]

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
        # Drop all tables to start fresh
        for table in ("audit_log", "blacklist", "users", "reports", "settings", "child_bots"):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

        # --- settings ---
        # New schema uses a composite PK (bot_id, key) for per-bot isolation.
        # bot_id = '' for the main bot; str(child_bot.id) for each child bot.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
              bot_id TEXT NOT NULL DEFAULT '',
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              PRIMARY KEY (bot_id, key)
            )
            """
        )
        # Migration: older deployments had a single-column PK on key only.
        # Add bot_id column if missing, then upgrade the PK when needed.
        conn.execute(
            "ALTER TABLE settings ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            """
            DO $$ BEGIN
              -- Drop the old single-column PK when bot_id is not yet part of it.
              IF EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conname = 'settings_pkey'
                  AND c.contype = 'p'
                  AND c.conrelid = 'settings'::regclass
                  AND array_length(c.conkey, 1) = 1
              ) THEN
                ALTER TABLE settings DROP CONSTRAINT settings_pkey;
                ALTER TABLE settings ADD PRIMARY KEY (bot_id, key);
              END IF;
            END $$
            """
        )

        # --- reports ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
              id SERIAL PRIMARY KEY,
              bot_id TEXT NOT NULL DEFAULT '',
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
        conn.execute(
            "ALTER TABLE reports ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reports_bot_status ON reports(bot_id, status, id DESC)
            """
        )
        # Keep old index for backward compatibility during transition
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, id DESC)
            """
        )

        # --- users ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              bot_id TEXT NOT NULL DEFAULT '',
              user_id BIGINT NOT NULL,
              username TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              PRIMARY KEY (bot_id, user_id)
            )
            """
        )
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            """
            DO $$ BEGIN
              IF EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conname = 'users_pkey'
                  AND c.contype = 'p'
                  AND c.conrelid = 'users'::regclass
                  AND array_length(c.conkey, 1) = 1
              ) THEN
                ALTER TABLE users DROP CONSTRAINT users_pkey;
                ALTER TABLE users ADD PRIMARY KEY (bot_id, user_id);
              END IF;
            END $$
            """
        )

        # --- blacklist ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
              bot_id TEXT NOT NULL DEFAULT '',
              user_id BIGINT NOT NULL,
              username TEXT,
              reason TEXT,
              added_at TEXT NOT NULL,
              PRIMARY KEY (bot_id, user_id)
            )
            """
        )
        conn.execute(
            "ALTER TABLE blacklist ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            """
            DO $$ BEGIN
              IF EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conname = 'blacklist_pkey'
                  AND c.contype = 'p'
                  AND c.conrelid = 'blacklist'::regclass
                  AND array_length(c.conkey, 1) = 1
              ) THEN
                ALTER TABLE blacklist DROP CONSTRAINT blacklist_pkey;
                ALTER TABLE blacklist ADD PRIMARY KEY (bot_id, user_id);
              END IF;
            END $$
            """
        )

        # --- audit_log (shared, not isolated per-bot) ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
              id SERIAL PRIMARY KEY,
              admin_id BIGINT NOT NULL,
              action TEXT NOT NULL,
              report_id INT,
              note TEXT,
              created_at TEXT NOT NULL
            )
            """
        )

        # --- child_bots ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS child_bots (
              id SERIAL PRIMARY KEY,
              token TEXT NOT NULL UNIQUE,
              bot_username TEXT,
              bot_name TEXT,
              owner_user_id BIGINT,
              created_at TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              admin_panel_url TEXT
            )
            """
        )
        conn.execute(
            "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS admin_panel_url TEXT"
        )

        # Insert default settings for the main bot (bot_id='') only if absent.
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (bot_id, key, value) VALUES ('', %s, %s) ON CONFLICT (bot_id, key) DO NOTHING",
                (key, value),
            )


def init_bot_settings(bot_id: str) -> None:
    """Seed all DEFAULT_SETTINGS rows for a new child bot (bot_id must be non-empty)."""
    if not bot_id:
        return
    with db_connection() as conn:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (bot_id, key, value) VALUES (%s, %s, %s) ON CONFLICT (bot_id, key) DO NOTHING",
                (bot_id, key, value),
            )
