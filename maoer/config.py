"""Central configuration for MaoerRecorder."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _load_dotenv() -> None:
    """Tiny .env loader so we don't pull in python-dotenv.

    Lines like ``KEY=value`` are set into os.environ if not already present.
    Comments (``#``) and blank lines are ignored. Values may be quoted.
    """
    env_path = Path(".env")
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


# Winget's user PATH update doesn't propagate to already-running shells.
# Probe a small list of well-known install dirs so we can resolve ffmpeg
# even when the parent shell didn't see the PATH change yet.
_FFMPEG_FALLBACKS = [
    Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    / "ffmpeg-8.1.1-full_build/bin/ffmpeg.exe",
    Path("C:/ProgramData/chocolatey/bin/ffmpeg.exe"),
    Path("C:/ffmpeg/bin/ffmpeg.exe"),
]


def _resolve_ffmpeg() -> str:
    """Return a usable ffmpeg path. Order: env, PATH, known install dirs."""
    explicit = os.getenv("FFMPEG_PATH")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in _FFMPEG_FALLBACKS:
        if cand.exists():
            return str(cand)
        # Some installs use a slightly different version dir; glob the parent.
        parent = cand.parent.parent
        if parent.exists():
            for hit in parent.glob("ffmpeg-*/bin/ffmpeg.exe"):
                return str(hit)
    # Last resort: hope it's on PATH at exec time.
    return explicit or "ffmpeg"


@dataclass(frozen=True)
class Config:
    room_id: int
    base_dir: Path
    ffmpeg_path: str

    # ffmpeg watchdog: if no bytes written for this long, restart segment.
    # With -flush_packets the file grows every second when healthy, so a real
    # stall is a genuine freeze; 12s tolerates brief CDN jitter (~5s seen) while
    # still recovering fast.
    max_no_data_seconds: int = 12
    # WebSocket watchdog: if no frame for this long, reload the page.
    max_ws_silent_seconds: int = 120
    # How long to wait for live-end confirmation before finalizing.
    media_drain_seconds: int = 120
    # API polling cadence — relaxed to avoid robotic patterns.
    # When no session is active.
    idle_poll_seconds: float = 8.0
    # When a session is running. We don't need to poll often: ffmpeg
    # and the WS already tell us when something breaks.
    active_poll_seconds: float = 30.0
    # Tick the WS supervisor this often (seconds).
    supervisor_tick_seconds: float = 1.0

    # Dual-ffmpeg hot standby: run two recording lanes so a single
    # crash/stall never gaps the audio. finalize stitches them gap-free.
    dual_record: bool = True
    # Stagger the second lane so the two rarely fail at the same instant.
    worker_b_delay: float = 8.0
    # Spare guest identities in the cookie pool. When a lane's cookie gets
    # burned (repeated empty crashes), it rotates onto a fresh spare instead
    # of hammering the throttled one — shortens the lane's downtime and cuts
    # the chance of both lanes being down at once.
    spare_cookies: int = 2
    # Alternate lane protocols (HLS / FLV). In theory different source
    # pipelines decorrelate CDN hiccups, but in practice the FLV endpoint's
    # signed URLs expire fast and 404-storm under stress — making the FLV lane
    # far more fragile than HLS. Default OFF; opt in per-room if it helps.
    heterogeneous_lanes: bool = False

    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


def load(room_id: int | None = None) -> Config:
    _load_dotenv()
    rid = room_id if room_id is not None else _env_int("MAOER_ROOM_ID", 868802213)
    base = Path(os.getenv("MAOER_BASE_DIR", "recordings")).resolve()
    ffmpeg = _resolve_ffmpeg()
    base.mkdir(parents=True, exist_ok=True)
    return Config(
        room_id=rid,
        base_dir=base,
        ffmpeg_path=ffmpeg,
        max_no_data_seconds=_env_int("MAOER_MAX_NO_DATA", 12),
        max_ws_silent_seconds=_env_int("MAOER_MAX_WS_SILENT", 120),
        idle_poll_seconds=_env_float("MAOER_IDLE_POLL", 8.0),
        active_poll_seconds=_env_float("MAOER_ACTIVE_POLL", 30.0),
        dual_record=_env_int("MAOER_DUAL_RECORD", 1) != 0,
        worker_b_delay=_env_float("MAOER_WORKER_B_DELAY", 8.0),
        spare_cookies=_env_int("MAOER_SPARE_COOKIES", 2),
        heterogeneous_lanes=_env_int("MAOER_HETERO_LANES", 0) != 0,
    )
