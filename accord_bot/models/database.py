"""Database connection and schema management."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def get_db_path() -> Path:
    """Get the database file path from environment or default."""
    return Path(os.getenv("ACCORD_DB_FILE", "accord.db"))


def connect_db() -> sqlite3.Connection:
    """Create a database connection with row factory."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def get_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a metadata value from the database."""
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"])


def set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a metadata value in the database."""
    conn.execute(
        """
        INSERT INTO metadata(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Run one-time schema migrations."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "confession_settings" in tables and "dm_panel_settings" not in tables:
        conn.execute("ALTER TABLE confession_settings RENAME TO dm_panel_settings")
        log.info("Migrated table confession_settings → dm_panel_settings")


def ensure_database() -> None:
    """Create database tables and indexes if they don't exist."""
    with connect_db() as conn:
        _migrate(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS consent_pairs (
                guild_id INTEGER NOT NULL,
                user_low INTEGER NOT NULL,
                user_high INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_low, user_high)
            );

            CREATE TABLE IF NOT EXISTS relationships (
                guild_id INTEGER NOT NULL,
                pair_key TEXT NOT NULL,
                request_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT,
                source_channel_id INTEGER,
                source_message_id INTEGER,
                PRIMARY KEY (guild_id, pair_key)
            );

            CREATE TABLE IF NOT EXISTS dm_requests (
                guild_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                request_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT,
                PRIMARY KEY (guild_id, requester_id, target_id)
            );

            CREATE TABLE IF NOT EXISTS request_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dm_panel_settings (
                guild_id INTEGER PRIMARY KEY,
                panel_channel_id INTEGER,
                panel_message_id INTEGER,
                target_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                message TEXT NOT NULL,
                actor_id INTEGER,
                user1_id INTEGER,
                user2_id INTEGER,
                request_type TEXT
            );

            -- Performance indexes
            CREATE INDEX IF NOT EXISTS idx_consent_pairs_guild
                ON consent_pairs(guild_id);

            CREATE INDEX IF NOT EXISTS idx_relationships_guild_pair
                ON relationships(guild_id, pair_key);

            CREATE INDEX IF NOT EXISTS idx_dm_requests_guild
                ON dm_requests(guild_id);

            CREATE INDEX IF NOT EXISTS idx_audit_log_guild
                ON audit_log(guild_id);

            CREATE INDEX IF NOT EXISTS idx_audit_log_users
                ON audit_log(guild_id, user1_id, user2_id);

            CREATE INDEX IF NOT EXISTS idx_audit_log_actor
                ON audit_log(guild_id, actor_id);
            """
        )


def iter_unique_pair_rows(pairs_by_guild: dict[int, set[tuple[int, int]]]):
    """Iterate over unique user pairs from the pairs_by_guild structure."""
    for guild_id, pairs in pairs_by_guild.items():
        seen: set[tuple[int, int]] = set()
        for a, b in pairs:
            if a == b:
                continue
            lo, hi = (a, b) if a < b else (b, a)
            if (lo, hi) in seen:
                continue
            seen.add((lo, hi))
            yield guild_id, lo, hi
