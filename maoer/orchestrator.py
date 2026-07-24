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
import os
import json
import signal
import threading
import time
from pathlib import Path

from .config import Config
from .danmaku import DanmakuClient
from .log import log
from .pool import CookiePool
from .recorder import RecordSession, finalize, open_session


def _prevent_system_sleep(enable: bool) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        es_continuous = 0x80000000
        es_system_required = 0x00000001
        flags = es_continuous | es_system_required if enable else es_continuous
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


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
        managed_stop_file = os.getenv("MAOER_STOP_FILE")
        self._stop_file = (
            Path(managed_stop_file).resolve()
            if managed_stop_file
            else Path(cfg.base_dir) / ".stop"
        )
        self._managed_stop_file = bool(managed_stop_file)
        status_file = os.getenv("MAOER_STATUS_FILE")
        self._status_file = Path(status_file).resolve() if status_file else None
        self._creator_name: str | None = None

    def _write_status(
        self,
        state: str,
        session: RecordSession | None = None,
        **extra: object,
    ) -> None:
        if self._status_file is None:
            return
        payload: dict[str, object] = {
            "status_protocol": 2,
            "state": state,
            "room_id": self.cfg.room_id,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "session_dir": str(session.session_dir) if session else None,
            "lanes": len(session.workers) if session else self.num_lanes,
            "lanes_alive": (
                sum(1 for worker in session.workers if worker._proc and worker._proc.poll() is None)
                if session
                else 0
            ),
            "last_audio_write": session.last_audio_write if session else None,
            "creator": self._creator_name,
        }
        payload.update(extra)
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self._status_file.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self._status_file)
        except OSError:
            pass

    def request_stop(self, *_: object) -> None:
        log.info("shutdown requested")
        self._stop.set()

    def run(self) -> None:
        self._write_status("starting")
        _prevent_system_sleep(True)
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
        # The dashboard gives every worker a fresh, unique stop path and owns
        # its lifecycle. Only the legacy shared sentinel may be stale here.
        # Clearing a managed path would race with a stop requested during the
        # relatively expensive browser/cookie-pool startup.
        if not self._managed_stop_file:
            try:
                self._stop_file.unlink()
            except OSError:
                pass
        log.info("guest mode — no login required")
        log.info("ffmpeg: %s", self.cfg.ffmpeg_path)
        log.info("lanes: %d (cookie-isolated, pool=%d)", self.num_lanes, size)
        try:
            self._loop(pool)
        except Exception as exc:
            # Catch anything that escapes the main loop so the crash is logged
            # rather than dying silently (a Playwright hang or surprise
            # exception otherwise leaves an empty log with no clue).
            log.exception("orchestrator crashed")
            self._write_status("error", error=str(exc))
            raise
        finally:
            pool.close()
            _prevent_system_sleep(False)

    def _loop(self, pool: CookiePool) -> None:
        api = pool.primary.api      # control path: single cookie
        ctx = pool.primary.ctx      # danmaku context
        session: RecordSession | None = None
        client: DanmakuClient | None = None
        media_gone_since: float | None = None
        next_api_check = 0.0

        log.info("entering main loop")
        self._write_status("monitoring")
        last_heartbeat = 0.0
        last_status_write = 0.0
        while not self._stop.is_set():
            now = time.time()

            if now - last_status_write > 2.0:
                last_status_write = now
                self._write_status("recording" if session else "monitoring", session)

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
                self._write_status("recording" if session else "monitoring", session)

            # Graceful-stop sentinel (Windows-safe path to trigger finalize).
            if self._stop_file.exists():
                log.info("stop sentinel found; shutting down")
                try:
                    self._stop_file.unlink()
                except OSError:
                    pass
                self._stop.set()
                self._write_status("stopping", session)
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
            if info:
                creator = api.creator_name(info)
                if creator and creator != "unknown":
                    self._creator_name = creator
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
                self._write_status("recording", session)

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
                    session.mark_live_offline()
                draining = (now - media_gone_since) > self.cfg.media_drain_seconds
            else:
                media_gone_since = None
                session.mark_live_online()
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
                self._write_status("finalizing", session)
                finalize(session)
                session = None
                self._write_status("monitoring")
                next_api_check = time.time() + _jitter(self.cfg.idle_poll_seconds)
                continue

            next_api_check = time.time() + _jitter(self.cfg.active_poll_seconds)
            self._stop.wait(self.cfg.supervisor_tick_seconds)

        # Shutting down.
        if client:
            client.stop()
        if session:
            session.stop("shutdown")
            self._write_status("finalizing", session)
            finalize(session)
        self._write_status("stopped")
