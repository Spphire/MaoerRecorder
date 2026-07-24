from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import backfill_recordings


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
REQUIRES_FFMPEG = pytest.mark.skipif(
    not FFMPEG or not FFPROBE, reason="ffmpeg tools are required"
)


def _write_segments(session: Path, rows: list[dict]) -> None:
    (session / "segments.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _make_ts(path: Path, frequency: int) -> None:
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration=2.2",
            "-map",
            "0:a:0",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "96k",
            "-f",
            "mpegts",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def test_infer_legacy_timeline_and_offline_boundary() -> None:
    meta = {
        "duration": 16963.43,
        "source_breakdown": {
            "primary_A": 16590.0,
            "backup_B": 37.9,
            "silence": 173.2,
        },
        "gaps": [{"at": 16639.4, "dur": 161.6}],
    }

    end = backfill_recordings.infer_timeline_end(meta)

    assert end == pytest.approx(16801.1)
    assert backfill_recordings.infer_offline_boundary(meta, end) == 16639.4


def test_unfinished_stages_are_reported(tmp_path: Path) -> None:
    base = tmp_path / "recordings"
    leftover = base / ".dashboard" / "historical-migration-work" / "session-1"
    leftover.mkdir(parents=True)

    assert backfill_recordings.unfinished_stages(base) == [leftover.resolve()]


def test_finalize_failure_keeps_original_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = tmp_path / "recordings"
    session = base / "123_creator" / "20260701_010203"
    session.mkdir(parents=True)
    source = session / "audio_A_0001.ts"
    source.write_bytes(b"original-ts")
    _write_segments(
        session,
        [
            {
                "worker": "A",
                "index": 1,
                "file": source.name,
                "t_start": 0.0,
            }
        ],
    )
    original_meta = {"room_id": 123, "duration": 1.0, "output": "final.mp3"}
    (session / "meta.json").write_text(
        json.dumps(original_meta), encoding="utf-8"
    )
    monkeypatch.setattr(
        backfill_recordings.recorder,
        "finalize",
        lambda _session: (_ for _ in ()).throw(RuntimeError("injected failure")),
    )

    with pytest.raises(RuntimeError, match="injected failure"):
        backfill_recordings.migrate_session(base, session, ffmpeg="ffmpeg")

    assert source.read_bytes() == b"original-ts"
    assert json.loads((session / "meta.json").read_text(encoding="utf-8")) == original_meta
    assert not (session / "final.m4a").exists()
    assert not (session / "source_merged.ts").exists()
    assert not (base / ".dashboard" / "historical-migration-work").exists()


@REQUIRES_FFMPEG
def test_migrate_session_publishes_verified_pair_and_removes_legacy_files(
    tmp_path: Path,
) -> None:
    base = tmp_path / "recordings"
    session = base / "123_creator" / "20260701_010203"
    session.mkdir(parents=True)
    source_a = session / "audio_A_0001.ts"
    source_b = session / "audio_B_0001.ts"
    _make_ts(source_a, 440)
    _make_ts(source_b, 660)
    _write_segments(
        session,
        [
            {"worker": "A", "index": 1, "file": source_a.name, "t_start": 0.0},
            {"worker": "B", "index": 1, "file": source_b.name, "t_start": 0.0},
        ],
    )
    meta = {
        "room_id": 123,
        "start_time": 123.0,
        "duration": 14.2,
        "audio_duration": 14.2,
        "source_breakdown": {
            "primary_A": 2.2,
            "backup_B": 0.0,
            "silence": 12.0,
        },
        "gaps": [{"at": 2.2, "dur": 12.0, "trailing": True}],
        "output": "final.mp3",
        "timeline_aligned": True,
    }
    (session / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (session / "final.mp3").write_bytes(b"legacy-derivative")

    result = backfill_recordings.migrate_session(
        base,
        session,
        ffmpeg=str(FFMPEG),
    )

    assert result["raw_files_removed"] == 2
    assert not source_a.exists()
    assert not source_b.exists()
    assert not (session / "final.mp3").exists()
    assert (session / "final.m4a").is_file()
    assert (session / "source_merged.ts").is_file()
    migrated = json.loads((session / "meta.json").read_text(encoding="utf-8"))
    assert migrated["processing_mode"] == "passthrough"
    assert migrated["trailing_trim"]["applied"] is True
    assert migrated["trailing_trim"]["removed_seconds"] > 9.0
    assert migrated["archive"]["raw_cleanup_complete"] is True
    assert migrated["historical_migration"]["status"] == "complete"
    assert migrated["historical_migration"]["legacy_mp3_removed"] is True
    assert not (base / ".dashboard" / "historical-migration-work").exists()
