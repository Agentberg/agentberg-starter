#!/usr/bin/env python3
"""Release notes generator — single source of truth is kit_manifest.json.

The `changelog` array in kit_manifest.json is the canonical, structured record of
what changed in every version. This script renders it into the two human/CI-facing
surfaces so they can never drift:

  release_notes.py --write              regenerate CHANGELOG.md from the manifest
  release_notes.py --check              exit 1 if CHANGELOG.md is out of sync (CI guard)
  release_notes.py --version X.Y.Z      print just that version's notes (for GH Release body)

Stdlib-only on purpose (the kit ships no build deps).
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "kit_manifest.json")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")

HEADER = (
    "# Changelog\n\n"
    "All notable changes to the Agentberg kit and CLI.\n\n"
    "This file is generated from `kit_manifest.json` — do not edit by hand.\n"
    "Run `python scripts/release_notes.py --write` after updating the manifest.\n"
)


def _load() -> dict:
    with open(MANIFEST) as f:
        return json.load(f)


def _entry_md(entry: dict) -> str:
    ver = entry.get("version", "?")
    date = entry.get("date", "")
    out = [f"## v{ver} — {date}".rstrip(" —")]
    files = entry.get("files")
    if files:
        out.append("")
        out.append(f"*Files:* {', '.join(files)}")
    out.append("")
    for item in entry.get("added", []):
        out.append(f"- {item}")
    out.append("")
    return "\n".join(out)


def render_changelog(manifest: dict) -> str:
    parts = [HEADER]
    for entry in manifest.get("changelog", []):
        parts.append(_entry_md(entry))
    return "\n".join(parts).rstrip() + "\n"


def notes_for(manifest: dict, version: str) -> str:
    version = version.lstrip("v")
    for entry in manifest.get("changelog", []):
        if entry.get("version") == version:
            # GH Release body: skip the "## vX" heading (the tag is the title already)
            body = ["### What changed", ""]
            for item in entry.get("added", []):
                body.append(f"- {item}")
            files = entry.get("files")
            if files:
                body += ["", f"*Files touched:* {', '.join(files)}"]
            return "\n".join(body) + "\n"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="regenerate CHANGELOG.md")
    g.add_argument("--check", action="store_true", help="fail if CHANGELOG.md is stale")
    g.add_argument("--version", metavar="X.Y.Z", help="print one version's notes")
    args = ap.parse_args()

    manifest = _load()

    if args.version:
        body = notes_for(manifest, args.version)
        if not body:
            print(f"error: version {args.version} not found in {MANIFEST}", file=sys.stderr)
            return 1
        sys.stdout.write(body)
        return 0

    rendered = render_changelog(manifest)

    if args.check:
        current = ""
        if os.path.exists(CHANGELOG):
            with open(CHANGELOG) as f:
                current = f.read()
        if current != rendered:
            print("CHANGELOG.md is out of sync with kit_manifest.json.", file=sys.stderr)
            print("Run: python scripts/release_notes.py --write", file=sys.stderr)
            return 1
        print("CHANGELOG.md is in sync.")
        return 0

    with open(CHANGELOG, "w") as f:
        f.write(rendered)
    print(f"Wrote {CHANGELOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
