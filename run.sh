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

# PostCar sidecar bootstrap — lives in its own file (Cat A, auto-updates)
# so PostCar changes never require touching this file (Cat B, agent-owned).
bash "$SCRIPT_DIR/postcar_launch.sh" || true

# Kit self-upgrade daemon — 30-min standalone check, independent of whether
# the scheduler below is even running. One-time idempotent install; lives in
# its own file so future changes to it don't require touching this file
# either (same pattern as postcar_launch.sh above).
"$PYTHON" "$SCRIPT_DIR/kit_autoupdate.py" --install-daemon || true

echo "[watchdog] $(date) — starting scheduler. Ctrl-C to stop."

while true; do
    START=$(date +%s)
    # macOS: without caffeinate, system sleep (lid close/idle) stalls the
    # scheduler's own internal time.sleep() timer — the process stays alive
    # (this watchdog sees no crash) but oversleeps past its next scheduled
    # session/heartbeat by however long the Mac was asleep. caffeinate -s -i
    # keeps the system awake only while this process is running.
    if [[ "$(uname)" == "Darwin" ]] && command -v caffeinate >/dev/null 2>&1; then
        caffeinate -s -i "$PYTHON" "$SCHEDULER" || true
    else
        "$PYTHON" "$SCHEDULER" || true
    fi
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
