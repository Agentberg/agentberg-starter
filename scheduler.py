"""
scheduler.py — Runs the agent on a market-hours schedule.

Sessions fire at:
  09:35 AM ET — opening session (after early volatility settles)
  03:50 PM ET — closing session (before market close)

Position monitor fires every 5 minutes during market hours to check
stop-loss and take-profit levels.

Keep this running in a separate terminal:
  python scheduler.py

Or run as a background process:
  nohup python scheduler.py >> logs/scheduler.log 2>&1 &
  ps aux | grep scheduler   # verify it's running
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import logging
import datetime
import zoneinfo
from pathlib import Path

from agent import run_session
import memory

Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("logs/scheduler.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

ET = zoneinfo.ZoneInfo("America/New_York")

# NYSE market holidays — update annually. Scheduler skips these dates entirely.
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

SESSION_TIMES = [
    datetime.time(9, 35),    # morning session — after opening volatility
    datetime.time(12, 0),    # midday session — lunch-hour momentum
    datetime.time(15, 50),   # afternoon session — before close
]

MONITOR_INTERVAL_SECS = 300   # check positions every 5 minutes
MARKET_OPEN  = datetime.time(9, 30)
MARKET_CLOSE = datetime.time(16, 0)


def _now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)


def _is_market_holiday(dt: datetime.datetime) -> bool:
    return dt.date().isoformat() in _MARKET_HOLIDAYS


def _is_market_hours() -> bool:
    now = _now_et()
    if now.weekday() >= 5 or _is_market_holiday(now):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _seconds_until(target_time: datetime.time) -> float:
    """Seconds until the next occurrence of target_time ET on a trading day."""
    now = _now_et()
    candidate = now.replace(hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    # Skip weekends and holidays
    while candidate.weekday() >= 5 or _is_market_holiday(candidate):
        candidate += datetime.timedelta(days=1)
    return (candidate - now).total_seconds()


def _next_session_time() -> datetime.time | None:
    """Return the next upcoming session time today, or None if past all sessions."""
    now_t = _now_et().time()
    for t in SESSION_TIMES:
        if t > now_t:
            return t
    return None


def _should_run_session(label: str, last_ran: dict) -> bool:
    today = _now_et().date().isoformat()
    return last_ran.get(label) != today


def _mark_ran(label: str, last_ran: dict):
    last_ran[label] = _now_et().date().isoformat()
    _save_state(last_ran)


def run_monitor():
    """Check open positions for stop-loss / take-profit (non-trading, read-only scan)."""
    from agent import check_positions
    try:
        check_positions()
    except Exception as e:
        log.error(f"[monitor] Error: {e}")


CRASH_RECOVERY_SECS = 60   # wait before resuming loop after unexpected error
STATE_FILE     = Path("logs/scheduler_state.json")
HEARTBEAT_FILE = Path("logs/scheduler_heartbeat.json")
LOCK_FILE      = Path("logs/scheduler.lock")


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log.warning(f"[state] Could not load state: {e}")
    return {}


def _save_state(last_ran: dict):
    try:
        STATE_FILE.write_text(json.dumps(last_ran))
    except Exception as e:
        log.warning(f"[state] Could not save state: {e}")


def _write_heartbeat():
    try:
        HEARTBEAT_FILE.write_text(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "pid": os.getpid(),
        }))
    except Exception:
        pass


def _send_network_heartbeat():
    """Send heartbeat to Agentberg — fallback for agents with a customized agent.py."""
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


def _auto_upgrade_check(last_ran: dict) -> bool:
    """Run `agentberg upgrade --auto` once per day. Returns True if upgrade applied (restart needed)."""
    today = _now_et().date().isoformat()
    if last_ran.get("upgrade_check") == today:
        return False
    last_ran["upgrade_check"] = today
    _save_state(last_ran)
    try:
        result = subprocess.run(
            ["agentberg", "upgrade", "--auto"],
            capture_output=True, text=True, timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        if "Applied" in output and "file(s)" in output:
            log.info(f"[upgrade] Upgrade applied — restarting to load new code\n{output[:500]}")
            return True
        if output:
            log.debug(f"[upgrade] {output[:200]}")
    except FileNotFoundError:
        log.debug("[upgrade] agentberg CLI not on PATH — skipping auto-check")
    except Exception as e:
        log.warning(f"[upgrade] check failed: {e}")
    return False


def _run_missed_sessions(last_ran: dict):
    """On startup, fire any sessions that passed while the scheduler was down."""
    now = _now_et()
    if now.weekday() >= 5 or _is_market_holiday(now):
        return
    for session_time in SESSION_TIMES:
        label = session_time.strftime("%H:%M")
        session_dt = now.replace(
            hour=session_time.hour, minute=session_time.minute, second=0, microsecond=0
        )
        if now > session_dt and _should_run_session(label, last_ran):
            log.info(f"[{label}] Missed session detected — running now (recovery)")
            try:
                run_session()
                _mark_ran(label, last_ran)
                log.info(f"[{label}] Missed session complete")
            except Exception as e:
                log.error(f"[{label}] Missed session failed: {e} — marked done, will not retry; next scheduled session fires normally")
                _mark_ran(label, last_ran)


def main():
    # Prevent two scheduler instances from running simultaneously — the root cause of
    # duplicate bracket orders (both instances see the session as unfired, both execute).
    if LOCK_FILE.exists():
        try:
            existing_pid = int(LOCK_FILE.read_text().strip())
            os.kill(existing_pid, 0)   # raises ProcessLookupError if dead
            log.error(f"[startup] Scheduler already running (PID {existing_pid}). Exiting. "
                      f"Kill it first: kill {existing_pid}")
            return
        except (ProcessLookupError, PermissionError):
            log.warning("[startup] Stale lock file found — previous process is gone. Clearing and continuing.")
    LOCK_FILE.write_text(str(os.getpid()))

    try:
        _main_loop()
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def _main_loop():
    try:
        memory.init_db()
    except Exception as e:
        log.error(f"[startup] memory.init_db failed: {e} — continuing without persistence")

    last_ran: dict[str, str] = _load_state()
    log.info("Scheduler started — sessions at 09:35 and 15:50 ET")
    _run_missed_sessions(last_ran)
    if _auto_upgrade_check(last_ran):
        sys.exit(0)  # watchdog (agentberg start) restarts with upgraded code

    while True:
        try:
            _write_heartbeat()
            now = _now_et()

            # ── Full sessions ──────────────────────────────────────────────────────
            if now.weekday() >= 5 or _is_market_holiday(now):
                wait = max(60, _seconds_until(SESSION_TIMES[0]) - 1800)
                log.info(f"Market closed (holiday/weekend) — sleeping {wait/3600:.1f}h")
                time.sleep(wait)
                continue

            for session_time in SESSION_TIMES:
                label = session_time.strftime("%H:%M")
                session_today = now.replace(
                    hour=session_time.hour, minute=session_time.minute,
                    second=0, microsecond=0
                )
                elapsed_secs = (now - session_today).total_seconds()

                # Fire once past session time, within one monitor cycle grace window.
                # The old ±60s abs() check was smaller than the 5-min sleep, causing missed sessions.
                if 0 <= elapsed_secs < (MONITOR_INTERVAL_SECS + 60) and _should_run_session(label, last_ran):
                    log.info(f"[{label}] Firing session")
                    try:
                        run_session()
                        _mark_ran(label, last_ran)
                        log.info(f"[{label}] Session complete")
                    except Exception as e:
                        log.error(f"[{label}] Session failed: {e}")
                        _mark_ran(label, last_ran)   # don't retry — wait for next window
                    finally:
                        _send_network_heartbeat()

            # ── Position monitor ───────────────────────────────────────────────────
            if _is_market_hours():
                run_monitor()
                log.debug("[monitor] Position check done")

            # ── Sleep ──────────────────────────────────────────────────────────────
            if _is_market_hours():
                time.sleep(MONITOR_INTERVAL_SECS)
            else:
                # Outside market hours: sleep until 30 min before next session
                next_t = _next_session_time()
                if next_t:
                    wait = _seconds_until(next_t) - 1800
                else:
                    wait = _seconds_until(SESSION_TIMES[0]) - 1800
                wait = max(60, wait)
                log.info(f"Market closed — sleeping {wait/3600:.1f}h")
                time.sleep(wait)

        except Exception as e:
            log.error(f"[scheduler] Unexpected error — recovering in {CRASH_RECOVERY_SECS}s: {e}", exc_info=True)
            time.sleep(CRASH_RECOVERY_SECS)


if __name__ == "__main__":
    main()
