from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from maoer import config, recorder


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
REQUIRES_FFMPEG = pytest.mark.skipif(
    not FFMPEG or not FFPROBE, reason="ffmpeg tools are required"
)


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _make_aac_ts(
    path: Path,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
    duration: float = 2.2,
    frequency: int = 440,
) -> None:
    _run(
        [
            str(FFMPEG),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate={sample_rate}:duration={duration}",
            "-map",
            "0:a:0",
            "-ac",
            str(channels),
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "96k",
            "-f",
            "mpegts",
            str(path),
        ]
    )


def _probe_audio(path: Path) -> tuple[dict, dict]:
    result = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,profile,sample_rate,channels,bit_rate",
            "-show_entries",
            "format=format_name,duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    for key in ("sample_rate", "channels", "bit_rate"):
        if stream.get(key) is not None:
            stream[key] = int(stream[key])
    return stream, payload["format"]


def _assert_fully_decodable(path: Path) -> None:
    result = subprocess.run(
        [
            str(FFMPEG),
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def _remux_to_adts(source: Path, out: Path, *, frames: int | None = None) -> None:
    command = [
        str(FFMPEG),
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-c:a",
        "copy",
    ]
    if frames is not None:
        command += ["-frames:a", str(frames)]
    command += ["-f", "adts", str(out)]
    _run(command)


def _session(tmp_path: Path, source: Path, *, label: str = "A") -> SimpleNamespace:
    worker = SimpleNamespace(
        label=label,
        segments={1: {"file": source.name, "t_start": 0.0}},
    )
    return SimpleNamespace(
        segment_index=1,
        cfg=SimpleNamespace(
            ffmpeg_path=str(FFMPEG),
            archive_merged_ts=True,
            delete_raw_segments_after_archive=True,
            trim_trailing_silence=True,
            trailing_trim_min_seconds=10.0,
            trailing_silence_keep_seconds=2.0,
        ),
        session_dir=tmp_path,
        workers=[worker],
        end_offset=1.0,
        stop_reason="live ended",
        live_offline_offset=None,
        start_mono=time.monotonic() - 99.0,
        start_wall=123.0,
        room_id=456,
    )


def _long_source_run(source: Path) -> list[dict]:
    return [
        {
            "path": source,
            "t_start": 0.0,
            "t_end": 2.0,
            "dur": 2.0,
            "source_start": 0.0,
        }
    ]


def test_finalize_storage_config_defaults_and_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAOER_BASE_DIR", str(tmp_path / "recordings"))
    monkeypatch.setattr(config, "_resolve_ffmpeg", lambda: "ffmpeg")
    names = (
        "MAOER_MEDIA_DRAIN",
        "MAOER_TRIM_TRAILING_SILENCE",
        "MAOER_TRAILING_TRIM_MIN",
        "MAOER_TRAILING_SILENCE_KEEP",
        "MAOER_ARCHIVE_MERGED_TS",
        "MAOER_DELETE_RAW_SEGMENTS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    defaults = config.load(123)
    assert defaults.media_drain_seconds == 120
    assert defaults.trim_trailing_silence is True
    assert defaults.trailing_trim_min_seconds == 10.0
    assert defaults.trailing_silence_keep_seconds == 2.0
    assert defaults.archive_merged_ts is True
    assert defaults.delete_raw_segments_after_archive is True

    monkeypatch.setenv("MAOER_MEDIA_DRAIN", "45")
    monkeypatch.setenv("MAOER_TRIM_TRAILING_SILENCE", "0")
    monkeypatch.setenv("MAOER_TRAILING_TRIM_MIN", "20.5")
    monkeypatch.setenv("MAOER_TRAILING_SILENCE_KEEP", "3.5")
    monkeypatch.setenv("MAOER_ARCHIVE_MERGED_TS", "0")
    monkeypatch.setenv("MAOER_DELETE_RAW_SEGMENTS", "0")

    overridden = config.load(123)
    assert overridden.media_drain_seconds == 45
    assert overridden.trim_trailing_silence is False
    assert overridden.trailing_trim_min_seconds == 20.5
    assert overridden.trailing_silence_keep_seconds == 3.5
    assert overridden.archive_merged_ts is False
    assert overridden.delete_raw_segments_after_archive is False


@REQUIRES_FFMPEG
def test_finalize_can_disable_transcode_fallback(tmp_path: Path) -> None:
    source = tmp_path / "audio_A_0001.ts"
    _make_aac_ts(source, sample_rate=44100, duration=1.0)
    session = _session(tmp_path, source)
    session.cfg.allow_transcode_fallback = False

    recorder.finalize(session)

    assert source.exists()
    assert not (tmp_path / "final.m4a").exists()
    assert not (tmp_path / "source_merged.ts").exists()
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["output"] == "(failed)"
    assert meta["archive"]["raw_cleanup_complete"] is False


def test_capture_worker_uses_audio_stream_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_popen(command: list[str], **_kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = _kwargs
        return SimpleNamespace(stderr=None)

    class NoopThread:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(recorder.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(recorder.threading, "Thread", NoopThread)
    pool = SimpleNamespace(
        acquire=lambda _label, kind: recorder.StreamCreds(
            cookie_header="", url="https://example.invalid/live", kind=kind
        )
    )
    cfg = SimpleNamespace(ffmpeg_path="ffmpeg", user_agent="test-agent")
    worker = recorder.FfmpegWorker(
        cfg,
        pool,
        tmp_path,
        room_id=123,
        label="A",
        start_mono=time.monotonic(),
        seg_log_path=tmp_path / "segments.jsonl",
        seg_log_lock=recorder.threading.Lock(),
    )

    worker._launch_segment()

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("-map") + 1] == "0:a:0"
    assert command[command.index("-c:a") + 1] == "copy"
    assert "-vn" in command
    assert "-sn" in command
    assert "-dn" in command
    assert "-b:a" not in command
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["creationflags"] == recorder._NO_WINDOW_CREATIONFLAGS


def test_media_helpers_run_without_windows_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"1.0\n")

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)

    assert recorder._probe_duration("ffprobe", tmp_path / "source.ts") == 1.0
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["creationflags"] == recorder._NO_WINDOW_CREATIONFLAGS


@REQUIRES_FFMPEG
def test_aac_silence_has_exact_frame_count(tmp_path: Path) -> None:
    frames = 17
    out = tmp_path / "silence.aac"

    info = recorder._make_aac_silence(str(FFMPEG), out, frames)

    assert info is not None
    assert info.frames == frames
    assert info.object_type == 2
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert recorder._scan_adts(out) == info
    assert not out.with_name("silence.encoded.aac").exists()
    _assert_fully_decodable(out)


def test_quantize_plan_rounds_on_one_cumulative_grid() -> None:
    durations = [0.01] * 1000
    plan = [
        ("silence", duration)
        if index % 2 == 0
        else ("audio", Path("unused.ts"), 0.0, duration)
        for index, duration in enumerate(durations)
    ]

    quantized, total_frames = recorder._quantize_plan(plan)

    frame_seconds = recorder._AAC_FRAME_SAMPLES / recorder._OUTPUT_SAMPLE_RATE
    total_seconds = math.fsum(durations)
    expected_frames = int(total_seconds / frame_seconds + 0.5)
    assert total_frames == expected_frames
    assert sum(frames for _item, frames in quantized) == expected_frames
    assert abs(total_frames * frame_seconds - total_seconds) <= frame_seconds / 2


@pytest.mark.parametrize(
    ("tail", "applied", "expected_end"),
    [(9.9, False, 10.9), (10.0, True, 3.0), (15.0, True, 3.0)],
)
def test_trailing_padding_trim_threshold(
    tail: float, applied: bool, expected_end: float
) -> None:
    plan: list[tuple] = [
        ("audio", Path("source.ts"), 0.0, 1.0),
        ("silence", tail),
    ]
    gaps = [{"at": 1.0, "dur": tail, "trailing": True}]

    end, silence, details = recorder._trim_trailing_padding(
        plan,
        gaps,
        1.0 + tail,
        tail,
        enabled=True,
        threshold=10.0,
        keep=2.0,
        safe_start=1.0,
    )

    assert details["applied"] is applied
    assert end == pytest.approx(expected_end)
    assert silence == pytest.approx(2.0 if applied else tail)
    assert plan[-1][1] == pytest.approx(2.0 if applied else tail)


def test_trailing_trim_never_removes_internal_or_source_silence() -> None:
    plan: list[tuple] = [
        ("audio", Path("quiet-source.ts"), 0.0, 20.0),
        ("silence", 30.0),
        ("audio", Path("source.ts"), 20.0, 1.0),
    ]
    gaps = [{"at": 20.0, "dur": 30.0}]

    end, silence, details = recorder._trim_trailing_padding(
        plan,
        gaps,
        51.0,
        30.0,
        enabled=True,
        threshold=10.0,
        keep=2.0,
        safe_start=0.0,
    )

    assert details["applied"] is False
    assert end == 51.0
    assert silence == 30.0
    assert plan[-1][0] == "audio"


def test_trailing_trim_preserves_unconfirmed_pre_offline_gap() -> None:
    plan: list[tuple] = [
        ("audio", Path("source.ts"), 0.0, 1.0),
        ("silence", 120.0),
    ]
    gaps = [{"at": 1.0, "dur": 120.0, "trailing": True}]

    end, silence, details = recorder._trim_trailing_padding(
        plan,
        gaps,
        121.0,
        120.0,
        enabled=True,
        threshold=10.0,
        keep=2.0,
        safe_start=11.0,
    )

    assert end == 13.0
    assert silence == 12.0
    assert details["applied"] is True
    assert details["pre_offline_seconds"] == 10.0
    assert details["confirmed_offline_seconds"] == 110.0
    assert details["kept_seconds"] == 12.0
    assert details["removed_seconds"] == 108.0


def test_trailing_trim_requires_confirmed_offline_boundary() -> None:
    plan: list[tuple] = [("silence", 120.0)]
    gaps = [{"at": 0.0, "dur": 120.0, "trailing": True}]

    end, silence, details = recorder._trim_trailing_padding(
        plan,
        gaps,
        120.0,
        120.0,
        enabled=True,
        threshold=10.0,
        keep=2.0,
        safe_start=None,
    )

    assert (end, silence) == (120.0, 120.0)
    assert details["applied"] is False
    assert details["reason"] == "no_confirmed_offline_boundary"


@REQUIRES_FFMPEG
def test_extract_aac_copy_snaps_non_aligned_seek_to_packet_grid(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    out = tmp_path / "slice.aac"
    _make_aac_ts(source, duration=2.2)

    info = recorder._extract_aac_copy(
        str(FFMPEG), source, seek=0.982333, take=1.1, frames=48, out=out
    )

    assert info is not None
    assert info.frames == 48
    _assert_fully_decodable(out)


@REQUIRES_FFMPEG
def test_merged_ts_validation_rejects_same_length_wrong_aac_payload(
    tmp_path: Path,
) -> None:
    canonical_source = tmp_path / "canonical.ts"
    wrong_archive = tmp_path / "wrong.ts"
    canonical_adts = tmp_path / "canonical.aac"
    _make_aac_ts(canonical_source, duration=1.0, frequency=440)
    _make_aac_ts(wrong_archive, duration=1.0, frequency=880)
    _remux_to_adts(canonical_source, canonical_adts)
    canonical_info = recorder._scan_adts(canonical_adts)
    assert canonical_info is not None

    validated = recorder._validate_merged_ts(
        str(FFMPEG),
        str(FFPROBE),
        wrong_archive,
        canonical_info.frames,
        canonical_adts,
    )

    assert validated is None
    assert not (tmp_path / "source_merged.validate.aac").exists()


@pytest.mark.parametrize(
    ("label", "breakdown_key"),
    [("A", "primary_A"), ("B", "backup_B")],
    ids=["primary", "backup"],
)
@REQUIRES_FFMPEG
def test_finalize_passthrough_is_packet_preserving_and_clamped_to_end_offset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    label: str,
    breakdown_key: str,
) -> None:
    source = tmp_path / f"audio_{label}_0001.ts"
    _make_aac_ts(source)
    session = _session(tmp_path, source, label=label)
    monkeypatch.setattr(
        recorder, "_collect_lane", lambda *_args: _long_source_run(source)
    )

    expected_frames = int(
        session.end_offset
        * recorder._OUTPUT_SAMPLE_RATE
        / recorder._AAC_FRAME_SAMPLES
        + 0.5
    )
    expected_adts = tmp_path / "expected.aac"
    _remux_to_adts(source, expected_adts, frames=expected_frames)

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    archive = tmp_path / "source_merged.ts"
    assert final.exists()
    assert archive.exists()
    assert not source.exists()
    assert not (tmp_path / "final.mp3").exists()
    assert not (tmp_path / "final.partial.m4a").exists()
    assert not (tmp_path / "source_merged.partial.ts").exists()
    assert not (tmp_path / "_parts").exists()
    _assert_fully_decodable(final)
    _assert_fully_decodable(archive)

    packet_durations = recorder._probe_packet_durations(str(FFPROBE), final)
    assert len(packet_durations) == expected_frames
    assert set(packet_durations) == {recorder._AAC_FRAME_SAMPLES}

    actual_adts = tmp_path / "actual.aac"
    archive_adts = tmp_path / "archive.aac"
    _remux_to_adts(final, actual_adts)
    _remux_to_adts(archive, archive_adts)
    assert actual_adts.read_bytes() == expected_adts.read_bytes()
    assert archive_adts.read_bytes() == expected_adts.read_bytes()

    stream, container = _probe_audio(final)
    assert stream["codec_name"] == "aac"
    assert stream["profile"] == "LC"
    assert stream["sample_rate"] == 48000
    assert stream["channels"] == 2
    assert "m4a" in container["format_name"]
    expected_duration = (
        expected_frames * recorder._AAC_FRAME_SAMPLES / recorder._OUTPUT_SAMPLE_RATE
    )
    assert float(container["duration"]) == pytest.approx(
        expected_duration, abs=recorder._AAC_FRAME_SAMPLES / recorder._OUTPUT_SAMPLE_RATE
    )

    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["duration"] == session.end_offset
    assert meta["audio_duration"] == round(expected_duration, 2)
    assert meta["source_breakdown"][breakdown_key] == session.end_offset
    assert meta["source_breakdown"]["silence"] == 0.0
    assert meta["gaps"] == []
    assert meta["output"] == "final.m4a"
    assert meta["timeline_aligned"] is True
    assert meta["audio_codec"] == "aac"
    assert meta["audio_profile"] == "LC"
    assert meta["audio_sample_rate"] == 48000
    assert meta["audio_channels"] == 2
    assert meta["audio_bit_rate"] == stream.get("bit_rate")
    assert meta["uniform_sample_rate"] is True
    assert meta["processing_mode"] == "passthrough"
    assert meta["source_audio_passthrough"] is True
    assert meta["fallback_reason"] is None
    assert meta["archive"]["output"] == "source_merged.ts"
    assert meta["archive"]["validated"] is True
    assert meta["archive"]["source_audio_passthrough"] is True
    assert meta["archive"]["raw_segments_removed"] == 1
    assert meta["archive"]["raw_cleanup_complete"] is True


@REQUIRES_FFMPEG
def test_passthrough_non_frame_aligned_slices_preserve_contiguous_packets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "audio_A_0001.ts"
    _make_aac_ts(source)
    session = _session(tmp_path, source)
    runs = [
        {
            "path": source,
            "t_start": 0.0,
            "t_end": 0.5,
            "dur": 0.5,
            "source_start": 0.0,
        },
        {
            "path": source,
            "t_start": 0.5,
            "t_end": 1.0,
            "dur": 0.5,
            "source_start": 0.5,
        },
    ]
    monkeypatch.setattr(recorder, "_collect_lane", lambda *_args: runs)

    expected_frames = int(
        session.end_offset
        * recorder._OUTPUT_SAMPLE_RATE
        / recorder._AAC_FRAME_SAMPLES
        + 0.5
    )
    expected_adts = tmp_path / "expected.aac"
    _remux_to_adts(source, expected_adts, frames=expected_frames)

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    archive = tmp_path / "source_merged.ts"
    actual_adts = tmp_path / "actual.aac"
    archive_adts = tmp_path / "archive.aac"
    _remux_to_adts(final, actual_adts)
    _remux_to_adts(archive, archive_adts)
    assert actual_adts.read_bytes() == expected_adts.read_bytes()
    assert archive_adts.read_bytes() == expected_adts.read_bytes()
    assert not source.exists()


@REQUIRES_FFMPEG
def test_primary_to_backup_switch_preserves_contiguous_packets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary_source = tmp_path / "audio_A_0001.ts"
    backup_source = tmp_path / "audio_B_0001.ts"
    _make_aac_ts(primary_source)
    shutil.copyfile(primary_source, backup_source)
    workers = [
        SimpleNamespace(
            label="A", segments={1: {"file": primary_source.name, "t_start": 0.0}}
        ),
        SimpleNamespace(
            label="B", segments={1: {"file": backup_source.name, "t_start": 0.0}}
        ),
    ]
    session = SimpleNamespace(
        segment_index=2,
        cfg=SimpleNamespace(ffmpeg_path=str(FFMPEG)),
        session_dir=tmp_path,
        workers=workers,
        end_offset=1.0,
        stop_reason="live ended",
        live_offline_offset=None,
        start_mono=time.monotonic() - 99.0,
        start_wall=123.0,
        room_id=456,
    )

    def collect_lane(
        _ffmpeg: str, _ffprobe: str, _session_dir: Path, segments: dict
    ) -> list[dict]:
        path = tmp_path / segments[1]["file"]
        end = 0.5 if path == primary_source else 1.0
        return [
            {
                "path": path,
                "t_start": 0.0,
                "t_end": end,
                "dur": end,
                "source_start": 0.0,
            }
        ]

    monkeypatch.setattr(recorder, "_collect_lane", collect_lane)

    expected_frames = int(
        session.end_offset
        * recorder._OUTPUT_SAMPLE_RATE
        / recorder._AAC_FRAME_SAMPLES
        + 0.5
    )
    expected_adts = tmp_path / "expected-switch.aac"
    _remux_to_adts(primary_source, expected_adts, frames=expected_frames)

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    archive = tmp_path / "source_merged.ts"
    actual_adts = tmp_path / "actual-switch.aac"
    archive_adts = tmp_path / "archive-switch.aac"
    _remux_to_adts(final, actual_adts)
    _remux_to_adts(archive, archive_adts)
    assert actual_adts.read_bytes() == expected_adts.read_bytes()
    assert archive_adts.read_bytes() == expected_adts.read_bytes()
    assert not primary_source.exists()
    assert not backup_source.exists()
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["source_breakdown"] == {
        "primary_A": 0.5,
        "backup_B": 0.5,
        "silence": 0.0,
    }
    assert meta["processing_mode"] == "passthrough"


@pytest.mark.parametrize(
    ("sample_rate", "channels"),
    [(44100, 2), (48000, 1)],
    ids=["sample-rate", "channels"],
)
@REQUIRES_FFMPEG
def test_finalize_fallback_normalizes_incompatible_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_rate: int,
    channels: int,
) -> None:
    source = tmp_path / "audio_A_0001.ts"
    _make_aac_ts(source, sample_rate=sample_rate, channels=channels)
    session = _session(tmp_path, source)
    monkeypatch.setattr(
        recorder, "_collect_lane", lambda *_args: _long_source_run(source)
    )

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    archive = tmp_path / "source_merged.ts"
    assert final.exists()
    assert not archive.exists()
    assert source.exists()
    assert not (tmp_path / "final.partial.m4a").exists()
    assert not (tmp_path / "_parts").exists()
    _assert_fully_decodable(final)

    stream, container = _probe_audio(final)
    assert stream["codec_name"] == "aac"
    assert stream["profile"] == "LC"
    assert stream["sample_rate"] == 48000
    assert stream["channels"] == 2
    assert "m4a" in container["format_name"]

    expected_frames = int(
        session.end_offset
        * recorder._OUTPUT_SAMPLE_RATE
        / recorder._AAC_FRAME_SAMPLES
        + 0.5
    )
    expected_duration = (
        expected_frames * recorder._AAC_FRAME_SAMPLES / recorder._OUTPUT_SAMPLE_RATE
    )
    decoded_duration = recorder._probe_decoded_duration(str(FFMPEG), final)
    assert decoded_duration == pytest.approx(
        expected_duration,
        abs=3 * recorder._AAC_FRAME_SAMPLES / recorder._OUTPUT_SAMPLE_RATE,
    )
    assert set(recorder._probe_packet_durations(str(FFPROBE), final)) == {
        recorder._AAC_FRAME_SAMPLES
    }

    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["duration"] == session.end_offset
    assert meta["audio_duration"] == round(decoded_duration, 2)
    assert meta["output"] == "final.m4a"
    assert meta["timeline_aligned"] is True
    assert meta["audio_codec"] == "aac"
    assert meta["audio_profile"] == "LC"
    assert meta["audio_sample_rate"] == 48000
    assert meta["audio_channels"] == 2
    assert meta["uniform_sample_rate"] is True
    assert meta["processing_mode"] == "transcoded"
    assert meta["source_audio_passthrough"] is False
    assert meta["fallback_reason"] == "source audio is not AAC-LC 48 kHz stereo"
    assert meta["archive"]["output"] == "(skipped)"
    assert meta["archive"]["raw_cleanup_reason"] == "transcoded_final"


@REQUIRES_FFMPEG
def test_passthrough_runtime_failure_uses_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "audio_A_0001.ts"
    _make_aac_ts(source)
    session = _session(tmp_path, source)
    monkeypatch.setattr(
        recorder, "_collect_lane", lambda *_args: _long_source_run(source)
    )
    monkeypatch.setattr(recorder, "_extract_aac_copy", lambda *_args: None)

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    archive = tmp_path / "source_merged.ts"
    assert final.exists()
    assert not archive.exists()
    assert source.exists()
    _assert_fully_decodable(final)
    stream, _container = _probe_audio(final)
    assert (stream["codec_name"], stream["profile"]) == ("aac", "LC")
    assert (stream["sample_rate"], stream["channels"]) == (48000, 2)
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["processing_mode"] == "transcoded"
    assert meta["source_audio_passthrough"] is False
    assert meta["fallback_reason"] == "AAC part 0 failed frame validation"
    assert meta["archive"]["output"] == "(skipped)"
    assert meta["archive"]["raw_segments_removed"] == 0


@REQUIRES_FFMPEG
def test_finalize_trims_only_long_synthetic_live_end_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "audio_A_0001.ts"
    _make_aac_ts(source)
    session = _session(tmp_path, source)
    session.end_offset = 122.0
    session.live_offline_offset = 2.0
    monkeypatch.setattr(
        recorder, "_collect_lane", lambda *_args: _long_source_run(source)
    )

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    archive = tmp_path / "source_merged.ts"
    expected_duration = 4.0
    frame_tolerance = 3 * recorder._AAC_FRAME_SAMPLES / recorder._OUTPUT_SAMPLE_RATE
    assert recorder._probe_decoded_duration(str(FFMPEG), final) == pytest.approx(
        expected_duration, abs=frame_tolerance
    )
    assert recorder._probe_decoded_duration(str(FFMPEG), archive) == pytest.approx(
        expected_duration, abs=frame_tolerance
    )
    assert not source.exists()

    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["capture_duration"] == 122.0
    assert meta["duration"] == 122.0
    assert meta["timeline_end_offset"] == 4.0
    assert meta["source_breakdown"] == {
        "primary_A": 2.0,
        "backup_B": 0.0,
        "silence": 2.0,
    }
    assert meta["gaps"] == [
        {
            "at": 2.0,
            "dur": 2.0,
            "trailing": True,
            "original_dur": 120.0,
            "trimmed": 118.0,
        }
    ]
    assert meta["trailing_trim"]["applied"] is True
    assert meta["trailing_trim"]["detected_seconds"] == 120.0
    assert meta["trailing_trim"]["kept_seconds"] == 2.0
    assert meta["trailing_trim"]["removed_seconds"] == 118.0


@REQUIRES_FFMPEG
def test_archive_failure_preserves_raw_segments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "audio_A_0001.ts"
    _make_aac_ts(source)
    original = source.read_bytes()
    session = _session(tmp_path, source)
    monkeypatch.setattr(
        recorder, "_collect_lane", lambda *_args: _long_source_run(source)
    )
    monkeypatch.setattr(recorder, "_mux_m4a_to_ts", lambda *_args: False)

    recorder.finalize(session)

    assert (tmp_path / "final.m4a").exists()
    assert not (tmp_path / "source_merged.ts").exists()
    assert source.read_bytes() == original
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["output"] == "final.m4a"
    assert meta["archive"]["output"] == "(failed)"
    assert meta["archive"]["validated"] is False
    assert meta["archive"]["raw_segments_removed"] == 0
    assert meta["archive"]["raw_segments_remaining"] == 1
    assert meta["archive"]["raw_cleanup_complete"] is False


@REQUIRES_FFMPEG
def test_unverified_source_segment_blocks_all_raw_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "audio_A_0001.ts"
    unverified = tmp_path / "audio_C_9999.ts"
    _make_aac_ts(source)
    shutil.copyfile(source, unverified)
    session = _session(tmp_path, source)
    monkeypatch.setattr(
        recorder, "_collect_lane", lambda *_args: _long_source_run(source)
    )

    recorder.finalize(session)

    assert (tmp_path / "final.m4a").exists()
    assert (tmp_path / "source_merged.ts").exists()
    assert source.exists()
    assert unverified.exists()
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["archive"]["validated"] is True
    assert meta["archive"]["raw_segments_removed"] == 0
    assert meta["archive"]["raw_segments_unverified"] == 1
    assert meta["archive"]["raw_unverified_files"] == ["audio_C_9999.ts"]
    assert meta["archive"]["raw_cleanup_reason"] == "unverified_segments"


def test_finalize_failure_is_atomic_and_cleans_temporary_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "audio_A_0001.ts"
    source.write_bytes(b"source")
    session = _session(tmp_path, source)
    existing = tmp_path / "final.m4a"
    existing.write_bytes(b"existing-final")
    monkeypatch.setattr(
        recorder, "_collect_lane", lambda *_args: _long_source_run(source)
    )
    monkeypatch.setattr(recorder, "_aac_passthrough_compatible", lambda *_args: True)
    monkeypatch.setattr(recorder, "_extract_aac_copy", lambda *_args: None)
    monkeypatch.setattr(
        recorder,
        "_render_pcm_timeline",
        lambda _ffmpeg, _plan, out: out.write_bytes(b"pcm") > 0,
    )
    monkeypatch.setattr(
        recorder,
        "_encode_pcm_m4a",
        lambda _ffmpeg, _pcm, out: out.write_bytes(b"partial") > 0,
    )
    monkeypatch.setattr(recorder, "_validate_m4a", lambda *_args, **_kwargs: None)

    recorder.finalize(session)

    assert existing.read_bytes() == b"existing-final"
    assert source.read_bytes() == b"source"
    assert not (tmp_path / "source_merged.ts").exists()
    assert not (tmp_path / "source_merged.partial.ts").exists()
    assert not (tmp_path / "final.partial.m4a").exists()
    assert not (tmp_path / "_parts").exists()
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["output"] == "(failed)"
    assert meta["timeline_aligned"] is False
    assert meta["processing_mode"] == "failed"
    assert meta["source_audio_passthrough"] is False


def test_probe_audio_runs_splits_large_pts_gaps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = b"1.400000,0.020000\n1.420000,0.020000\n2.440000,0.020000\n"
    monkeypatch.setattr(
        recorder.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )

    runs = recorder._probe_audio_runs("ffprobe", tmp_path / "source.ts")

    assert len(runs) == 2
    assert runs[0] == pytest.approx((0.0, 0.04))
    assert runs[1] == pytest.approx((1.04, 1.06))
