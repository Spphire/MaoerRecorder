"""Guest-mode Playwright context with stealth + warmup.

The browser is shared across the run. ``warmup`` visits missevan home pages
to collect the tracking cookies (HMACCOUNT, Hm_*, buvid3) so subsequent HTTP
and HLS requests look like a real visitor's.

All Playwright operations must happen on a single thread (the main
orchestrator thread). The CookieJar's ``snapshot`` is therefore called
exclusively from that thread.
"""
from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, sync_playwright

from .config import Config
from .log import log


# Runs in every page before any site script. Hides automation telltales.
STEALTH_INIT = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en']
    });
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin' },
            { name: 'Chrome PDF Viewer' },
            { name: 'Native Client' },
        ]
    });
    window.chrome = window.chrome || { runtime: {} };
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
        navigator.permissions.query = (p) =>
            p && p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(p);
    }
}
"""


def new_stealth_context(browser, cfg: Config) -> BrowserContext:
    """Create one isolated stealth guest context on an existing browser.

    Each context has its own cookie jar, so N contexts on one browser are N
    independent guest identities (different buvid3/MSESSID after warmup).
    """
    ctx = browser.new_context(
        user_agent=cfg.user_agent,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    ctx.add_init_script(STEALTH_INIT)
    return ctx


@contextmanager
def open_browser(cfg: Config, headless: bool = True):
    """Open one Playwright browser. Use new_stealth_context() for each identity.

    sync_playwright() may only be entered once per thread, so a multi-identity
    pool must share a single browser and create multiple contexts from it.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        try:
            yield browser
        finally:
            try:
                browser.close()
            except Exception:
                pass


@contextmanager
def open_context(cfg: Config, headless: bool = True) -> Iterator[BrowserContext]:
    """Open a single stealthy guest context. Single-thread use only."""
    with open_browser(cfg, headless) as browser:
        ctx = new_stealth_context(browser, cfg)
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def warmup(ctx: BrowserContext) -> None:
    """Visit missevan home pages to collect tracking cookies.

    Done once at startup. Non-fatal on failure — recording still proceeds.
    """
    urls = [
        "https://www.missevan.com/",
        "https://fm.missevan.com/",
    ]
    page = ctx.new_page()
    try:
        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500 + random.randint(0, 800))
            except Exception as exc:
                log.debug("warmup %s failed: %s", url, exc)
    finally:
        try:
            page.close()
        except Exception:
            pass
    log.info("warmup done; cookies=%s", list(cookies_dict(ctx).keys()))


def cookies_dict(ctx: BrowserContext) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in ctx.cookies():
        if "missevan.com" not in c.get("domain", ""):
            continue
        out[c["name"]] = c["value"]
    return out


class CookieJar:
    """Lazily-refreshed snapshot of browser cookies.

    ``snapshot`` must be called from the same thread that owns ``ctx``
    (the main thread). Background threads should pre-fetch and cache.
    """

    def __init__(self, ctx: BrowserContext, min_refresh: float = 5.0) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._min_refresh = min_refresh

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            if time.monotonic() - self._fetched_at > self._min_refresh:
                self._cache = cookies_dict(self._ctx)
                self._fetched_at = time.monotonic()
            return dict(self._cache)

    def header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.snapshot().items())
