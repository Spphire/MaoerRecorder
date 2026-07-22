"""Persistent multi-room process management for the local dashboard."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

import psutil


ACTIVE_STATES = {
    "starting",
    "monitoring",
    "recording",
    "finalizing",
    "stopping",
    "restarting",
    "unresponsive",
}
ROOM_ID_RE = re.compile(r"^[1-9][0-9]{0,14}$")
FINAL_OUTPUT_FALLBACKS = ("final.m4a", "final.mp3", "final.ts")
FINAL_OUTPUT_SUFFIXES = frozenset({".m4a", ".mp3", ".ts"})


def app_dir() -> Path:
    """Return the portable application directory in source and frozen modes."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Return the read-only bundled resource directory."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root).resolve() if frozen_root else app_dir()


def resolve_recordings_dir() -> Path:
    configured = os.getenv("MAOER_BASE_DIR")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = app_dir() / path
    else:
        path = app_dir() / "recordings"
        # A build run directly from this repository should adopt the source
        # dashboard's existing tasks and recordings. Once the onedir bundle is
        # copied elsewhere it remains portable and stores data beside the EXE.
        if getattr(sys, "frozen", False):
            project_root = app_dir().parent.parent
            project_recordings = project_root / "recordings"
            if (project_root / ".git").exists() and project_recordings.exists():
                path = project_recordings
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_state_dir(recordings_dir: Path) -> Path:
    configured = os.getenv("MAOER_DASHBOARD_STATE_DIR")
    path = Path(configured).expanduser() if configured else recordings_dir / ".dashboard"
    if not path.is_absolute():
        path = app_dir() / path
    path = path.resolve()
    (path / "logs").mkdir(parents=True, exist_ok=True)
    (path / "signals").mkdir(parents=True, exist_ok=True)
    return path


def validate_room_id(value: Any) -> int:
    text = str(value).strip()
    if not ROOM_ID_RE.fullmatch(text):
        raise ValueError("房间 ID 必须是 1 到 15 位正整数")
    return int(text)


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _tail_text(path: Path, max_bytes: int = 192 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _safe_session_output(session_dir: Path, value: Any) -> Path | None:
    """Resolve a metadata output without allowing absolute or escaping paths."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name.casefold() in {"(failed)", "failed"}:
        return None

    posix_path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        return None

    try:
        root = session_dir.resolve(strict=True)
        candidate = (root / Path(name)).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        if (
            not candidate.is_file()
            or candidate.suffix.casefold() not in FINAL_OUTPUT_SUFFIXES
            or candidate.stat().st_size <= 0
        ):
            return None
    except OSError:
        return None
    return candidate


def _read_session_metadata(session_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _resolve_session_output(
    session_dir: Path, metadata: dict[str, Any] | None,
) -> Path | None:
    # An explicit output is authoritative. Invalid or failed values must not be
    # hidden by a stale legacy file left in the same session.
    if metadata is not None and "output" in metadata:
        return _safe_session_output(session_dir, metadata["output"])
    for name in FINAL_OUTPUT_FALLBACKS:
        output = _safe_session_output(session_dir, name)
        if output is not None:
            return output
    return None


def _metadata_duration(metadata: dict[str, Any] | None) -> float:
    if metadata is None:
        return 0.0
    for field in ("audio_duration", "duration"):
        raw = metadata.get(field)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


@dataclass
class RecordingTask:
    room_id: int
    instance_id: str
    status: str = "stopped"
    pid: int | None = None
    process_created_at: float | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    exit_code: int | None = None
    restart_count: int = 0
    error: str | None = None
    log_path: str = ""
    stop_path: str = ""
    status_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RecordingTask":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: raw[key] for key in allowed if key in raw})


class RecordingManager:
    """Own recorder subprocesses while preserving enough state to adopt them."""

    def __init__(
        self,
        recordings_dir: Path | None = None,
        state_dir: Path | None = None,
        command_factory: Callable[..., list[str]] | None = None,
        monitor: bool = True,
    ) -> None:
        self.recordings_dir = (recordings_dir or resolve_recordings_dir()).resolve()
        self.state_dir = (state_dir or resolve_state_dir(self.recordings_dir)).resolve()
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "signals").mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "tasks.json"
        self._command_factory = command_factory
        self._tasks: dict[int, RecordingTask] = {}
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._stats_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._load()
        self.refresh()
        self._monitor_thread: threading.Thread | None = None
        if monitor:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="recording-process-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            for item in raw.get("tasks", []):
                task = RecordingTask.from_dict(item)
                self._tasks[task.room_id] = task
        except (OSError, ValueError, TypeError):
            self._tasks = {}

    def _save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": _iso_now(),
            "tasks": [asdict(task) for task in self._tasks.values()],
        }
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.state_path)

    def _monitor_loop(self) -> None:
        while not self._closed.wait(2.0):
            self.refresh()

    def close(self) -> None:
        """Stop dashboard monitoring; recorder subprocesses intentionally remain."""
        self._closed.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=3)
        with self._lock:
            self._save()

    def _worker_command(self, room_id: int, instance_id: str) -> list[str]:
        if self._command_factory:
            return self._command_factory(room_id, instance_id)
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "--record-worker",
                "--room",
                str(room_id),
                "--managed-instance",
                instance_id,
            ]
        return [
            sys.executable,
            str(app_dir() / "main.py"),
            "record",
            "--room",
            str(room_id),
            "--managed-instance",
            instance_id,
        ]

    @staticmethod
    def _process_for(task: RecordingTask) -> psutil.Process | None:
        if not task.pid:
            return None
        try:
            process = psutil.Process(task.pid)
            if task.process_created_at is not None:
                if abs(process.create_time() - task.process_created_at) > 2.0:
                    return None
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            if task.instance_id:
                try:
                    command_line = process.cmdline()
                    if task.instance_id not in command_line:
                        return None
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    return None
            return process
        except (psutil.Error, OSError):
            return None

    def _is_alive(self, task: RecordingTask) -> bool:
        local = self._processes.get(task.room_id)
        if local is not None:
            if local.poll() is None:
                return True
            task.exit_code = local.returncode
            self._processes.pop(task.room_id, None)
        return self._process_for(task) is not None

    @staticmethod
    def _derive_live_status(task: RecordingTask) -> str:
        if task.status in {"stopping", "restarting"}:
            return task.status
        try:
            state = json.loads(Path(task.status_path).read_text(encoding="utf-8"))
            if state.get("pid") == task.pid and state.get("state") in {
                "starting",
                "monitoring",
                "recording",
                "finalizing",
                "stopping",
                "error",
            }:
                updated_at = float(state.get("updated_at") or 0)
                if (
                    int(state.get("status_protocol") or 1) >= 2
                    and state.get("state") != "finalizing"
                    and time.time() - updated_at > 45
                ):
                    return "unresponsive"
                return str(state["state"])
        except (OSError, ValueError, TypeError):
            pass
        log_text = _tail_text(Path(task.log_path))
        live_index = max(
            log_text.rfind("live started"),
            log_text.rfind("heartbeat: session=yes"),
        )
        idle_index = max(
            log_text.rfind("live ended"),
            log_text.rfind("session stop:"),
            log_text.rfind("heartbeat: session=no"),
            log_text.rfind("entering main loop"),
        )
        if live_index > idle_index:
            return "recording"
        if time.time() - _parse_iso(task.started_at) < 45:
            return "starting"
        return "monitoring"

    def refresh(self) -> None:
        changed = False
        with self._lock:
            for task in self._tasks.values():
                alive = self._is_alive(task)
                if alive:
                    next_status = self._derive_live_status(task)
                elif task.status in ACTIVE_STATES:
                    next_status = "error" if task.exit_code not in (None, 0) else "stopped"
                    task.stopped_at = task.stopped_at or _iso_now()
                    task.pid = None
                    task.process_created_at = None
                else:
                    next_status = task.status
                if task.status != next_status:
                    task.status = next_status
                    changed = True
            if changed:
                self._save()

    def start(self, room_id: int | str, *, is_restart: bool = False) -> dict[str, Any]:
        room_id = validate_room_id(room_id)
        with self._lock:
            existing = self._tasks.get(room_id)
            if existing and self._is_alive(existing):
                raise RuntimeError("该房间已有运行中的录制进程")

            instance_id = uuid.uuid4().hex
            log_path = self.state_dir / "logs" / f"room-{room_id}.log"
            stop_path = self.state_dir / "signals" / f"{room_id}-{instance_id}.stop"
            status_path = self.state_dir / "signals" / f"{room_id}-{instance_id}.json"
            try:
                stop_path.unlink()
            except OSError:
                pass

            task = existing or RecordingTask(room_id=room_id, instance_id=instance_id)
            task.instance_id = instance_id
            task.status = "starting"
            task.pid = None
            task.process_created_at = None
            task.started_at = _iso_now()
            task.stopped_at = None
            task.exit_code = None
            task.error = None
            task.log_path = str(log_path)
            task.stop_path = str(stop_path)
            task.status_path = str(status_path)
            if is_restart:
                task.restart_count += 1

            env = os.environ.copy()
            env.update(
                {
                    "MAOER_BASE_DIR": str(self.recordings_dir),
                    "MAOER_STOP_FILE": str(stop_path),
                    "MAOER_STATUS_FILE": str(status_path),
                    "MAOER_MANAGED_ROOM_ID": str(room_id),
                    "MAOER_LOG_FILE": str(log_path),
                    "PYTHONUTF8": "1",
                    "PYTHONUNBUFFERED": "1",
                }
            )
            creationflags = 0
            popen_kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True

            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with log_path.open("ab", buffering=0) as log_handle:
                    marker = (
                        f"\n=== dashboard launch {task.started_at} "
                        f"instance={instance_id} ===\n"
                    )
                    log_handle.write(marker.encode("utf-8"))
                    process = subprocess.Popen(
                        self._worker_command(room_id, instance_id),
                        cwd=str(app_dir()),
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                        close_fds=True,
                        **popen_kwargs,
                    )
                task.pid = process.pid
                try:
                    task.process_created_at = psutil.Process(process.pid).create_time()
                except psutil.Error:
                    task.process_created_at = time.time()
                self._processes[room_id] = process
                self._tasks[room_id] = task
                self._save()
            except Exception as exc:
                task.status = "error"
                task.error = str(exc)
                task.stopped_at = _iso_now()
                self._tasks[room_id] = task
                self._save()
                raise RuntimeError(f"无法启动录制进程：{exc}") from exc
            return self.task_detail(room_id)

    def request_stop(self, room_id: int | str) -> dict[str, Any]:
        room_id = validate_room_id(room_id)
        with self._lock:
            task = self._require_task(room_id)
            if not self._is_alive(task):
                task.status = "stopped"
                task.pid = None
                self._save()
                return self.task_detail(room_id)
            Path(task.stop_path).parent.mkdir(parents=True, exist_ok=True)
            Path(task.stop_path).touch()
            task.status = "stopping"
            self._save()
            return self.task_detail(room_id)

    def force_stop(self, room_id: int | str) -> dict[str, Any]:
        room_id = validate_room_id(room_id)
        with self._lock:
            task = self._require_task(room_id)
            process = self._process_for(task)
            if process is not None:
                targets: list[psutil.Process] = []
                try:
                    targets.extend(process.children(recursive=True))
                except psutil.Error:
                    pass
                targets.append(process)
                for target in targets:
                    try:
                        target.terminate()
                    except psutil.Error:
                        pass
                _, alive = psutil.wait_procs(targets, timeout=3)
                for target in alive:
                    try:
                        target.kill()
                    except psutil.Error:
                        pass
            local = self._processes.pop(room_id, None)
            if local is not None:
                try:
                    local.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                task.exit_code = local.returncode
            task.status = "stopped"
            task.pid = None
            task.process_created_at = None
            task.stopped_at = _iso_now()
            self._save()
            return self.task_detail(room_id)

    def restart(self, room_id: int | str) -> dict[str, Any]:
        room_id = validate_room_id(room_id)
        with self._lock:
            task = self._require_task(room_id)
            if not self._is_alive(task):
                return self.start(room_id, is_restart=True)
            Path(task.stop_path).touch()
            task.status = "restarting"
            self._save()
            threading.Thread(
                target=self._finish_restart,
                args=(room_id, task.instance_id),
                name=f"restart-room-{room_id}",
                daemon=True,
            ).start()
            return self.task_detail(room_id)

    def _finish_restart(self, room_id: int, instance_id: str) -> None:
        deadline = time.time() + 300
        while time.time() < deadline and not self._closed.wait(1):
            with self._lock:
                task = self._tasks.get(room_id)
                if task is None or task.instance_id != instance_id:
                    return
                if not self._is_alive(task):
                    break
        if self._closed.is_set():
            return
        if time.time() >= deadline:
            try:
                self.force_stop(room_id)
            except (KeyError, ValueError):
                return
        if self._closed.is_set():
            return
        try:
            self.start(room_id, is_restart=True)
        except RuntimeError as exc:
            with self._lock:
                task = self._tasks.get(room_id)
                if task:
                    task.status = "error"
                    task.error = str(exc)
                    self._save()

    def remove(self, room_id: int | str) -> None:
        room_id = validate_room_id(room_id)
        with self._lock:
            task = self._require_task(room_id)
            if self._is_alive(task):
                raise RuntimeError("请先停止该录制进程")
            self._tasks.pop(room_id, None)
            self._processes.pop(room_id, None)
            self._save()

    def stop_all(self) -> int:
        count = 0
        with self._lock:
            room_ids = list(self._tasks)
        for room_id in room_ids:
            try:
                with self._lock:
                    task = self._tasks[room_id]
                    alive = self._is_alive(task)
                if alive:
                    self.request_stop(room_id)
                    count += 1
            except (KeyError, RuntimeError, ValueError):
                continue
        return count

    def open_recordings(self, room_id: int | str | None = None) -> Path:
        target = self.recordings_dir
        if room_id is not None:
            rid = validate_room_id(room_id)
            matches = sorted(self.recordings_dir.glob(f"{rid}_*"))
            if matches:
                target = matches[0]
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            opener = shutil.which("xdg-open")
            if opener:
                subprocess.Popen([opener, str(target)])
        return target

    def _require_task(self, room_id: int) -> RecordingTask:
        try:
            return self._tasks[room_id]
        except KeyError as exc:
            raise KeyError("未找到该录制任务") from exc

    def task_logs(self, room_id: int | str, lines: int = 180) -> dict[str, Any]:
        room_id = validate_room_id(room_id)
        with self._lock:
            task = self._require_task(room_id)
            text = _tail_text(Path(task.log_path), max_bytes=512 * 1024)
        selected = text.splitlines()[-max(1, min(lines, 1000)) :]
        return {"room_id": room_id, "lines": selected, "path": task.log_path}

    def _room_recording_stats(self, room_id: int) -> dict[str, Any]:
        cached = self._stats_cache.get(room_id)
        if cached and time.monotonic() - cached[0] < 10.0:
            return dict(cached[1])
        room_dirs = [path for path in self.recordings_dir.glob(f"{room_id}_*") if path.is_dir()]
        creator = None
        creator_updated_at = 0.0
        sessions = 0
        finalized = 0
        total_bytes = 0
        duration = 0.0
        last_activity = 0.0
        for room_dir in room_dirs:
            prefix = f"{room_id}_"
            if room_dir.name.startswith(prefix):
                try:
                    room_updated_at = room_dir.stat().st_mtime
                except OSError:
                    room_updated_at = 0.0
                if room_updated_at >= creator_updated_at:
                    creator = room_dir.name[len(prefix) :] or creator
                    creator_updated_at = room_updated_at
            for session_dir in room_dir.iterdir():
                if not session_dir.is_dir() or not re.fullmatch(r"20\d{6}_\d{6}", session_dir.name):
                    continue
                sessions += 1
                try:
                    last_activity = max(last_activity, session_dir.stat().st_mtime)
                except OSError:
                    pass
                metadata = _read_session_metadata(session_dir)
                if _resolve_session_output(session_dir, metadata) is not None:
                    finalized += 1
                    duration += _metadata_duration(metadata)
                try:
                    for item in session_dir.rglob("*"):
                        if item.is_file():
                            total_bytes += item.stat().st_size
                except OSError:
                    pass
        result = {
            "creator": creator,
            "sessions": sessions,
            "finalized": finalized,
            "bytes": total_bytes,
            "duration_seconds": round(duration, 2),
            "last_activity": (
                datetime.fromtimestamp(last_activity).astimezone().isoformat(timespec="seconds")
                if last_activity
                else None
            ),
        }
        self._stats_cache[room_id] = (time.monotonic(), result)
        return dict(result)

    def task_detail(self, room_id: int | str) -> dict[str, Any]:
        room_id = validate_room_id(room_id)
        with self._lock:
            task = self._require_task(room_id)
            data = asdict(task)
            data["alive"] = self._is_alive(task)
            try:
                runtime = json.loads(Path(task.status_path).read_text(encoding="utf-8"))
                data["runtime"] = runtime if runtime.get("pid") == task.pid else {}
            except (OSError, ValueError, TypeError):
                data["runtime"] = {}
        runtime = data["runtime"]
        process = self._process_for(task) if data["alive"] else None
        if process is not None:
            try:
                children = process.children(recursive=True)
                runtime["lanes_alive"] = sum(
                    1 for child in children if child.name().lower() == "ffmpeg.exe"
                )
            except psutil.Error:
                pass

        session_dir = runtime.get("session_dir")
        if session_dir:
            try:
                session_path = Path(str(session_dir)).resolve()
                session_path.relative_to(self.recordings_dir)
                audio_files = list(session_path.glob("audio_*.ts"))
                if audio_files:
                    stats = [item.stat() for item in audio_files]
                    runtime["last_audio_write"] = max(item.st_mtime for item in stats)
                    runtime["session_bytes"] = sum(item.st_size for item in stats)
            except (OSError, ValueError):
                pass

        if data["status"] == "recording":
            lanes = int(runtime.get("lanes") or 0)
            lanes_alive = int(runtime.get("lanes_alive") or 0)
            last_write = float(runtime.get("last_audio_write") or 0)
            state_age = time.time() - float(runtime.get("updated_at") or time.time())
            if lanes and 0 < lanes_alive < lanes:
                data["status"] = "degraded"
            elif lanes and lanes_alive == 0 and state_age > 15 and time.time() - last_write > 30:
                data["status"] = "unresponsive"

        recordings = self._room_recording_stats(room_id)
        if not recordings.get("creator") and runtime.get("creator"):
            recordings["creator"] = str(runtime["creator"])
        data["recordings"] = recordings
        return data

    def snapshot(self) -> dict[str, Any]:
        self.refresh()
        with self._lock:
            room_ids = sorted(self._tasks)
        tasks = [self.task_detail(room_id) for room_id in room_ids]
        known_ids = set(room_ids)
        # Historical recordings remain visible even if their old task was
        # removed from the dashboard registry.
        for room_dir in self.recordings_dir.iterdir():
            if not room_dir.is_dir():
                continue
            match = re.match(r"^([1-9][0-9]{0,14})_", room_dir.name)
            if match:
                known_ids.add(int(match.group(1)))
        history = [self._room_recording_stats(room_id) | {"room_id": room_id} for room_id in sorted(known_ids)]
        presets_by_room = {item["room_id"]: dict(item) for item in history}
        for task in tasks:
            stats = task["recordings"]
            if stats.get("creator"):
                existing = presets_by_room.get(task["room_id"])
                if not existing or not existing.get("creator"):
                    presets_by_room[task["room_id"]] = stats | {"room_id": task["room_id"]}
        presets = [presets_by_room[room_id] for room_id in sorted(presets_by_room)]
        total_sessions = sum(item["sessions"] for item in history)
        total_bytes = sum(item["bytes"] for item in history)
        try:
            disk = shutil.disk_usage(self.recordings_dir)
            disk_free = disk.free
            disk_total = disk.total
        except OSError:
            disk_free = 0
            disk_total = 0
        return {
            "tasks": tasks,
            "history": history,
            "presets": presets,
            "summary": {
                "processes": len(tasks),
                "active": sum(1 for item in tasks if item["alive"]),
                "recording": sum(1 for item in tasks if item["status"] in {"recording", "degraded"}),
                "sessions": total_sessions,
                "bytes": total_bytes,
                "disk_free": disk_free,
                "disk_total": disk_total,
            },
            "recordings_dir": str(self.recordings_dir),
            "updated_at": _iso_now(),
        }
