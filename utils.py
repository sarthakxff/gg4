"""
utils.py — Shared helpers: logging setup, time formatting, embed builders, token bucket.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional

import discord

import config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    logging.basicConfig(level=level, format=fmt)
    # Quieten noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Token Bucket rate limiter
# ---------------------------------------------------------------------------
class TokenBucket:
    """Allow at most `rate` tokens per `period` seconds."""

    def __init__(self, rate: int, period: float = 60.0):
        self._rate = rate
        self._period = period
        self._tokens = float(rate)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            refill = elapsed * (self._rate / self._period)
            self._tokens = min(self._rate, self._tokens + refill)
            self._last_refill = now

            if self._tokens < 1:
                wait = (1 - self._tokens) / (self._rate / self._period)
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_str() -> str:
    return utcnow().strftime("%Y-%m-%d %H:%M:%S")


def add_seconds(seconds: float) -> str:
    from datetime import timedelta
    return (utcnow() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def jitter(lo: int, hi: int) -> float:
    """Random float seconds in [lo, hi] with slight jitter."""
    return random.uniform(lo, hi)


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def parse_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ago(dt: Optional[datetime]) -> str:
    if dt is None:
        return "never"
    delta = utcnow() - dt
    return human_duration(delta.total_seconds()) + " ago"


# ---------------------------------------------------------------------------
# Discord embed factories
# ---------------------------------------------------------------------------
STATUS_EMOJI = {
    "available": "🟢",
    "unavailable": "🔴",
    None: "⬜",
}

STATUS_COLOUR = {
    "available": discord.Colour.green(),
    "unavailable": discord.Colour.red(),
    None: discord.Colour.greyple(),
}


def embed_account_down(username: str) -> discord.Embed:
    e = discord.Embed(
        title="🔴 Account Unavailable",
        description=f"**@{username}** is no longer accessible on Instagram.",
        colour=discord.Colour.red(),
        timestamp=utcnow(),
    )
    e.add_field(name="Profile", value=f"https://instagram.com/{username}", inline=False)
    e.set_footer(text="Instagram Monitor")
    return e


def embed_account_restored(username: str) -> discord.Embed:
    e = discord.Embed(
        title="🟢 Account Restored",
        description=f"**@{username}** is accessible on Instagram again.",
        colour=discord.Colour.green(),
        timestamp=utcnow(),
    )
    e.add_field(name="Profile", value=f"https://instagram.com/{username}", inline=False)
    e.set_footer(text="Instagram Monitor")
    return e


def embed_status(row: dict) -> discord.Embed:
    username = row["username"]
    status = row.get("last_status")
    checked = parse_utc(row.get("last_checked"))
    emoji = STATUS_EMOJI.get(status, "⬜")
    colour = STATUS_COLOUR.get(status, discord.Colour.greyple())

    e = discord.Embed(
        title=f"{emoji} @{username}",
        colour=colour,
        timestamp=utcnow(),
    )
    e.add_field(name="Status", value=status or "Unknown", inline=True)
    e.add_field(name="Last Checked", value=ago(checked), inline=True)
    e.add_field(name="Total Checks", value=str(row.get("check_count", 0)), inline=True)
    e.add_field(name="Profile", value=f"https://instagram.com/{username}", inline=False)
    e.set_footer(text="Instagram Monitor")
    return e


def embed_list(rows: list[dict], user: discord.User | discord.Member) -> discord.Embed:
    e = discord.Embed(
        title=f"📋 Tracked Accounts — {user.display_name}",
        colour=discord.Colour.blurple(),
        timestamp=utcnow(),
    )
    if not rows:
        e.description = "You're not tracking any accounts yet.\nUse `/track <username>` to start."
        return e

    lines = []
    for r in rows:
        status = r.get("last_status")
        emoji = STATUS_EMOJI.get(status, "⬜")
        checked = parse_utc(r.get("last_checked"))
        lines.append(f"{emoji} **@{r['username']}** — {status or 'pending'} ({ago(checked)})")

    e.description = "\n".join(lines)
    e.set_footer(text=f"{len(rows)} account(s) tracked  •  Instagram Monitor")
    return e


def embed_error(message: str) -> discord.Embed:
    return discord.Embed(
        title="❌ Error",
        description=message,
        colour=discord.Colour.red(),
        timestamp=utcnow(),
    )


def embed_success(message: str) -> discord.Embed:
    return discord.Embed(
        title="✅ Success",
        description=message,
        colour=discord.Colour.green(),
        timestamp=utcnow(),
    )


def embed_analytics(stats: dict) -> discord.Embed:
    e = discord.Embed(
        title="📊 Monitor Analytics",
        colour=discord.Colour.gold(),
        timestamp=utcnow(),
    )
    e.add_field(name="👤 Users", value=str(stats.get("total_users", 0)), inline=True)
    e.add_field(name="🔍 Tracked", value=str(stats.get("total_tracked", 0)), inline=True)
    e.add_field(name="📡 Total Checks", value=str(stats.get("total_checks", 0) or 0), inline=True)
    e.add_field(name="🔴 Down Events", value=str(stats.get("down_events", 0)), inline=True)
    e.add_field(name="🟢 Restored Events", value=str(stats.get("restored_events", 0)), inline=True)
    e.add_field(name="📋 Total Events", value=str(stats.get("total_events", 0)), inline=True)
    e.set_footer(text="Instagram Monitor — Admin Analytics")
    return e
