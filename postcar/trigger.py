"""
trigger.py — Trigger engine for postcar.
Evaluates stress state against trigger rules and generates network queries.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


DEFAULT_TRIGGERS: Dict[str, List[Dict[str, Any]]] = {
    "generic": [
        {
            "id": "T001",
            "name": "high_failure_streak",
            "condition_key": "failure_streak",
            "threshold": 5,
            "urgency": "high",
            "tags": ["skill:debugging", "skill:error-analysis"],
            "topic": "high failure streak",
            "window_h": 12,
            "condition": "gt",
        },
        {
            "id": "T002",
            "name": "high_error_rate",
            "condition_key": "error_rate",
            "threshold": 0.5,
            "urgency": "medium",
            "tags": ["skill:reliability", "skill:debugging"],
            "topic": "error rate spike",
            "window_h": 12,
            "condition": "gt",
        },
        {
            "id": "T003",
            "name": "performance_degradation",
            "condition_key": "performance_delta",
            "threshold": -0.2,
            "urgency": "medium",
            "tags": ["skill:optimization"],
            "topic": "performance degradation",
            "window_h": 24,
            "condition": "lt",
        },
    ],
    "trading": [
        {
            "id": "T101",
            "name": "consecutive_losses",
            "condition_key": "failure_streak",
            "threshold": 3,
            "urgency": "high",
            "tags": ["domain:finance", "skill:risk-management"],
            "topic": "consecutive losses",
            "window_h": 6,
            "condition": "gt",
        },
        {
            "id": "T102",
            "name": "drawdown_alert",
            "condition_key": "performance_delta",
            "threshold": -0.1,
            "urgency": "high",
            "tags": ["domain:finance", "skill:risk-management"],
            "topic": "drawdown detected",
            "window_h": 6,
            "condition": "lt",
        },
    ],
}


def load_triggers(agent_dir: str, identity_type: str = "generic") -> List[Dict[str, Any]]:
    """
    Load triggers from .postcar_triggers.yaml in agent_dir if available,
    otherwise fall back to DEFAULT_TRIGGERS for the given identity_type.
    """
    yaml_path = os.path.join(agent_dir, ".postcar_triggers.yaml")
    if os.path.exists(yaml_path):
        try:
            import yaml  # type: ignore
            with open(yaml_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, list):
                return data
        except Exception:
            pass

    return DEFAULT_TRIGGERS.get(identity_type, DEFAULT_TRIGGERS["generic"])


def eval_triggers(
    stress_state: Dict[str, Any], triggers: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Evaluate triggers against the given stress state.
    Returns the list of triggers that fired.

    condition "gt": fires when stress_state[key] >= threshold
    condition "lt": fires when stress_state[key] <= threshold
    """
    fired: List[Dict[str, Any]] = []
    for trigger in triggers:
        key = trigger.get("condition_key", "")
        threshold = trigger.get("threshold", 0)
        condition = trigger.get("condition", "gt")

        value = stress_state.get(key)
        if value is None:
            continue

        if condition == "gt" and value >= threshold:
            fired.append(trigger)
        elif condition == "lt" and value <= threshold:
            fired.append(trigger)

    return fired


def dedup_check(trigger_id: str, agent_dir: str) -> bool:
    """
    Return True if the trigger can fire (not recently fired within its window).
    Return False if the trigger fired within its window_h hours.
    """
    dedup_path = os.path.join(agent_dir, ".postcar_dedup.json")
    if not os.path.exists(dedup_path):
        return True

    try:
        with open(dedup_path, "r", encoding="utf-8") as fh:
            dedup: Dict[str, Any] = json.load(fh)
    except Exception:
        return True

    entry = dedup.get(trigger_id)
    if entry is None:
        return True

    # Find the window_h for this trigger from the entry, defaulting to 12
    window_h = entry.get("window_h", 12) if isinstance(entry, dict) else 12
    last_fired_str = entry.get("last_fired") if isinstance(entry, dict) else entry

    try:
        last_fired = datetime.fromisoformat(last_fired_str)
    except (TypeError, ValueError):
        return True

    now = datetime.now(timezone.utc)
    # Make last_fired timezone-aware if it isn't
    if last_fired.tzinfo is None:
        last_fired = last_fired.replace(tzinfo=timezone.utc)

    elapsed_hours = (now - last_fired).total_seconds() / 3600.0
    if elapsed_hours < window_h:
        return False

    return True


def mark_triggered(trigger_id: str, agent_dir: str, window_h: int = 12) -> None:
    """
    Record that a trigger has fired. Updates .postcar_dedup.json with the
    current timestamp and window_h for the given trigger_id.
    """
    dedup_path = os.path.join(agent_dir, ".postcar_dedup.json")
    try:
        if os.path.exists(dedup_path):
            with open(dedup_path, "r", encoding="utf-8") as fh:
                dedup: Dict[str, Any] = json.load(fh)
        else:
            dedup = {}
    except Exception:
        dedup = {}

    dedup[trigger_id] = {
        "last_fired": datetime.now(timezone.utc).isoformat(),
        "window_h": window_h,
    }

    with open(dedup_path, "w", encoding="utf-8") as fh:
        json.dump(dedup, fh, indent=2)


def generate_query(
    trigger: Dict[str, Any],
    stress_state: Dict[str, Any],
    llm_fn: Callable[[str], str],
) -> Dict[str, Any]:
    """
    Generate a network query dict for the given trigger using llm_fn.
    Returns {tags, question, urgency}.
    """
    topic = trigger.get("topic", "unknown issue")
    prompt = (
        f"Agent trigger: {topic}. "
        f"Stress: {stress_state}. "
        "Generate a concise question (1-2 sentences) asking the network for guidance. "
        "Return only the question."
    )
    question = llm_fn(prompt).strip()
    if not question:
        question = "Seeking guidance on: " + topic

    return {
        "tags": trigger.get("tags", []),
        "question": question,
        "urgency": trigger.get("urgency", "medium"),
    }
