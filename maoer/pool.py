"""Cookie pool: a registry of independent guest identities.

Why: when two ffmpeg lanes pull the same room with the SAME guest cookie, the
server sees one viewer opening two stream connections and throttles it (proven
on high-traffic rooms — both lanes stalled almost every second). Two DIFFERENT
guest cookies look like two independent viewers and pull cleanly.

Each identity = its own warmed-up Playwright context + CookieJar + MaoerApi, so
it fetches its OWN signed HLS URL with its OWN cookie. A recording lane acquires
one identity; the orchestrator's control path (live detection + danmaku) reuses
identity 0 — that identity then hosts exactly one stream + the WS, which is what
a normal viewer does, so it doesn't trip the per-cookie stream throttle.

Threading: refresh() does Playwright and MUST run on the main thread. acquire()
and creds() only read cached snapshots under a lock, so worker threads can call
them safely at ffmpeg launch.
"""
from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from typing import Any

from .api import MaoerApi
from .auth import CookieJar, new_stealth_context, open_browser, warmup
from .config import Config
from .log import log
from .recorder import StreamCreds


class _Identity:
    __slots__ = ("idx", "ctx", "jar", "api", "cookie", "url_hls", "url_flv",
                 "refreshed_at", "last_used")

    def __init__(self, idx: int, ctx: Any, jar: CookieJar, api: MaoerApi) -> None:
        self.idx = idx
        self.ctx = ctx
        self.jar = jar
        self.api = api
        self.cookie: str = ""
        self.url_hls: str | None = None
        self.url_flv: str | None = None
        self.refreshed_at: float = 0.0
        self.last_used: float = 0.0  # monotonic; for LRU cooldown on rotate


class CookiePool:
    """N independent guest identities. Main-thread refresh, thread-safe acquire."""

    def __init__(self, cfg: Config, size: int, reserve_control: bool = False) -> None:
        self.cfg = cfg
        self.size = max(1, size)
        # When True, identity 0 is reserved for the orchestrator's control path
        # (live polling + danmaku WS) and recording lanes draw from 1..N. This
        # keeps each recorded stream on a cookie that does nothing else — top
        # rooms throttle a cookie that runs control+WS+stream all at once.
        self.reserve_control = reserve_control and self.size > 1
        self._stack = ExitStack()
        self._identities: list[_Identity] = []
        self._lock = threading.Lock()
        self._assigned: dict[str, int] = {}  # lane label -> identity idx

    @property
    def _rec_start(self) -> int:
        return 1 if self.reserve_control else 0

    # ---------- setup / teardown (main thread) ----------

    def open(self) -> None:
        """Open and warm up all identities. Main thread only."""
        browser = self._stack.enter_context(open_browser(self.cfg, headless=True))
        for i in range(self.size):
            ctx = new_stealth_context(browser, self.cfg)
            self._stack.callback(ctx.close)
            warmup(ctx)
            jar = CookieJar(ctx)
            api = MaoerApi(self.cfg, jar, ctx)
            self._identities.append(_Identity(i, ctx, jar, api))
        log.info("cookie pool: %d independent guest identities ready", self.size)

    def close(self) -> None:
        for ident in self._identities:
            try:
                ident.api.close()
            except Exception:
                pass
        try:
            self._stack.close()
        except Exception:
            pass

    # ---------- control path uses identity 0 ----------

    @property
    def primary(self) -> _Identity:
        return self._identities[0]

    # ---------- refresh (main thread, does Playwright) ----------

    def refresh_recording(self) -> None:
        """Refresh signed URLs (both HLS + FLV) + cookie for every recording
        identity. Main thread only (does Playwright). The control identity (0,
        when reserved) is not refreshed here — the orchestrator polls it.
        """
        for ident in self._identities[self._rec_start:]:
            try:
                _live, info = ident.api.live_info()
            except Exception as exc:
                log.debug("pool refresh #%d failed: %s", ident.idx, exc)
                continue
            if info:
                hls = ident.api.hls_url(info)
                flv = ident.api.flv_url(info)
                if hls or flv:
                    with self._lock:
                        ident.cookie = ident.jar.header()
                        ident.url_hls = hls
                        ident.url_flv = flv
                        ident.refreshed_at = time.monotonic()

    def any_recording_creds(self) -> bool:
        with self._lock:
            return any((i.url_hls or i.url_flv)
                       for i in self._identities[self._rec_start:])

    # ---------- acquire (worker thread, lock-only) ----------

    def acquire(self, label: str, kind: str = "hls") -> StreamCreds | None:
        """Return creds for the identity assigned to ``label`` for ``kind``
        ("hls" or "flv"). Stable mapping: each lane gets its own identity.
        Returns None if not refreshed yet or the requested URL is unavailable.
        """
        with self._lock:
            idx = self._assigned.get(label)
            if idx is None:
                used = set(self._assigned.values())
                idx = next(
                    (i for i in range(self._rec_start, self.size) if i not in used),
                    self._rec_start,
                )
                self._assigned[label] = idx
                self._identities[idx].last_used = time.monotonic()
                log.info("lane %s -> cookie identity #%d (%s)", label, idx, kind)
            ident = self._identities[idx]
            url = ident.url_flv if kind == "flv" else ident.url_hls
            # Fall back to the other protocol if the preferred one is missing.
            if not url:
                url = ident.url_hls or ident.url_flv
                kind = "hls" if url == ident.url_hls else "flv"
            if not url:
                return None
            return StreamCreds(cookie_header=ident.cookie, url=url, kind=kind)

    def rotate(self, label: str) -> bool:
        """Swap a lane onto a fresh spare identity (its cookie looks burned).

        Picks the least-recently-used free identity so a just-burned cookie
        gets maximum cooldown before reuse. Returns True if a different
        identity was assigned; False when no other identity is free. The old
        identity is released back to the spare pool. Worker-thread safe."""
        with self._lock:
            old = self._assigned.get(label)
            used = {v for k, v in self._assigned.items() if k != label}
            free = [i for i in range(self._rec_start, self.size)
                    if i not in used and i != old]
            if not free:
                return False
            # Least-recently-used first → longest cooldown for burned cookies.
            spare = min(free, key=lambda i: self._identities[i].last_used)
            self._assigned[label] = spare
            self._identities[spare].last_used = time.monotonic()
            log.info("lane %s rotated cookie #%s -> #%d (burned)", label, old, spare)
            return True

