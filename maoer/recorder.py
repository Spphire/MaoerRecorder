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
import queue
import re
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
                "-c:a", "aac",
                "-b:a", "128k",
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
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0,
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
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return float((proc.stdout or b"").decode("utf-8", "replace").strip() or 0.0)
    except Exception:
        return 0.0


def _make_silence(ffmpeg: str, out: Path, seconds: float) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi",
             "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
             "-t", f"{seconds:.3f}", "-c:a", "libmp3lame", "-q:a", "2", str(out)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def _slice_to_mp3(ffmpeg: str, src: Path, seek: float, take: float, out: Path) -> bool:
    """Extract [seek, seek+take] of a segment's audio as MP3.

    Normalizing every piece to identical MP3 params is what makes the final
    concat reliable — raw mpegts segments carry a stray mpeg2video stream and
    48kHz AAC, which silently breaks the concat demuxer if mixed.
    """
    args = [ffmpeg, "-y"]
    if seek > 0.01:
        args += ["-ss", f"{seek:.3f}"]
    args += ["-i", str(src)]
    if take > 0.01:
        args += ["-t", f"{take:.3f}"]
    args += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)]
    try:
        proc = subprocess.run(
            args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def _collect_lane(ffprobe: str, session_dir: Path, segments: dict) -> list[dict]:
    """Return sorted [{path, t_start, t_end, dur}] of real (non-empty) segments."""
    out = []
    for idx in sorted(segments):
        meta = segments[idx]
        p = session_dir / meta["file"]
        if not (p.exists() and p.stat().st_size > 0):
            continue
        dur = _probe_duration(ffprobe, p)
        if dur <= 0:
            continue
        t0 = float(meta.get("t_start", 0.0))
        out.append({"path": p, "t_start": t0, "t_end": t0 + dur, "dur": dur})
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
        lanes[w.label] = _collect_lane(ffprobe, session.session_dir, w.segments)
    primary = lanes.get("A", [])
    backups: list[list[dict]] = [lanes[k] for k in lanes if k != "A"]
    backup = backups[0] if backups else []

    if not primary and not backup:
        log.warning("no usable segments in any lane; nothing to merge")
        return

    # Determine end of timeline.
    all_ends = [s["t_end"] for lane in lanes.values() for s in lane]
    end = session.end_offset or (max(all_ends) if all_ends else 0.0)

    # Greedy timeline walk: A preferred, B fills A's gaps, else silence.
    plan: list[tuple] = []  # ('audio', path, seek, take) | ('silence', dur)
    gaps: list[dict] = []   # auditable record of every both-lanes-down stretch
    cov_a = cov_b = cov_sil = 0.0
    timeline = 0.0
    guard = 0
    while timeline < end - _GAP_THRESHOLD_SECONDS and guard < 100000:
        guard += 1
        a = _covering(primary, timeline)
        if a:
            seek = timeline - a["t_start"]
            take = a["t_end"] - timeline
            plan.append(("audio", a["path"], seek, take))
            cov_a += take
            timeline = a["t_end"]
            continue
        b = _covering(backup, timeline)
        if b:
            na = _next_start_after(primary, timeline)
            stop_at = min(b["t_end"], na) if na is not None else b["t_end"]
            take = stop_at - timeline
            if take > 0.01:
                plan.append(("audio", b["path"], timeline - b["t_start"], take))
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
            gaps.append({"at": round(timeline, 1), "dur": round(gap, 1)})
            cov_sil += gap
        timeline = nxt

    # Trailing silence to session end.
    if end - timeline > _GAP_THRESHOLD_SECONDS:
        plan.append(("silence", end - timeline))
        gaps.append({"at": round(timeline, 1), "dur": round(end - timeline, 1),
                     "trailing": True})
        cov_sil += end - timeline

    # Render each plan item to a uniform MP3 part, then concat-copy.
    parts_dir = session.session_dir / "_parts"
    parts_dir.mkdir(exist_ok=True)
    concat = session.session_dir / "concat.txt"
    lines: list[str] = []
    for i, item in enumerate(plan):
        out = parts_dir / f"p_{i:05d}.mp3"
        if item[0] == "audio":
            _, path, seek, take = item
            if take <= 0.01:
                continue
            if _slice_to_mp3(ffmpeg, path, seek, take, out):
                lines.append(f"file '{out.relative_to(session.session_dir).as_posix()}'")
            else:
                log.warning("slice failed: %s [%.1f+%.1f]", path.name, seek, take)
        else:
            _, dur = item
            if _make_silence(ffmpeg, out, dur):
                lines.append(f"file '{out.relative_to(session.session_dir).as_posix()}'")

    if not lines:
        log.warning("no parts rendered; aborting merge")
        return
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

    final = session.session_dir / "final.mp3"
    ok = False
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-c", "copy", str(final)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        ok = proc.returncode == 0 and final.exists() and final.stat().st_size > 0
        if not ok:
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
            log.warning("mp3 concat rc=%s: %s", proc.returncode, tail)
    except Exception as exc:
        log.warning("mp3 concat failed: %s", exc)

    if ok and parts_dir.exists():
        for f in parts_dir.glob("*.mp3"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            parts_dir.rmdir()
        except OSError:
            pass

    final_dur = _probe_duration(ffprobe, final) if ok else 0.0
    wall = round(time.monotonic() - session.start_mono, 2)
    meta = {
        "room_id": session.room_id,
        "start_time": session.start_wall,
        "duration": wall,
        "audio_duration": round(final_dur, 2),
        "dual_record": len(session.workers) > 1,
        "source_breakdown": {
            "primary_A": round(cov_a, 1),
            "backup_B": round(cov_b, 1),
            "silence": round(cov_sil, 1),
        },
        # Every both-lanes-down stretch (where silence was inserted), with its
        # offset into final.mp3. Cross-check each against record.log's per-lane
        # restart timestamps to confirm both lanes were truly down.
        "gaps": gaps,
        "output": "final.mp3" if ok else "(failed)",
        "timeline_aligned": True,
    }
    (session.session_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "final: %s | dur=%.0fs (A=%.0fs B=%.0fs sil=%.0fs)",
        "final.mp3" if ok else "FAILED",
        final_dur, cov_a, cov_b, cov_sil,
    )
