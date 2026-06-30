#!/usr/bin/env bash
# run.sh — watchdog wrapper for scheduler.py
#
# Auto-restarts the scheduler if it crashes or exits for any reason.
# Backoff: 5s → 10s → 20s … up to 300s between restarts.
# Reset to 5s whenever the scheduler runs for at least 60 seconds.
#
# Usage:
#   ./run.sh                           # foreground (Ctrl-C to stop)
#   nohup ./run.sh >> logs/run.log 2>&1 &   # background (survives terminal close)
#   ps aux | grep scheduler            # verify
#   tail -f logs/scheduler.log         # watch logs

set -euo pipefail

BACKOFF=5
MAX_BACKOFF=300
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEDULER="$SCRIPT_DIR/scheduler.py"
PYTHON="${PYTHON:-python3}"

mkdir -p logs

# Install any missing prerequisites before first start
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo "[startup] Checking prerequisites..."
    "$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet --disable-pip-version-check
fi

# PostCar sidecar — peer network (auto-registers, runs every 5 min, self-upgrades)
if [ -f "$SCRIPT_DIR/postcar/postcar_kit.py" ]; then
    echo "[startup] Starting PostCar sidecar…"
    "$PYTHON" "$SCRIPT_DIR/postcar/postcar_kit.py" --agent-dir "$SCRIPT_DIR" &
fi

echo "[watchdog] $(date) — starting scheduler. Ctrl-C to stop."

while true; do
    START=$(date +%s)
    "$PYTHON" "$SCHEDULER" || true
    END=$(date +%s)
    ELAPSED=$(( END - START ))

    if [ "$ELAPSED" -gt 60 ]; then
        BACKOFF=5   # ran long enough — reset backoff
    fi

    echo "[watchdog] $(date) — scheduler stopped (ran ${ELAPSED}s) — restarting in ${BACKOFF}s"
    sleep "$BACKOFF"

    BACKOFF=$(( BACKOFF * 2 ))
    [ "$BACKOFF" -gt "$MAX_BACKOFF" ] && BACKOFF=$MAX_BACKOFF
done
