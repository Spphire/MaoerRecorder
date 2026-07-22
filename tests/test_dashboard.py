from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from maoer import config
from maoer.dashboard import create_app
from maoer.process_manager import RecordingManager, resolve_recordings_dir, validate_room_id


FAKE_WORKER = r"""
import json
import os
import sys
import time
from pathlib import Path

instance = sys.argv[1]
stop_path = Path(os.environ['MAOER_STOP_FILE'])
status_path = Path(os.environ['MAOER_STATUS_FILE'])
payload = {
    'state': 'monitoring',
    'room_id': int(os.environ['MAOER_MANAGED_ROOM_ID']),
    'pid': os.getpid(),
    'updated_at': time.time(),
    'instance': instance,
}
status_path.write_text(json.dumps(payload), encoding='utf-8')
while not stop_path.exists():
    time.sleep(0.05)
payload['state'] = 'stopped'
payload['updated_at'] = time.time()
status_path.write_text(json.dumps(payload), encoding='utf-8')
"""


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


@pytest.fixture
def manager(tmp_path: Path):
    recordings = tmp_path / "recordings"
    state = tmp_path / "state"

    def command(_room: int, instance: str) -> list[str]:
        return [sys.executable, "-c", FAKE_WORKER, instance]

    value = RecordingManager(recordings, state, command_factory=command, monitor=False)
    yield value
    for task in value.snapshot()["tasks"]:
        if task["alive"]:
            value.force_stop(task["room_id"])
    value.close()


def test_room_id_validation() -> None:
    assert validate_room_id("868802213") == 868802213
    for invalid in ("", "0", "-1", "12.3", "1 && calc", "1" * 16):
        with pytest.raises(ValueError):
            validate_room_id(invalid)


def test_two_rooms_stop_independently(manager: RecordingManager) -> None:
    first = manager.start(10001)
    second = manager.start(10002)
    wait_for(lambda: (manager.refresh() or True) and manager.task_detail(10001)["status"] == "monitoring")
    wait_for(lambda: (manager.refresh() or True) and manager.task_detail(10002)["status"] == "monitoring")

    manager.request_stop(10001)
    wait_for(lambda: (manager.refresh() or True) and not manager.task_detail(10001)["alive"])

    assert manager.task_detail(10002)["alive"] is True
    assert first["stop_path"] != second["stop_path"]


def test_registry_adopts_verified_worker(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    state = tmp_path / "state"

    def command(_room: int, instance: str) -> list[str]:
        return [sys.executable, "-c", FAKE_WORKER, instance]

    first = RecordingManager(recordings, state, command_factory=command, monitor=False)
    first.start(20001)
    wait_for(lambda: (first.refresh() or True) and first.task_detail(20001)["status"] == "monitoring")
    first.close()

    second = RecordingManager(recordings, state, command_factory=command, monitor=False)
    try:
        assert second.task_detail(20001)["alive"] is True
        second.request_stop(20001)
        wait_for(lambda: (second.refresh() or True) and not second.task_detail(20001)["alive"])
    finally:
        if second.task_detail(20001)["alive"]:
            second.force_stop(20001)
        second.close()


def test_api_requires_token_and_validates_room(manager: RecordingManager) -> None:
    app = create_app(manager, csrf_token="test-token")
    client = app.test_client()

    assert client.get("/api/health").status_code == 200
    assert client.post("/api/tasks", json={"room_id": "30001"}).status_code == 403

    headers = {"X-Maoer-Token": "test-token"}
    invalid = client.post("/api/tasks", json={"room_id": "bad"}, headers=headers)
    assert invalid.status_code == 400

    created = client.post("/api/tasks", json={"room_id": "30001"}, headers=headers)
    assert created.status_code == 201
    assert created.get_json()["task"]["room_id"] == 30001

    duplicate = client.post("/api/tasks", json={"room_id": "30001"}, headers=headers)
    assert duplicate.status_code == 409


def test_recording_stats_resolve_mixed_final_outputs_safely(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    room = recordings / "40001_主播"
    m4a = room / "20260701_010203"
    legacy_mp3 = room / "20260702_010203"
    no_meta_m4a = room / "20260703_010203"
    no_meta_ts = room / "20260704_010203"
    raw_only = room / "20260705_010203"
    failed = room / "20260706_010203"
    escaped = room / "20260707_010203"
    absolute = room / "20260708_010203"
    missing_explicit = room / "20260709_010203"
    non_audio = room / "20260710_010203"
    empty_output = room / "20260711_010203"
    sessions = (
        m4a,
        legacy_mp3,
        no_meta_m4a,
        no_meta_ts,
        raw_only,
        failed,
        escaped,
        absolute,
        missing_explicit,
        non_audio,
        empty_output,
    )
    for session in sessions:
        session.mkdir(parents=True)

    (m4a / "final.m4a").write_bytes(b"m4a")
    (m4a / "meta.json").write_text(
        json.dumps({"output": "final.m4a", "audio_duration": 125.5}),
        encoding="utf-8",
    )

    (legacy_mp3 / "final.mp3").write_bytes(b"mp3")
    (legacy_mp3 / "meta.json").write_text(
        json.dumps({"duration": "60.25"}), encoding="utf-8"
    )
    (no_meta_m4a / "final.m4a").write_bytes(b"orphan-m4a")
    (no_meta_ts / "final.ts").write_bytes(b"legacy-ts")
    (raw_only / "audio_A_0001.ts").write_bytes(b"raw")

    # Explicit failure or invalid output values remain unfinalized even when a
    # fallback-looking file exists in the session.
    (failed / "final.mp3").write_bytes(b"stale")
    (failed / "meta.json").write_text(
        json.dumps({"output": "(failed)", "audio_duration": 999}), encoding="utf-8"
    )

    outside = room / "outside.mp3"
    outside.write_bytes(b"outside")
    (escaped / "meta.json").write_text(
        json.dumps({"output": "../outside.mp3", "audio_duration": 999}),
        encoding="utf-8",
    )

    absolute_output = tmp_path / "absolute.mp3"
    absolute_output.write_bytes(b"absolute")
    (absolute / "meta.json").write_text(
        json.dumps({"output": str(absolute_output), "audio_duration": 999}),
        encoding="utf-8",
    )

    (missing_explicit / "final.mp3").write_bytes(b"stale-legacy")
    (missing_explicit / "meta.json").write_text(
        json.dumps({"output": "final.m4a", "audio_duration": 999}), encoding="utf-8"
    )

    (non_audio / "payload.json").write_text("{}", encoding="utf-8")
    (non_audio / "meta.json").write_text(
        json.dumps({"output": "payload.json", "audio_duration": 999}), encoding="utf-8"
    )

    (empty_output / "final.m4a").write_bytes(b"")
    (empty_output / "meta.json").write_text(
        json.dumps({"output": "final.m4a", "audio_duration": 999}), encoding="utf-8"
    )

    value = RecordingManager(recordings, tmp_path / "state", monitor=False)
    try:
        history = value.snapshot()["history"]
        assert history[0]["sessions"] == len(sessions)
        assert history[0]["finalized"] == 4
        assert history[0]["creator"] == "主播"
        assert value.snapshot()["presets"][0]["creator"] == "主播"
        assert value.snapshot()["summary"]["disk_free"] > 0
        assert history[0]["duration_seconds"] == 185.75
        expected_bytes = sum(
            item.stat().st_size
            for session in sessions
            for item in session.rglob("*")
            if item.is_file()
        )
        assert history[0]["bytes"] == expected_bytes
    finally:
        value.close()


def test_latest_room_directory_supplies_description(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    old_room = recordings / "50001_旧描述"
    new_room = recordings / "50001_新描述"
    (old_room / "20260701_010203").mkdir(parents=True)
    time.sleep(0.02)
    (new_room / "20260702_010203").mkdir(parents=True)

    value = RecordingManager(recordings, tmp_path / "state", monitor=False)
    try:
        assert value.snapshot()["history"][0]["creator"] == "新描述"
    finally:
        value.close()


def test_in_repo_frozen_build_reuses_project_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    executable = project / "dist" / "MaoerRecorder" / "MaoerRecorder.exe"
    executable.parent.mkdir(parents=True)
    (project / ".git").mkdir()
    expected = project / "recordings"
    expected.mkdir()

    monkeypatch.delenv("MAOER_BASE_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert resolve_recordings_dir() == expected.resolve()


def test_frozen_build_prefers_bundled_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundled = bundle / "vendor" / "ffmpeg" / "ffmpeg.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"bundled")
    system = tmp_path / "system" / "ffmpeg.exe"
    system.parent.mkdir()
    system.write_bytes(b"system")

    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(config.shutil, "which", lambda _name: str(system))

    assert config._resolve_ffmpeg() == str(bundled)
