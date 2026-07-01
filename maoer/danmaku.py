"""WebSocket danmaku capture.

Single-thread Playwright discipline: all page operations happen on the
orchestrator's tick. The supervisor is just a state machine; there is no
background thread that calls Playwright.

WebSocket ``framereceived`` callbacks run on Playwright's own event loop
thread (driven by ``tick``), so we never touch Playwright from those — we
only enqueue raw bytes for the writer thread to parse and dispatch.
"""
from __future__ import annotations

import json
import queue
import random
import threading
import time
from typing import Any, Callable

import brotli
from playwright.sync_api import BrowserContext, Page, WebSocket

from .config import Config
from .log import log


FrameSink = Callable[[dict[str, Any]], None]


def _parse_frame(payload: bytes | str) -> dict[str, Any] | None:
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:
            return None
    candidates = (payload[4:], payload)
    for body in candidates:
        try:
            decoded = brotli.decompress(body)
            return json.loads(decoded.decode("utf-8"))
        except Exception:
            pass
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            pass
    return None


class DanmakuClient:
    """Main-thread-driven WS supervisor.

    Usage:
        dm = DanmakuClient(cfg, ctx, sink)
        dm.start(page_url)         # opens initial page
        ...
        dm.tick()                  # call from main loop, frequently
        ...
        dm.stop()
    """

    def __init__(
        self,
        cfg: Config,
        ctx: BrowserContext,
        sink: FrameSink,
    ) -> None:
        self.cfg = cfg
        self.ctx = ctx
        self.sink = sink

        self._page_url: str = ""
        self._page: Page | None = None
        self._ws_url_substr = "im.missevan.com"

        self._queue: queue.Queue[bytes | str] = queue.Queue(maxsize=10000)
        self._stopped = threading.Event()
        self._writer = threading.Thread(
            target=self._writer_loop, daemon=True, name="dm-writer"
        )

        # Health, all updated on Playwright/main thread or via atomic write.
        self._connected = False
        self._last_msg_at: float = 0.0
        self._last_msg_lock = threading.Lock()
        self._reload_due_at: float = 0.0  # 0 = no pending reload
        self._next_backoff: float = 1.0
        self._open_at: float = 0.0  # when current page was opened

    # ---------- lifecycle ----------

    def start(self, page_url: str) -> None:
        self._page_url = page_url
        self._writer.start()
        self._open_page()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._queue.put_nowait(b"")
        except queue.Full:
            pass
        self._close_page()

    # ---------- main-thread tick ----------

    def tick(self) -> None:
        """Drive the supervisor state machine. Called from main loop."""
        if self._stopped.is_set():
            return

        # Pending reload?
        if self._reload_due_at and time.time() >= self._reload_due_at:
            self._reload_due_at = 0.0
            self._open_page()
            return

        page = self._page
        if page is None or page.is_closed():
            self._schedule_reload("page-missing")
            return

        # Connection grace.
        if not self._connected:
            age = time.time() - self._open_at
            if age > 20.0 and self._last_msg_at == 0.0:
                self._schedule_reload("ws-not-up")
            return

        # Steady-state silence check.
        with self._last_msg_lock:
            last = self._last_msg_at
        if last > 0 and (time.time() - last) > self.cfg.max_ws_silent_seconds:
            self._schedule_reload("ws-silent")

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_msg_at(self) -> float:
        with self._last_msg_lock:
            return self._last_msg_at

    # ---------- page ops (main thread only) ----------

    def _open_page(self) -> None:
        self._close_page()
        try:
            page = self.ctx.new_page()
            page.on("websocket", self._bind_ws)
            page.on("crash", lambda *_: self._note_disconnect("crash"))
            page.on("close", lambda *_: self._note_disconnect("page-close"))
            log.info("dm opening %s", self._page_url)
            page.goto(self._page_url, wait_until="domcontentloaded", timeout=30000)
            self._page = page
            self._open_at = time.time()
            # Success → halve backoff for next attempt.
            self._next_backoff = max(1.0, self._next_backoff / 2)
        except Exception as exc:
            log.warning("dm page open failed: %s", exc)
            self._schedule_reload("open-failed")

    def _close_page(self) -> None:
        page = self._page
        self._page = None
        self._connected = False
        if page:
            try:
                page.close()
            except Exception:
                pass

    def _schedule_reload(self, reason: str) -> None:
        if self._reload_due_at:
            return  # already scheduled
        delay = self._next_backoff * (1.0 + random.uniform(-0.2, 0.4))
        self._reload_due_at = time.time() + delay
        self._next_backoff = min(30.0, self._next_backoff * 2)
        log.info("dm schedule reload (%s) in %.1fs", reason, delay)
        # Close now so the next tick won't see a half-dead page.
        self._close_page()

    # ---------- WS callbacks (Playwright thread) ----------

    def _bind_ws(self, ws: WebSocket) -> None:
        if self._ws_url_substr not in ws.url:
            return
        log.info("ws connected: %s", ws.url)
        self._connected = True

        def on_frame(frame: bytes | str) -> None:
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(frame)
                except Exception:
                    pass

        ws.on("framereceived", on_frame)
        ws.on("close", lambda *_: self._note_disconnect("ws-close"))
        ws.on("socketerror", lambda *_: self._note_disconnect("ws-error"))

    def _note_disconnect(self, why: str) -> None:
        if self._connected:
            log.warning("ws/page down: %s", why)
        self._connected = False

    # ---------- writer thread ----------

    def _writer_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if not item:
                continue
            with self._last_msg_lock:
                self._last_msg_at = time.time()
            data = _parse_frame(item)
            if not data:
                continue
            try:
                self.sink(data)
            except Exception as exc:
                log.warning("dm sink raised: %s", exc)
