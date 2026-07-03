"""
postcar_adapter.py — this kit's own accountability layer on top of PostCar's
generic guidance-decision API.

PostCar (postcar/postcar_check.py) is common infrastructure shared across every
platform/agent that uses it — this kit, SMoney, Gpower, minig, and others all
clone the identical, unmodified file from github.com/postcar-agent/postcar-agent.
Its own decide_guidance() deliberately stays generic (a decision plus an optional
free-text outcome_note) — baking any one platform's specific accountability
policy into that shared file would impose it on every other adopter too.

This kit's own policy — requiring the agent to justify a use/no-use call against
the same judgment dimensions PostCar's own evaluation already scored, not just an
optional free-text note that could be left empty — lives here instead, one layer
up. Call decide_guidance_with_rationale() below, not
postcar_check.decide_guidance() directly, whenever this kit's agent records a
guidance decision.
"""

import json
import sys
from pathlib import Path

_POSTCAR_DIR = Path(__file__).parent / "postcar"
if str(_POSTCAR_DIR) not in sys.path:
    sys.path.insert(0, str(_POSTCAR_DIR))

# Same three judgment dimensions PostCar's own _EVAL_PROMPT scores for every
# guidance record (see the record's "evaluation" field) — not a new rubric to
# invent, just requiring the agent to say where it agreed or disagreed with the
# one PostCar already gave it.
_RATIONALE_DIMENSIONS = ("thesis_validity", "goal_alignment", "risk_note")


def _validate_rationale(rationale: dict) -> None:
    if not isinstance(rationale, dict):
        raise ValueError(
            "rationale must be a dict — see decide_guidance_with_rationale() docstring for shape"
        )
    notes = rationale.get("dimension_notes")
    if not isinstance(notes, dict):
        raise ValueError("rationale['dimension_notes'] must be a dict")
    missing = [d for d in _RATIONALE_DIMENSIONS if not str(notes.get(d, "")).strip()]
    if missing:
        raise ValueError(f"rationale['dimension_notes'] missing/empty for: {', '.join(missing)}")
    if not str(rationale.get("evidence", "")).strip():
        raise ValueError("rationale['evidence'] is required — what data/observation actually drove this")
    if not isinstance(rationale.get("agrees_with_recommendation"), bool):
        raise ValueError("rationale['agrees_with_recommendation'] must be true/false")


def decide_guidance_with_rationale(message_id: str, decision: str, rationale: dict) -> bool:
    """This kit's required entrypoint for recording a PostCar guidance decision
    (pending/acked -> use/no-use). Use this instead of importing postcar_check
    and calling decide_guidance() directly — it enforces this kit's own
    accountability policy before handing off to PostCar's unchanged, generic API.

    `rationale` is required and must be a dict shaped like:
        {
            "agrees_with_recommendation": True,   # did you follow PostCar's own
                                                    # apply/hold/reject suggestion?
            "dimension_notes": {
                "thesis_validity": "one line: agree/disagree + why",
                "goal_alignment":  "one line: agree/disagree + why",
                "risk_note":       "one line: agree/disagree + why",
            },
            "evidence": "what data/observation actually drove the use/no-use call",
        }
    Raises ValueError if incomplete — a use/no-use call with no reasoning
    attached is exactly the gap this closes. Auto-expire on non-engagement is a
    separate path inside postcar_check itself and correctly never asks for or
    fabricates a rationale — this function is only for a real, deliberate
    decision.

    The structured rationale is JSON-serialized into PostCar's outcome_note
    field, so it's still visible to anyone reading .postcar_guidance directly."""
    _validate_rationale(rationale)
    import postcar_check  # local import: only resolvable once postcar/ is bootstrapped
    return postcar_check.decide_guidance(message_id, decision, outcome_note=json.dumps(rationale))
