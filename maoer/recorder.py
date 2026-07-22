"""Recording session: dual-ffmpeg hot-standby supervision + chat log writer.

Why dual ffmpeg: with a single ffmpeg, *any* restart (URL expiry, CDN
hiccup, stall, crash) is an audio gap. We run two ffmpeg workers pulling the
same HLS stream, started a few seconds apart. When one dies/stalls, the other
is still capturing, so coverage is continuous. ``finalize`` then stitches a
single gap-free track: worker A is primary, A's gaps are filled from worker B,
and only stretches missing from both become silence.

Threading model:
  - main thread (orchestrator) calls ``start``/``stop`` and refreshes the
    shared ``StreamProvider`` (HLS URL + cookie header)
  - each FfmpegWorker owns: one supervisor thread (launch + watchdog) and one
    short-lived stderr-reader thread per ffmpeg launch. Workers never touch
    Playwright; they only read the atomic ``StreamProvider``.
  - one chat writer thread (session level) drains the danmaku queue to disk

All timeline stamps (segment ``t_start``, chat ``t_audio``, ``end_offset``)
share one monotonic origin (``RecordSession.start_mono``) so audio and chat
stay aligned — see finalize.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .log import log


# ffmpeg stderr substrings that mean "this stream is broken, restart now".
# Case-insensitive substring match against each line.
FFMPEG_FATAL_PATTERNS = re.compile(
    r"("
    r"server returned 4\d\d"           # HTTP 4xx (expired sig etc.)
    r"|server returned 5\d\d"           # HTTP 5xx
    r"|connection reset"
    r"|connection refused"
    r"|connection timed out"
    r"|no route to host"
    r"|end of file"                     # premature EOF on HLS source
    r"|invalid data found"              # garbled stream
    r"|hls: cannot reload"              # m3u8 reload failure
    r"|failed to open segment"
    r")",
    re.IGNORECASE,
)

# Gaps shorter than this are normal segment-boundary jitter, not real drops.
_GAP_THRESHOLD_SECONDS = 0.5
_OUTPUT_SAMPLE_RATE = 48000
_OUTPUT_CHANNELS = 2
_OUTPUT_BIT_RATE = "128k"
_AAC_FRAME_SAMPLES = 1024
_FINAL_OUTPUT_NAME = "final.m4a"
_NO_WINDOW_CREATIONFLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)


def _run_hidden(*popenargs: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    """Run a media helper without allocating a Windows console window."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("creationflags", _NO_WINDOW_CREATIONFLAGS)
    return subprocess.run(*popenargs, **kwargs)


def _capture_audio_args() -> list[str]:
    """Keep the platform AAC bitstream and discard non-audio streams."""
    return ["-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy"]


def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "unknown"


@dataclass
class StreamCreds:
    """Snapshot of credentials needed to pull a stream. Owned by main thread."""
    cookie_header: str
    url: str
    kind: str = "hls"  # "hls" or "flv" — selects which pull URL ffmpeg uses


class StreamProvider:
    """Atomic shared snapshot of HLS URL + cookies.

    Writers (main thread) call ``update``; workers read via ``get`` from
    background threads. Pure-Python locking — never touches Playwright.
    """

    def __init__(self, initial: StreamCreds) -> None:
        self._lock = threading.Lock()
        self._creds = initial

    def update(self, creds: StreamCreds) -> None:
        with self._lock:
            self._creds = creds

    def get(self) -> StreamCreds:
        with self._lock:
            return self._creds


# ============================ ffmpeg worker ============================

class FfmpegWorker:
    """One ffmpeg recording lane. Supervises a single ffmpeg, restarting it
    on crash/stall/fatal-stderr, recording each segment's timeline position.

    Multiple workers share the session's ``start_mono`` (so ``t_start`` values
    are directly comparable across lanes) and the ``segments.jsonl`` log
    (guarded by ``seg_log_lock``).
    """

    def __init__(
        self,
        cfg: Config,
        pool: Any,
        session_dir: Path,
        room_id: int,
        label: str,
        start_mono: float,
        seg_log_path: Path,
        seg_log_lock: threading.Lock,
        kind: str = "hls",
    ) -> None:
        self.cfg = cfg
        self.pool = pool  # CookiePool; worker only calls pool.acquire(label)
        self.session_dir = session_dir
        self.room_id = room_id
        self.label = label
        self.kind = kind  # "hls" or "flv" — heterogeneous lanes decorrelate
        self.start_mono = start_mono
        self._seg_log_path = seg_log_path
        self._seg_log_lock = seg_log_lock

        self._proc: subprocess.Popen[bytes] | None = None
        self._proc_lock = threading.Lock()
        self._epoch = 0
        self.segment_index = 0
        self._current_segment: Path | None = None

        # 1-based segment index -> {"file": str, "t_start": float}
        self.segments: dict[int, dict[str, Any]] = {}

        self._restart_event = threading.Event()
        self._restart_reason = ""
        self.url_refresh_requested = threading.Event()
        self._empty_restart_streak = 0

        self.last_audio_write: float | None = None

        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_delay = 0.0

    # ---------- lifecycle ----------

    @property
    def running(self) -> bool:
        return not self._stopped.is_set()

    def start(self, delay: float = 0.0) -> None:
        self._start_delay = delay
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"worker-{self.label}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._kill()

    def _run(self) -> None:
        if self._start_delay > 0:
            if self._stopped.wait(self._start_delay):
                return
        self._launch_segment()
        self._watchdog_loop()

    # ---------- ffmpeg launch ----------

    def _launch_segment(self) -> None:
        # Acquire this lane's distinct guest identity (cookie + signed URL) for
        # this lane's protocol. Main thread refreshes the pool, so creds are
        # normally ready; wait briefly if not (first launch racing refresh).
        creds = self.pool.acquire(self.label, self.kind)
        if creds is None:
            for _ in range(20):
                if self._stopped.wait(0.5):
                    return
                creds = self.pool.acquire(self.label, self.kind)
                if creds is not None:
                    break
        if creds is None:
            log.warning("[%s] no creds available; will retry", self.label)
            self._request_restart("no creds")
            return
        with self._proc_lock:
            self.segment_index += 1
            self._epoch += 1
            epoch = self._epoch
            out_path = self.session_dir / f"audio_{self.label}_{self.segment_index:04d}.ts"
            self._current_segment = out_path
            headers = [
                f"User-Agent: {self.cfg.user_agent}",
                "Accept: */*",
                "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding: identity",
                "Origin: https://fm.missevan.com",
                f"Referer: https://fm.missevan.com/live/{self.room_id}",
                "Sec-Fetch-Dest: empty",
                "Sec-Fetch-Mode: cors",
                "Sec-Fetch-Site: cross-site",
            ]
            if creds.cookie_header:
                headers.append(f"Cookie: {creds.cookie_header}")
            cmd = [
                self.cfg.ffmpeg_path,
                "-y",
                "-user_agent", self.cfg.user_agent,
                "-headers", "\r\n".join(headers) + "\r\n",
                "-loglevel", "error",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                # Absorb transient failures inside ffmpeg instead of dying, so
                # the watchdog doesn't have to restart (each restart is a gap).
                # 4xx is deliberately excluded: an expired signature needs a
                # fresh URL, not a reconnect loop on the dead one.
                "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "5xx",
                "-rw_timeout", "30000000",
                "-fflags", "+genpts+igndts+discardcorrupt",
                "-i", creds.url,
                *_capture_audio_args(),
                "-flush_packets", "1",
                "-f", "mpegts",
                str(out_path),
            ]
            log.info("[%s/%s] ffmpeg start segment %d → %s",
                     self.label, creds.kind, self.segment_index, out_path.name)
            self._restart_event.clear()
            self._restart_reason = ""

            t_start = round(time.monotonic() - self.start_mono, 3)
            self.segments[self.segment_index] = {
                "file": out_path.name, "t_start": t_start,
            }
            try:
                with self._seg_log_lock, self._seg_log_path.open("a", encoding="utf-8") as sf:
                    sf.write(json.dumps(
                        {"worker": self.label, "index": self.segment_index,
                         "file": out_path.name, "t_start": t_start},
                        ensure_ascii=False,
                    ) + "\n")
            except Exception as exc:
                log.debug("segments.jsonl write failed: %s", exc)

            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=_NO_WINDOW_CREATIONFLAGS,
            )
            threading.Thread(
                target=self._stderr_reader, args=(self._proc, epoch),
                daemon=True, name=f"stderr-{self.label}-{epoch}",
            ).start()

    def _stderr_reader(self, proc: subprocess.Popen[bytes], epoch: int) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                if self._stopped.is_set() or epoch != self._epoch:
                    return
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                log.debug("[%s] ffmpeg: %s", self.label, line)
                if FFMPEG_FATAL_PATTERNS.search(line):
                    log.warning("[%s] ffmpeg fatal: %s", self.label, line)
                    self._request_restart(f"stderr: {line[:80]}")
                    return
        except Exception as exc:
            log.debug("[%s] stderr reader exit: %s", self.label, exc)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _request_restart(self, reason: str) -> None:
        if self._restart_event.is_set():
            return
        self._restart_reason = reason
        self._restart_event.set()

    def _kill(self) -> None:
        with self._proc_lock:
            proc = self._proc
            if proc and proc.poll() is None:
                # Live process (stall case): terminate and wait briefly —
                # streaming ffmpeg responds to SIGTERM fast.
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _discard_empty_segment(self) -> None:
        seg = self._current_segment
        if not seg:
            return
        try:
            if seg.exists() and seg.stat().st_size == 0:
                seg.unlink()
                self.segments.pop(self.segment_index, None)
                log.debug("[%s] removed empty segment %s", self.label, seg.name)
        except OSError:
            pass

    # ---------- watchdog ----------

    def _watchdog_loop(self) -> None:
        backoff = 1.0
        last_size = 0
        last_grow = time.time()

        while not self._stopped.wait(1.0):
            proc = self._proc
            seg = self._current_segment
            if not proc or not seg:
                continue

            try:
                if seg.exists():
                    size = seg.stat().st_size
                    if size > last_size:
                        last_size = size
                        last_grow = time.time()
                        self.last_audio_write = time.time()
            except OSError:
                pass

            died = proc.poll() is not None
            stalled = (time.time() - last_grow) > self.cfg.max_no_data_seconds
            kicked = self._restart_event.is_set()
            if not (died or stalled or kicked):
                backoff = max(1.0, backoff / 2)
                continue

            if died:
                reason = f"exited rc={proc.returncode}"
            elif kicked:
                reason = self._restart_reason or "explicit kick"
            else:
                reason = f"stalled {time.time() - last_grow:.0f}s"

            produced = last_size > 0
            self._empty_restart_streak = 0 if produced else self._empty_restart_streak + 1
            log.warning("[%s] restart: %s (bytes=%d, empty_streak=%d)",
                        self.label, reason, last_size, self._empty_restart_streak)
            self._kill()
            self._discard_empty_segment()
            if self._stopped.is_set():
                return

            if not produced:
                # Empty crash ⇒ this cookie's URL is likely dead/throttled.
                # On a repeat (streak ≥ 2) the cookie itself looks burned —
                # rotate this lane onto a fresh spare identity so we stop
                # hammering the bad one (this is the main cause of a lane
                # staying down long enough to overlap the other lane's jitter).
                if self._empty_restart_streak >= 2:
                    try:
                        if self.pool.rotate(self.label):
                            self._empty_restart_streak = 0
                    except Exception as exc:
                        log.debug("[%s] rotate failed: %s", self.label, exc)
                # Ask main thread for a fresh URL and wait briefly for it,
                # escalating with the streak.
                self.url_refresh_requested.set()
                wait = min(15.0, 2.0 * self._empty_restart_streak)
                deadline = time.time() + wait
                while (self.url_refresh_requested.is_set()
                       and time.time() < deadline
                       and not self._stopped.is_set()):
                    time.sleep(0.5)
                backoff = min(30.0, max(backoff, 2.0) * 2)
            else:
                # Had data: stream is fine, restart immediately (no sleep).
                backoff = 1.0

            if self._stopped.is_set():
                return
            last_size = 0
            last_grow = time.time()
            try:
                self._launch_segment()
            except Exception as exc:
                log.error("[%s] restart failed: %s", self.label, exc)


# ============================ session ============================

class RecordSession:
    """Coordinates one or two FfmpegWorkers + the danmaku chat writer."""

    def __init__(
        self,
        cfg: Config,
        pool: Any,
        room_id: int,
        info: dict[str, Any],
        session_dir: Path,
        num_lanes: int,
    ) -> None:
        self.cfg = cfg
        self.pool = pool
        self.room_id = room_id
        self.info = info
        self.session_dir = session_dir

        self.start_wall = time.time()
        self.start_mono = time.monotonic()
        self.end_offset: float | None = None

        self._stopped = threading.Event()

        # Shared segments log.
        seg_log = session_dir / "segments.jsonl"
        seg_lock = threading.Lock()

        # One lane per recording slot. Each lane acquires its OWN guest identity
        # from the cookie pool (distinct cookie + signed URL), so the server
        # sees independent viewers — not one viewer opening N stream connections,
        # which top rooms throttle. Lanes also alternate protocol (HLS/FLV) when
        # heterogeneous mode is on, so a source-side hiccup on one pipeline
        # doesn't stall both lanes at the same instant.
        labels = ["A", "B", "C", "D"]
        if cfg.heterogeneous_lanes:
            kinds = ["hls", "flv", "hls", "flv"]
        else:
            kinds = ["hls"] * 4
        self.workers: list[FfmpegWorker] = [
            FfmpegWorker(cfg, pool, session_dir, room_id, labels[i],
                         self.start_mono, seg_log, seg_lock, kind=kinds[i])
            for i in range(num_lanes)
        ]

        # Chat (danmaku) — independent of recording lanes.
        self.last_ws_msg: float | None = None
        self._chat_q: queue.Queue[str] = queue.Queue(maxsize=20000)
        self._chat_path = session_dir / "chat.jsonl"
        self._chat_writer = threading.Thread(
            target=self._chat_writer_loop, daemon=True, name="chat-writer")

    # ---------- lifecycle ----------

    @property
    def running(self) -> bool:
        return not self._stopped.is_set()

    @property
    def last_audio_write(self) -> float | None:
        """Newest write across all lanes — any lane alive counts as alive."""
        ts = [w.last_audio_write for w in self.workers if w.last_audio_write]
        return max(ts) if ts else None

    @property
    def segment_index(self) -> int:
        """Total segments launched across lanes (for status display)."""
        return sum(w.segment_index for w in self.workers)

    def start(self) -> None:
        self._chat_writer.start()
        self.workers[0].start(delay=0.0)
        for w in self.workers[1:]:
            # Stagger B so the two lanes rarely fail at the same instant.
            w.start(delay=self.cfg.worker_b_delay)
        log.info("recording: %d lane(s)%s", len(self.workers),
                 f", B staggered {self.cfg.worker_b_delay:.0f}s"
                 if len(self.workers) > 1 else "")

    def stop(self, reason: str) -> None:
        if self._stopped.is_set():
            return
        log.info("session stop: %s", reason)
        self.end_offset = round(time.monotonic() - self.start_mono, 3)
        self._stopped.set()
        for w in self.workers:
            w.stop()
        try:
            self._chat_q.put_nowait("")
        except queue.Full:
            pass

    # ---------- chat ----------

    def append_chat(self, payload: dict[str, Any]) -> None:
        """Thread-safe. Called from the WS frame handler."""
        self.last_ws_msg = time.time()
        try:
            line = json.dumps(
                {"t_audio": round(time.monotonic() - self.start_mono, 3),
                 "t_wall": time.time(), "data": payload},
                ensure_ascii=False,
            )
        except (TypeError, ValueError) as exc:
            log.debug("chat encode failed: %s", exc)
            return
        try:
            self._chat_q.put_nowait(line)
        except queue.Full:
            try:
                self._chat_q.get_nowait()
                self._chat_q.put_nowait(line)
            except Exception:
                pass

    def _chat_writer_loop(self) -> None:
        with self._chat_path.open("a", encoding="utf-8") as f:
            while not self._stopped.is_set():
                try:
                    line = self._chat_q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if not line:
                    continue
                try:
                    f.write(line + "\n")
                    f.flush()
                except Exception as exc:
                    log.warning("chat write failed: %s", exc)
            try:
                while True:
                    line = self._chat_q.get_nowait()
                    if line:
                        f.write(line + "\n")
            except queue.Empty:
                pass


def open_session(
    cfg: Config, pool: Any, info: dict[str, Any], num_lanes: int,
) -> RecordSession:
    creator = sanitize((info.get("room") or {}).get("creator_username") or "unknown")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = cfg.base_dir / f"{cfg.room_id}_{creator}" / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    return RecordSession(cfg, pool, cfg.room_id, info, session_dir, num_lanes)


# ============================ finalize ============================

def _ffprobe_path(ffmpeg_path: str) -> str:
    p = Path(ffmpeg_path)
    cand = p.with_name("ffprobe.exe" if p.suffix.lower() == ".exe" else "ffprobe")
    if cand.exists():
        return str(cand)
    import shutil
    return shutil.which("ffprobe") or "ffprobe"


def _probe_duration(ffprobe: str, path: Path) -> float:
    try:
        proc = _run_hidden(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return float((proc.stdout or b"").decode("utf-8", "replace").strip() or 0.0)
    except Exception:
        return 0.0


def _probe_decoded_duration(ffmpeg: str, path: Path) -> float:
    """Measure the audio timeline after normalizing its source timestamps."""
    try:
        proc = _run_hidden(
            [
                ffmpeg, "-v", "error", "-xerror", "-i", str(path),
                "-map", "0:a:0",
                "-af", (
                    "asetpts=PTS-STARTPTS,"
                    f"aresample={_OUTPUT_SAMPLE_RATE}:async=1000:first_pts=0"
                ),
                "-progress", "pipe:1", "-nostats",
                "-f", "null", "-",
            ],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        output = (proc.stdout or b"").decode("utf-8", "replace")
        values = [
            int(line.partition("=")[2])
            for line in output.splitlines()
            if line.startswith("out_time_us=")
            and line.partition("=")[2].strip().lstrip("-").isdigit()
        ]
        if values and proc.returncode == 0:
            return max(values) / 1_000_000.0
    except Exception:
        pass
    return 0.0


def _probe_audio_runs(ffprobe: str, path: Path) -> list[tuple[float, float]]:
    """Return continuous audio PTS ranges, relative to the first packet."""
    try:
        proc = _run_hidden(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "packet=pts_time,duration_time",
                "-of", "csv=p=0", str(path),
            ],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return []
        first_pts: float | None = None
        run_start = run_end = 0.0
        runs: list[tuple[float, float]] = []
        for raw_line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
            fields = raw_line.split(",", 2)
            if len(fields) < 2:
                continue
            try:
                pts = float(fields[0])
                duration = float(fields[1])
            except ValueError:
                continue
            if first_pts is None:
                first_pts = pts
                run_start = 0.0
            packet_start = max(0.0, pts - first_pts)
            packet_end = packet_start + max(0.0, duration)
            if packet_start - run_end > _GAP_THRESHOLD_SECONDS:
                if run_end - run_start > 0.01:
                    runs.append((run_start, run_end))
                run_start = packet_start
            run_end = max(run_end, packet_end)
        if first_pts is not None and run_end - run_start > 0.01:
            runs.append((run_start, run_end))
        return runs
    except Exception:
        return []


@dataclass(frozen=True)
class AdtsInfo:
    frames: int
    object_type: int
    sample_rate: int
    channels: int
    mpeg_id: int

    @property
    def config(self) -> tuple[int, int, int, int]:
        return self.object_type, self.sample_rate, self.channels, self.mpeg_id


_ADTS_SAMPLE_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000,
    22050, 16000, 12000, 11025, 8000, 7350,
)


def _scan_adts(path: Path) -> AdtsInfo | None:
    """Validate every ADTS frame and return its uniform stream config."""
    try:
        size = path.stat().st_size
        if size <= 0:
            return None
        frames = 0
        config: tuple[int, int, int, int] | None = None
        with path.open("rb") as stream:
            pos = 0
            while pos < size:
                stream.seek(pos)
                header = stream.read(7)
                if len(header) != 7 or header[0] != 0xFF or (header[1] & 0xF6) != 0xF0:
                    return None
                if header[6] & 0x03:
                    return None
                object_type = ((header[2] >> 6) & 0x03) + 1
                sample_index = (header[2] >> 2) & 0x0F
                if sample_index >= len(_ADTS_SAMPLE_RATES):
                    return None
                sample_rate = _ADTS_SAMPLE_RATES[sample_index]
                channels = ((header[2] & 0x01) << 2) | ((header[3] >> 6) & 0x03)
                mpeg_id = (header[1] >> 3) & 0x01
                frame_length = (
                    ((header[3] & 0x03) << 11)
                    | (header[4] << 3)
                    | ((header[5] >> 5) & 0x07)
                )
                header_length = 7 if header[1] & 0x01 else 9
                if frame_length < header_length or pos + frame_length > size:
                    return None
                current = (object_type, sample_rate, channels, mpeg_id)
                if config is None:
                    config = current
                elif current != config:
                    return None
                frames += 1
                pos += frame_length
        if not config or frames <= 0:
            return None
        return AdtsInfo(frames, *config)
    except OSError:
        return None


def _probe_audio_spec(ffprobe: str, path: Path) -> dict[str, Any]:
    try:
        proc = _run_hidden(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries",
                "stream=codec_name,profile,sample_rate,channels,bit_rate",
                "-of", "json", str(path),
            ],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        payload = json.loads((proc.stdout or b"{}").decode("utf-8", "replace"))
        streams = payload.get("streams", [])
        if proc.returncode != 0 or not streams:
            return {}
        info = dict(streams[0])
        for key in ("sample_rate", "channels", "bit_rate"):
            try:
                info[key] = int(info[key])
            except (KeyError, TypeError, ValueError):
                info[key] = None
        return info
    except (OSError, TypeError, ValueError):
        return {}


def _aac_passthrough_compatible(ffprobe: str, paths: set[Path]) -> bool:
    for path in paths:
        info = _probe_audio_spec(ffprobe, path)
        if (
            info.get("codec_name") != "aac"
            or info.get("profile") != "LC"
            or info.get("sample_rate") != _OUTPUT_SAMPLE_RATE
            or info.get("channels") != _OUTPUT_CHANNELS
        ):
            return False
    return bool(paths)


def _quantize_plan(plan: list[tuple]) -> tuple[list[tuple[tuple, int]], int]:
    """Assign every plan item to one cumulative AAC frame grid.

    Audio seeks move by the same rounding carry as their output boundary. This
    keeps adjacent slices content-contiguous instead of skipping or repeating
    a source frame whenever a non-frame-aligned boundary rounds up or down.
    """
    frame_rate = _OUTPUT_SAMPLE_RATE / _AAC_FRAME_SAMPLES
    cumulative = 0.0
    previous_end = 0
    quantized: list[tuple[tuple, int]] = []
    for item in plan:
        item_start = cumulative
        duration = float(item[3] if item[0] == "audio" else item[1])
        cumulative += max(0.0, duration)
        frame_end = int(cumulative * frame_rate + 0.5)
        frames = frame_end - previous_end
        if frames > 0:
            quantized_item = item
            if item[0] == "audio":
                _, path, seek, take = item
                quantized_start = previous_end / frame_rate
                adjusted_seek = max(0.0, float(seek) + quantized_start - item_start)
                quantized_item = ("audio", path, adjusted_seek, take)
            quantized.append((quantized_item, frames))
        previous_end = frame_end
    return quantized, previous_end


def _extract_aac_copy(
    ffmpeg: str, src: Path, seek: float, take: float, frames: int, out: Path,
) -> AdtsInfo | None:
    """Copy a frame-aligned AAC interval from an input segment to ADTS."""
    frame_seconds = _AAC_FRAME_SAMPLES / _OUTPUT_SAMPLE_RATE
    args = [ffmpeg, "-y", "-i", str(src)]
    if seek > 0.000001:
        args += ["-ss", f"{max(0.0, seek - 0.000001):.6f}"]
    args += [
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-t", f"{max(take, frames * frame_seconds) + frame_seconds:.6f}",
        "-c:a", "copy", "-frames:a", str(frames), "-f", "adts", str(out),
    ]
    try:
        proc = _run_hidden(
            args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info = _scan_adts(out) if proc.returncode == 0 else None
        return info if info and info.frames == frames else None
    except OSError:
        return None


def _make_aac_silence(ffmpeg: str, out: Path, frames: int) -> AdtsInfo | None:
    """Encode silence, then frame-trim away the native AAC encoder flush."""
    encoded = out.with_name(f"{out.stem}.encoded.aac")
    try:
        proc = _run_hidden(
            [
                ffmpeg, "-y", "-f", "lavfi", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={_OUTPUT_SAMPLE_RATE}",
                "-frames:a", str(frames), "-c:a", "aac", "-profile:a", "aac_low",
                "-b:a", _OUTPUT_BIT_RATE, "-ar", str(_OUTPUT_SAMPLE_RATE),
                "-ac", str(_OUTPUT_CHANNELS), "-f", "adts", str(encoded),
            ],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return None
        proc = _run_hidden(
            [
                ffmpeg, "-y", "-i", str(encoded), "-map", "0:a:0",
                "-c:a", "copy", "-frames:a", str(frames), "-f", "adts", str(out),
            ],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        info = _scan_adts(out) if proc.returncode == 0 else None
        return info if info and info.frames == frames else None
    except OSError:
        return None
    finally:
        try:
            encoded.unlink()
        except OSError:
            pass


def _join_adts(parts: list[Path], joined: Path) -> AdtsInfo | None:
    try:
        with joined.open("wb") as target:
            for part in parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        return _scan_adts(joined)
    except OSError:
        return None


def _mux_adts_to_m4a(ffmpeg: str, joined: Path, out: Path) -> bool:
    try:
        proc = _run_hidden(
            [
                ffmpeg, "-y", "-f", "aac", "-i", str(joined),
                "-map", "0:a:0", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart", "-f", "ipod", str(out),
            ],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
            log.warning("M4A remux failed: %s", tail)
        return proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    except OSError:
        return False


def _probe_packet_durations(ffprobe: str, path: Path) -> list[int]:
    try:
        proc = _run_hidden(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "packet=duration", "-of", "csv=p=0", str(path),
            ],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return []
        return [
            int(line.split(",", 1)[0])
            for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines()
            if line.split(",", 1)[0].strip().isdigit()
        ]
    except (OSError, ValueError):
        return []


def _validate_m4a(
    ffmpeg: str, ffprobe: str, path: Path, expected_frames: int,
    *, exact_packets: bool,
) -> tuple[dict[str, Any], float] | None:
    info = _probe_audio_spec(ffprobe, path)
    if (
        info.get("codec_name") != "aac"
        or info.get("profile") != "LC"
        or info.get("sample_rate") != _OUTPUT_SAMPLE_RATE
        or info.get("channels") != _OUTPUT_CHANNELS
    ):
        return None
    packet_durations = _probe_packet_durations(ffprobe, path)
    if not packet_durations or any(value != _AAC_FRAME_SAMPLES for value in packet_durations):
        return None
    if exact_packets and len(packet_durations) != expected_frames:
        return None
    duration = _probe_decoded_duration(ffmpeg, path)
    expected_duration = expected_frames * _AAC_FRAME_SAMPLES / _OUTPUT_SAMPLE_RATE
    tolerance = (_AAC_FRAME_SAMPLES / _OUTPUT_SAMPLE_RATE) * (1 if exact_packets else 3)
    if duration <= 0 or abs(duration - expected_duration) > tolerance:
        return None
    return info, duration


def _write_zeros(stream: Any, size: int) -> None:
    block = b"\0" * (1024 * 1024)
    remaining = size
    while remaining > 0:
        chunk = block if remaining >= len(block) else block[:remaining]
        stream.write(chunk)
        remaining -= len(chunk)


def _render_pcm_timeline(
    ffmpeg: str, quantized: list[tuple[tuple, int]], out: Path,
) -> bool:
    bytes_per_sample = _OUTPUT_CHANNELS * 2
    try:
        with out.open("wb", buffering=0) as target:
            for item, frames in quantized:
                samples = frames * _AAC_FRAME_SAMPLES
                expected_bytes = samples * bytes_per_sample
                if item[0] == "silence":
                    _write_zeros(target, expected_bytes)
                    continue
                _, src, seek, _take = item
                start = target.tell()
                args = [ffmpeg, "-v", "error"]
                if seek > 0.01:
                    args += ["-ss", f"{seek:.6f}"]
                args += [
                    "-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn",
                    "-af", (
                        "asetpts=PTS-STARTPTS,"
                        f"aresample={_OUTPUT_SAMPLE_RATE}:async=1000:first_pts=0,"
                        f"atrim=end_sample={samples},asetpts=N/SR/TB"
                    ),
                    "-ar", str(_OUTPUT_SAMPLE_RATE), "-ac", str(_OUTPUT_CHANNELS),
                    "-c:a", "pcm_s16le", "-f", "s16le", "-",
                ]
                proc = _run_hidden(
                    args, check=False, stdout=target, stderr=subprocess.DEVNULL)
                if proc.returncode != 0:
                    return False
                actual = target.tell() - start
                if actual > expected_bytes:
                    target.truncate(start + expected_bytes)
                    target.seek(0, os.SEEK_END)
                elif actual < expected_bytes:
                    _write_zeros(target, expected_bytes - actual)
        return out.exists() and out.stat().st_size > 0
    except OSError:
        return False


def _encode_pcm_m4a(ffmpeg: str, pcm: Path, out: Path) -> bool:
    try:
        proc = _run_hidden(
            [
                ffmpeg, "-y", "-f", "s16le", "-ar", str(_OUTPUT_SAMPLE_RATE),
                "-ac", str(_OUTPUT_CHANNELS), "-i", str(pcm),
                "-c:a", "aac", "-profile:a", "aac_low", "-b:a", _OUTPUT_BIT_RATE,
                "-movflags", "+faststart", "-f", "ipod", str(out),
            ],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
            log.warning("AAC fallback encode failed: %s", tail)
        return proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    except OSError:
        return False


def _collect_lane(
    ffmpeg: str, ffprobe: str, session_dir: Path, segments: dict,
) -> list[dict]:
    """Return continuous audio ranges from all real, non-empty segments."""
    out = []
    for idx in sorted(segments):
        meta = segments[idx]
        p = session_dir / meta["file"]
        if not (p.exists() and p.stat().st_size > 0):
            continue
        t0 = float(meta.get("t_start", 0.0))
        runs = _probe_audio_runs(ffprobe, p)
        if not runs:
            dur = _probe_decoded_duration(ffmpeg, p) or _probe_duration(ffprobe, p)
            runs = [(0.0, dur)] if dur > 0 else []
        for source_start, source_end in runs:
            dur = source_end - source_start
            out.append({
                "path": p,
                "t_start": t0 + source_start,
                "t_end": t0 + source_end,
                "dur": dur,
                "source_start": source_start,
            })
    out.sort(key=lambda s: s["t_start"])
    return out


def _covering(lane: list[dict], t: float) -> dict | None:
    """Return a segment whose [t_start, t_end) contains t (with small margin)."""
    for s in lane:
        if s["t_start"] <= t < (s["t_end"] - 0.1):
            return s
    return None


def _next_start_after(lane: list[dict], t: float) -> float | None:
    cands = [s["t_start"] for s in lane if s["t_start"] > t]
    return min(cands) if cands else None


def finalize(session: RecordSession) -> None:
    if session.segment_index == 0:
        log.info("no segments produced; skipping merge")
        return

    ffmpeg = session.cfg.ffmpeg_path
    ffprobe = _ffprobe_path(ffmpeg)

    # Collect each lane. Worker A is primary; any others are backups.
    lanes: dict[str, list[dict]] = {}
    for w in session.workers:
        lanes[w.label] = _collect_lane(
            ffmpeg, ffprobe, session.session_dir, w.segments)
    primary = lanes.get("A", [])
    backups: list[list[dict]] = [lanes[k] for k in lanes if k != "A"]
    backup = backups[0] if backups else []

    if not primary and not backup:
        log.warning("no usable segments in any lane; nothing to merge")
        return

    # Determine end of timeline.
    all_ends = [s["t_end"] for lane in lanes.values() for s in lane]
    end = (
        session.end_offset
        if session.end_offset is not None
        else (max(all_ends) if all_ends else 0.0)
    )

    # Greedy timeline walk: A preferred, B fills A's gaps, else silence.
    plan: list[tuple] = []  # ('audio', path, seek, take) | ('silence', dur)
    gaps: list[dict] = []   # auditable record of every both-lanes-down stretch
    cov_a = cov_b = cov_sil = 0.0
    timeline = 0.0
    guard = 0
    while timeline < end - 0.000001 and guard < 100000:
        guard += 1
        a = _covering(primary, timeline)
        if a:
            seek = a.get("source_start", 0.0) + timeline - a["t_start"]
            stop_at = min(a["t_end"], end)
            take = stop_at - timeline
            plan.append(("audio", a["path"], seek, take))
            cov_a += take
            timeline = stop_at
            continue
        b = _covering(backup, timeline)
        if b:
            na = _next_start_after(primary, timeline)
            stop_at = min(b["t_end"], end, na if na is not None else end)
            take = stop_at - timeline
            if take > 0.01:
                seek = b.get("source_start", 0.0) + timeline - b["t_start"]
                plan.append(("audio", b["path"], seek, take))
                cov_b += take
                timeline = stop_at
                continue
        # Neither lane covers ``timeline`` — silence to the next available start.
        na = _next_start_after(primary, timeline)
        nb = _next_start_after(backup, timeline)
        nxts = [x for x in (na, nb, end) if x is not None and x > timeline]
        nxt = min(nxts) if nxts else end
        gap = nxt - timeline
        if gap > _GAP_THRESHOLD_SECONDS:
            plan.append(("silence", gap))
            gap_meta = {"at": round(timeline, 1), "dur": round(gap, 1)}
            if nxt >= end - _GAP_THRESHOLD_SECONDS:
                gap_meta["trailing"] = True
            gaps.append(gap_meta)
            cov_sil += gap
        timeline = nxt

    # Trailing silence to session end.
    if end - timeline > _GAP_THRESHOLD_SECONDS:
        plan.append(("silence", end - timeline))
        gaps.append({"at": round(timeline, 1), "dur": round(end - timeline, 1),
                     "trailing": True})
        cov_sil += end - timeline

    quantized, total_frames = _quantize_plan(plan)
    parts_dir = session.session_dir / "_parts"
    parts_dir.mkdir(exist_ok=True)
    for stale in parts_dir.iterdir():
        if stale.is_file():
            try:
                stale.unlink()
            except OSError:
                pass

    final = session.session_dir / _FINAL_OUTPUT_NAME
    partial = session.session_dir / "final.partial.m4a"
    joined = parts_dir / "joined.aac"
    pcm = parts_dir / "timeline.s16le"
    ok = False
    processing_mode = "passthrough"
    fallback_reason: str | None = None
    media_info: dict[str, Any] = {}
    final_dur = 0.0

    source_paths = {
        item[1] for item, _frames in quantized if item[0] == "audio"
    }
    if not _aac_passthrough_compatible(ffprobe, source_paths):
        fallback_reason = "source audio is not AAC-LC 48 kHz stereo"
    else:
        parts: list[Path] = []
        expected_config: tuple[int, int, int, int] | None = None
        for index, (item, frames) in enumerate(quantized):
            out = parts_dir / f"p_{index:05d}.aac"
            if item[0] == "audio":
                _, path, seek, take = item
                adts = _extract_aac_copy(ffmpeg, path, seek, take, frames, out)
            else:
                adts = _make_aac_silence(ffmpeg, out, frames)
            if (
                not adts
                or adts.object_type != 2
                or adts.sample_rate != _OUTPUT_SAMPLE_RATE
                or adts.channels != _OUTPUT_CHANNELS
            ):
                fallback_reason = f"AAC part {index} failed frame validation"
                break
            if expected_config is None:
                expected_config = adts.config
            elif adts.config != expected_config:
                fallback_reason = f"AAC part {index} has incompatible stream config"
                break
            parts.append(out)

        if len(parts) == len(quantized):
            joined_info = _join_adts(parts, joined)
            if (
                not joined_info
                or joined_info.frames != total_frames
                or joined_info.config != expected_config
            ):
                fallback_reason = "joined AAC stream failed frame validation"
            else:
                try:
                    partial.unlink()
                except OSError:
                    pass
                if _mux_adts_to_m4a(ffmpeg, joined, partial):
                    validated = _validate_m4a(
                        ffmpeg, ffprobe, partial, total_frames, exact_packets=True)
                    if validated:
                        media_info, final_dur = validated
                        ok = True
                    else:
                        fallback_reason = "M4A passthrough output failed validation"
                else:
                    fallback_reason = "M4A passthrough mux failed"

    if not ok:
        processing_mode = "transcoded"
        log.warning("AAC passthrough unavailable; using one-pass fallback: %s",
                    fallback_reason or "unknown reason")
        try:
            partial.unlink()
        except OSError:
            pass
        if _render_pcm_timeline(ffmpeg, quantized, pcm) and _encode_pcm_m4a(
            ffmpeg, pcm, partial
        ):
            validated = _validate_m4a(
                ffmpeg, ffprobe, partial, total_frames, exact_packets=False)
            if validated:
                media_info, final_dur = validated
                ok = True

    if ok:
        try:
            os.replace(partial, final)
        except OSError as exc:
            log.warning("publishing final M4A failed: %s", exc)
            ok = False

    for temp in (partial, joined, pcm):
        try:
            temp.unlink()
        except OSError:
            pass
    if parts_dir.exists():
        for temp in parts_dir.iterdir():
            if temp.is_file():
                try:
                    temp.unlink()
                except OSError:
                    pass
        try:
            parts_dir.rmdir()
        except OSError:
            pass

    wall = round(time.monotonic() - session.start_mono, 2)
    duration = round(session.end_offset if session.end_offset is not None else wall, 2)
    meta = {
        "room_id": session.room_id,
        "start_time": session.start_wall,
        "duration": duration,
        "audio_duration": round(final_dur, 2),
        "dual_record": len(session.workers) > 1,
        "source_breakdown": {
            "primary_A": round(cov_a, 1),
            "backup_B": round(cov_b, 1),
            "silence": round(cov_sil, 1),
        },
        # Every both-lanes-down stretch (where silence was inserted), with its
        # offset into the final audio. Cross-check each against record.log's per-lane
        # restart timestamps to confirm both lanes were truly down.
        "gaps": gaps,
        "output": _FINAL_OUTPUT_NAME if ok else "(failed)",
        "timeline_aligned": ok,
        "audio_codec": media_info.get("codec_name") if ok else None,
        "audio_profile": media_info.get("profile") if ok else None,
        "audio_sample_rate": media_info.get("sample_rate") if ok else None,
        "audio_channels": media_info.get("channels") if ok else None,
        "audio_bit_rate": media_info.get("bit_rate") if ok else None,
        "uniform_sample_rate": bool(ok),
        "processing_mode": processing_mode if ok else "failed",
        "source_audio_passthrough": bool(ok and processing_mode == "passthrough"),
        "fallback_reason": fallback_reason if processing_mode == "transcoded" else None,
    }
    (session.session_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "final: %s | dur=%.0fs (A=%.0fs B=%.0fs sil=%.0fs)",
        _FINAL_OUTPUT_NAME if ok else "FAILED",
        final_dur, cov_a, cov_b, cov_sil,
    )
