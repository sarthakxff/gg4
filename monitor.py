"""
monitor.py — Async scheduler + Instagram availability checker.

Architecture:
  - A single background task (MonitorScheduler.run) runs indefinitely.
  - It fetches accounts that are due for a check from the DB (queue-based).
  - Each check is dispatched as a separate asyncio task, bounded by a semaphore.
  - Rate limiting is enforced via a TokenBucket shared across all check tasks.
  - When a status change is detected, callbacks registered by the bot are invoked.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional

import httpx

import config
import db
from utils import TokenBucket, add_seconds, jitter, utcnow, utcnow_str

logger = logging.getLogger(__name__)

# Type alias for notification callback
NotifyCallback = Callable[[str, str, list[int]], Awaitable[None]]
# (username, event_type, user_ids) → None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


async def _check_instagram(client: httpx.AsyncClient, username: str) -> str:
    """
    Returns 'available' or 'unavailable'.
    Raises httpx.HTTPStatusError for 429/403 (caller handles backoff).
    """
    url = f"https://www.instagram.com/{username}/"
    resp = await client.get(url, headers=_HEADERS, follow_redirects=True, timeout=config.REQUEST_TIMEOUT)

    if resp.status_code in (429, 403):
        resp.raise_for_status()   # signals rate-limit to caller

    # 404 → account doesn't exist / is banned
    if resp.status_code == 404:
        return "unavailable"

    # 200 with "Sorry, this page isn't available." in body → also unavailable
    if resp.status_code == 200:
        body = resp.text[:4096]
        if "Sorry, this page" in body or "isn&#39;t available" in body or "Page Not Found" in body:
            return "unavailable"
        return "available"

    # Any other 4xx/5xx → treat as unavailable but don't trigger rate-limit path
    return "unavailable"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
class MonitorScheduler:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(config.SEMAPHORE_LIMIT)
        self._rate_limiter = TokenBucket(config.MAX_REQUESTS_PER_MINUTE, period=60.0)
        self._notify_cb: Optional[NotifyCallback] = None
        self._running = False
        self._client: Optional[httpx.AsyncClient] = None

    def set_notify_callback(self, cb: NotifyCallback) -> None:
        self._notify_cb = cb

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=config.REQUEST_TIMEOUT,
        )
        asyncio.create_task(self._run_loop(), name="monitor-loop")
        logger.info("Monitor scheduler started.")

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
        logger.info("Monitor scheduler stopped.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def _run_loop(self) -> None:
        while self._running:
            try:
                due = await db.get_accounts_due(limit=50)
                if due:
                    tasks = [
                        asyncio.create_task(self._process_account(row), name=f"chk-{row['username']}")
                        for row in due
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    # Nothing due — sleep briefly before next poll
                    await asyncio.sleep(config.QUEUE_SLEEP_INTERVAL)
            except Exception:
                logger.exception("Unexpected error in monitor loop")
                await asyncio.sleep(10)

    # ------------------------------------------------------------------
    # Per-account check
    # ------------------------------------------------------------------
    async def _process_account(self, row: dict) -> None:
        async with self._semaphore:
            await self._rate_limiter.acquire()

            account_id: int = row["id"]
            username: str = row["username"]
            prev_status: Optional[str] = row.get("last_status")
            alert_mode: bool = bool(row.get("alert_mode"))
            alert_mode_until: Optional[str] = row.get("alert_mode_until")

            # Check if alert_mode has expired
            if alert_mode and alert_mode_until:
                try:
                    until_dt = datetime.strptime(alert_mode_until, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                    if utcnow() >= until_dt:
                        alert_mode = False
                        alert_mode_until = None
                except ValueError:
                    alert_mode = False

            new_status: Optional[str] = None
            backoff_applied = False

            for attempt in range(config.MAX_RETRIES):
                try:
                    assert self._client is not None
                    new_status = await _check_instagram(self._client, username)
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code if exc.response else 0
                    if code in (429, 403):
                        # Exponential backoff
                        wait = min(
                            config.BACKOFF_BASE ** (attempt + 2) + random.uniform(0, 5),
                            config.MAX_BACKOFF,
                        )
                        logger.warning("Rate-limited on @%s (HTTP %d). Backoff %.0fs.", username, code, wait)
                        backoff_until = add_seconds(wait)
                        await db.set_backoff(account_id, backoff_until)
                        backoff_applied = True
                        break
                    logger.warning("HTTP error checking @%s: %s (attempt %d)", username, exc, attempt + 1)
                    await asyncio.sleep(config.BACKOFF_BASE ** attempt)
                except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                    logger.warning("Network error checking @%s: %s (attempt %d)", username, exc, attempt + 1)
                    await asyncio.sleep(config.BACKOFF_BASE ** attempt)
                except Exception:
                    logger.exception("Unexpected error checking @%s (attempt %d)", username, attempt + 1)
                    await asyncio.sleep(config.BACKOFF_BASE ** attempt)

            if backoff_applied:
                return

            if new_status is None:
                # All retries exhausted — log but don't update status
                logger.error("All retries failed for @%s; skipping update.", username)
                user_ids = await db.get_trackers(username)
                for uid in user_ids:
                    await db.log_event(username, uid, "error", "All retries failed")
                # Schedule next check normally
                next_check = add_seconds(jitter(config.NORMAL_CHECK_INTERVAL_MIN, config.NORMAL_CHECK_INTERVAL_MAX))
                await db.update_account_after_check(
                    account_id, username, prev_status or "unknown",
                    next_check, alert_mode, alert_mode_until,
                )
                return

            # ---------------------------------------------------------
            # Determine if status changed
            # ---------------------------------------------------------
            status_changed = (prev_status is not None) and (new_status != prev_status)

            if status_changed:
                logger.info("@%s status: %s → %s", username, prev_status, new_status)
                alert_mode = True
                alert_mode_until = add_seconds(config.ALERT_MODE_DURATION)

                user_ids = await db.get_trackers(username)
                event_type = "restored" if new_status == "available" else "down"

                for uid in user_ids:
                    await db.log_event(username, uid, event_type, f"{prev_status} → {new_status}")

                if self._notify_cb and user_ids:
                    try:
                        await self._notify_cb(username, event_type, user_ids)
                    except Exception:
                        logger.exception("Notify callback failed for @%s", username)
            else:
                logger.debug("@%s status unchanged: %s", username, new_status)

            # ---------------------------------------------------------
            # Schedule next check
            # ---------------------------------------------------------
            if alert_mode:
                interval = jitter(config.ALERT_CHECK_INTERVAL_MIN, config.ALERT_CHECK_INTERVAL_MAX)
            else:
                interval = jitter(config.NORMAL_CHECK_INTERVAL_MIN, config.NORMAL_CHECK_INTERVAL_MAX)

            next_check = add_seconds(interval)
            await db.update_account_after_check(
                account_id, username, new_status,
                next_check, alert_mode, alert_mode_until,
            )


# Singleton
scheduler = MonitorScheduler()
