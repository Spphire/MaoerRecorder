"""One-click dashboard and hidden recorder-worker entry point."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
import webbrowser
from datetime import datetime


def _setup_dashboard_log() -> None:
    from maoer.process_manager import resolve_recordings_dir, resolve_state_dir

    path = resolve_state_dir(resolve_recordings_dir()) / "dashboard.log"
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def _existing_dashboard_url(host: str, port: int) -> str | None:
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(f"{url}api/health", timeout=0.6) as response:
            if response.status == 200 and b"MaoerRecorder" in response.read():
                return url
    except Exception:
        return None
    return None


def _run_worker(room: int, instance: str | None) -> int:
    from main import main as recorder_main

    args = ["record", "--room", str(room)]
    if instance:
        args.extend(["--managed-instance", instance])
    return recorder_main(args)


def _run_self_test() -> int:
    from playwright.sync_api import sync_playwright

    from maoer.config import _resolve_ffmpeg
    from maoer.process_manager import resolve_recordings_dir, resolve_state_dir

    report = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "frozen": bool(getattr(sys, "frozen", False)),
        "ffmpeg": False,
        "ffprobe": False,
        "aac_m4a": False,
        "browser": False,
        "errors": [],
    }
    ffmpeg = os.path.abspath(_resolve_ffmpeg())
    ffprobe_name = "ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe"
    ffprobe = os.path.join(os.path.dirname(ffmpeg), ffprobe_name)
    for name, executable in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        try:
            result = subprocess.run(
                [executable, "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            report[name] = result.returncode == 0
            if result.returncode != 0:
                report["errors"].append(f"{name} exited with {result.returncode}")
        except Exception as exc:
            report["errors"].append(f"{name}: {exc}")
    if report["ffmpeg"] and report["ffprobe"]:
        try:
            with tempfile.TemporaryDirectory(prefix="maoer-self-test-") as temp_dir:
                adts_path = os.path.join(temp_dir, "sample.aac")
                sample_path = os.path.join(temp_dir, "sample.m4a")
                encode = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel", "error",
                        "-nostdin",
                        "-y",
                        "-f", "lavfi",
                        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-t", "0.05",
                        "-map", "0:a:0",
                        "-c:a", "aac",
                        "-profile:a", "aac_low",
                        "-b:a", "128k",
                        "-ar", "48000",
                        "-ac", "2",
                        "-f", "adts",
                        adts_path,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
                if encode.returncode != 0:
                    detail = (
                        (encode.stderr or b"").decode("utf-8", "replace").strip()[-400:]
                    )
                    raise RuntimeError(f"ffmpeg exited with {encode.returncode}: {detail}")

                mux = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel", "error",
                        "-nostdin",
                        "-y",
                        "-f", "aac",
                        "-i", adts_path,
                        "-map", "0:a:0",
                        "-c:a", "copy",
                        "-bsf:a", "aac_adtstoasc",
                        "-movflags", "+faststart",
                        "-f", "ipod",
                        sample_path,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
                if mux.returncode != 0:
                    detail = (
                        (mux.stderr or b"").decode("utf-8", "replace").strip()[-400:]
                    )
                    raise RuntimeError(f"M4A mux exited with {mux.returncode}: {detail}")

                probe = subprocess.run(
                    [
                        ffprobe,
                        "-v", "error",
                        "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name,profile,sample_rate,channels",
                        "-of", "json",
                        sample_path,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
                if probe.returncode != 0:
                    detail = (
                        (probe.stderr or b"").decode("utf-8", "replace").strip()[-400:]
                    )
                    raise RuntimeError(f"ffprobe exited with {probe.returncode}: {detail}")
                streams = json.loads(
                    (probe.stdout or b"").decode("utf-8", "replace")
                ).get("streams") or []
                if not streams:
                    raise RuntimeError("ffprobe returned no audio stream")
                stream = streams[0]
                actual = (
                    stream.get("codec_name"),
                    stream.get("profile"),
                    int(stream.get("sample_rate") or 0),
                    int(stream.get("channels") or 0),
                )
                expected = ("aac", "LC", 48000, 2)
                if actual != expected:
                    raise RuntimeError(
                        f"unexpected audio stream {actual!r}, expected {expected!r}"
                    )
                report["aac_m4a"] = True
        except Exception as exc:
            report["errors"].append(f"aac_m4a: {exc}")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("about:blank")
            browser.close()
        report["browser"] = True
    except Exception as exc:
        report["errors"].append(f"browser: {exc}")

    report_path = resolve_state_dir(resolve_recordings_dir()) / "self-test.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(
        report[key] for key in ("ffmpeg", "ffprobe", "aac_m4a", "browser")
    ) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MaoerRecorder 控制面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("MAOER_DASHBOARD_PORT", "8765")))
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--record-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--room", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--managed-instance", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.record_worker:
        if not args.room:
            parser.error("worker mode requires --room")
        return _run_worker(args.room, args.managed_instance)
    if args.self_test:
        return _run_self_test()

    existing = _existing_dashboard_url(args.host, args.port)
    if existing:
        if not args.no_browser:
            webbrowser.open(existing)
        return 0

    from maoer.dashboard import DashboardServer, create_app
    from maoer.process_manager import RecordingManager

    _setup_dashboard_log()
    manager = RecordingManager()
    try:
        server = DashboardServer(create_app(manager), args.host, args.port)
    except OSError as exc:
        print(f"无法启动控制面板：{exc}", file=sys.stderr)
        manager.close()
        return 1
    server.start()

    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(server.url)).start()

    closed = threading.Event()

    def shutdown() -> None:
        if closed.is_set():
            return
        closed.set()
        server.stop()
        manager.close()

    if sys.platform == "win32" and not args.no_tray:
        try:
            from maoer.tray import run_tray

            run_tray(server.url, manager.recordings_dir, manager.stop_all, shutdown)
        except Exception as exc:
            print(f"托盘图标启动失败：{exc}", file=sys.stderr)
            try:
                while not closed.wait(1):
                    pass
            except KeyboardInterrupt:
                shutdown()
    else:
        print(f"MaoerRecorder dashboard: {server.url}")
        try:
            while not closed.wait(1):
                pass
        except KeyboardInterrupt:
            shutdown()
    return 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
