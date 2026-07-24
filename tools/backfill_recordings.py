#!/usr/bin/env python
"""Safely consolidate completed recordings created before v0.3.0.

The migration runs ``recorder.finalize`` against hard-linked TS files in an
isolated directory. The original session is changed only after both staged
outputs decode fully and expose byte-identical AAC frames.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maoer import recorder  # noqa: E402
from maoer.config import load as load_config  # noqa: E402
from maoer.log import setup as setup_log  # noqa: E402


RAW_SEGMENT_RE = re.compile(r"^audio_([A-D])_([0-9]+)\.ts$")
ACTIVE_SESSION_STATES = {"recording", "finalizing"}
EXPECTED_AUDIO_SPEC = {
    "codec_name": "aac",
    "profile": "LC",
    "sample_rate": 48000,
    "channels": 2,
}


class MigrationError(RuntimeError):
    pass


def _json_load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _append_journal(path: Path, event: dict[str, Any]) -> None:
    payload = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **event,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def infer_timeline_end(meta: dict[str, Any]) -> float | None:
    capture_duration = _positive_float(meta.get("capture_duration"))
    if capture_duration is not None:
        return capture_duration

    duration = _positive_float(meta.get("duration"))
    breakdown = meta.get("source_breakdown")
    breakdown_total = None
    if isinstance(breakdown, dict):
        values = [
            _positive_float(breakdown.get(name)) or 0.0
            for name in ("primary_A", "backup_B", "silence")
        ]
        if any(values):
            breakdown_total = sum(values)
    if (
        duration is not None
        and breakdown_total is not None
        and duration - breakdown_total > 2.0
    ):
        return breakdown_total
    return duration or breakdown_total


def infer_offline_boundary(meta: dict[str, Any], timeline_end: float | None) -> float | None:
    if timeline_end is None:
        return None
    gaps = meta.get("gaps")
    if not isinstance(gaps, list) or not gaps or not isinstance(gaps[-1], dict):
        return None
    last = gaps[-1]
    start = _nonnegative_float(last.get("at"))
    duration = _positive_float(last.get("dur"))
    if start is None or duration is None:
        return None
    gap_end = start + duration
    if abs(gap_end - timeline_end) <= 2.0:
        return start
    return None


def _room_id(session_dir: Path, meta: dict[str, Any]) -> int:
    try:
        room_id = int(meta.get("room_id"))
        if room_id > 0:
            return room_id
    except (TypeError, ValueError):
        pass
    match = re.match(r"^([1-9][0-9]*)_", session_dir.parent.name)
    if not match:
        raise MigrationError(f"cannot infer room ID from {session_dir}")
    return int(match.group(1))


def _start_time(session_dir: Path, meta: dict[str, Any]) -> float:
    value = _positive_float(meta.get("start_time"))
    if value is not None:
        return value
    try:
        return datetime.strptime(session_dir.name, "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return session_dir.stat().st_mtime


def _raw_segments(session_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in session_dir.iterdir()
        if path.is_file() and RAW_SEGMENT_RE.fullmatch(path.name)
    )


def _read_workers(session_dir: Path) -> tuple[list[SimpleNamespace], int, set[str]]:
    log_path = session_dir / "segments.jsonl"
    if not log_path.is_file():
        raise MigrationError("segments.jsonl is missing")
    grouped: dict[str, dict[int, dict[str, Any]]] = {}
    logged_files: set[str] = set()
    rows = 0
    for line_number, raw_line in enumerate(
        log_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
            label = str(item["worker"])
            index = int(item["index"])
            filename = str(item["file"])
            start = float(item["t_start"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError(f"invalid segments.jsonl row {line_number}: {exc}") from exc
        match = RAW_SEGMENT_RE.fullmatch(filename)
        if (
            not match
            or match.group(1) != label
            or int(match.group(2)) != index
            or index <= 0
            or not math.isfinite(start)
            or start < 0
            or Path(filename).name != filename
        ):
            raise MigrationError(f"unsafe segments.jsonl row {line_number}")
        lane = grouped.setdefault(label, {})
        value = {"file": filename, "t_start": start}
        if index in lane and lane[index] != value:
            raise MigrationError(f"conflicting segment index {label}/{index}")
        lane[index] = value
        logged_files.add(filename)
        rows += 1
    workers = [
        SimpleNamespace(label=label, segments=segments)
        for label, segments in sorted(grouped.items())
    ]
    if not workers or rows == 0:
        raise MigrationError("segments.jsonl has no usable rows")
    return workers, rows, logged_files


def _active_session_dirs(base_dir: Path) -> set[Path]:
    active: set[Path] = set()
    now = time.time()
    signals = base_dir / ".dashboard" / "signals"
    if not signals.is_dir():
        return active
    for path in signals.glob("*.json"):
        value = _json_load(path)
        try:
            updated_at = float(value.get("updated_at") or 0)
        except (TypeError, ValueError):
            continue
        if (
            value.get("state") not in ACTIVE_SESSION_STATES
            or now - updated_at > 90.0
            or not value.get("session_dir")
        ):
            continue
        try:
            active.add(Path(value["session_dir"]).resolve())
        except (OSError, TypeError, ValueError):
            continue
    return active


def discover_sessions(base_dir: Path) -> list[Path]:
    sessions: list[Path] = []
    active = _active_session_dirs(base_dir)
    for room_dir in base_dir.iterdir():
        if not room_dir.is_dir() or room_dir.name.startswith("."):
            continue
        for session_dir in room_dir.iterdir():
            if (
                session_dir.is_dir()
                and session_dir.resolve() not in active
                and (session_dir / "segments.jsonl").is_file()
                and _raw_segments(session_dir)
            ):
                sessions.append(session_dir.resolve())
    return sorted(sessions, key=lambda item: sum(p.stat().st_size for p in _raw_segments(item)))


def unfinished_stages(base_dir: Path) -> list[Path]:
    work_root = base_dir / ".dashboard" / "historical-migration-work"
    if not work_root.is_dir():
        return []
    return sorted(path.resolve() for path in work_root.iterdir() if path.is_dir())


def _decode_fully(ffmpeg: str, path: Path) -> None:
    result = recorder._run_hidden(
        [
            ffmpeg,
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
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[-500:]
        raise MigrationError(f"{path.name} does not decode fully: {detail}")


def validate_output_pair(ffmpeg: str, final: Path, archive: Path, scratch: Path) -> dict[str, Any]:
    ffprobe = recorder._ffprobe_path(ffmpeg)
    for path in (final, archive):
        if not path.is_file() or path.stat().st_size <= 0:
            raise MigrationError(f"missing output: {path.name}")
        _decode_fully(ffmpeg, path)
        spec = recorder._probe_audio_spec(ffprobe, path)
        for name, expected in EXPECTED_AUDIO_SPEC.items():
            if spec.get(name) != expected:
                raise MigrationError(
                    f"unexpected {path.name} {name}: {spec.get(name)!r}"
                )
    final_aac = scratch / "verify-final.aac"
    archive_aac = scratch / "verify-archive.aac"
    try:
        if not recorder._demux_to_adts(ffmpeg, final, final_aac):
            raise MigrationError("cannot demux final.m4a for parity check")
        if not recorder._demux_to_adts(ffmpeg, archive, archive_aac):
            raise MigrationError("cannot demux source_merged.ts for parity check")
        if not recorder._files_equal(final_aac, archive_aac):
            raise MigrationError("M4A and merged TS AAC frames differ")
    finally:
        for path in (final_aac, archive_aac):
            try:
                path.unlink()
            except OSError:
                pass
    return {
        "final_sha256": _sha256(final),
        "archive_sha256": _sha256(archive),
        "final_bytes": final.stat().st_size,
        "archive_bytes": archive.stat().st_size,
    }


def _safe_stage_path(base_dir: Path, session_dir: Path) -> Path:
    work_root = (base_dir / ".dashboard" / "historical-migration-work").resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    stage = (work_root / f"{session_dir.parent.name}__{session_dir.name}__{uuid.uuid4().hex}").resolve()
    try:
        stage.relative_to(work_root)
    except ValueError as exc:
        raise MigrationError("staging path escaped migration root") from exc
    stage.mkdir()
    return stage


def _remove_stage(base_dir: Path, stage: Path) -> None:
    work_root = (base_dir / ".dashboard" / "historical-migration-work").resolve()
    resolved = stage.resolve()
    try:
        resolved.relative_to(work_root)
    except ValueError as exc:
        raise MigrationError("refusing to remove staging path outside migration root") from exc
    shutil.rmtree(resolved, ignore_errors=False)
    try:
        work_root.rmdir()
    except OSError:
        pass


def _stage_session(
    base_dir: Path,
    session_dir: Path,
    raw_files: list[Path],
    ffmpeg: str | None,
) -> tuple[Path, SimpleNamespace, dict[str, Any], dict[str, Any]]:
    meta_path = session_dir / "meta.json"
    original_meta = _json_load(meta_path)
    workers, segment_rows, logged_files = _read_workers(session_dir)
    unlogged = [path.name for path in raw_files if path.name not in logged_files]
    if unlogged:
        raise MigrationError(f"raw TS files are absent from segments.jsonl: {unlogged}")

    stage = _safe_stage_path(base_dir, session_dir)
    try:
        recovery = stage / "recovery-links"
        recovery.mkdir()
        for source in raw_files:
            os.link(source, stage / source.name)
            os.link(source, recovery / source.name)
        legacy_mp3 = session_dir / "final.mp3"
        if legacy_mp3.is_file():
            os.link(legacy_mp3, recovery / legacy_mp3.name)
        room_id = _room_id(session_dir, original_meta)
        cfg = load_config(room_id)
        cfg_values = {
            name: getattr(cfg, name)
            for name in cfg.__dataclass_fields__
        }
        if ffmpeg:
            cfg_values["ffmpeg_path"] = str(Path(ffmpeg).resolve())
        cfg_values["allow_transcode_fallback"] = False
        timeline_end = infer_timeline_end(original_meta)
        offline_boundary = infer_offline_boundary(original_meta, timeline_end)
        if original_meta:
            stop_reason = "live ended" if offline_boundary is not None else "historical migration"
        else:
            stop_reason = "recovered crash"
        session = SimpleNamespace(
            segment_index=segment_rows,
            cfg=SimpleNamespace(**cfg_values),
            session_dir=stage,
            workers=workers,
            end_offset=timeline_end,
            stop_reason=stop_reason,
            live_offline_offset=offline_boundary,
            start_mono=time.monotonic(),
            start_wall=_start_time(session_dir, original_meta),
            room_id=room_id,
        )
        audit = {
            "original_meta_present": bool(original_meta),
            "original_meta_sha256": _sha256(meta_path) if meta_path.is_file() else None,
            "original_output": original_meta.get("output"),
            "timeline_end_inferred": timeline_end,
            "offline_boundary_inferred": offline_boundary,
            "stop_reason_inferred": stop_reason,
            "segment_rows": segment_rows,
        }
        return stage, session, original_meta, audit
    except Exception:
        _remove_stage(base_dir, stage)
        raise


def _validate_staged(stage: Path, ffmpeg: str) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = _json_load(stage / "meta.json")
    archive_meta = meta.get("archive")
    if (
        meta.get("output") != "final.m4a"
        or meta.get("processing_mode") != "passthrough"
        or not isinstance(archive_meta, dict)
        or archive_meta.get("output") != "source_merged.ts"
        or archive_meta.get("validated") is not True
        or archive_meta.get("raw_cleanup_complete") is not True
    ):
        raise MigrationError(f"staged finalize did not satisfy cleanup contract: {meta}")
    pair = validate_output_pair(
        ffmpeg,
        stage / "final.m4a",
        stage / "source_merged.ts",
        stage,
    )
    if _raw_segments(stage):
        raise MigrationError("staged finalize left raw TS links behind")
    return meta, pair


def _publish(
    base_dir: Path,
    session_dir: Path,
    stage: Path,
    raw_files: list[Path],
    staged_meta: dict[str, Any],
    pair: dict[str, Any],
    audit: dict[str, Any],
    journal: Path,
    ffmpeg: str,
) -> dict[str, Any]:
    final_target = session_dir / "final.m4a"
    archive_target = session_dir / "source_merged.ts"
    final_temporary = session_dir / ".historical-final.partial.m4a"
    archive_temporary = session_dir / ".historical-archive.partial.ts"
    for path in (final_temporary, archive_temporary):
        try:
            path.unlink()
        except OSError:
            pass

    os.replace(stage / "final.m4a", final_temporary)
    os.replace(stage / "source_merged.ts", archive_temporary)
    if (
        _sha256(final_temporary) != pair["final_sha256"]
        or _sha256(archive_temporary) != pair["archive_sha256"]
    ):
        raise MigrationError("output hash changed while moving from staging")
    os.replace(archive_temporary, archive_target)
    os.replace(final_temporary, final_target)
    published_pair = validate_output_pair(
        ffmpeg,
        final_target,
        archive_target,
        stage,
    )
    if (
        published_pair["final_sha256"] != pair["final_sha256"]
        or published_pair["archive_sha256"] != pair["archive_sha256"]
    ):
        raise MigrationError("published output hashes do not match staged outputs")
    _append_journal(
        journal,
        {"event": "outputs_published", "session": str(session_dir), **pair},
    )

    recovery = stage / "recovery-links"
    legacy_mp3 = session_dir / "final.mp3"
    raw_bytes = sum(path.stat().st_size for path in raw_files)
    deleted: list[Path] = []
    try:
        for source in raw_files:
            source.unlink()
            deleted.append(source)
        if legacy_mp3.is_file():
            legacy_mp3.unlink()
            deleted.append(legacy_mp3)

        migration = {
            "version": 1,
            "status": "complete",
            "migrated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **audit,
            **pair,
            "legacy_mp3_removed": legacy_mp3 in deleted,
        }
        staged_meta["historical_migration"] = migration
        archive_meta = staged_meta["archive"]
        archive_meta["raw_segments_found"] = len(raw_files)
        archive_meta["raw_bytes_before"] = raw_bytes
        archive_meta["raw_segments_removed"] = len(raw_files)
        archive_meta["raw_bytes_removed"] = raw_bytes
        archive_meta["raw_segments_remaining"] = 0
        archive_meta["raw_cleanup_complete"] = True
        archive_meta["raw_cleanup_reason"] = "complete"
        _atomic_write_json(session_dir / "meta.json", staged_meta)
    except Exception:
        restore_errors: list[str] = []
        for source in deleted:
            recovery_link = recovery / source.name
            if recovery_link.exists() and not source.exists():
                try:
                    os.link(recovery_link, source)
                except OSError as exc:
                    restore_errors.append(f"{source.name}: {exc}")
        if restore_errors:
            (stage / "KEEP_FOR_RECOVERY.txt").write_text(
                "\n".join(restore_errors) + "\n", encoding="utf-8"
            )
        raise

    return {
        "raw_files_removed": len(raw_files),
        "raw_bytes_removed": staged_meta["archive"]["raw_bytes_removed"],
        "legacy_mp3_removed": staged_meta["historical_migration"]["legacy_mp3_removed"],
        **pair,
    }


def migrate_session(
    base_dir: Path,
    session_dir: Path,
    *,
    ffmpeg: str | None = None,
    journal: Path | None = None,
) -> dict[str, Any]:
    base_dir = base_dir.resolve()
    session_dir = session_dir.resolve()
    try:
        session_dir.relative_to(base_dir)
    except ValueError as exc:
        raise MigrationError("session is outside the recordings directory") from exc
    if session_dir in _active_session_dirs(base_dir):
        raise MigrationError("session is currently active")
    raw_files = _raw_segments(session_dir)
    if not raw_files:
        raise MigrationError("session has no raw TS files")
    raw_bytes = sum(path.stat().st_size for path in raw_files)
    required_free = int(raw_bytes * 2.3)
    free = shutil.disk_usage(base_dir).free
    if free < required_free:
        raise MigrationError(
            f"insufficient free space: {free} bytes available, {required_free} required"
        )
    journal = journal or base_dir / ".dashboard" / "historical-migration.jsonl"
    _append_journal(
        journal,
        {
            "event": "started",
            "session": str(session_dir),
            "raw_files": len(raw_files),
            "raw_bytes": raw_bytes,
            "free_bytes": free,
        },
    )
    stage: Path | None = None
    preserve_stage_on_failure = False
    try:
        stage, session, _original_meta, audit = _stage_session(
            base_dir, session_dir, raw_files, ffmpeg
        )
        _append_journal(
            journal,
            {"event": "staged", "session": str(session_dir), **audit},
        )
        recorder.finalize(session)
        staged_meta, pair = _validate_staged(
            stage, str(session.cfg.ffmpeg_path)
        )
        _append_journal(
            journal,
            {"event": "staged_validated", "session": str(session_dir), **pair},
        )
        # From this point onward the stage holds recovery hardlinks. Keep it
        # intact on any publish error so even a failed restoration cannot make
        # the source bytes unreachable.
        preserve_stage_on_failure = True
        result = _publish(
            base_dir,
            session_dir,
            stage,
            raw_files,
            staged_meta,
            pair,
            audit,
            journal,
            str(session.cfg.ffmpeg_path),
        )
        _remove_stage(base_dir, stage)
        stage = None
        preserve_stage_on_failure = False
        _append_journal(
            journal,
            {"event": "completed", "session": str(session_dir), **result},
        )
        return result
    except Exception as exc:
        _append_journal(
            journal,
            {"event": "failed", "session": str(session_dir), "error": str(exc)},
        )
        raise
    finally:
        if (
            stage is not None
            and not preserve_stage_on_failure
            and not (stage / "KEEP_FOR_RECOVERY.txt").exists()
        ):
            try:
                _remove_stage(base_dir, stage)
            except OSError:
                pass


def _summary(session_dir: Path) -> dict[str, Any]:
    raw = _raw_segments(session_dir)
    meta = _json_load(session_dir / "meta.json")
    end = infer_timeline_end(meta)
    return {
        "session": str(session_dir),
        "raw_files": len(raw),
        "raw_bytes": sum(path.stat().st_size for path in raw),
        "existing_output": meta.get("output"),
        "timeline_end": end,
        "offline_boundary": infer_offline_boundary(meta, end),
        "recovering_crash": not bool(meta),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir", type=Path, default=ROOT / "recordings", help="recordings root"
    )
    parser.add_argument("--session", type=Path, action="append", default=[])
    parser.add_argument("--ffmpeg", type=str, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    setup_log()
    base_dir = args.base_dir.resolve()
    if not base_dir.is_dir():
        parser.error(f"recordings directory does not exist: {base_dir}")
    unfinished = unfinished_stages(base_dir)
    if args.apply and unfinished:
        names = ", ".join(str(path) for path in unfinished)
        raise MigrationError(
            "unfinished migration recovery directories exist; inspect them before "
            f"starting another batch: {names}"
        )
    sessions = (
        [path.resolve() for path in args.session]
        if args.session
        else discover_sessions(base_dir)
    )
    sessions = sorted(
        sessions,
        key=lambda item: sum(path.stat().st_size for path in _raw_segments(item)),
    )
    if not args.apply:
        print(json.dumps({"apply": False, "sessions": [_summary(p) for p in sessions]}, ensure_ascii=False, indent=2))
        return 0

    results = []
    for index, session_dir in enumerate(sessions, start=1):
        if _active_session_dirs(base_dir):
            raise MigrationError("a live recording started; historical migration stopped")
        before_free = shutil.disk_usage(base_dir).free
        print(
            json.dumps(
                {
                    "event": "processing",
                    "index": index,
                    "total": len(sessions),
                    **_summary(session_dir),
                    "free_bytes": before_free,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        result = migrate_session(
            base_dir,
            session_dir,
            ffmpeg=args.ffmpeg,
        )
        after_free = shutil.disk_usage(base_dir).free
        item = {
            "session": str(session_dir),
            **result,
            "free_bytes_before": before_free,
            "free_bytes_after": after_free,
        }
        results.append(item)
        print(json.dumps({"event": "completed", **item}, ensure_ascii=False), flush=True)
    print(json.dumps({"event": "all_completed", "results": results}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
