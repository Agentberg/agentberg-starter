"""
Generic adapter — fallback for unknown agent frameworks.
Reads stress indicators from .postcar_state.json if agent writes it,
otherwise returns neutral state.
"""

from __future__ import annotations
import json
import os


def read_state(agent_dir: str) -> dict:
    state = {
        "failure_streak": 0,
        "performance_delta": 0.0,
        "error_rate": 0.0,
        "open_positions": 0,
    }
    state_file = os.path.join(agent_dir, ".postcar_state.json")
    if os.path.exists(state_file):
        try:
            data = json.loads(open(state_file).read())
            indicators = data.get("stress_indicators", {})
            state.update({k: v for k, v in indicators.items() if k in state})
        except Exception:
            pass
    return state
