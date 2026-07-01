"""Main orchestration loop.

All Playwright access happens on this thread. We tick the danmaku
supervisor on every loop iteration and refresh the StreamProvider so
ffmpeg always has a fresh signed HLS URL on segment restart.

Polling cadence is jittered to avoid a robotic, easily-rate-limited
heartbeat. The API client signals back-pressure via ``rate_limited_until``,
which we honor here.
"""
from __future__ import annotations

import random
import signal
import threading
import time
from pathlib import Path

from .config import Config
from .danmaku import DanmakuClient
from .log import log
from .pool import CookiePool
from .recorder import RecordSession, finalize, open_session


def _jitter(base: float, frac: float = 0.3) -> float:
    return max(0.5, base * (1.0 + random.uniform(-frac, frac)))


class Orchestrator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self.num_lanes = 2 if cfg.dual_record else 1
        # Sentinel for graceful stop on Windows, where terminate() hard-kills
        # the process and bypasses the signal handler (so finalize never runs).
        # `stop.sh` touches this file; the loop sees it and shuts down cleanly.
        self._stop_file = Path(cfg.base_dir) / ".stop"

    def request_stop(self, *_: object) -> None:
        log.info("shutdown requested")
        self._stop.set()

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        try:
            signal.signal(signal.SIGTERM, self.request_stop)
        except (AttributeError, ValueError):
            pass

        # The cookie pool holds independent guest identities. In dual mode we
        # reserve identity 0 for the control path (live polling + danmaku WS)
        # and give each recording lane its own identity — so every recorded
        # stream sits on a cookie that does nothing else. Top rooms throttle a
        # cookie running control+WS+stream together, which is what made the
        # shared-identity lane thrash.
        # Pool = [control] + recording lanes + spares (dual mode). Spares let a
        # lane rotate off a burned cookie instead of hammering it.
        if self.cfg.dual_record:
            size = 1 + self.num_lanes + max(0, self.cfg.spare_cookies)
        else:
            size = 1
        pool = CookiePool(self.cfg, size=size, reserve_control=self.cfg.dual_record)
        pool.open()
        # Clear any stale stop sentinel from a previous run.
        try:
            self._stop_file.unlink()
        except OSError:
            pass
        log.info("guest mode — no login required")
        log.info("ffmpeg: %s", self.cfg.ffmpeg_path)
        log.info("lanes: %d (cookie-isolated, pool=%d)", self.num_lanes, size)
        try:
            self._loop(pool)
        except Exception:
            # Catch anything that escapes the main loop so the crash is logged
            # rather than dying silently (a Playwright hang or surprise
            # exception otherwise leaves an empty log with no clue).
            log.exception("orchestrator crashed")
            raise
        finally:
            pool.close()

    def _loop(self, pool: CookiePool) -> None:
        api = pool.primary.api      # control path: single cookie
        ctx = pool.primary.ctx      # danmaku context
        session: RecordSession | None = None
        client: DanmakuClient | None = None
        media_gone_since: float | None = None
        next_api_check = 0.0

        log.info("entering main loop")
        last_heartbeat = 0.0
        while not self._stop.is_set():
            now = time.time()

            # Heartbeat every 5 min so a silent hang is visible in the log:
            # without it, INFO is silent during idle polling and a crashed
            # process looks identical to a healthy idle one.
            if now - last_heartbeat > 300:
                last_heartbeat = now
                log.info(
                    "heartbeat: session=%s, ffmpeg_alive=%d",
                    "yes" if session else "no",
                    sum(1 for w in (session.workers if session else [])
                        if w._proc and w._proc.poll() is None),
                )

            # Graceful-stop sentinel (Windows-safe path to trigger finalize).
            if self._stop_file.exists():
                log.info("stop sentinel found; shutting down")
                try:
                    self._stop_file.unlink()
                except OSError:
                    pass
                self._stop.set()
                break

            if client:
                client.tick()

            # A lane asked for fresh URLs (its cached signed URL likely expired
            # — the classic restart-storm cause). Refresh the whole pool so each
            # identity gets a fresh cookie+URL, bypassing the poll cadence but
            # respecting the rate-limit cooldown.
            if (
                session is not None
                and now >= api.rate_limited_until
                and any(w.url_refresh_requested.is_set() for w in session.workers)
            ):
                pool.refresh_recording()
                for w in session.workers:
                    w.url_refresh_requested.clear()
                log.info("pool refreshed on watchdog request")

            if now < max(api.rate_limited_until, next_api_check):
                self._stop.wait(self.cfg.supervisor_tick_seconds)
                continue

            live, info = api.live_info()
            log.debug("poll: live=%s, info=%s", live, bool(info))

            if session is None:
                next_api_check = time.time() + _jitter(self.cfg.idle_poll_seconds)
                if not live or not info:
                    self._stop.wait(self.cfg.supervisor_tick_seconds)
                    continue

                # Populate recording identities' creds before starting lanes.
                pool.refresh_recording()
                if not pool.any_recording_creds():
                    log.warning("no hls url yet; will retry")
                    continue

                session = open_session(self.cfg, pool, info, self.num_lanes)
                log.info("🔴 live started → %s", session.session_dir)
                session.start()

                client = DanmakuClient(self.cfg, ctx, session.append_chat)
                client.start(f"https://fm.missevan.com/live/{self.cfg.room_id}")
                media_gone_since = None
                next_api_check = time.time() + _jitter(self.cfg.active_poll_seconds)
                continue

            # Session running.
            now = time.time()
            audio_dead = (
                session.last_audio_write
                and (now - session.last_audio_write) > self.cfg.media_drain_seconds
            )

            if not live:
                if media_gone_since is None:
                    media_gone_since = now
                draining = (now - media_gone_since) > self.cfg.media_drain_seconds
            else:
                media_gone_since = None
                draining = False
                # Mid-stream: refresh recording identities so each lane has a
                # fresh signed URL ready for its next ffmpeg restart.
                pool.refresh_recording()

            should_end = (not live) and (audio_dead or draining)
            if should_end:
                log.info("live ended, finalizing")
                if client:
                    client.stop()
                    client = None
                session.stop("live ended")
                finalize(session)
                session = None
                next_api_check = time.time() + _jitter(self.cfg.idle_poll_seconds)
                continue

            next_api_check = time.time() + _jitter(self.cfg.active_poll_seconds)
            self._stop.wait(self.cfg.supervisor_tick_seconds)

        # Shutting down.
        if client:
            client.stop()
        if session:
            session.stop("shutdown")
            finalize(session)
