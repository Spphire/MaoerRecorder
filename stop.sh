#!/usr/bin/env bash
# Gracefully stop the recorder so it finalizes the current session.
# Uses a stop sentinel (Windows-safe: terminate() hard-kills and skips
# finalize). The recorder polls recordings/.stop and shuts down cleanly.
cd "$(dirname "$0")"
PID=$(cat record.pid 2>/dev/null)
BASE="${MAOER_BASE_DIR:-recordings}"
mkdir -p "$BASE"
touch "$BASE/.stop"
echo "Stop sentinel written ($BASE/.stop); recorder will finalize and exit."

if [ -n "$PID" ]; then
  py -c "
import psutil
try:
    p = psutil.Process($PID)
    print('waiting up to 90s for clean finalize...')
    p.wait(90)
    print('exited cleanly')
except psutil.NoSuchProcess:
    print('not running')
except psutil.TimeoutExpired:
    print('still running after 90s; finalize may be large — check status.sh')
"
fi
rm -f record.pid
