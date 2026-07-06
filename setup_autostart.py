#!/usr/bin/env python3
"""
setup_autostart.py — Register this agent as a supervised background service.

macOS  → installs ~/Library/LaunchAgents/ai.agentberg.<agent_id>.plist (launchd).
Linux  → installs a systemd --user unit (~/.config/systemd/user/agentberg-<agent_id>.service).

Either way: the scheduler starts on login/boot and restarts automatically if it
crashes. Without this, `python scheduler.py` (or `nohup ./run.sh &`) has NO
supervisor — if the process dies (crash, reboot, killed terminal), nothing
brings it back and the agent silently goes dark.

Run once from inside your agent folder:
    python3 setup_autostart.py

To uninstall:
    python3 setup_autostart.py --uninstall
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"


def _read_agent_id(folder: Path) -> str:
    # Try .agent_id file first (written by register on first run)
    aid_file = folder / ".agent_id"
    if aid_file.exists():
        v = aid_file.read_text().strip()
        if v:
            return v
    # Fall back to .env AGENT_ID
    env_file = folder / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            m = re.match(r"^\s*AGENT_ID\s*=\s*['\"]?([^'\"#\s]+)", line)
            if m:
                return m.group(1)
    return folder.name.lower().replace(" ", "-")


def _safe_label(agent_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9.\-]", "-", agent_id).lower()


def _python_path() -> str:
    venv = Path(sys.executable).parent.parent
    if (venv / "bin" / "python3").exists():
        return str(venv / "bin" / "python3")
    return sys.executable


def _exec_parts(folder: Path, python: str) -> list[str]:
    run_sh = folder / "run.sh"
    if run_sh.exists():
        return ["/bin/bash", str(run_sh)]
    parts = [python, str(folder / "scheduler.py")]
    # macOS: prefix with caffeinate so system sleep can't stall scheduler.py's
    # own time.sleep() timer (same issue run.sh's loop guards against — see
    # its caffeinate comment). run.sh's own path already handles this itself;
    # this is only for the no-run.sh direct-scheduler.py fallback.
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        parts = ["/usr/bin/caffeinate", "-s", "-i"] + parts
    return parts


# ── macOS (launchd) ──────────────────────────────────────────────────────────

def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def _generate_plist(label: str, folder: Path, python: str) -> str:
    logs = folder / "logs"
    stdout = str(logs / "autostart.log")
    stderr = str(logs / "autostart-err.log")
    parts = _exec_parts(folder, python)
    prog_args = "\n".join(f"        <string>{p}</string>" for p in parts)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
{prog_args}
    </array>

    <key>WorkingDirectory</key>
    <string>{folder}</string>

    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{stdout}</string>

    <key>StandardErrorPath</key>
    <string>{stderr}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
"""


def install_launchd(folder: Path) -> None:
    agent_id = _read_agent_id(folder)
    label = f"ai.agentberg.{_safe_label(agent_id)}"
    plist_path = _plist_path(label)
    python = _python_path()

    (folder / "logs").mkdir(exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    plist_path.write_text(_generate_plist(label, folder, python))
    print(f"  Wrote {plist_path}")

    result = subprocess.run(["launchctl", "load", str(plist_path)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR loading plist: {result.stderr.strip()}")
        sys.exit(1)

    print(f"  Loaded service: {label}")
    print(f"  Agent '{agent_id}' will now start automatically on login and restart on crash.")
    print(f"  Logs → {folder}/logs/autostart.log")
    print()
    print(f"  To stop:      launchctl unload {plist_path}")
    print(f"  To uninstall: python3 setup_autostart.py --uninstall")


def uninstall_launchd(folder: Path) -> None:
    agent_id = _read_agent_id(folder)
    label = f"ai.agentberg.{_safe_label(agent_id)}"
    plist_path = _plist_path(label)

    if not plist_path.exists():
        print(f"  No plist found at {plist_path} — nothing to uninstall.")
        return

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink()
    print(f"  Unloaded and removed {plist_path}")


# ── Linux (systemd --user) ───────────────────────────────────────────────────

def _unit_name(agent_id: str) -> str:
    return f"agentberg-{_safe_label(agent_id)}.service"


def _unit_path(unit: str) -> Path:
    return SYSTEMD_USER_DIR / unit


def _generate_unit(agent_id: str, folder: Path, python: str) -> str:
    exec_start = " ".join(_exec_parts(folder, python))
    return f"""[Unit]
Description=Agentberg agent scheduler ({agent_id})
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory={folder}
ExecStart={exec_start}
Restart=always
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

[Install]
WantedBy=default.target
"""


def _systemctl_user_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(["systemctl", "--user", "status"], capture_output=True, text=True)
    # rc 0/3/4 all mean the user manager is reachable (3/4 = no units loaded yet)
    return result.returncode in (0, 1, 3, 4) and "Failed to connect" not in result.stderr


def install_systemd(folder: Path) -> None:
    if not _systemctl_user_available():
        sys.exit(
            "ERROR: systemd --user is not reachable in this session (common on bare SSH\n"
            "sessions / containers without a logind session). Fix:\n"
            "  1. Ensure you're on a real login session (not `su`), or\n"
            "  2. Run: export XDG_RUNTIME_DIR=/run/user/$(id -u) and retry, or\n"
            "  3. Ask your operator for a root-level systemd unit instead."
        )

    agent_id = _read_agent_id(folder)
    unit = _unit_name(agent_id)
    unit_path = _unit_path(unit)
    python = _python_path()

    (folder / "logs").mkdir(exist_ok=True)
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    unit_path.write_text(_generate_unit(agent_id, folder, python))
    print(f"  Wrote {unit_path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    result = subprocess.run(["systemctl", "--user", "enable", "--now", unit],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR enabling unit: {result.stderr.strip()}")
        sys.exit(1)

    print(f"  Enabled + started: {unit}")
    print(f"  Agent '{agent_id}' will now start automatically and restart on crash.")

    linger = subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")],
                            capture_output=True, text=True)
    if linger.returncode == 0:
        print(f"  Linger enabled — service also survives logout (headless VPS safe).")
    else:
        print(f"  ⚠ Could not enable linger automatically ({linger.stderr.strip() or 'no permission'}).")
        print(f"    On a VPS, run as root: loginctl enable-linger {os.environ.get('USER', '<user>')}")
        print(f"    Without it, the service stops when your SSH session ends.")

    print()
    print(f"  Logs:         journalctl --user -u {unit} -f")
    print(f"  To stop:      systemctl --user stop {unit}")
    print(f"  To uninstall: python3 setup_autostart.py --uninstall")


def uninstall_systemd(folder: Path) -> None:
    agent_id = _read_agent_id(folder)
    unit = _unit_name(agent_id)
    unit_path = _unit_path(unit)

    if not unit_path.exists():
        print(f"  No unit found at {unit_path} — nothing to uninstall.")
        return

    subprocess.run(["systemctl", "--user", "disable", "--now", unit], capture_output=True)
    unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print(f"  Disabled and removed {unit_path}")


# ── entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    folder = Path.cwd()
    if not (folder / "scheduler.py").exists():
        sys.exit("ERROR: Run this from inside your agent folder (scheduler.py not found).")

    uninstall = "--uninstall" in sys.argv

    if sys.platform == "darwin":
        uninstall_launchd(folder) if uninstall else install_launchd(folder)
    elif sys.platform.startswith("linux"):
        uninstall_systemd(folder) if uninstall else install_systemd(folder)
    else:
        sys.exit(f"ERROR: setup_autostart.py does not support platform '{sys.platform}' "
                 f"(supported: macOS, Linux).")


if __name__ == "__main__":
    main()
