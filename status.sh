#!/usr/bin/env bash
# Quick status check for the currently-running recorder session.
# Usage: ./status.sh
cd "$(dirname "$0")"

py -c "
import json, os, time, psutil, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path

# 1. recorder process
pid_file = Path('record.pid')
pid = None
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
    except: pass

if pid:
    try:
        p = psutil.Process(pid)
        age = int(time.time() - p.create_time())
        h, m, s = age // 3600, (age % 3600) // 60, age % 60
        print(f'recorder  : PID {pid}, running {h:02d}:{m:02d}:{s:02d}, '
              f'mem {p.memory_info().rss / 1024 / 1024:.1f} MB')
    except psutil.NoSuchProcess:
        print(f'recorder  : PID {pid} EXITED')
else:
    print('recorder  : (record.pid not found)')

# 2. ffmpeg lanes
ff = [p for p in psutil.process_iter(['pid','name','create_time'])
      if (p.info.get('name') or '').lower() == 'ffmpeg.exe']
if ff:
    ages = ', '.join(f'{int(time.time()-p.info[\"create_time\"])}s' for p in ff)
    print(f'ffmpeg    : {len(ff)} lane(s) running ({ages})')
else:
    print('ffmpeg    : (none)')

# 3. current session
rec = Path('recordings')
sessions = sorted(
    (d for r in rec.glob('*/') for d in r.glob('*/') if d.is_dir()),
    key=lambda d: d.stat().st_mtime,
    reverse=True,
)
if sessions:
    s = sessions[0]
    print(f'session   : {s}')
    segs = sorted(s.glob('audio_*.ts'))
    total_bytes = sum(seg.stat().st_size for seg in segs)
    # Break down by lane (audio_A_*, audio_B_*).
    by_lane = {}
    for seg in segs:
        lane = seg.name.split('_')[1] if seg.name.count('_') >= 2 else '?'
        by_lane.setdefault(lane, []).append(seg)
    lane_str = ', '.join(
        f'{k}:{len(v)}seg' for k, v in sorted(by_lane.items()))
    print(f'segments  : {len(segs)} files ({lane_str}), {total_bytes / 1024 / 1024:.2f} MB')
    if segs:
        latest = max(segs, key=lambda p: p.stat().st_mtime)
        age = int(time.time() - latest.stat().st_mtime)
        print(f'last write: {latest.name} ({age}s ago, {latest.stat().st_size / 1024:.0f} KB)')

    chat = s / 'chat.jsonl'
    if chat.exists():
        n_lines = sum(1 for _ in chat.open('r', encoding='utf-8', errors='ignore'))
        # Sample last message
        last_msg = None
        with chat.open('r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                try:
                    e = json.loads(line)
                    d = e.get('data') or {}
                    if isinstance(d, dict) and d.get('type') == 'message':
                        last_msg = (e['t_audio'],
                                    d.get('user',{}).get('username','?'),
                                    d.get('message','')[:40])
                except: pass
        print(f'chat lines: {n_lines}')
        if last_msg:
            t, u, m = last_msg
            print(f'last msg  : t+{t:.1f}s [{u}] {m}')
else:
    print('session   : (no recording sessions yet)')

# 4. final output presence
finals = sorted(
    list(rec.glob('*/*/final.m4a'))
    + list(rec.glob('*/*/final.mp3'))
    + list(rec.glob('*/*/final.ts')),
    key=lambda f: f.stat().st_mtime, reverse=True,
)
if finals:
    print(f'last final: {finals[0]} ({finals[0].stat().st_size / 1024 / 1024:.2f} MB)')
"
