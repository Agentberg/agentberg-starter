"""
stress.py — Adapter state to stress level classifier.
Maps agent state indicators to a named stress level.
"""

from __future__ import annotations
import os
import sys


NEUTRAL_STATE = {
    "failure_streak": 0,
    "performance_delta": 0.0,
    "error_rate": 0.0,
    "open_positions": 0,
}


def detect_framework(agent_dir: str) -> str:
    """Return 'agentberg' if memory.py exists in agent_dir, else 'generic'."""
    if os.path.exists(os.path.join(agent_dir, "memory.py")):
        return "agentberg"
    return "generic"


def read_adapter_state(agent_dir: str) -> dict:
    """
    Read stress indicators from the appropriate adapter for the given agent_dir.
    Returns neutral state on any error.
    """
    neutral = dict(NEUTRAL_STATE)
    framework = detect_framework(agent_dir)

    # Ensure parent directory of stress.py is on sys.path so adapters/ is importable
    this_dir = os.path.dirname(os.path.abspath(__file__))
    if this_dir not in sys.path:
        sys.path.insert(0, this_dir)

    try:
        if framework == "agentberg":
            from adapters import agentberg as adapter
        else:
            from adapters import generic as adapter
        return adapter.read_state(agent_dir)
    except Exception:
        return neutral


def stress_to_level(state: dict) -> str:
    """
    Map a state dict to a stress level string.
    Returns one of: 'normal', 'elevated', 'high', 'critical'.
    Rules evaluated in order; first match wins.
    """
    failure_streak = state.get("failure_streak", 0)
    error_rate = state.get("error_rate", 0.0)
    performance_delta = state.get("performance_delta", 0.0)

    if failure_streak >= 8 or error_rate >= 0.8:
        return "critical"
    if failure_streak >= 5 or error_rate >= 0.5:
        return "high"
    if failure_streak >= 3 or error_rate >= 0.3 or performance_delta <= -0.15:
        return "elevated"
    return "normal"


def compute_stress_summary(agent_dir: str) -> dict:
    """
    Return a summary dict with stress level, raw indicators, and detected framework.
    """
    state = read_adapter_state(agent_dir)
    level = stress_to_level(state)
    return {
        "level": level,
        "indicators": state,
        "framework": detect_framework(agent_dir),
    }
