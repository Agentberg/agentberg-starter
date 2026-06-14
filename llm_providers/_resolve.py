"""
_resolve.py — robustly locate a provider's CLI, even when the kit runs in a process
with a minimal PATH (cron, a double-clicked launcher, a GUI-spawned process).

The #1 support issue: an LLM CLI is installed where the user's *interactive* shell can
find it (e.g. Claude's native installer drops `claude` in ~/.local/bin and adds it to
PATH via .zshrc), but a non-login process inherits a minimal PATH, so shutil.which()
misses it. We try, in order: an explicit env override, the current PATH, the common
install dirs, and finally the user's login+interactive shell (the catch-all — it sees
exactly what their terminal sees).
"""

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def _candidate_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / ".local" / "bin",                    # Claude native installer, pipx, uv
        Path("/opt/homebrew/bin"),                  # Homebrew (Apple Silicon)
        Path("/usr/local/bin"),                     # Homebrew (Intel) / misc
        home / ".npm-global" / "bin",               # npm global (custom prefix)
        Path("/usr/local/lib/node_modules/.bin"),   # npm global (default prefix)
        home / ".bun" / "bin",                      # bun
        home / "bin",
    ]


def _login_shell_which(name: str) -> str | None:
    """Ask the user's login+interactive shell to resolve `name` from THEIR PATH."""
    if os.name == "nt":
        return None
    shell = os.environ.get("SHELL", "/bin/bash")
    try:
        out = subprocess.run(
            [shell, "-ilc", f"command -v {name} 2>/dev/null"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return None
    for line in reversed(out.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("/") and os.path.isfile(line) and os.access(line, os.X_OK):
            return line
    return None


@lru_cache(maxsize=None)
def find_cli(name: str, env_var: str | None = None) -> str | None:
    """
    Absolute path to CLI `name`, or None. Honors $<env_var> as an explicit override
    (e.g. CLAUDE_BIN). Result is cached for the process so the login-shell probe runs
    at most once per CLI.
    """
    if env_var:
        override = os.environ.get(env_var)
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
    found = shutil.which(name)
    if found:
        return found
    names = [name, name + ".cmd", name + ".exe"] if os.name == "nt" else [name]
    for d in _candidate_dirs():
        for n in names:
            fp = d / n
            if fp.is_file() and os.access(fp, os.X_OK):
                return str(fp)
    return _login_shell_which(name)
