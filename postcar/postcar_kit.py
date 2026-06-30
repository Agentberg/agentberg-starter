"""
postcar_kit.py — Main 5-minute scheduler entry point for PostCar kit.

Usage (add to start.sh):
    python postcar/postcar_kit.py --agent-dir . &
"""

from __future__ import annotations

import argparse
import logging
import os
import py_compile
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

VERSION = "0.3.0"
CYCLE_SECONDS = 300
UPGRADE_CHECK_FILE = ".postcar_upgrade_check"   # stores epoch of last daily check
UPGRADE_FLAG_FILE = ".postcar_upgrade.flag"     # written after swap; signals restart needed
_GITHUB_KIT_URL = "https://raw.githubusercontent.com/ganeshnallasivam-cell/postcar-agent/main/postcar_kit.py"
_DEFAULT_RELAY_URL = "https://postcar.dev"

# ---------------------------------------------------------------------------
# upgrade helpers
# ---------------------------------------------------------------------------


def _should_check_upgrade(agent_dir: str) -> bool:
    check_file = Path(agent_dir, UPGRADE_CHECK_FILE)
    if not check_file.exists():
        return True
    try:
        return (time.time() - float(check_file.read_text().strip())) >= 86400
    except Exception:
        return True


def check_and_upgrade(
    agent_dir: str,
    client: "PostCarClient",
    logger: logging.Logger,
) -> bool:
    """Daily check: download newer postcar_kit.py, compile-test, atomic swap, write flag."""
    if not _should_check_upgrade(agent_dir):
        return False

    # Stamp immediately — don't hammer relay on every cycle if download fails
    Path(agent_dir, UPGRADE_CHECK_FILE).write_text(str(time.time()))

    try:
        version_info = client.get_version()
        if not version_info:
            return False
        remote_version = version_info.get("version", "")
        if not remote_version or remote_version == VERSION:
            logger.info(f"PostCar kit up to date (v{VERSION})")
            return False

        logger.info(f"New version available: {remote_version} (current: {VERSION}). Upgrading...")

        # Relay endpoint first, GitHub raw fallback
        new_content: str = ""
        for url in [f"{client.relay_url}/download/postcar_kit", _GITHUB_KIT_URL]:
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                    new_content = resp.read().decode("utf-8")
                if len(new_content) > 200:
                    break
            except Exception:
                continue

        if len(new_content) < 200:
            logger.warning("Downloaded content too short — aborting upgrade")
            return False

        kit_path = Path(__file__).resolve()
        tmp_path = kit_path.with_suffix(".py.new")
        tmp_path.write_text(new_content, encoding="utf-8")

        # Compile-test — reject corrupt downloads before touching live file
        try:
            py_compile.compile(str(tmp_path), doraise=True)
        except py_compile.PyCompileError as exc:
            logger.error(f"Upgrade compile failed: {exc} — aborting")
            tmp_path.unlink(missing_ok=True)
            return False

        # Atomic swap
        os.replace(str(tmp_path), str(kit_path))

        # Flag tells start.sh / orchestrator to restart the kit process
        Path(agent_dir, UPGRADE_FLAG_FILE).write_text(remote_version)
        logger.info(f"PostCar kit upgraded to v{remote_version}. Restart to activate.")
        return True

    except Exception as exc:
        logger.error(f"Upgrade check failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Ensure the directory containing this file is on sys.path so sibling modules
# (stress, trigger, inbox, llm, relay_client) are importable regardless of cwd.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from relay_client import PostCarClient  # noqa: E402


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def setup_logging(agent_dir: str) -> logging.Logger:
    """Create and return a logger that writes to .postcar.log and stderr."""
    logger = logging.getLogger("postcar_kit")
    logger.setLevel(logging.INFO)

    # Avoid adding duplicate handlers if setup_logging is called more than once.
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        file_handler = logging.FileHandler(
            os.path.join(agent_dir, ".postcar.log"), encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(fmt)

        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(fmt)

        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logger


# ---------------------------------------------------------------------------
# load_client
# ---------------------------------------------------------------------------


def load_client(agent_dir: str) -> Optional[PostCarClient]:
    """Load PostCar credentials from agent_dir and return a client.

    Returns None if any required env var (relay_url, agent_id, agent_key)
    is missing or blank.
    """
    try:
        return PostCarClient.from_env(agent_dir)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


def run_once(agent_dir: str, client: PostCarClient, logger: logging.Logger) -> None:
    """Execute one full PostCar cycle."""

    # 1. Compute stress summary
    from stress import compute_stress_summary  # noqa: PLC0415
    summary = compute_stress_summary(agent_dir)

    # 2. Send heartbeat
    client.heartbeat(stress=summary["level"], version=VERSION)

    # 3. Evaluate triggers
    from trigger import (  # noqa: PLC0415
        load_triggers,
        eval_triggers,
        dedup_check,
        mark_triggered,
        generate_query,
    )
    triggers = load_triggers(agent_dir)
    fired = eval_triggers(summary["indicators"], triggers)

    # 4. For each fired trigger that passes dedup, generate and send a query
    from llm import call_llm  # noqa: PLC0415

    for trigger in fired:
        if dedup_check(trigger["id"], agent_dir):
            query = generate_query(trigger, summary["indicators"], call_llm)
            client.send_query(
                query["tags"],
                query["question"],
                urgency=query["urgency"],
            )
            mark_triggered(trigger["id"], agent_dir, trigger["window_h"])
            logger.info(f"Query sent for trigger {trigger['id']}")

    # 5. Process incoming offers
    from inbox import execute_inbox_cycle  # noqa: PLC0415
    result = execute_inbox_cycle(client, agent_dir)

    # 6. Daily upgrade check (no-op if checked within last 24 h)
    check_and_upgrade(agent_dir, client, logger)

    # 7. Cycle summary log
    logger.info(
        f"Cycle done. stress={summary['level']}, "
        f"triggers_fired={len(fired)}, "
        f"offers_applied={result.get('applied', 0)}"
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="PostCar kit scheduler")
    parser.add_argument(
        "--agent-dir",
        default=".",
        help="Path to the agent directory (default: current directory)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle then exit (no daemon loop)",
    )
    args = parser.parse_args()

    agent_dir = os.path.abspath(args.agent_dir)
    logger = setup_logging(agent_dir)
    client = load_client(agent_dir)

    if client is None:
        logger.warning(
            "No credentials found (POSTCAR_RELAY_URL / POSTCAR_AGENT_ID / "
            "POSTCAR_AGENT_KEY). Running in observe-only mode."
        )

    if args.once:
        if client:
            run_once(agent_dir, client, logger)
        return

    # Write PID file so external tooling can track / stop the scheduler.
    pid_path = Path(agent_dir, ".postcar_running.pid")
    pid_path.write_text(str(os.getpid()))

    stop_event = threading.Event()

    def _handle_stop(signum, frame):  # noqa: ANN001, ANN202
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    logger.info(f"PostCar kit v{VERSION} started. Cycle every {CYCLE_SECONDS}s.")

    # First-run: auto-register if no POSTCAR_AGENT_ID in env
    if not os.environ.get("POSTCAR_AGENT_ID"):
        from context_builder import auto_register  # noqa: PLC0415
        profile = auto_register(agent_dir, client)
        if profile.get("registered"):
            # Reload client with newly assigned credentials
            os.environ["POSTCAR_AGENT_ID"] = profile["agent_id"]
            os.environ["POSTCAR_AGENT_KEY"] = profile["agent_key"]
            client = load_client(agent_dir)

    while not stop_event.is_set():
        try:
            if client:
                run_once(agent_dir, client, logger)
        except Exception as e:  # noqa: BLE001
            logger.error(str(e))
        stop_event.wait(timeout=CYCLE_SECONDS)

    pid_path.unlink(missing_ok=True)
    logger.info("PostCar kit stopped.")


if __name__ == "__main__":
    main()
