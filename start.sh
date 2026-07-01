#!/usr/bin/env bash
# Cross-shell launcher for MaoerRecorder.
# Usage: ./start.sh [room_id]
set -e
cd "$(dirname "$0")"
ROOM="${1:-868802213}"
exec py main.py record --room "$ROOM"
