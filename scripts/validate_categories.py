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

Two exemptions, both self-asserted flags on the changelog entry (auditable in the
manifest diff, not inferred from content):

- "postcar_exempt": true — PostCar (the self-installing comms sidecar) is
  platform-mandated infra, never agent customisation, and is meant to auto-apply
  with zero agent config. PostCar-only changes to a CAT_B_REQUIRE file (e.g.
  run.sh's bootstrap line) are legitimately Cat A under this exemption.
- "cat_b_bootstrap": true — a CAT_B_REQUIRE file being introduced for the first
  time (e.g. risk_params.py in v2.10.30) needs to reach every upgrading agent
  once so it exists locally at all; upgrade.py's own apply loop still refuses to
  overwrite it if it's already present, so this only ever creates, never clobbers.

  validate_categories.py          exit 1 on any violation

Stdlib-only (the kit ships no build deps).
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "kit_manifest.json")

sys.path.insert(0, ROOT)
from upgrade import CAT_B_PROTECT as CAT_B_REQUIRE  # single source of truth --
# this used to be a second, hand-maintained copy of the protect-list that could
# (and did) drift from upgrade.py's own CAT_B_PROTECT. Import it instead.

VALID = {"0", "A", "B", "C"}

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
        exempt = entry.get("postcar_exempt") or entry.get("cat_b_bootstrap")
        if cat in ("0", "A") and _vtuple(ver) >= MODEL_VERSION and not exempt:
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
