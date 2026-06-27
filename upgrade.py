#!/usr/bin/env python3
"""
upgrade.py — One-time kit upgrade for Agentberg agents.

Run from inside your agent folder:

    python3 upgrade.py          (Mac / Linux)
    python upgrade.py           (Windows)

Downloads the latest kit from GitHub. Applies safe platform files only.
Never touches your trading logic (risk.py, alpaca.py, scheduler.py, etc.).

No CLI required. No packages to install. Just Python.
"""

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse
from pathlib import Path

KIT_URL = (
    "https://github.com/Agentberg/agentberg-starter/"
    "archive/refs/heads/main.tar.gz"
)

# These files are NEVER auto-applied — they are the agent's trading edge.
CAT_B_PROTECT = frozenset({
    "risk.py", "config.py", "identity.py", "character.py",
    "alpaca.py", "structures.py", "setup.py", "run.sh",
})

# CLI / dev / packaging — never go into agent folders.
SCAFFOLD_EXCLUDE = frozenset({
    "agentberg_cli", "pyproject.toml", ".github", "tests", "__pycache__",
    "LEGACY_AGENT_UPGRADE.md", "INSTALL.md", "START.md",
})

ADOPTED_FILE = ".agentberg_adopted.json"
IGNORE = {".env", ".git", "__pycache__", "logs", "agent.db",
          "agent.db-journal", ".agent_key", ADOPTED_FILE}


# ── helpers ──────────────────────────────────────────────────────────────────

def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def _load_adopted(folder: Path) -> dict:
    try:
        return json.loads((folder / ADOPTED_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_adopted(folder: Path, data: dict) -> None:
    (folder / ADOPTED_FILE).write_text(json.dumps(data, indent=2))


def _folder_version(folder: Path) -> str:
    try:
        return json.loads((folder / "kit_manifest.json").read_text()).get("version", "0.0.0")
    except (FileNotFoundError, json.JSONDecodeError):
        return "0.0.0"


def _file_hashes(folder: Path) -> dict:
    hashes = {}
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(folder).as_posix()
        top = rel.split("/")[0]
        if top in IGNORE or top in SCAFFOLD_EXCLUDE or rel.endswith(".pyc"):
            continue
        hashes[rel] = _sha256(p)
    return hashes


def _pending(manifest: dict, adopted_ver: str) -> list:
    av = _vtuple(adopted_ver)
    entries = [e for e in manifest.get("changelog", [])
               if _vtuple(e.get("version", "0")) > av]
    return sorted(entries, key=lambda e: _vtuple(e.get("version", "0")))


def _fetch() -> bytes:
    print("  Downloading latest kit from GitHub…")
    req = urllib.request.Request(KIT_URL, headers={"User-Agent": "agentberg-upgrade"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _extract(data: bytes, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    root_str = str(target.resolve())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        repo_root = members[0].name.split("/")[0] if members else ""
        for m in members:
            rel = m.name[len(repo_root) + 1:] if m.name.startswith(repo_root + "/") else m.name
            if not rel:
                continue
            dest = (target / rel).resolve()
            if not str(dest).startswith(root_str):
                continue  # path-traversal guard
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(m)
                if f:
                    dest.write_bytes(f.read())


# ── heartbeat ────────────────────────────────────────────────────────────────

def _load_env(folder: Path) -> dict:
    env = {}
    env_file = folder / ".env"
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _post_json(url: str, data: dict, headers: dict | None = None, timeout: int = 15) -> bool:
    headers = headers or {}
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception as e:
        print(f"  POST {url} failed: {e}")
        return False


def _send_heartbeat(env: dict, base_url: str, kit_version: str) -> bool:
    agent_id = env.get("AGENT_ID")
    if not agent_id:
        return False
    ok = _post_json(f"{base_url}/heartbeat", {"agent_id": agent_id, "kit_version": kit_version})
    if ok:
        print(f"  Heartbeat: sent (agent={agent_id}, kit=v{kit_version})")
    else:
        print(f"  Heartbeat: failed — will retry on next scheduler run")
    return ok


def _send_upgrade_report(
    env: dict,
    base_url: str,
    from_version: str | None,
    to_version: str,
    files_applied: list,
    files_protected: list,
    heartbeat_ok: bool,
) -> None:
    agent_id = env.get("AGENT_ID")
    if not agent_id or not base_url:
        return
    _post_json(f"{base_url}/telemetry/upgrade", {
        "agent_id": agent_id,
        "from_version": from_version,
        "to_version": to_version,
        "files_applied": files_applied,
        "files_protected": files_protected,
        "heartbeat_ok": heartbeat_ok,
    })


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    folder = Path.cwd()
    print(f"\n  Agentberg Kit Upgrade")
    print(f"  Folder : {folder}\n")

    if not (folder / "kit_manifest.json").exists():
        sys.exit(
            "  ERROR: No kit_manifest.json found.\n"
            "  Run this script from inside your agent folder."
        )

    adopted = _load_adopted(folder)
    if not adopted:
        cur = _folder_version(folder)
        _save_adopted(folder, {"version": cur, "files": _file_hashes(folder)})
        print(f"  Created baseline (v{cur}). Re-run to upgrade.")
        return

    try:
        data = _fetch()
    except Exception as e:
        sys.exit(f"  ERROR: Could not download kit: {e}")

    with tempfile.TemporaryDirectory() as tmp:
        newdir = Path(tmp) / "kit"
        _extract(data, newdir)

        try:
            new_manifest = json.loads((newdir / "kit_manifest.json").read_text())
        except Exception:
            sys.exit("  ERROR: Could not read manifest from downloaded kit.")

        latest = new_manifest.get("version", "0.0.0")
        if _vtuple(latest) <= _vtuple(adopted["version"]):
            print(f"  Already up to date (v{adopted['version']}).")
            return

        from_version = adopted["version"]
        print(f"  Upgrade: v{from_version} → v{latest}")

        all_pending   = _pending(new_manifest, from_version)
        auto_entries  = [e for e in all_pending if str(e.get("category")) in ("0", "A")]
        manual_entries = [e for e in all_pending if str(e.get("category")) not in ("0", "A")]

        print(f"  Auto (Cat 0/A): {len(auto_entries)} release(s)")
        print(f"  Manual (Cat B/C — your logic, untouched): {len(manual_entries)} release(s)\n")

        # De-duped file list from all auto entries
        seen, files_auto = set(), []
        for e in auto_entries:
            for rel in e.get("files", []):
                if rel not in seen:
                    files_auto.append(rel)
                    seen.add(rel)

        # Snapshot before touching anything
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = folder.parent / f"{folder.name}-backup-{ts}"
        print(f"  Creating backup → {backup.name}")
        shutil.copytree(str(folder), str(backup))

        # Apply
        applied, protected = [], []
        for rel in files_auto:
            top = rel.split("/")[0]
            if top in SCAFFOLD_EXCLUDE:
                continue
            if top in CAT_B_PROTECT:
                protected.append(rel)
                continue
            src = newdir / rel
            if not src.is_file():
                continue
            cur = folder / rel
            if cur.exists() and _sha256(cur) == _sha256(src):
                continue  # already identical
            cur.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(cur))
            applied.append(rel)

        adopted["version"] = latest
        _save_adopted(folder, adopted)

        if applied:
            print(f"\n  Applied {len(applied)} file(s):")
            for rel in applied:
                print(f"    + {rel}")
        else:
            print("\n  All Cat 0/A files already up to date.")

        if protected:
            print(f"\n  Protected (your alpha — untouched):")
            for rel in protected:
                print(f"    ~ {rel}")

        if manual_entries:
            print(f"\n  Manual review (Cat B/C — see UPGRADING.md):")
            for e in manual_entries:
                print(f"    [{e.get('category','?')}] v{e['version']} — {', '.join(e.get('files', []))}")

        print(f"\n  Done. Now at v{latest}.")
        print(f"  Backup saved at: {backup}")
        print(f"\n  Restart your scheduler to load the new code.")

        env = _load_env(folder)
        base_url = env.get("AGENTBERG_URL", "https://agentberg.ai").rstrip("/")
        hb_ok = _send_heartbeat(env, base_url, latest)
        _send_upgrade_report(
            env, base_url,
            from_version=from_version,
            to_version=latest,
            files_applied=applied,
            files_protected=protected,
            heartbeat_ok=hb_ok,
        )
        print()


if __name__ == "__main__":
    main()
