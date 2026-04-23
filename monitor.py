"""
monitor.py — Async scheduler + Instagram availability checker.
Supports residential proxies and RapidAPI to bypass Instagram rate limits.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional

import httpx

import config
import db
from utils import TokenBucket, add_seconds, jitter, utcnow, utcnow_str

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[str, str, list[int]], Awaitable[None]]

# ---------------------------------------------------------------------------
# Rotating User Agents
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
]

_BASE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}


def _random_headers() -> dict:
    return {**_BASE_HEADERS, "User-Agent": random.choice(_USER_AGENTS)}


# ---------------------------------------------------------------------------
# Proxy list support
# ---------------------------------------------------------------------------
def _load_proxies() -> list[str]:
    proxies = []

    single = os.getenv("PROXY_URL", "").strip()
    if single:
        proxies.append(single)

    multi = os.getenv("PROXY_URLS", "").strip()
    if multi:
        proxies.extend([p.strip() for p in multi.split(",") if p.strip()])

    proxy_file = os.getenv("PROXY_FILE", "").strip()
    if proxy_file and os.path.exists(proxy_file):
        with open(proxy_file) as f:
            proxies.extend([line.strip() for line in f if line.strip() and not line.startswith("#")])

    seen = set()
    unique = []
    for p in proxies:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    if unique:
        logger.info("Loaded %d proxy/proxies.", len(unique))
    else:
        logger.warning("No proxies configured. Requests go via Railway IP (may get rate-limited).")

    return unique


class ProxyRotator:
    def __init__(self, proxies: list[str]):
        self._proxies = proxies
        self._index = 0
        self._backoff: dict[str, float] = {}

    def get(self) -> Optional[str]:
        if not self._proxies:
            return None
        now = asyncio.get_event_loop().time()
        for _ in range(len(self._proxies)):
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            if now >= self._backoff.get(proxy, 0):
                return proxy
        return min(self._proxies, key=lambda p: self._backoff.get(p, 0))

    def penalize(self, proxy: str, seconds: float = 60.0) -> None:
        until = asyncio.get_event_loop().time() + seconds
        self._backoff[proxy] = max(self._backoff.get(proxy, 0), until)
        logger.warning("Proxy backed off for %.0fs.", seconds)


# ---------------------------------------------------------------------------
# RapidAPI checker — used as primary for protected/celebrity accounts
# ---------------------------------------------------------------------------
async def _check_via_rapidapi(client: httpx.AsyncClient, username: str) -> str:
    key = os.getenv("RAPIDAPI_KEY", "")
    host = os.getenv("RAPIDAPI_HOST", "instagram-scraper-api2.p.rapidapi.com")

    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": host,
    }
    url = f"https://{host}/v1/info"
    params = {"username_or_id_or_url": username}

    resp = await client.get(url, headers=headers, params=params, timeout=config.REQUEST_TIMEOUT)

    if resp.status_code == 404:
        return "unavailable"
    if resp.status_code == 200:
        data = resp.json()
        if data.get("data") or data.get("user"):
            return "available"
        return "unavailable"
    if resp.status_code in (429, 403):
        resp.raise_for_status()

    return "unavailable"


# ---------------------------------------------------------------------------
# Direct Instagram checker
# ---------------------------------------------------------------------------
async def _check_direct(client: httpx.AsyncClient, username: str, proxy: Optional[str]) -> str:
    url = f"https://www.instagram.com/{username}/"
    headers = _random_headers()
    kwargs = dict(headers=headers, follow_redirects=True, timeout=config.REQUEST_TIMEOUT)

    if proxy:
        transport = httpx.AsyncHTTPTransport(proxy=proxy)
        async with httpx.AsyncClient(transport=transport, timeout=config.REQUEST_TIMEOUT) as px_client:
            resp = await px_client.get(url, **kwargs)
    else:
        resp = await client.get(url, **kwargs)

    if resp.status_code in (429, 403):
        resp.raise_for_status()

    if resp.status_code == 404:
        return "unavailable"

    if resp.status_code == 200:
        body = resp.text[:8192]
        unavailable_signals = [
            "Sorry, this page",
            "isn&#39;t available",
            "Page Not Found",
            "content unavailable",
            "page you were looking for",
        ]
        if any(s in body for s in unavailable_signals):
            return "unavailable"
        return "available"

    return "unavailable"


# ---------------------------------------------------------------------------
# Smart check: RapidAPI first if available, proxy/direct as fallback
# ---------------------------------------------------------------------------
async def _check_instagram(
    client: httpx.AsyncClient,
    username: str,
    proxy_rotator: Optional[ProxyRotator],
    use_rapidapi: bool,
) -> str:
    """
    Smart fallback chain:
    1. RapidAPI (if configured) — most reliable, bypasses all IP bans
    2. Proxy direct check (if configured)
    3. Raw direct check (Railway IP — last resort)
    """

    # --- Try RapidAPI FIRST (most reliable for all accounts) ---
    if use_rapidapi:
        try:
            result = await _check_via_rapidapi(client, username)
            logger.debug("@%s checked via RapidAPI: %s", username, result)
            return result
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response else 0
            if code == 429:
                logger.warning("RapidAPI rate-limited for @%s. Falling back to proxy/direct.", username)
            else:
                logger.debug("RapidAPI error for @%s: HTTP %d. Trying next method.", username, code)
        except Exception as exc:
            logger.debug("RapidAPI exception for @%s: %s. Trying next method.", username, exc)

    # --- Try proxy direct check ---
    proxy = proxy_rotator.get() if proxy_rotator else None
    if proxy:
        try:
            result = await _check_direct(client, username, proxy)
            logger.debug("@%s checked via proxy: %s", username, result)
            return result
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response else 0
            if code in (429, 403) and proxy_rotator:
                proxy_rotator.penalize(proxy, seconds=random.uniform(60, 120))
            logger.debug("Proxy check failed for @%s (HTTP %d). Trying direct.", username, code)
        except Exception as exc:
            logger.debug("Proxy check error for @%s: %s. Trying direct.", username, exc)

    # --- Last resort: raw direct (Railway IP) ---
    logger.debug("@%s falling back to raw direct check.", username)
    return await _check_direct(client, username, proxy=None)


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
        self._proxy_rotator: Optional[ProxyRotator] = None
        self._use_rapidapi = False

    def set_notify_callback(self, cb: NotifyCallback) -> None:
        self._notify_cb = cb

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        proxies = _load_proxies()
        self._proxy_rotator = ProxyRotator(proxies) if proxies else None
        self._use_rapidapi = bool(os.getenv("RAPIDAPI_KEY", "").strip())

        if self._use_rapidapi:
            logger.info("RapidAPI is PRIMARY method (most reliable).")
        if self._proxy_rotator:
            logger.info("Proxy rotation configured as fallback.")
        if not self._use_rapidapi and not self._proxy_rotator:
            logger.warning("No proxy or RapidAPI configured — using raw Railway IP (expect 429s on popular accounts).")

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

    async def _run_loop(self) -> None:
        while self._running:
            try:
                due = await db.get_accounts_due(limit=50)
                if due:
                    tasks = [
                        asyncio.create_task(
                            self._process_account(row),
                            name=f"chk-{row['username']}"
                        )
                        for row in due
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    await asyncio.sleep(config.QUEUE_SLEEP_INTERVAL)
            except Exception:
                logger.exception("Unexpected error in monitor loop")
                await asyncio.sleep(10)

    async def _process_account(self, row: dict) -> None:
        async with self._semaphore:
            await self._rate_limiter.acquire()
            await asyncio.sleep(random.uniform(0.5, 2.0))

            account_id: int = row["id"]
            username: str = row["username"]
            prev_status: Optional[str] = row.get("last_status")
            alert_mode: bool = bool(row.get("alert_mode"))
            alert_mode_until: Optional[str] = row.get("alert_mode_until")

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
                    new_status = await _check_instagram(
                        self._client,
                        username,
                        self._proxy_rotator,
                        self._use_rapidapi,
                    )
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code if exc.response else 0
                    if code in (429, 403):
                        wait = min(
                            config.BACKOFF_BASE ** (attempt + 2) + random.uniform(5, 15),
                            config.MAX_BACKOFF,
                        )
                        logger.warning("Rate-limited on @%s (HTTP %d). Backoff %.0fs.", username, code, wait)
                        await db.set_backoff(account_id, add_seconds(wait))
                        backoff_applied = True
                        break
                    logger.warning("HTTP error @%s: %s (attempt %d)", username, exc, attempt + 1)
                    await asyncio.sleep(config.BACKOFF_BASE ** attempt)
                except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                    logger.warning("Network error @%s: %s (attempt %d)", username, exc, attempt + 1)
                    await asyncio.sleep(config.BACKOFF_BASE ** attempt)
                except Exception:
                    logger.exception("Unexpected error checking @%s (attempt %d)", username, attempt + 1)
                    await asyncio.sleep(config.BACKOFF_BASE ** attempt)

            if backoff_applied:
                return

            if new_status is None:
                logger.error("All retries failed for @%s; skipping.", username)
                user_ids = await db.get_trackers(username)
                for uid in user_ids:
                    await db.log_event(username, uid, "error", "All retries failed")
                next_check = add_seconds(jitter(config.NORMAL_CHECK_INTERVAL_MIN, config.NORMAL_CHECK_INTERVAL_MAX))
                await db.update_account_after_check(
                    account_id, username, prev_status or "unknown",
                    next_check, alert_mode, alert_mode_until,
                )
                return

            status_changed = (prev_status is not None) and (new_status != prev_status)

            if status_changed:
                logger.info("@%s status: %s -> %s", username, prev_status, new_status)
                alert_mode = True
                alert_mode_until = add_seconds(config.ALERT_MODE_DURATION)
                user_ids = await db.get_trackers(username)
                event_type = "restored" if new_status == "available" else "down"

                for uid in user_ids:
                    await db.log_event(username, uid, event_type, f"{prev_status} -> {new_status}")

                if self._notify_cb and user_ids:
                    try:
                        await self._notify_cb(username, event_type, user_ids)
                    except Exception:
                        logger.exception("Notify callback failed for @%s", username)
            else:
                logger.debug("@%s status unchanged: %s", username, new_status)

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
