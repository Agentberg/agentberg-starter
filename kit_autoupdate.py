#!/usr/bin/env python3
"""
kit_autoupdate.py — standalone 30-min upgrade-check daemon for agentberg-starter.

Runs independently of the trading scheduler so a crashed/idle agent still stays
current -- agent.py's own Step 9 upgrade check only fires inside a live trading
session, throttled to once/24h, so a dead scheduler process silently freezes
the kit version forever (confirmed real: alphaforge, kit v2.10.16, ~22h
heartbeat silence, auto-upgrade never ran because nothing triggered it).

Cheap by design: only fetches kit_manifest.json (a few KB, via raw GitHub
content) to compare versions every cycle. The full kit download + file apply
(upgrade.py) only runs when a newer Cat 0/A version is actually available --
not a full tarball pull on every tick.

NOTE: files in upgrade.py's CAT_B_PROTECT set (risk.py, config.py, identity.py,
character.py, alpaca.py, structures.py, setup.py, run.sh) are NEVER auto-applied
by upgrade.py regardless of how often this checks or what category the
changelog lists -- that's a deliberate "never silently overwrite trading edge"
guard, not a bug this script works around. A faster check interval speeds up
every OTHER file (agent.py, llm.py, knowledge.py, etc.) but does not get
postcar (run.sh) or the guidance-overrides reader (config.py) onto an
already-installed agent automatically -- those still need a one-time manual
`python3 upgrade.py` or re-run of setup by the operator.

Install the recurring check:  python3 kit_autoupdate.py --install-daemon
Run a single check manually:  python3 kit_autoupdate.py --check
Force config.py/risk.py sync right now (no daemon needed): python3 kit_autoupdate.py --force-sync

To enable the config.py/risk.py force-sync diagnostic on this agent, add to .env:
    AGENTBERG_FORCE_SYNC_CONFIG=true
Remove that line (or set to false) to go back to normal Cat B protection.
"""

import hashlib
import json
import os

# Same NO_PROXY fix as upgrade.py -- see that file's comment for the full
# diagnosis (a stray system proxy, not a missing CA, confirmed live
# 2026-07-06 on SMoney). Must run before any HTTPS call this daemon makes.
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
MANIFEST_URL = "https://raw.githubusercontent.com/Agentberg/agentberg-starter/main/kit_manifest.json"
RAW_BASE = "https://raw.githubusercontent.com/Agentberg/agentberg-starter/main"
CHECK_INTERVAL_SECONDS = 1800  # 30 min

# Diagnostic-only, opt-in: force config.py + risk.py to byte-match upstream
# `main` every cycle, regardless of version/category -- used to rule out
# config/logic divergence as a cause of fleet-wide underperformance. OFF by
# default for every kit user; only agents with AGENTBERG_FORCE_SYNC_CONFIG=true
# in their .env do this. Deliberately bypasses upgrade.py's CAT_B_PROTECT
# guard for just these two files -- character.json-driven personalization is
# untouched (a separate file, re-applied by config.py's own overlay logic on
# every run), but any hand-edit made directly to config.py/risk.py source
# outside of character.json gets silently overwritten every 30 min while this
# is on. Meant to be temporary -- turn it back off once config is ruled out
# (or ruled in) as a cause.
FORCE_SYNC_FILES = ("config.py", "risk.py")


def _load_env() -> None:
    """Minimal .env loader, no python-dotenv dependency -- same approach
    upgrade.py already uses. This script is invoked standalone by
    launchd/cron, which does not source .env the way the main agent process
    (via python-dotenv in config.py) does, so AGENTBERG_FORCE_SYNC_CONFIG
    would silently never be seen without this. Never overwrites a variable
    already set in the real process environment."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _vtuple(v: str) -> tuple:
    try:
        return tuple(int(p) for p in v.split("."))
    except Exception:
        return (0,)


def _local_version() -> str:
    try:
        return json.loads((HERE / "kit_manifest.json").read_text()).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def _remote_version() -> str | None:
    try:
        req = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": "agentberg-kit-autoupdate"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("version")
    except Exception as e:
        print(f"[kit-autoupdate] manifest check failed: {e}")
        return None


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def force_sync_config() -> None:
    """Unconditionally overwrite config.py/risk.py with the current upstream
    `main` content, independent of version comparison -- runs every cycle
    this is enabled, not just when a new kit version happens to touch these
    files. Backs up the previous local copy (timestamped) before overwriting
    so nothing is unrecoverable if this needs to be reverted per-agent."""
    if os.environ.get("AGENTBERG_FORCE_SYNC_CONFIG", "").strip().lower() != "true":
        return
    for rel in FORCE_SYNC_FILES:
        local_path = HERE / rel
        try:
            req = urllib.request.Request(f"{RAW_BASE}/{rel}", headers={"User-Agent": "agentberg-kit-autoupdate"})
            with urllib.request.urlopen(req, timeout=15) as r:
                upstream = r.read()
        except Exception as e:
            print(f"[kit-autoupdate] force-sync fetch failed for {rel}: {e}")
            continue
        local_hash = _sha256_bytes(local_path.read_bytes()) if local_path.exists() else None
        if local_hash == _sha256_bytes(upstream):
            continue
        ts = time.strftime("%Y%m%d-%H%M%S")
        if local_path.exists():
            backup_path = HERE / f"{rel}.pre-forcesync-{ts}.bak"
            backup_path.write_bytes(local_path.read_bytes())
        local_path.write_bytes(upstream)
        print(f"[kit-autoupdate] force-synced {rel} to upstream main (backup: {rel}.pre-forcesync-{ts}.bak)")


def check_and_apply() -> None:
    force_sync_config()

    local = _local_version()
    remote = _remote_version()
    if remote is None:
        return
    if _vtuple(remote) <= _vtuple(local):
        print(f"[kit-autoupdate] up to date (v{local})")
        return
    print(f"[kit-autoupdate] update available: v{local} -> v{remote} — invoking upgrade.py")
    try:
        # No --no-restart here: that flag exists for when the SCHEDULER itself
        # calls upgrade.py mid-session (scheduler_core.auto_upgrade_check(),
        # which does its own sys.exit(0) instead of self-restarting). This
        # daemon is a separate standalone process, so it's the one thing that
        # CAN safely trigger the restart -- confirmed real 2026-07-06: gpower
        # picked up interconnect.py on disk at 13:48 (this daemon applied it)
        # but its scheduler.py process (started 08:18AM) never restarted, so
        # the live process kept running for 5.5+ hours with none of the new
        # code loaded -- 3 real peer messages sat unanswered as a direct
        # result. upgrade.py's own _restart_scheduler() already does this
        # safely (SIGTERM via logs/scheduler.lock's PID, then relaunch) --
        # just stop suppressing it here.
        r = subprocess.run(
            [sys.executable, str(HERE / "upgrade.py")],
            capture_output=True, text=True, timeout=180, cwd=str(HERE),
        )
        print(r.stdout[-1500:])
        if r.returncode != 0:
            print(f"[kit-autoupdate] upgrade.py failed: {(r.stderr or '')[:300]}")
    except Exception as e:
        print(f"[kit-autoupdate] upgrade.py invocation error: {e}")


def install_daemon() -> None:
    """Idempotent, one-shot install -- same lesson already learned the hard
    way by postcar's own daemon installer: never unload+reload an
    already-installed launchd job. macOS's background-task-management
    throttle can deregister it outright on a redundant reload (real outage,
    2026-07-01: postcar's --check job vanished this way on SMoney, Gpower,
    and miniG). Gate on a sentinel file; once installed, never touch it
    again from this script."""
    sentinel = HERE / ".kit_autoupdate_daemon_installed"
    if sentinel.exists():
        print("[kit-autoupdate] daemon already installed, skipping")
        return

    python_bin = sys.executable
    script_path = str(Path(__file__).resolve())
    agent_name = HERE.resolve().name.replace(" ", "_").lower()

    if sys.platform == "darwin":
        label = f"com.agentberg.{agent_name}.kitcheck"
        plist_dir = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(plist_dir, exist_ok=True)
        plist_path = os.path.join(plist_dir, f"{label}.plist")
        log_path = str(HERE / ".kit_autoupdate.log")
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{script_path}</string>
        <string>--check</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{HERE}</string>
    <key>StartInterval</key>
    <integer>{CHECK_INTERVAL_SECONDS}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:{os.path.dirname(python_bin)}</string>
    </dict>
</dict>
</plist>"""
        try:
            with open(plist_path, "w") as f:
                f.write(plist)
            subprocess.run(["launchctl", "load", "-w", plist_path], capture_output=True, check=True)
            sentinel.write_text("launchd")
            print(f"[kit-autoupdate] daemon installed: {label} (every {CHECK_INTERVAL_SECONDS // 60} min)")
        except Exception as e:
            print(f"[kit-autoupdate] launchd install failed: {e}")
    else:
        try:
            minute_expr = f"*/{CHECK_INTERVAL_SECONDS // 60}"
            cron_line = (f"{minute_expr} * * * * cd {HERE} && {python_bin} {script_path} --check "
                         f">> {HERE}/.kit_autoupdate.log 2>&1")
            existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
            if cron_line not in existing:
                new_crontab = existing.rstrip("\n") + "\n" + cron_line + "\n"
                subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            sentinel.write_text("cron")
            print(f"[kit-autoupdate] daemon installed via cron (every {CHECK_INTERVAL_SECONDS // 60} min)")
        except Exception as e:
            print(f"[kit-autoupdate] cron install failed: {e}")


if __name__ == "__main__":
    _load_env()
    if len(sys.argv) >= 2 and sys.argv[1] == "--install-daemon":
        install_daemon()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--check":
        check_and_apply()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--force-sync":
        force_sync_config()
    else:
        print("Usage: python3 kit_autoupdate.py --install-daemon | --check | --force-sync")
