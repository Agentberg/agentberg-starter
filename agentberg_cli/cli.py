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
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

KIT_TARBALL = (
    "https://github.com/Agentberg/agentberg-starter/"
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
_SCAFFOLD_EXCLUDE = {"agentberg_cli", "pyproject.toml", ".github", "tests", "__pycache__",
                     "LEGACY_AGENT_UPGRADE.md", "INSTALL.md", "START.md"}

# Files that are NEVER auto-applied by the upgrade CLI, regardless of manifest category.
# These are agent-alpha: the agent's competitive edge. Historical manifest entries
# may have tagged some of these Cat A (old semantic = "propose first", not "overwrite").
# This runtime guard is the safety net — no misconfiguration can bypass it.
_CAT_B_PROTECT = frozenset({
    "risk.py",        # agent's risk parameters
    "config.py",      # agent's trading config
    "identity.py",    # agent's network identity / credentials
    "character.py",   # agent's persona and goals
    "alpaca.py",      # broker credentials and order logic
    "structures.py",  # agent's data model (customisation surface)
    "setup.py",       # initial setup script (one-shot, not upgradeable)
    "run.sh",         # startup script (agent-specific)
})


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

def _fetch_kit_bytes() -> bytes:
    """Download the latest kit tarball over HTTPS (GitHub is the trust anchor)."""
    req = urllib.request.Request(KIT_TARBALL, headers={"User-Agent": "agentberg-cli"})
    with urllib.request.urlopen(req, timeout=60) as resp:   # follows redirects
        return resp.read()


def _extract_kit(data: bytes, target: Path, exclude: bool = True) -> None:
    """Extract the editable kit files from a tarball into target (path-traversal safe).

    exclude=True drops CLI/dev/packaging files (for the user's folder); exclude=False
    extracts everything (used when staging the new kit to a temp dir for upgrade).
    """
    target.mkdir(parents=True, exist_ok=True)
    target_root = target.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        root = members[0].name.split("/")[0] if members else ""
        for m in members:
            rel = m.name[len(root) + 1:] if m.name.startswith(root + "/") else m.name
            if not rel or (exclude and rel.split("/")[0] in _SCAFFOLD_EXCLUDE):
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


def _download_kit(target: Path) -> None:
    """Download the latest kit tarball and extract the editable files into target."""
    print("  fetching the latest kit…")
    _extract_kit(_fetch_kit_bytes(), target)


# ── upgrade (pull-to-review + Category 0 auto-apply) ──────────────────────────────

ADOPTED_FILE = ".agentberg_adopted.json"
# Folder entries that are local state, never kit code — excluded from baselining.
_UPGRADE_IGNORE = {".env", ".git", "__pycache__", "logs", "agent.db", "agent.db-journal",
                   ".agent_key", ADOPTED_FILE}


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kit_file_hashes(target: Path) -> dict:
    """sha256 of every kit file in the folder, by POSIX-relative path."""
    hashes = {}
    for p in sorted(target.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(target).as_posix()
        top = rel.split("/")[0]
        if top in _UPGRADE_IGNORE or top in _SCAFFOLD_EXCLUDE or rel.endswith(".pyc"):
            continue
        if rel.endswith(".command") or rel.endswith(".bat"):  # generated launcher
            continue
        hashes[rel] = _sha256(p)
    return hashes


def _load_adopted(folder: Path) -> dict:
    try:
        return json.loads((folder / ADOPTED_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_adopted(folder: Path, data: dict) -> None:
    (folder / ADOPTED_FILE).write_text(json.dumps(data, indent=2))


def _folder_kit_version(folder: Path) -> str:
    try:
        return json.loads((folder / "kit_manifest.json").read_text()).get("version", "0.0.0")
    except (FileNotFoundError, json.JSONDecodeError):
        return "0.0.0"


def _sync_pyproject_version(folder: Path, version: str) -> None:
    """Update the version = line in pyproject.toml if it exists in the agent folder."""
    pf = folder / "pyproject.toml"
    if not pf.exists():
        return
    lines = pf.read_text().splitlines()
    out, updated = [], False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("version") and "=" in stripped and not updated:
            key = ln.split("=")[0]
            ln = f'{key}= "{version}"'
            updated = True
        out.append(ln)
    if updated:
        pf.write_text("\n".join(out) + "\n")


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


def _phone_home_cli() -> None:
    """Anonymous fire-once install ping from the CLI path. Never blocks init."""
    try:
        id_file = STATE_DIR / "kit_id"
        if id_file.exists():
            kit_id = id_file.read_text().strip()
        else:
            import uuid as _uuid
            kit_id = str(_uuid.uuid4())
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            id_file.write_text(kit_id)
        import json as _json
        data = _json.dumps({
            "kit_id": kit_id,
            "ts": int(time.time()),
            "source": "cli_init",
            "platform": sys.platform,
        }).encode()
        req = urllib.request.Request(
            "https://agentberg.ai/telemetry/install",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def cmd_init(args) -> None:
    target = Path(os.path.expanduser(args.dir)) if args.dir else DEFAULT_DIR
    print(f"Setting up your Agentberg trader in: {target}")
    if target.exists() and any(target.iterdir()) and not args.force:
        sys.exit(f"{target} exists and is not empty — use --force to overwrite or pick --dir.")

    _download_kit(target)
    # Record the adopted baseline: version + per-file hashes. Upgrade uses this to tell
    # an untouched file (safe to auto-replace) from one the agent has customized.
    _save_adopted(target, {"version": _folder_kit_version(target),
                           "files": _kit_file_hashes(target)})
    llm = _choose_llm(args.llm, args.no_input)
    agent_id = _prompt("AGENT_ID (your agent's unique name): ", args.agent_id, args.no_input)
    key = _prompt("Alpaca PAPER API key (enter to skip): ", args.alpaca_key, args.no_input)
    secret = _prompt("Alpaca PAPER secret (enter to skip): ", args.alpaca_secret, args.no_input)
    _write_env(target, llm, agent_id, key, secret)
    launcher = _generate_chat_launcher(target, llm)
    _save_state({"folder": str(target), "llm": llm})
    _phone_home_cli()

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
    import time as _time
    folder = _folder(args)
    backoff = 5
    max_backoff = 300
    print("[watchdog] Starting scheduler — auto-restarts on crash. Ctrl-C to stop.")
    while True:
        t0 = _time.monotonic()
        result = subprocess.run([sys.executable, "scheduler.py"], cwd=folder)
        elapsed = _time.monotonic() - t0
        if elapsed > 60:
            backoff = 5   # reset: it ran long enough to be considered healthy
        code = result.returncode
        label = "exited cleanly" if code == 0 else f"crashed (code {code})"
        print(f"[watchdog] Scheduler {label} — restarting in {backoff}s (Ctrl-C to abort)")
        try:
            _time.sleep(backoff)
        except KeyboardInterrupt:
            print("[watchdog] Stopped.")
            break
        backoff = min(backoff * 2, max_backoff)


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


def _pending_entries(new_manifest: dict, adopted_version: str) -> list[dict]:
    """Changelog entries newer than the adopted version, oldest-first."""
    av = _vtuple(adopted_version)
    entries = [e for e in new_manifest.get("changelog", []) if _vtuple(e.get("version", "0")) > av]
    return sorted(entries, key=lambda e: _vtuple(e.get("version", "0")))


def cmd_upgrade(args) -> None:
    """Upgrade the kit: auto-apply Cat 0/A changes to untouched files (snapshot +
    compile-verify + rollback), then surface Cat B/C items for manual review."""
    folder = _folder(args)
    auto = True  # always auto-apply; --auto flag kept for backwards compat

    adopted = _load_adopted(folder)
    if not adopted:
        # No baseline (older folder) — record the current state and stop. Without a
        # baseline we cannot tell a customized file from an untouched one.
        cur_ver = _folder_kit_version(folder)
        _save_adopted(folder, {"version": cur_ver, "files": _kit_file_hashes(folder)})
        print(f"Recorded current folder as baseline (v{cur_ver}). Re-run to upgrade.")
        return

    print("  fetching the latest kit…")
    try:
        data = _fetch_kit_bytes()
    except Exception as e:
        sys.exit(f"Could not fetch the kit: {e}")

    with tempfile.TemporaryDirectory() as tmp:
        newdir = Path(tmp) / "kit"
        _extract_kit(data, newdir, exclude=False)
        try:
            new_manifest = json.loads((newdir / "kit_manifest.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            sys.exit("Latest kit has no readable manifest — aborting.")

        latest = new_manifest.get("version", "0.0.0")
        if _vtuple(latest) <= _vtuple(adopted["version"]):
            print(f"Already current (v{adopted['version']}).")
            return

        pending = _pending_entries(new_manifest, adopted["version"])
        # Category IS the gate. Kit author decides by tagging:
        #   Cat 0 / A → always overwrite (platform files agents should not customise)
        #   Cat B / C → never auto (agent's alpha: risk params, identity, merge-only configs)
        auto_entries = [e for e in pending if str(e.get("category")) in ("0", "A")]
        hard_block   = [e for e in pending if str(e.get("category")) not in ("0", "A")]

        print(f"\nUpgrade available: v{adopted['version']} → v{latest}")
        print(f"  Cat 0/A (auto-apply, always): {len(auto_entries)} version(s)")
        print(f"  Cat B/C (manual after auto):  {len(hard_block)} version(s)")

        # ── STEP 1: AUTO-APPLY Cat 0 + A (always, no hash gate) ─────────────────
        applied, missing = [], []
        if auto_entries:
            # GATE 1: snapshot before touching anything.
            ts = time.strftime("%Y%m%d-%H%M%S")
            backup = folder.parent / f"{folder.name}-backup-{ts}"
            shutil.copytree(folder, backup)
            print(f"\n  snapshot: {backup}")

            # De-duped file list from all auto-eligible entries.
            seen_rels: set[str] = set()
            files_auto: list[str] = []
            for e in auto_entries:
                for rel in e.get("files", []):
                    if rel not in seen_rels:
                        files_auto.append(rel)
                        seen_rels.add(rel)

            for rel in files_auto:
                if rel.split("/")[0] in _SCAFFOLD_EXCLUDE:
                    continue  # never inject CLI/dev files into agent folders
                if rel.split("/")[0] in _CAT_B_PROTECT:
                    continue  # runtime guard: agent-alpha files never auto-apply
                src = newdir / rel
                if not src.is_file():
                    missing.append(rel)
                    continue
                cur = folder / rel
                if cur.exists() and _sha256(cur) == _sha256(src):
                    continue  # already identical — no-op
                cur.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, cur)
                applied.append(rel)

            # GATE 2: byte-compile applied Python — broken file rolls back.
            pyfiles = [str(folder / r) for r in applied if r.endswith(".py")]
            if pyfiles:
                res = subprocess.run([sys.executable, "-m", "py_compile", *pyfiles],
                                     capture_output=True, text=True)
                if res.returncode != 0:
                    shutil.rmtree(folder)
                    shutil.move(str(backup), str(folder))
                    sys.exit(f"Compile failed — rolled back from snapshot.\n{res.stderr}")

        # Version always advances after Cat 0/A applied. Cat B/C surface separately.
        adopted["version"] = latest
        _sync_pyproject_version(folder, latest)
        _save_adopted(folder, adopted)

        if applied:
            print(f"\n✓ Applied {len(applied)} file(s) from {len(auto_entries)} Cat 0/A release(s).")
            for rel in applied:
                print(f"    updated  {rel}")
        elif auto_entries:
            print(f"\n  Cat 0/A files already up to date.")
        else:
            print(f"\n  No Cat 0/A changes in this upgrade.")
        for rel in missing:
            print(f"    missing  {rel}  (not in latest kit — skipped)")
        print(f"\n  Now at v{latest}.")

        # ── STEP 2: SURFACE Cat B/C for manual review ────────────────────────────
        if hard_block:
            print(f"\n  ── Manual review required ──────────────────────────────────")
            print(f"  {len(hard_block)} Cat B/C release(s) — apply via UPGRADING.md:")
            for e in hard_block:
                print(f"     [{e.get('category','?')}] v{e['version']} — {', '.join(e.get('files', []))}")
            print(f"  After applying: run `agentberg adopt` to record the new baseline.")

        # ── Signal running scheduler to restart with new code ────────────────────
        if applied:
            lock = folder / "logs" / "scheduler.lock"
            if lock.exists():
                try:
                    pid = int(lock.read_text().strip())
                    os.kill(pid, 15)  # SIGTERM — graceful stop; watchdog (agentberg start) restarts
                    print(f"\n  Scheduler (PID {pid}) signaled to restart — new code will load in ~5s.")
                except Exception:
                    print(f"\n  Upgrade applied. Restart your scheduler to load the new code.")
            sys.exit(2)  # exit code 2 = upgrade was applied; watchdog restarts


def cmd_update(args) -> None:
    # `update` and `upgrade` are identical — both apply Cat 0/A automatically.
    cmd_upgrade(args)


def cmd_adopt(args) -> None:
    """Record current folder state as the new adoption baseline.

    Use after manually applying a Cat B/C change so the adopted version
    reflects what's actually installed. Without --file: re-baselines all files.
    With --file FILE: re-baselines that file only.
    """
    folder = _folder(args)
    adopted = _load_adopted(folder)
    if not adopted:
        sys.exit("No adoption baseline — run `agentberg upgrade` first to create one.")

    target = getattr(args, "file", None)

    if target:
        rel = target.replace("\\", "/").lstrip("/")
        path = folder / rel
        if not path.is_file():
            sys.exit(f"Not found in agent folder: {rel}")
        adopted["files"][rel] = _sha256(path)
        _save_adopted(folder, adopted)
        print(f"Adopted {rel} — baseline reset to current on-disk hash.")
        print(f"Future upstream changes to this file will auto-apply if untouched.")
    else:
        new_hashes = _kit_file_hashes(folder)
        adopted["files"] = new_hashes
        _save_adopted(folder, adopted)
        print(f"Full re-baseline: {len(new_hashes)} file(s) recorded.")
        print(f"Run `agentberg upgrade` to apply any pending changes.")


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
        ("update", cmd_update, "apply kit updates (Cat 0/A auto, Cat B/C flagged)"),
    ]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--dir", help="trader folder (default: the one from init)")
        sp.set_defaults(func=fn)

    pu = sub.add_parser("upgrade", help="upgrade the kit; --auto applies Cat 0/A to untouched files")
    pu.add_argument("--dir", help="trader folder (default: the one from init)")
    pu.add_argument("--auto", action="store_true",
                    help="auto-apply Cat 0/A changes to untouched files (snapshot + rollback)")
    pu.set_defaults(func=cmd_upgrade)

    pa = sub.add_parser("adopt", help="record current folder state as new baseline (after manual Cat B/C apply)")
    pa.add_argument("--dir", help="trader folder (default: the one from init)")
    pa.add_argument("--file", help="specific file to re-baseline (e.g. scheduler.py); omit for full re-baseline")
    pa.set_defaults(func=cmd_adopt)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
