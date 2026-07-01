#!/usr/bin/env python
"""Analyze dual-lane coverage from a session's segments.jsonl.

Finds intervals where BOTH lanes were down simultaneously (real audio gaps)
vs single-lane gaps the other lane covered.
"""
import json
import subprocess
import sys
from pathlib import Path

FFPROBE = (r"C:\Users\yibo\AppData\Local\Microsoft\WinGet\Packages"
           r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
           r"\ffmpeg-8.1.1-full_build\bin\ffprobe.exe")


def dur(p: Path) -> float:
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                            "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", str(p)],
                           capture_output=True, text=True)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def lane_intervals(sess: Path, label: str):
    iv = []
    for line in (sess / "segments.jsonl").open(encoding="utf-8"):
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("worker") != label:
            continue
        f = sess / d["file"]
        if not (f.exists() and f.stat().st_size > 0):
            continue
        t0 = float(d["t_start"])
        iv.append((t0, t0 + dur(f)))
    iv.sort()
    return iv


def covered(iv, t):
    return any(a <= t < b for a, b in iv)


def main():
    sess = Path(sys.argv[1])
    labels = sorted({json.loads(l)["worker"]
                     for l in (sess / "segments.jsonl").open(encoding="utf-8")
                     if l.strip()})
    lanes = {lb: lane_intervals(sess, lb) for lb in labels}
    end = max((b for iv in lanes.values() for _, b in iv), default=0.0)

    step = 0.5
    both_down = []
    cur = None
    t = 0.0
    while t < end:
        if not any(covered(iv, t) for iv in lanes.values()):
            if cur is None:
                cur = t
        else:
            if cur is not None:
                both_down.append((cur, t))
                cur = None
        t += step
    if cur is not None:
        both_down.append((cur, end))

    total_gap = sum(b - a for a, b in both_down)
    print(f"session : {sess.name}")
    for lb, iv in lanes.items():
        cov = sum(b - a for a, b in iv)
        print(f"  lane {lb}: {len(iv)} segs, covers {cov:.1f}s")
    print(f"timeline: 0 ~ {end:.1f}s")
    if end:
        print(f"BOTH-DOWN (real gaps): {len(both_down)} events, "
              f"total {total_gap:.1f}s ({total_gap/end*100:.2f}%)")
    for a, b in both_down:
        if b - a >= step:
            print(f"   gap {a:.1f}s ~ {b:.1f}s  ({b-a:.1f}s)")


if __name__ == "__main__":
    main()
