"""
scheduler.py — the main loop: fires sessions on schedule, heartbeat, holiday/
weekend detection, crash recovery. Structural mechanism with zero human-set
values -- Cat 0/A, updates freely with the kit.

Your own schedule (when sessions fire, market hours, position-check cadence)
lives in schedule_config.py (Cat B) -- edit that file, not this one.

Run:
  python scheduler.py

Background:
  nohup python scheduler.py >> logs/scheduler.log 2>&1 &
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path as _Path

# ── Prerequisite bootstrap — runs before any third-party imports ─────────────
# Checks for required packages and auto-installs from requirements.txt if any
# are missing. Silent if everything is already installed.
def _ensure_prerequisites() -> None:
    _req_file = _Path(__file__).parent / "requirements.txt"
    if not _req_file.exists():
        return
    packages = [
        line.split(">=")[0].split("==")[0].split("[")[0].strip().replace("-", "_").lower()
        for line in _req_file.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    missing = []
    for pkg in packages:
        import_name = {"python_dotenv": "dotenv"}.get(pkg, pkg)
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[startup] Missing packages: {', '.join(missing)} — auto-installing...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(_req_file)],
            stdout=subprocess.DEVNULL,
        )
        print("[startup] Prerequisites installed — continuing")

_ensure_prerequisites()
# ─────────────────────────────────────────────────────────────────────────────

import datetime
import logging
import time

from agent import run_session
import memory
import scheduler_core as core
from schedule_config import SESSION_TIMES, MONITOR_INTERVAL_SECS, MARKET_OPEN, MARKET_CLOSE

_Path("logs").mkdir(exist_ok=True)

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

# How often heartbeat fires during a long idle wait (holiday/weekend, or the
# gap between sessions) -- independent of MONITOR_INTERVAL_SECS, which is only
# for in-session position checks. Infrastructure, not agent customisation --
# lives here, not in schedule_config.py.
HEARTBEAT_IDLE_INTERVAL_SECS = 3600

# ── Internal helpers ────────────────────────────────────────────────────────────

def _is_market_hours() -> bool:
    now = core.now_et()
    if now.weekday() >= 5 or core.is_market_holiday(now):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _seconds_until(target_time: datetime.time) -> float:
    now = core.now_et()
    candidate = now.replace(
        hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    while candidate.weekday() >= 5 or core.is_market_holiday(candidate):
        candidate += datetime.timedelta(days=1)
    return (candidate - now).total_seconds()


def _next_session_time() -> datetime.time | None:
    now_t = core.now_et().time()
    for t in SESSION_TIMES:
        if t > now_t:
            return t
    return None


def _should_run_session(label: str, last_ran: dict) -> bool:
    return last_ran.get(label) != core.now_et().date().isoformat()


def _mark_ran(label: str, last_ran: dict) -> None:
    last_ran[label] = core.now_et().date().isoformat()
    core.save_state(last_ran)


def _sleep_with_heartbeat(total_seconds: float) -> None:
    """Sleep in chunks, sending a heartbeat between each -- not one giant blocking
    time.sleep(). A holiday/long-weekend wait can span 70+ hours; without this, the
    network's last_seen_at goes dark for the entire wait (network heartbeat only
    otherwise fires from the finally: block after a trading session actually runs,
    which never happens on a holiday). This is heartbeat only -- it does not call
    auto_upgrade_check or anything upgrade-related; that stays exactly where it is,
    checked once at startup before the main loop begins."""
    remaining = total_seconds
    while remaining > 0:
        chunk = min(HEARTBEAT_IDLE_INTERVAL_SECS, remaining)
        time.sleep(chunk)
        remaining -= chunk
        core.write_heartbeat()
        core.send_network_heartbeat()
        _run_interconnect()


def run_monitor() -> None:
    from agent import check_positions
    try:
        check_positions()
    except Exception as e:
        log.error(f"[monitor] Error: {e}")
    _run_interconnect()


def _run_interconnect() -> None:
    """Host-agent side of postcar's draft/confirm/report loop (interconnect.py) --
    runs independently of check_positions() (which early-returns with zero open
    positions) since postcar messages arrive regardless of position state. Called
    both from the market-hours monitor cycle (MONITOR_INTERVAL_SECS, 5 min) and
    the idle heartbeat cycle (hourly when market's closed) -- reuses existing
    timers rather than adding a third independent one."""
    try:
        import interconnect
        interconnect.run_all()
    except Exception as e:
        log.error(f"[interconnect] Error: {e}")


def _run_missed_sessions(last_ran: dict) -> None:
    now = core.now_et()
    if now.weekday() >= 5 or core.is_market_holiday(now):
        return
    for session_time in SESSION_TIMES:
        label = session_time.strftime("%H:%M")
        session_dt = now.replace(
            hour=session_time.hour, minute=session_time.minute, second=0, microsecond=0
        )
        if now > session_dt and _should_run_session(label, last_ran):
            log.info(f"[{label}] Missed session — running now (recovery)")
            try:
                run_session()
                _mark_ran(label, last_ran)
                log.info(f"[{label}] Missed session complete")
            except Exception as e:
                log.error(f"[{label}] Missed session failed: {e} — marking done, not retrying")
                _mark_ran(label, last_ran)


# ── Main loop ───────────────────────────────────────────────────────────────────

def _main_loop() -> None:
    try:
        memory.init_db()
    except Exception as e:
        log.error(f"[startup] memory.init_db failed: {e} — continuing without persistence")

    last_ran: dict[str, str] = core.load_state()
    log.info("Scheduler started — sessions at 09:35, 12:00, 15:50 ET")
    _run_missed_sessions(last_ran)
    if core.auto_upgrade_check(last_ran):
        sys.exit(0)  # agentberg start watchdog restarts with upgraded code

    while True:
        try:
            core.write_heartbeat()
            now = core.now_et()

            if now.weekday() >= 5 or core.is_market_holiday(now):
                wait = max(60, _seconds_until(SESSION_TIMES[0]) - 1800)
                log.info(f"Market closed (holiday/weekend) — sleeping {wait/3600:.1f}h "
                         f"(heartbeat every {HEARTBEAT_IDLE_INTERVAL_SECS/3600:.1f}h)")
                _sleep_with_heartbeat(wait)
                continue

            for session_time in SESSION_TIMES:
                label = session_time.strftime("%H:%M")
                session_today = now.replace(
                    hour=session_time.hour, minute=session_time.minute,
                    second=0, microsecond=0
                )
                elapsed_secs = (now - session_today).total_seconds()

                if 0 <= elapsed_secs < (MONITOR_INTERVAL_SECS + 60) and _should_run_session(label, last_ran):
                    log.info(f"[{label}] Firing session")
                    try:
                        run_session()
                        _mark_ran(label, last_ran)
                        log.info(f"[{label}] Session complete")
                    except Exception as e:
                        log.error(f"[{label}] Session failed: {e}")
                        _mark_ran(label, last_ran)
                    finally:
                        core.send_network_heartbeat()

            if _is_market_hours():
                run_monitor()
                log.debug("[monitor] Position check done")

            if not _is_market_hours() and _should_run_session("eod_reconcile", last_ran):
                log.info("[eod] Reconciling ledger against broker fills...")
                try:
                    from agent import eod_reconcile
                    eod_reconcile()
                except Exception as e:
                    log.error(f"[eod] reconcile failed: {e}")
                _mark_ran("eod_reconcile", last_ran)

            if _is_market_hours():
                time.sleep(MONITOR_INTERVAL_SECS)
            else:
                next_t = _next_session_time()
                wait = _seconds_until(next_t if next_t else SESSION_TIMES[0]) - 1800
                wait = max(60, wait)
                log.info(f"Market closed — sleeping {wait/3600:.1f}h")
                _sleep_with_heartbeat(wait)

        except Exception as e:
            log.error(
                f"[scheduler] Unexpected error — recovering in {core.CRASH_RECOVERY_SECS}s: {e}",
                exc_info=True,
            )
            time.sleep(core.CRASH_RECOVERY_SECS)


def main() -> None:
    if core.LOCK_FILE.exists():
        try:
            existing_pid = int(core.LOCK_FILE.read_text().strip())
            import os
            os.kill(existing_pid, 0)
            log.error(
                f"[startup] Scheduler already running (PID {existing_pid}). "
                f"Kill it first: kill {existing_pid}"
            )
            return
        except (ProcessLookupError, PermissionError):
            log.warning("[startup] Stale lock — previous process gone. Clearing and continuing.")
    core.LOCK_FILE.write_text(str(__import__("os").getpid()))
    try:
        _main_loop()
    finally:
        core.LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
