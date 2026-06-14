"""
agentberg CLI — install, scaffold, run, and chat with your Agentberg trading agent.

  agentberg init     scaffold an editable trader folder, choose your LLM, write .env
  agentberg run      run one trading session in your folder
  agentberg start    run the live scheduler (market-hours loop)
  agentberg chat     open your chosen LLM in the trader folder
  agentberg update   refresh the kit in your folder (pull-to-review)

The CLI is the front door; the kit (agentberg-starter) is the engine. `init` copies
an EDITABLE kit into your folder — it's yours to edit, nothing is hidden. The CLI is
stdlib-only so it installs cleanly via pipx/uv with no build step.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

KIT_TARBALL = (
    "https://github.com/ganeshnallasivam-cell/agentberg-starter/"
    "archive/refs/heads/main.tar.gz"
)
STATE_DIR = Path(os.path.expanduser("~/.agentberg"))
STATE_FILE = STATE_DIR / "cli.json"
DEFAULT_DIR = Path(os.path.expanduser("~/agentberg-trader"))

# provider key -> (LLM_PROVIDER value, interactive chat CLI command or None)
PROVIDERS: dict[str, tuple[str, str | None]] = {
    "claude":   ("claude",   "claude"),
    "gemini":   ("gemini",   "agy"),
    "openai":   ("openai",   "codex"),
    "deepseek": ("deepseek", None),   # API only — no interactive chat REPL
    "none":     ("",         None),   # rule-based, no LLM
}

# Opt-in installer commands per provider (verified 2026-06). Sign-in stays manual.
# All three CLIs install into ~/.local/bin, which the kit's resolver already finds.
LLM_INSTALL: dict[str, dict] = {
    "claude": {
        "posix": "curl -fsSL https://claude.ai/install.sh | bash",
        "nt": 'powershell -ExecutionPolicy ByPass -c "irm https://claude.ai/install.ps1 | iex"',
        "signin": "claude",
    },
    "gemini": {
        "posix": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
        "nt": "curl -fsSL https://antigravity.google/cli/install.cmd -o install.cmd && install.cmd && del install.cmd",
        "signin": "agy",
    },
    "openai": {
        "posix": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
        "nt": 'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"',
        "signin": "codex",
    },
    "deepseek": {
        "posix": f'"{sys.executable}" -m pip install --user openai',
        "nt": f'"{sys.executable}" -m pip install --user openai',
        "signin": None,   # API key in .env, no sign-in
    },
}

# Not copied into the user's editable folder (CLI/dev/packaging only).
_SCAFFOLD_EXCLUDE = {"agentberg_cli", "pyproject.toml", ".github", "tests", "__pycache__"}


# ── state ───────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(d, indent=2))


def _folder(args) -> Path:
    d = getattr(args, "dir", None) or _load_state().get("folder")
    if not d:
        sys.exit("No trader folder known — run `agentberg init` first (or pass --dir).")
    return Path(os.path.expanduser(d))


# ── scaffolding ─────────────────────────────────────────────────────────────────

def _download_kit(target: Path) -> None:
    """Download the latest kit tarball and extract the editable files into target."""
    print("  fetching the latest kit…")
    req = urllib.request.Request(KIT_TARBALL, headers={"User-Agent": "agentberg-cli"})
    with urllib.request.urlopen(req, timeout=60) as resp:   # follows redirects
        data = resp.read()
    target.mkdir(parents=True, exist_ok=True)
    target_root = target.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        root = members[0].name.split("/")[0] if members else ""
        for m in members:
            rel = m.name[len(root) + 1:] if m.name.startswith(root + "/") else m.name
            if not rel or rel.split("/")[0] in _SCAFFOLD_EXCLUDE:
                continue
            dest = (target / rel).resolve()
            if not str(dest).startswith(str(target_root)):
                continue  # guard against path traversal
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(m)
                if f:
                    dest.write_bytes(f.read())


# ── .env ────────────────────────────────────────────────────────────────────────

def _upsert(text: str, key: str, value: str) -> str:
    out, found = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(f"{key}=") or s.startswith(f"# {key}=") or s.startswith(f"#{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def _write_env(target: Path, llm: str, agent_id: str, key: str, secret: str) -> None:
    example = target / ".env.example"
    text = example.read_text() if example.exists() else ""
    if agent_id:
        text = _upsert(text, "AGENT_ID", agent_id)
    if key:
        text = _upsert(text, "ALPACA_API_KEY", key)
    if secret:
        text = _upsert(text, "ALPACA_SECRET_KEY", secret)
    text = _upsert(text, "LLM_PROVIDER", PROVIDERS[llm][0] or "none")
    (target / ".env").write_text(text)


def _find_cli(name: str) -> str | None:
    """Locate a CLI even under a minimal PATH (mirrors the kit's llm_providers resolver)."""
    p = shutil.which(name)
    if p:
        return p
    home = Path.home()
    for d in (home / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
              home / ".npm-global/bin", home / ".bun/bin", home / "bin"):
        fp = d / name
        if fp.is_file() and os.access(fp, os.X_OK):
            return str(fp)
    if os.name != "nt":
        try:
            shell = os.environ.get("SHELL", "/bin/bash")
            out = subprocess.run([shell, "-ilc", f"command -v {name} 2>/dev/null"],
                                 capture_output=True, text=True, timeout=8)
            for line in reversed(out.stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("/") and os.access(line, os.X_OK):
                    return line
        except Exception:
            pass
    return None


# ── chat launcher (generated locally → no Gatekeeper/SmartScreen, no signing) ────

def _generate_chat_launcher(target: Path, llm: str) -> Path | None:
    cmd = PROVIDERS[llm][1]
    if not cmd:
        return None  # deepseek/none have no interactive chat REPL
    if os.name == "nt":
        path = target / "Agentberg Chat.bat"
        path.write_text(
            "@echo off\r\n"
            'set "PATH=%USERPROFILE%\\.local\\bin;%PATH%"\r\n'   # Claude native installer dir
            f'cd /d "{target}"\r\n'
            f"{cmd}\r\n"
            "pause\r\n"
        )
    else:
        # Launch through the user's login+interactive shell so it inherits the SAME
        # PATH as their terminal — the CLI (e.g. claude in ~/.local/bin) is found even
        # though a double-clicked .command otherwise gets a minimal PATH.
        path = target / "Agentberg Chat.command"
        path.write_text(
            "#!/bin/bash\n"
            f"exec \"${{SHELL:-/bin/bash}}\" -ilc 'cd \"{target}\" && exec {cmd}'\n"
        )
        path.chmod(0o755)
    return path


# ── commands ─────────────────────────────────────────────────────────────────────

def _choose_llm(preset: str | None, no_input: bool) -> str:
    if preset:
        return preset
    if no_input:
        return "none"
    print("\nWhich AI should rank your trades?")
    print("  1) Claude      (claude CLI · subscription)")
    print("  2) Gemini      (agy CLI · no API key)")
    print("  3) OpenAI      (codex CLI · no API key)")
    print("  4) DeepSeek    (API key · ~$0.001/cycle)")
    print("  5) None        (free rule-based ranking)")
    pick = input("Choose [1-5, default 5]: ").strip() or "5"
    return {"1": "claude", "2": "gemini", "3": "openai", "4": "deepseek", "5": "none"}.get(pick, "none")


def _prompt(label: str, preset: str, no_input: bool) -> str:
    if preset:
        return preset
    if no_input:
        return ""
    return input(label).strip()


def _install_llm(llm: str) -> None:
    spec = LLM_INSTALL.get(llm)
    if not spec:
        return
    cmd = spec["nt" if os.name == "nt" else "posix"]
    print(f"\nInstalling the {llm} CLI…\n  $ {cmd}")
    try:
        subprocess.run(cmd, shell=True)
    except Exception as e:
        print(f"  install failed ({e}) — install manually (see README).")
        return
    signin = spec.get("signin")
    if signin:
        print(f"\n  ✓ Installed. SIGN IN once: open a NEW terminal, run `{signin}`, and follow")
        print(f"    the browser prompt. After that your agent ranks with {llm}.")
    elif llm == "deepseek":
        print("\n  ✓ openai SDK installed. Add your key to .env:  DEEPSEEK_API_KEY=sk-…")
        print("    (free key at https://platform.deepseek.com)")


def _maybe_install_llm(llm: str, args) -> None:
    if llm == "none":
        return
    cli_cmd = PROVIDERS[llm][1]
    if cli_cmd is not None and _find_cli(cli_cmd) is not None:
        return  # already installed
    want = args.install_llm
    if not want and not args.no_input:
        ans = input(f"\nInstall the {llm} CLI now? (you sign in manually after) [y/N]: ").strip().lower()
        want = ans in ("y", "yes")
    if want:
        _install_llm(llm)
    else:
        tip = "install it and sign in" if cli_cmd else "`pip install openai` and set DEEPSEEK_API_KEY"
        print(f"\n  ⚠ {llm} not set up yet — {tip} to enable AI ranking (free rule-based until then).")


def cmd_init(args) -> None:
    target = Path(os.path.expanduser(args.dir)) if args.dir else DEFAULT_DIR
    print(f"Setting up your Agentberg trader in: {target}")
    if target.exists() and any(target.iterdir()) and not args.force:
        sys.exit(f"{target} exists and is not empty — use --force to overwrite or pick --dir.")

    _download_kit(target)
    llm = _choose_llm(args.llm, args.no_input)
    agent_id = _prompt("AGENT_ID (your agent's unique name): ", args.agent_id, args.no_input)
    key = _prompt("Alpaca PAPER API key (enter to skip): ", args.alpaca_key, args.no_input)
    secret = _prompt("Alpaca PAPER secret (enter to skip): ", args.alpaca_secret, args.no_input)
    _write_env(target, llm, agent_id, key, secret)
    launcher = _generate_chat_launcher(target, llm)
    _save_state({"folder": str(target), "llm": llm})

    print("\n✓ Trader folder ready.")
    print(f"  Folder:  {target}")
    print(f"  LLM:     {llm}  (LLM_PROVIDER={PROVIDERS[llm][0] or 'none'})")
    if launcher:
        print(f"  Chat:    double-click '{launcher.name}' in that folder to chat with your agent")
    _maybe_install_llm(llm, args)
    print("\nNext steps:")
    print(f"  cd {target} && pip install -r requirements.txt")
    print("  agentberg run        # one session   |   agentberg start   # live scheduler")


def cmd_run(args) -> None:
    subprocess.run([sys.executable, "agent.py"], cwd=_folder(args))


def cmd_start(args) -> None:
    subprocess.run([sys.executable, "scheduler.py"], cwd=_folder(args))


def cmd_chat(args) -> None:
    folder = _folder(args)
    llm = _load_state().get("llm", "none")
    cmd = PROVIDERS.get(llm, ("", None))[1]
    if not cmd:
        sys.exit(f"Your configured LLM ('{llm}') has no chat CLI — re-run `agentberg init` and pick Claude/Gemini/OpenAI.")
    if _find_cli(cmd) is None:
        print(f"  heads up: couldn't locate '{cmd}' — if chat fails, install the {llm} CLI, "
              f"sign in, and open a new terminal (or set its *_BIN env var).")
    # Launch through the user's login+interactive shell so the CLI is found under the
    # same PATH as their terminal — not the minimal PATH this process may have.
    if os.name == "nt":
        env = os.environ.copy()
        env["PATH"] = os.path.expanduser(r"~\.local\bin") + os.pathsep + env.get("PATH", "")
        subprocess.run(cmd, cwd=folder, shell=True, env=env)
    else:
        shell = os.environ.get("SHELL", "/bin/bash")
        subprocess.run([shell, "-ilc", f'cd "{folder}" && exec {cmd}'], cwd=folder)


def cmd_update(args) -> None:
    folder = _folder(args)
    print(f"Pull-to-review for: {folder}")
    print("New kit code is never auto-applied. To adopt the latest safely, follow")
    print("UPGRADING.md in your folder: it diffs the new version and proposes only")
    print("strategy-neutral changes for your review. Check the latest version at")
    print("https://agentberg.ai/kit/manifest")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="agentberg", description="Run and chat with your Agentberg trading agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="scaffold an editable trader folder")
    pi.add_argument("--dir", help="target folder (default ~/agentberg-trader)")
    pi.add_argument("--llm", choices=list(PROVIDERS), help="LLM provider (skip the prompt)")
    pi.add_argument("--agent-id")
    pi.add_argument("--alpaca-key")
    pi.add_argument("--alpaca-secret")
    pi.add_argument("--install-llm", action="store_true", help="install the chosen LLM CLI automatically")
    pi.add_argument("--force", action="store_true", help="overwrite a non-empty folder")
    pi.add_argument("--no-input", action="store_true", help="don't prompt (for scripts/tests)")
    pi.set_defaults(func=cmd_init)

    for name, fn, help_ in [
        ("run", cmd_run, "run one trading session"),
        ("start", cmd_start, "run the live scheduler"),
        ("chat", cmd_chat, "chat with your LLM in the trader folder"),
        ("update", cmd_update, "pull-to-review the latest kit"),
    ]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--dir", help="trader folder (default: the one from init)")
        sp.set_defaults(func=fn)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
