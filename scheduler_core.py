"""
scheduler_core.py — Network, infrastructure, and upgrade plumbing (Cat 0).

Auto-updates on every kit release. Never customise this file — put agent-specific
trading schedule and session logic in scheduler.py (Cat B).

Responsibilities:
  - Market holiday calendar (kept current by the kit)
  - Local + network heartbeat
  - Daily auto-upgrade check (agentberg upgrade)
  - Shared state persistence (scheduler_state.json)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import zoneinfo
from pathlib import Path

log = logging.getLogger(__name__)

ET = zoneinfo.ZoneInfo("America/New_York")

# NYSE market holidays — the kit keeps this list current via Cat 0 updates.
# Do not edit manually; use scheduler.py for agent-specific schedule changes.
_MARKET_HOLIDAYS: set[str] = {
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

CRASH_RECOVERY_SECS = 60
STATE_FILE     = Path("logs/scheduler_state.json")
HEARTBEAT_FILE = Path("logs/scheduler_heartbeat.json")
LOCK_FILE      = Path("logs/scheduler.lock")


# ── Time utilities ──────────────────────────────────────────────────────────────

def now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)


def is_market_holiday(dt: datetime.datetime) -> bool:
    return dt.date().isoformat() in _MARKET_HOLIDAYS


# ── State persistence ───────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log.warning(f"[state] Could not load state: {e}")
    return {}


def save_state(last_ran: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(last_ran))
    except Exception as e:
        log.warning(f"[state] Could not save state: {e}")


# ── Heartbeat ───────────────────────────────────────────────────────────────────

def write_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.write_text(json.dumps({
            "ts":  datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "pid": os.getpid(),
        }))
    except Exception:
        pass


def send_network_heartbeat() -> None:
    """Send heartbeat to Agentberg network."""
    try:
        import cfg
        from agentberg import AgentbergClient
        kit_version = None
        manifest = Path(__file__).parent / "kit_manifest.json"
        if manifest.exists():
            kit_version = json.loads(manifest.read_text()).get("version")
        universe_size = sum(len(v) for v in cfg.WATCHLIST.values())
        AgentbergClient(cfg.AGENTBERG_URL, cfg.AGENT_ID).send_heartbeat(
            kit_version=kit_version, universe_size=universe_size
        )
        log.debug("[heartbeat] sent")
    except Exception as e:
        log.debug(f"[heartbeat] {e}")


# ── Auto-upgrade ────────────────────────────────────────────────────────────────

def auto_upgrade_check(last_ran: dict) -> bool:
    """Run `agentberg upgrade` once per day. Returns True if restart needed."""
    today = now_et().date().isoformat()
    if last_ran.get("upgrade_check") == today:
        return False
    last_ran["upgrade_check"] = today
    save_state(last_ran)
    try:
        result = subprocess.run(
            ["agentberg", "upgrade"],
            capture_output=True, text=True, timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 2:
            log.info(f"[upgrade] Upgrade applied — restarting\n{output[:500]}")
            return True
        if output:
            log.debug(f"[upgrade] {output[:200]}")
    except FileNotFoundError:
        log.debug("[upgrade] agentberg CLI not on PATH — skipping")
    except Exception as e:
        log.warning(f"[upgrade] check failed: {e}")
    return False
