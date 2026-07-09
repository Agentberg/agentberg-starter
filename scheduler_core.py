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
STATE_FILE        = Path("logs/scheduler_state.json")
HEARTBEAT_FILE    = Path("logs/scheduler_heartbeat.json")
LOCK_FILE         = Path("logs/scheduler.lock")
SESSION_STATE_FILE = Path("logs/session_state.json")


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


def _check_and_report_crash() -> None:
    """Detect session crash via state flag written by agent.py.

    agent.py writes 'in_progress' at session start, 'ok' at session end.
    If send_network_heartbeat() (called in finally: after every session) sees
    'in_progress', the session raised an exception and never wrote 'ok'.
    """
    if not SESSION_STATE_FILE.exists():
        return
    try:
        state = json.loads(SESSION_STATE_FILE.read_text())
    except Exception:
        return
    if state.get("result") != "in_progress":
        return
    started_at = state.get("ts", "unknown")
    log.warning(f"[session] Crash detected (started {started_at}) — filing support trap")
    try:
        import config as cfg
        from agentberg import AgentbergClient
        kit_version = None
        manifest = Path(__file__).parent / "kit_manifest.json"
        if manifest.exists():
            kit_version = json.loads(manifest.read_text()).get("version")
        AgentbergClient(cfg.AGENTBERG_URL, cfg.AGENT_ID).report_issue(
            trap_name="SESSION_CRASH",
            concern="Session started but did not complete — unhandled exception detected",
            severity="high",
            diagnostics={"session_started_at": started_at},
            kit_version=kit_version,
        )
        SESSION_STATE_FILE.write_text(json.dumps({"result": "crash_reported", "ts": started_at}))
    except Exception as e:
        log.warning(f"[session] Crash trap filing failed: {e}")


def send_network_heartbeat() -> None:
    """Send heartbeat to Agentberg network. Also detects session crashes via state flag."""
    _check_and_report_crash()
    try:
        import config as cfg
        from agentberg import AgentbergClient
        kit_version = None
        manifest = Path(__file__).parent / "kit_manifest.json"
        if manifest.exists():
            kit_version = json.loads(manifest.read_text()).get("version")
        # universe_size intentionally omitted here -- this lightweight liveness ping
        # has no live candidate data (that only exists mid-session in agent.py). The
        # server COALESCEs missing fields onto the existing value, so leaving it out
        # preserves the real post-injection scan count from the last full session
        # instead of clobbering it with a lower static watchlist-size approximation.
        AgentbergClient(cfg.AGENTBERG_URL, cfg.AGENT_ID).send_heartbeat(
            kit_version=kit_version
        )
        log.debug("[heartbeat] sent")
    except Exception as e:
        log.error(f"[heartbeat] failed to send: {e}")


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


def run_session_guarded(run_fn) -> None:
    """Call run_fn() (typically agent.run_session). On unhandled exception, file a support trap
    and re-raise so the scheduler loop can recover normally.

    Agents call this instead of run_session() directly to get automatic crash reporting:

        import scheduler_core as core
        core.run_session_guarded(run_session)
    """
    try:
        run_fn()
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        log.error(f"[session] Unhandled crash — filing support trap: {exc}")
        try:
            import config as cfg
            from agentberg import AgentbergClient
            kit_version = None
            manifest = Path(__file__).parent / "kit_manifest.json"
            if manifest.exists():
                kit_version = json.loads(manifest.read_text()).get("version")
            AgentbergClient(cfg.AGENTBERG_URL, cfg.AGENT_ID).report_issue(
                trap_name="SESSION_CRASH",
                concern=f"Unhandled exception in run_session: {exc}",
                severity="high",
                diagnostics={"traceback": tb[-1500:]},
                kit_version=kit_version,
            )
        except Exception as trap_err:
            log.warning(f"[session] Trap filing failed: {trap_err}")
        raise
