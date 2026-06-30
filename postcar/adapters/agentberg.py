"""
Agentberg adapter — reads agent state from agentberg-starter memory module.
PostCar uses this read-only to assess stress without touching agent code.
"""

from __future__ import annotations
import sys
import os


def read_state(agent_dir: str) -> dict:
    """
    Returns generic stress indicators derived from agentberg memory module.
    Never writes. Never modifies agent state.
    """
    sys.path.insert(0, agent_dir)
    state = {
        "failure_streak": 0,
        "performance_delta": 0.0,
        "error_rate": 0.0,
        "open_positions": 0,
    }
    try:
        import memory
        try:
            recent = memory.get_recent_trades(limit=10)
            outcomes = [t.get("pnl", 0) for t in recent if t.get("pnl") is not None]
            streak = 0
            for pnl in reversed(outcomes):
                if pnl < 0:
                    streak += 1
                else:
                    break
            state["failure_streak"] = streak
        except Exception:
            pass

        try:
            stats = memory.get_summary_stats(days=7)
            state["performance_delta"] = stats.get("net_pnl", 0.0)
        except Exception:
            pass

        try:
            open_trades = memory.get_open_trades()
            state["open_positions"] = len(open_trades)
        except Exception:
            pass

    except ImportError:
        pass
    finally:
        if agent_dir in sys.path:
            sys.path.remove(agent_dir)

    return state
