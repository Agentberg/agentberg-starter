#!/usr/bin/env bash
# postcar_launch.sh — PostCar sidecar bootstrap.
#
# Cat A (safe to auto-apply): self-contained comms-sidecar plumbing, never
# agent customisation. Kept as its own file — not inlined in run.sh — so
# PostCar updates never touch run.sh, which is Cat B (agent's own startup
# customisation, never auto-applied). Called by run.sh; failures here must
# never take down the watchdog, so run.sh invokes this with `|| true`.
#
# Clones once, self-updates via `git pull` on every cycle.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

if [ ! -d "$SCRIPT_DIR/postcar/.git" ]; then
    echo "[startup] Cloning PostCar sidecar…"
    rm -rf "$SCRIPT_DIR/postcar"
    git clone --quiet https://github.com/postcar-agent/postcar-agent.git "$SCRIPT_DIR/postcar" || true
fi
if [ -f "$SCRIPT_DIR/postcar/postcar_check.py" ]; then
    "$PYTHON" "$SCRIPT_DIR/postcar/postcar_check.py" --check
fi
