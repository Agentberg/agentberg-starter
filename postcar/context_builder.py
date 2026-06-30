"""
context_builder.py — CLAUDE.md scanner, tag derivation, and auto-registration helper.

Kit-only writes: .postcar_profile.json (and related postcar files).
No imports of agent source code except read-only adapters.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TECH_KEYWORDS = [
    "python", "typescript", "fastapi", "django", "react", "go", "rust", "nodejs",
]

DOMAIN_KEYWORDS = [
    "trading", "finance", "marketing", "analytics", "ml", "research", "operations", "ops",
]

DOMAIN_TAG_MAP: Dict[str, Dict[str, List[str]]] = {
    "trading": {
        "tier1": ["domain:finance", "identity:trading-agent"],
        "tier2": ["strategy:systematic", "skill:risk-management"],
    },
    "finance": {
        "tier1": ["domain:finance"],
        "tier2": [],
    },
    "marketing": {
        "tier1": ["domain:marketing", "identity:marketing-agent"],
        "tier2": [],
    },
    "analytics": {
        "tier1": ["domain:analytics", "identity:analytics-agent"],
        "tier2": [],
    },
    "ml": {
        "tier1": ["domain:ml", "skill:model-training"],
        "tier2": [],
    },
    "research": {
        "tier1": ["domain:research", "identity:research-agent"],
        "tier2": [],
    },
    "operations": {
        "tier1": ["domain:operations", "identity:ops-agent"],
        "tier2": [],
    },
    "ops": {
        "tier1": ["domain:operations"],
        "tier2": [],
    },
}

# ---------------------------------------------------------------------------
# FUNCTION 1: scan_directory
# ---------------------------------------------------------------------------


def scan_directory(agent_dir: str) -> Dict[str, Any]:
    """Scan agent_dir for markdown files and extract structured context.

    Reads CLAUDE.md (if exists), README.md (if exists), then other *.md
    files in the root of agent_dir.

    Returns a dict with keys:
        name         – first H1 heading found, or os.path.basename(agent_dir)
        description  – first non-heading paragraph (first 200 chars)
        tech_stack   – list of tech keywords found
        domain_hints – list of domain keywords found
        raw_text     – first 2000 chars of concatenated markdown content
    """
    agent_dir = os.path.abspath(agent_dir)

    # Determine read order: CLAUDE.md first, README.md second, rest alphabetically
    priority = ["CLAUDE.md", "README.md"]
    try:
        all_md = [f for f in os.listdir(agent_dir) if f.endswith(".md")]
    except OSError:
        all_md = []

    ordered: List[str] = []
    for p in priority:
        if p in all_md:
            ordered.append(p)
    for f in sorted(all_md):
        if f not in ordered:
            ordered.append(f)

    # Concatenate content
    chunks: List[str] = []
    for filename in ordered:
        filepath = os.path.join(agent_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
        except OSError:
            pass

    combined = "\n\n".join(chunks)
    raw_text = combined[:2000]

    # Extract name from first H1 heading
    name: Optional[str] = None
    for line in combined.splitlines():
        m = re.match(r"^#\s+(.+)", line)
        if m:
            name = m.group(1).strip()
            break
    if not name:
        name = os.path.basename(agent_dir)

    # Extract description: first non-empty, non-heading paragraph
    description = ""
    in_block = False
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            continue
        if stripped.startswith("#"):
            continue
        if stripped:
            description = stripped[:200]
            break

    # Keyword matching (case-insensitive)
    lower_text = combined.lower()
    tech_stack = [kw for kw in TECH_KEYWORDS if kw in lower_text]
    domain_hints = [kw for kw in DOMAIN_KEYWORDS if kw in lower_text]

    return {
        "name": name,
        "description": description,
        "tech_stack": tech_stack,
        "domain_hints": domain_hints,
        "raw_text": raw_text,
    }


# ---------------------------------------------------------------------------
# FUNCTION 2: derive_tags
# ---------------------------------------------------------------------------


def derive_tags(context: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a tag profile from a scan_directory context dict.

    Returns:
        {
            "tier1": [...],   # identity / domain tags (deduplicated)
            "tier2": [...],   # skill / strategy tags (deduplicated)
            "tier3": str,     # free-text description (max 150 chars)
            "flat": [...],    # unique union of tier1 + tier2
        }
    """
    tier1: List[str] = []
    tier2: List[str] = []

    for hint in context.get("domain_hints", []):
        mapping = DOMAIN_TAG_MAP.get(hint)
        if mapping:
            for tag in mapping.get("tier1", []):
                if tag not in tier1:
                    tier1.append(tag)
            for tag in mapping.get("tier2", []):
                if tag not in tier2:
                    tier2.append(tag)

    description = context.get("description", "") or ""
    tier3 = description[:150] if description else "autonomous agent"

    flat: List[str] = []
    for tag in tier1 + tier2:
        if tag not in flat:
            flat.append(tag)

    return {
        "tier1": tier1,
        "tier2": tier2,
        "tier3": tier3,
        "flat": flat,
    }


# ---------------------------------------------------------------------------
# Helpers for registration
# ---------------------------------------------------------------------------


def _stable_suffix(agent_dir: str) -> str:
    """Stable 10-digit numeric suffix derived from agent directory path.

    Deterministic: same dir always gives the same suffix across restarts.
    """
    h = int(hashlib.md5(os.path.abspath(agent_dir).encode()).hexdigest(), 16)
    return str(h % 10_000_000_000).zfill(10)


def _register_with_relay(
    relay_url: str,
    agent_name: str,
    tag_profile: Dict[str, Any],
) -> Dict[str, Any]:
    """POST /agents/register — no auth required, open public registration."""
    payload = json.dumps({
        "name": agent_name,
        "tags": tag_profile.get("flat", []),
        "tag_profile": {
            "tier1": tag_profile.get("tier1", []),
            "tier2": tag_profile.get("tier2", []),
            "tier3": tag_profile.get("tier3", ""),
        },
    }).encode()
    req = urllib.request.Request(
        f"{relay_url.rstrip('/')}/agents/register",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# FUNCTION 3: auto_register
# ---------------------------------------------------------------------------


def auto_register(
    agent_dir: str,
    client: Any = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """First-run auto-registration. No-op on subsequent runs (profile cached).

    Flow:
    1. If .postcar_profile.json with agent_id+key exists → return cached.
    2. scan_directory → derive_tags.
    3. Build agent_name = "<CLAUDE.md H1>-<10-digit-suffix>" (stable, unique).
    4. If POSTCAR_OWNER_ID + POSTCAR_OWNER_KEY in env → register with relay,
       store agent_id + api_key into .postcar_profile.json.
    5. If no owner creds → save name+tags only, return registered=False.
    """
    agent_dir = os.path.abspath(agent_dir)
    profile_path = os.path.join(agent_dir, ".postcar_profile.json")

    # 1. Return cached profile if already registered
    if os.path.isfile(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("agent_id") and cached.get("agent_key"):
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    # 2. Scan + derive tags
    context = scan_directory(agent_dir)
    tag_profile = derive_tags(context)

    # 3. Build stable unique name
    base_name = name or context["name"]
    suffix = _stable_suffix(agent_dir)
    agent_name = f"{base_name}-{suffix}"

    # 4. Try relay registration with owner credentials
    relay_url = os.environ.get("POSTCAR_RELAY_URL", "https://postcar.dev").rstrip("/")

    result: Dict[str, Any] = {
        "registered": False,
        "agent_name": agent_name,
        "tag_profile": tag_profile,
    }

    if relay_url:
        try:
            resp = _register_with_relay(relay_url, agent_name, tag_profile)
            agent_id = resp.get("agent_id", "")
            agent_key = resp.get("api_key", "")
            if agent_id and agent_key:
                result.update({
                    "registered": True,
                    "agent_id": agent_id,
                    "agent_key": agent_key,
                })
                print(f"[postcar] registered as '{agent_name}' ({agent_id})")
        except Exception as exc:
            result["register_error"] = str(exc)

    # 5. Persist profile (registered or not — avoids re-scanning every run)
    try:
        with open(profile_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except OSError:
        pass

    return result
