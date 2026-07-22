from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from maoer import recorder


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
            f"sine=frequency=440:sample_rate={sample_rate}:duration={duration}",
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
        cfg=SimpleNamespace(ffmpeg_path=str(FFMPEG)),
        session_dir=tmp_path,
        workers=[worker],
        end_offset=1.0,
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

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    assert final.exists()
    assert not (tmp_path / "final.mp3").exists()
    assert not (tmp_path / "final.partial.m4a").exists()
    assert not (tmp_path / "_parts").exists()
    _assert_fully_decodable(final)

    expected_frames = int(
        session.end_offset
        * recorder._OUTPUT_SAMPLE_RATE
        / recorder._AAC_FRAME_SAMPLES
        + 0.5
    )
    packet_durations = recorder._probe_packet_durations(str(FFPROBE), final)
    assert len(packet_durations) == expected_frames
    assert set(packet_durations) == {recorder._AAC_FRAME_SAMPLES}

    expected_adts = tmp_path / "expected.aac"
    actual_adts = tmp_path / "actual.aac"
    _remux_to_adts(source, expected_adts, frames=expected_frames)
    _remux_to_adts(final, actual_adts)
    assert actual_adts.read_bytes() == expected_adts.read_bytes()

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

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    expected_frames = int(
        session.end_offset
        * recorder._OUTPUT_SAMPLE_RATE
        / recorder._AAC_FRAME_SAMPLES
        + 0.5
    )
    expected_adts = tmp_path / "expected.aac"
    actual_adts = tmp_path / "actual.aac"
    _remux_to_adts(source, expected_adts, frames=expected_frames)
    _remux_to_adts(final, actual_adts)
    assert actual_adts.read_bytes() == expected_adts.read_bytes()


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

    recorder.finalize(session)

    final = tmp_path / "final.m4a"
    expected_frames = int(
        session.end_offset
        * recorder._OUTPUT_SAMPLE_RATE
        / recorder._AAC_FRAME_SAMPLES
        + 0.5
    )
    expected_adts = tmp_path / "expected-switch.aac"
    actual_adts = tmp_path / "actual-switch.aac"
    _remux_to_adts(primary_source, expected_adts, frames=expected_frames)
    _remux_to_adts(final, actual_adts)
    assert actual_adts.read_bytes() == expected_adts.read_bytes()
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
    assert final.exists()
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
    assert final.exists()
    _assert_fully_decodable(final)
    stream, _container = _probe_audio(final)
    assert (stream["codec_name"], stream["profile"]) == ("aac", "LC")
    assert (stream["sample_rate"], stream["channels"]) == (48000, 2)
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["processing_mode"] == "transcoded"
    assert meta["source_audio_passthrough"] is False
    assert meta["fallback_reason"] == "AAC part 0 failed frame validation"


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
