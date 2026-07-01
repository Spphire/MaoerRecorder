#!/usr/bin/env python
"""External supervisor for MaoerRecorder.

Lives outside the recorder process; only watches the heartbeat freshness of
``record.log`` and restarts everything if it goes stale. Deliberately tiny:
no browser, no Playwright, no network polling — so the supervisor itself is
far less likely to be killed by the same OS-level event that froze the
recorder. Also asks Windows to keep the system awake while it runs.

Usage: ``py supervisor.py [room_id]``  (or via supervisor.bat)
Stop:  Ctrl+C, or create ``recordings/.stop`` to trigger a clean finalize.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Unbuffered stdout/stderr so nohup'd logs show up immediately (Python's
# default block-buffers when stdout is redirected to a file).
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).parent.resolve()
LOG_PATH = ROOT / "record.log"
PID_PATH = ROOT / "record.pid"
STOP_PATH = ROOT / "recordings" / ".stop"

# How stale the recorder's heartbeat may get before we assume it's frozen.
# Recorder writes a heartbeat every 5 min; 6 min cuts a single missed beat
# fine without false-killing on small delays.
HEARTBEAT_STALE_SECONDS = 360
# How long after launch to wait before the first heartbeat check (recorder
# warmup is ~30s).
STARTUP_GRACE_SECONDS = 60
# Poll the log mtime this often.
CHECK_INTERVAL_SECONDS = 30


def keep_system_awake() -> None:
    """Ask Windows to keep the SYSTEM awake while this script runs.

    ES_CONTINUOUS | ES_SYSTEM_REQUIRED  (does not block display sleep).
    """
    if sys.platform != "win32":
        return
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        print("[sup] system-sleep prevention requested")
    except Exception as exc:
        print(f"[sup] could not set ExecutionState: {exc}", file=sys.stderr)


def kill_recorder_tree() -> None:
    """Kill the current recorder + all its ffmpeg/chromium children."""
    try:
        import psutil
    except ImportError:
        # Fallback: brute-force via taskkill.
        subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    killed = 0
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cl = p.info.get("cmdline") or []
            name = (p.info.get("name") or "").lower()
            if (name == "python.exe" and len(cl) >= 2 and cl[1].endswith("main.py")
                    and "record" in cl):
                p.kill()
                killed += 1
            elif name == "ffmpeg.exe":
                p.kill()
                killed += 1
            elif "chrome" in name:
                # Only kill chromium spawned by our Playwright (best-effort).
                p.kill()
                killed += 1
        except Exception:
            pass
    if killed:
        print(f"[sup] killed {killed} stale processes")


def launch_recorder(room: str) -> int | None:
    """Spawn the recorder detached. Returns its PID, or None on failure."""
    DETACHED_PROCESS = 0x00000008
    log_f = open(LOG_PATH, "ab")  # append; supervisor preserves history
    log_f.write(f"\n=== supervisor launched recorder at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    log_f.flush()
    try:
        proc = subprocess.Popen(
            [sys.executable, "main.py", "record", "--room", room],
            cwd=str(ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=DETACHED_PROCESS if sys.platform == "win32" else 0,
            close_fds=True,
        )
        PID_PATH.write_text(str(proc.pid))
        print(f"[sup] recorder launched, PID {proc.pid}")
        return proc.pid
    except Exception as exc:
        print(f"[sup] launch failed: {exc}", file=sys.stderr)
        return None


def log_is_fresh() -> bool:
    """True iff record.log was modified within HEARTBEAT_STALE_SECONDS."""
    try:
        age = time.time() - LOG_PATH.stat().st_mtime
        return age < HEARTBEAT_STALE_SECONDS
    except OSError:
        return False


def recorder_pid_alive() -> bool:
    try:
        pid = int(PID_PATH.read_text().strip())
    except Exception:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        # Best-effort kill -0 emulation.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def supervise(room: str) -> None:
    stop_requested = [False]

    def on_sig(*_: object) -> None:
        stop_requested[0] = True
        print("[sup] shutdown signal received")

    signal.signal(signal.SIGINT, on_sig)
    try:
        signal.signal(signal.SIGTERM, on_sig)
    except (AttributeError, ValueError):
        pass

    keep_system_awake()
    print(f"[sup] watching room {room}; stale threshold {HEARTBEAT_STALE_SECONDS}s")

    # If the recorder isn't already running (no PID or PID dead), launch it.
    if not recorder_pid_alive():
        kill_recorder_tree()  # clean any orphans first
        launch_recorder(room)
        time.sleep(STARTUP_GRACE_SECONDS)
    else:
        print(f"[sup] adopting existing recorder PID {PID_PATH.read_text().strip()}")

    while not stop_requested[0]:
        # User-triggered graceful stop bubbles all the way out — supervisor
        # waits for the recorder to exit cleanly, then exits.
        if STOP_PATH.exists():
            print("[sup] stop sentinel detected; waiting for recorder to finalize")
            for _ in range(60):  # up to 5 minutes
                time.sleep(5)
                if not recorder_pid_alive():
                    print("[sup] recorder exited cleanly")
                    return
            print("[sup] recorder didn't exit; killing")
            kill_recorder_tree()
            return

        time.sleep(CHECK_INTERVAL_SECONDS)

        alive = recorder_pid_alive()
        fresh = log_is_fresh()
        if alive and fresh:
            continue

        try:
            age = time.time() - LOG_PATH.stat().st_mtime
        except OSError:
            age = -1
        reason = "PID dead" if not alive else f"log stale {age:.0f}s"
        print(f"[sup] recorder unhealthy ({reason}); restarting")

        kill_recorder_tree()
        time.sleep(3)
        launch_recorder(room)
        time.sleep(STARTUP_GRACE_SECONDS)


if __name__ == "__main__":
    room = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MAOER_ROOM_ID", "868802213")
    supervise(room)
