"""Local Flask application for the MaoerRecorder control panel."""
from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import BaseWSGIServer, make_server

from .process_manager import RecordingManager, validate_room_id


def create_app(manager: RecordingManager, csrf_token: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        JSON_AS_ASCII=False,
        MAOER_CSRF_TOKEN=csrf_token or secrets.token_urlsafe(32),
    )

    @app.after_request
    def secure_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def protect_mutations():
        if request.path.startswith("/api/") and request.method in {"POST", "DELETE", "PUT", "PATCH"}:
            if request.headers.get("X-Maoer-Token") != app.config["MAOER_CSRF_TOKEN"]:
                return jsonify({"ok": False, "error": "请求令牌无效，请刷新控制面板"}), 403
            if request.mimetype != "application/json":
                return jsonify({"ok": False, "error": "请求格式必须为 JSON"}), 415
        return None

    @app.get("/")
    def index():
        return render_template("dashboard.html", csrf_token=app.config["MAOER_CSRF_TOKEN"])

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "service": "MaoerRecorder"})

    @app.get("/api/status")
    def status():
        return jsonify({"ok": True, **manager.snapshot()})

    @app.post("/api/tasks")
    def create_task():
        body = request.get_json(silent=True) or {}
        try:
            room_id = validate_room_id(body.get("room_id", ""))
            task = manager.start(room_id)
            return jsonify({"ok": True, "task": task}), 201
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.get("/api/tasks/<room_id>/logs")
    def task_logs(room_id: str):
        try:
            lines = int(request.args.get("lines", "180"))
            return jsonify({"ok": True, **manager.task_logs(room_id, lines)})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc.args[0])}), 404

    @app.post("/api/tasks/<room_id>/<action>")
    def task_action(room_id: str, action: str):
        try:
            if action == "start":
                task = manager.start(room_id)
            elif action == "stop":
                task = manager.request_stop(room_id)
            elif action == "force-stop":
                task = manager.force_stop(room_id)
            elif action == "restart":
                task = manager.restart(room_id)
            elif action == "remove":
                manager.remove(room_id)
                return jsonify({"ok": True})
            elif action == "open-folder":
                path = manager.open_recordings(room_id)
                return jsonify({"ok": True, "path": str(path)})
            else:
                return jsonify({"ok": False, "error": "未知操作"}), 404
            return jsonify({"ok": True, "task": task})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc.args[0])}), 404
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/recordings/open")
    def open_recordings():
        try:
            path = manager.open_recordings()
            return jsonify({"ok": True, "path": str(path)})
        except OSError as exc:
            return jsonify({"ok": False, "error": f"无法打开录制目录：{exc}"}), 500

    @app.post("/api/tasks/stop-all")
    def stop_all():
        count = manager.stop_all()
        return jsonify({"ok": True, "count": count})

    @app.errorhandler(404)
    def not_found(_: Any):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "接口不存在"}), 404
        return "Not found", 404

    @app.errorhandler(Exception)
    def unexpected_error(exc: Exception):
        app.logger.exception("dashboard request failed")
        return jsonify({"ok": False, "error": f"控制面板内部错误：{exc}"}), 500

    return app


class DashboardServer:
    """Controllable threaded WSGI server used by the tray application."""

    def __init__(self, app: Flask, host: str, port: int) -> None:
        self._server: BaseWSGIServer = make_server(host, port, app, threaded=True)
        self.host = host
        self.port = self._server.server_port
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="dashboard-http-server",
            daemon=True,
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()
