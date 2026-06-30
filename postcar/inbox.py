"""
inbox.py — Offer filter and guidance delivery for PostCar agents.

Filters incoming offers using a 4-parameter scoring model:
  validity, credibility, alignment, risk
Writes accepted guidance to .postcar_guidance.md in the agent directory.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Risk detection patterns
# ---------------------------------------------------------------------------

CHANGE_INDICATORS: List[str] = [
    r"change\s+(?:your|the)\s+(?:parameter|config|setting|threshold|limit)",
    r"reduce\s+(?:position|size|exposure)",
    r"increase\s+(?:position|exposure|risk)",
    r"switch\s+to",
    r"disable\s+",
    r"stop\s+",
]


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def compute_validity_score(offer: Dict[str, Any]) -> float:
    """Return 0.0 if content empty/<10 chars, 1.0 if >=100 chars, else 0.5."""
    content = offer.get("content", "")
    if not content or len(content) < 10:
        return 0.0
    if len(content) >= 100:
        return 1.0
    return 0.5


def compute_credibility_score(offer: Dict[str, Any]) -> float:
    """Return credibility/100 clamped to [0.0, 1.0]. Default credibility=50."""
    raw = offer.get("credibility", 50)
    return min(1.0, max(0.0, raw / 100.0))


def compute_alignment_score(
    offer: Dict[str, Any], agent_tier1_tags: Optional[List[str]]
) -> float:
    """Return intersection fraction vs agent_tier1_tags. Neutral 0.5 if no tags."""
    if not agent_tier1_tags:
        return 0.5
    offer_tags = offer.get("from_agent_tags", [])
    if not offer_tags:
        return 0.5
    intersection = len(set(offer_tags) & set(agent_tier1_tags))
    return min(1.0, intersection / max(len(agent_tier1_tags), 1))


def compute_risk_score(offer: Dict[str, Any]) -> float:
    """Return fraction of CHANGE_INDICATORS matched, capped at 1.0."""
    content = offer.get("content", "")
    matches = sum(
        1 for p in CHANGE_INDICATORS if re.search(p, content, re.IGNORECASE)
    )
    return min(1.0, matches / 3.0)


# ---------------------------------------------------------------------------
# Filter decision
# ---------------------------------------------------------------------------


def filter_offer(
    offer: Dict[str, Any], agent_tier1_tags: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Score an offer and return decision dict with decision, reason, score."""
    v = compute_validity_score(offer)
    c = compute_credibility_score(offer)
    a = compute_alignment_score(offer, agent_tier1_tags)
    r = compute_risk_score(offer)

    score = v * 0.3 + c * 0.3 + a * 0.2 + (1.0 - r) * 0.2

    if score >= 0.6:
        decision = "APPLY"
    elif score >= 0.4:
        decision = "DEFER"
    else:
        decision = "REJECT"

    reason = (
        "score=" + str(round(score, 2))
        + " v=" + str(round(v, 2))
        + " c=" + str(round(c, 2))
        + " a=" + str(round(a, 2))
        + " r=" + str(round(r, 2))
    )

    return {"decision": decision, "reason": reason, "score": score}


# ---------------------------------------------------------------------------
# Guidance writer
# ---------------------------------------------------------------------------


def write_guidance(
    applied_offers: List[Dict[str, Any]], agent_dir: str
) -> Optional[str]:
    """Write .postcar_guidance.md from applied offers. Returns path or None."""
    if not applied_offers:
        return None

    guidance_path = os.path.join(agent_dir, ".postcar_guidance.md")
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines: List[str] = [f"# PostCar Guidance — {timestamp}\n"]
    for offer in applied_offers:
        from_agent = offer.get("from_agent", "unknown")
        credibility = offer.get("credibility", 50)
        content = offer.get("content", "")
        score = offer.get("_score", 0.0)
        lines.append(
            f"\n## Recommendation from {from_agent} (credibility: {credibility})\n"
        )
        lines.append(f"{content}\n")
        lines.append(f"\n*Score: {score:.0%}*\n")

    with open(guidance_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    return guidance_path


# ---------------------------------------------------------------------------
# Inbox cycle
# ---------------------------------------------------------------------------


def execute_inbox_cycle(
    client: Any,
    agent_dir: str,
    agent_tier1_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Fetch, filter, and act on all pending offers. Returns cycle summary."""
    offers = client.get_offers() or []

    applied: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for offer in offers:
        result = filter_offer(offer, agent_tier1_tags)
        decision = result["decision"]

        if decision == "APPLY":
            offer["_score"] = result["score"]
            applied.append(offer)
            client.rate_offer(offer["offer_id"], "useful")
        elif decision == "DEFER":
            deferred.append(offer)
        else:  # REJECT
            rejected.append(offer)
            if offer.get("credibility", 50) < 30:
                client.rate_offer(offer["offer_id"], "unrelated")

    guidance = write_guidance(applied, agent_dir)

    return {
        "applied": len(applied),
        "deferred": len(deferred),
        "rejected": len(rejected),
        "guidance_path": guidance,
    }
