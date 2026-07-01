"""HTTP API helpers.

Primary path: ``page.evaluate(fetch(...))`` from inside an already-loaded
missevan page. The browser supplies its real TLS fingerprint, full
``sec-ch-ua-*`` header set, and the complete tracking-cookie chain — so the
API call is indistinguishable from one made while the user watches the page.

Fallback path: a plain ``requests`` session whose cookies are re-synced from
the browser jar before every call. We only fall through to this when the
browser path errors out.

On 403/429 we mark ``rate_limited_until`` so the orchestrator can back off
for several minutes instead of hammering.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

import requests
from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout

from .auth import CookieJar
from .config import Config
from .log import log


_FETCH_JS = """
async (url) => {
    try {
        const r = await fetch(url, {
            credentials: 'include',
            headers: { 'Accept': 'application/json, text/plain, */*' },
        });
        const txt = await r.text();
        return { status: r.status, body: txt };
    } catch (e) {
        return { status: 0, body: String((e && e.message) || e) };
    }
}
"""


class MaoerApi:
    """All HTTP API access. Main-thread only."""

    def __init__(self, cfg: Config, jar: CookieJar, ctx: BrowserContext) -> None:
        self.cfg = cfg
        self.jar = jar
        self.ctx = ctx
        self._http = requests.Session()
        self._page: Page | None = None
        self.rate_limited_until: float = 0.0

    # ---------- page ----------

    def _ensure_page(self) -> Page | None:
        if self._page and not self._page.is_closed():
            return self._page
        try:
            page = self.ctx.new_page()
            page.set_default_timeout(15000)
            page.goto(
                "https://fm.missevan.com/",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            self._page = page
            return page
        except Exception as exc:
            log.debug("api page open failed, will use requests fallback: %s", exc)
            self._page = None
            return None

    def close(self) -> None:
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

    # ---------- public ----------

    def live_info(self) -> tuple[bool, dict[str, Any] | None]:
        url = f"https://fm.missevan.com/api/v2/live/{self.cfg.room_id}"
        payload = self._get_json(url)
        if payload is None:
            return False, None
        info = payload.get("info") or {}
        room = info.get("room") or {}
        status = room.get("status") or {}
        return bool(status.get("broadcasting")), (info if info else None)

    def hls_url(self, info: dict[str, Any]) -> str | None:
        channel = (info.get("room") or {}).get("channel") or {}
        return channel.get("hls_pull_url") or channel.get("pull_url")

    def flv_url(self, info: dict[str, Any]) -> str | None:
        channel = (info.get("room") or {}).get("channel") or {}
        return channel.get("flv_pull_url")

    def creator_name(self, info: dict[str, Any]) -> str:
        return (info.get("room") or {}).get("creator_username") or "unknown"

    # ---------- internals ----------

    def _get_json(self, url: str) -> dict[str, Any] | None:
        page = self._ensure_page()
        if page:
            try:
                res = page.evaluate(_FETCH_JS, url)
                status = int(res.get("status") or 0)
                body = res.get("body") or ""
                if status == 200 and body:
                    try:
                        return json.loads(body)
                    except ValueError as exc:
                        log.debug("browser fetch bad JSON: %s", exc)
                elif status in (403, 429):
                    self._mark_rate_limited(status)
                    return None
                else:
                    log.debug("browser fetch status=%s", status)
            except PWTimeout as exc:
                log.warning("browser fetch timeout: %s", exc)
                self.close()
            except Exception as exc:
                log.warning("browser fetch errored: %s", exc)
                self.close()

        return self._fallback_get_json(url)

    def _fallback_get_json(self, url: str) -> dict[str, Any] | None:
        self._http.cookies.clear()
        for k, v in self.jar.snapshot().items():
            self._http.cookies.set(k, v, domain=".missevan.com")
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Origin": "https://fm.missevan.com",
            "Referer": f"https://fm.missevan.com/live/{self.cfg.room_id}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not.A/Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        try:
            r = self._http.get(url, headers=headers, timeout=10)
            if r.status_code in (403, 429):
                self._mark_rate_limited(r.status_code)
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            log.debug("fallback request failed: %s", exc)
            return None
        except ValueError as exc:
            log.debug("fallback bad JSON: %s", exc)
            return None

    def _mark_rate_limited(self, status: int) -> None:
        cooldown = 300 + random.uniform(0, 300)  # 5–10 min, jittered
        self.rate_limited_until = time.time() + cooldown
        log.warning("api status=%s; backing off %.0fs", status, cooldown)
