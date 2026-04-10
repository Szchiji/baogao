import logging
import os
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras

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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, id DESC)
            """
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
        # Insert default settings only if they don't already exist, so existing
        # configured values are never overwritten on restart/redeploy.
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (key, value),
            )
