#!/usr/bin/env python3
"""Validate the upgrade-category tags in kit_manifest.json.

Every changelog entry must carry a `category` of 0, A, B, or C (see UPGRADING.md).

Under the current upgrade model:
  Cat 0 / A  — always auto-apply (no hash check). Kit author takes responsibility.
  Cat B      — never auto. Agent's competitive edge: risk params, credentials, identity.
  Cat C      — merge-not-replace (e.g. .env.example keys).

The guard here is CAT_B_REQUIRE: files that MUST be Cat B because auto-applying them
would overwrite agent-specific customisations that are their trading edge.
If any of those files appear in a Cat 0 or Cat A entry, it's a mis-tag — reject it.

  validate_categories.py          exit 1 on any violation

Stdlib-only (the kit ships no build deps).
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "kit_manifest.json")

VALID = {"0", "A", "B", "C"}

# Files that must ALWAYS be Cat B — auto-applying these overwrites agent's alpha.
# Kit author must never tag these Cat 0 or Cat A (from MODEL_VERSION onwards).
CAT_B_REQUIRE = {
    "risk.py",       # agent's risk parameters
    "config.py",     # agent's trading config
    "identity.py",   # agent's network identity
    "character.py",  # agent's persona / goals
    "alpaca.py",     # broker credentials / order logic
    "structures.py", # agent's data model (customisation surface)
    "setup.py",      # initial setup script (one-shot, not upgradeable)
    "run.sh",        # startup script (agent-specific)
}

# Entries before this version used old Cat A = "propose-first" (not auto-apply).
# CAT_B_REQUIRE guard only applies from this version onward.
MODEL_VERSION = (2, 8, 12)


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def main() -> int:
    with open(MANIFEST) as f:
        manifest = json.load(f)

    errors: list[str] = []
    for entry in manifest.get("changelog", []):
        ver = entry.get("version", "?")
        cat = str(entry.get("category", ""))
        if cat not in VALID:
            errors.append(f"v{ver}: category {entry.get('category')!r} not in {sorted(VALID)}")
            continue
        if cat in ("0", "A") and _vtuple(ver) >= MODEL_VERSION:
            bad = [f for f in entry.get("files", []) if f.split("/")[0] in CAT_B_REQUIRE]
            if bad:
                errors.append(
                    f"v{ver}: Cat {cat} but touches agent-alpha file(s): {bad}. "
                    f"These must be Cat B (never auto-apply)."
                )

    if errors:
        print("Category validation FAILED:")
        for e in errors:
            print(f"  x {e}")
        return 1
    print(f"Category validation OK — {len(manifest.get('changelog', []))} entries tagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
