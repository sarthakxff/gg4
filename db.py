"""
db.py — SQLite persistence layer (fully async via aiosqlite).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tracked_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL COLLATE NOCASE,
    user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    last_status     TEXT,                          -- 'available' | 'unavailable' | NULL
    last_checked    TEXT,
    alert_mode      INTEGER NOT NULL DEFAULT 0,    -- 1 = high-frequency checks
    alert_mode_until TEXT,
    next_check      TEXT,
    backoff_until   TEXT,
    check_count     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (username, user_id)
);

CREATE INDEX IF NOT EXISTS idx_ta_username ON tracked_accounts(username);
CREATE INDEX IF NOT EXISTS idx_ta_next_check ON tracked_accounts(next_check);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL COLLATE NOCASE,
    user_id     INTEGER NOT NULL,
    event_type  TEXT    NOT NULL,  -- 'down' | 'restored' | 'check' | 'error'
    detail      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_username ON events(username);
CREATE INDEX IF NOT EXISTS idx_events_created  ON events(created_at);
"""

# ---------------------------------------------------------------------------
# Connection pool (single shared connection is fine for SQLite+WAL)
# ---------------------------------------------------------------------------
_conn: Optional[aiosqlite.Connection] = None
_lock = asyncio.Lock()


async def get_conn() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        async with _lock:
            if _conn is None:
                os.makedirs(os.path.dirname(config.DATABASE_PATH) or ".", exist_ok=True)
                _conn = await aiosqlite.connect(config.DATABASE_PATH, check_same_thread=False)
                _conn.row_factory = aiosqlite.Row
                await _conn.executescript(_SCHEMA)
                await _conn.commit()
                logger.info("Database initialised at %s", config.DATABASE_PATH)
    return _conn


async def close():
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------
async def ensure_user(user_id: int) -> None:
    db = await get_conn()
    await db.execute(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Tracked accounts CRUD
# ---------------------------------------------------------------------------
async def add_tracking(user_id: int, username: str) -> bool:
    """Returns True if newly added, False if already tracked."""
    await ensure_user(user_id)
    db = await get_conn()
    try:
        await db.execute(
            """
            INSERT INTO tracked_accounts (username, user_id)
            VALUES (?, ?)
            """,
            (username.lower(), user_id),
        )
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def remove_tracking(user_id: int, username: str) -> bool:
    """Returns True if a row was deleted."""
    db = await get_conn()
    cursor = await db.execute(
        "DELETE FROM tracked_accounts WHERE username = ? AND user_id = ?",
        (username.lower(), user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_tracked(user_id: int) -> list[dict]:
    db = await get_conn()
    cursor = await db.execute(
        """
        SELECT username, last_status, last_checked, check_count
        FROM tracked_accounts
        WHERE user_id = ?
        ORDER BY username
        """,
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def count_tracked(user_id: int) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM tracked_accounts WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_account_status(username: str) -> Optional[dict]:
    db = await get_conn()
    cursor = await db.execute(
        """
        SELECT username, last_status, last_checked, check_count
        FROM tracked_accounts
        WHERE username = ?
        LIMIT 1
        """,
        (username.lower(),),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_trackers(username: str) -> list[int]:
    """Return all user_ids tracking this username."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM tracked_accounts WHERE username = ?",
        (username.lower(),),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Scheduler helpers — all accounts due for a check
# ---------------------------------------------------------------------------
async def get_accounts_due(limit: int = 50) -> list[dict]:
    """Return accounts whose next_check is NULL or in the past, not in backoff."""
    now = _utcnow()
    db = await get_conn()
    cursor = await db.execute(
        """
        SELECT id, username, last_status, alert_mode, alert_mode_until
        FROM tracked_accounts
        WHERE (next_check IS NULL OR next_check <= ?)
          AND (backoff_until IS NULL OR backoff_until <= ?)
        ORDER BY next_check ASC NULLS FIRST
        LIMIT ?
        """,
        (now, now, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_account_after_check(
    account_id: int,
    username: str,
    new_status: str,
    next_check: str,
    alert_mode: bool,
    alert_mode_until: Optional[str],
) -> None:
    db = await get_conn()
    await db.execute(
        """
        UPDATE tracked_accounts
        SET last_status    = ?,
            last_checked   = ?,
            next_check     = ?,
            alert_mode     = ?,
            alert_mode_until = ?,
            check_count    = check_count + 1
        WHERE id = ?
        """,
        (
            new_status,
            _utcnow(),
            next_check,
            1 if alert_mode else 0,
            alert_mode_until,
            account_id,
        ),
    )
    await db.commit()


async def set_backoff(account_id: int, until: str) -> None:
    db = await get_conn()
    await db.execute(
        "UPDATE tracked_accounts SET backoff_until = ? WHERE id = ?",
        (until, account_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
async def log_event(username: str, user_id: int, event_type: str, detail: str = "") -> None:
    db = await get_conn()
    await db.execute(
        "INSERT INTO events (username, user_id, event_type, detail) VALUES (?, ?, ?, ?)",
        (username.lower(), user_id, event_type, detail),
    )
    await db.commit()


async def get_events(username: str, limit: int = 20) -> list[dict]:
    db = await get_conn()
    cursor = await db.execute(
        """
        SELECT event_type, detail, created_at
        FROM events
        WHERE username = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (username.lower(), limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
async def global_stats() -> dict:
    db = await get_conn()

    async def scalar(sql, *args):
        cur = await db.execute(sql, args)
        row = await cur.fetchone()
        return row[0] if row else 0

    return {
        "total_users": await scalar("SELECT COUNT(*) FROM users"),
        "total_tracked": await scalar("SELECT COUNT(*) FROM tracked_accounts"),
        "total_checks": await scalar("SELECT SUM(check_count) FROM tracked_accounts"),
        "total_events": await scalar("SELECT COUNT(*) FROM events"),
        "down_events": await scalar("SELECT COUNT(*) FROM events WHERE event_type='down'"),
        "restored_events": await scalar("SELECT COUNT(*) FROM events WHERE event_type='restored'"),
    }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
