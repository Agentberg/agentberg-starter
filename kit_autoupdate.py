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
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
MANIFEST_URL = "https://raw.githubusercontent.com/Agentberg/agentberg-starter/main/kit_manifest.json"
CHECK_INTERVAL_SECONDS = 1800  # 30 min


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


def check_and_apply() -> None:
    local = _local_version()
    remote = _remote_version()
    if remote is None:
        return
    if _vtuple(remote) <= _vtuple(local):
        print(f"[kit-autoupdate] up to date (v{local})")
        return
    print(f"[kit-autoupdate] update available: v{local} -> v{remote} — invoking upgrade.py")
    try:
        r = subprocess.run(
            [sys.executable, str(HERE / "upgrade.py"), "--no-restart"],
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
    if len(sys.argv) >= 2 and sys.argv[1] == "--install-daemon":
        install_daemon()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--check":
        check_and_apply()
    else:
        print("Usage: python3 kit_autoupdate.py --install-daemon | --check")
