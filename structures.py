"""
structures.py — the defined-risk-only enforcement layer for complex trades.

AGENTS.md defines WHICH multi-leg structures the agent may build (the allowlist)
and the laws they obey. This module is where those laws are MECHANICALLY enforced,
so a leg-role or atomicity mistake can never reach the broker.

Two gates, both fail-closed:

  build-time  — validate_structure(): a structure whose type we don't recognise,
                or whose max_loss isn't a bounded positive number, is rejected
                before any order is sent. Forbidden is the default for everything.
  action-time — leg_action_allowed(): a single leg of an open structure may never
                be closed/cancelled on its own — the only legal exit is closing the
                whole structure. This is the gate the naked-leg bug needs.

The registry below is the code-side mirror of the AGENTS.md allowlist. A structure
the kit cannot price to a bounded max_loss is simply not registered, and therefore
cannot be built — which is exactly the fail-closed guarantee. To add a structure,
add a registry entry whose every short leg is covered by a long; nothing else
needs to change.
"""

from __future__ import annotations

import math

# Each registry entry declares its legs by role, in order:
#   "long"  — pays premium; defined-risk by itself (max loss = premium paid).
#   "short" — collects premium; safe ONLY when a long leg covers it (a spread).
# A naked short has unbounded/large undefined risk and cannot exist in any
# registered structure, so every entry here is shorts-covered by construction.
STRUCTURE_REGISTRY: dict[str, dict] = {
    # Bull call / bear put — the one multi-leg structure this kit builds today.
    "debit_vertical": {
        "legs": ("long", "short"),     # long engine + short financier (same underlying)
        "shorts_covered": True,        # the long leg caps the short's risk at the width
        "max_loss": "net debit paid",
    },
}


def validate_structure(structure_type: str, max_loss: float | None, legs: list[dict]) -> tuple[bool, str]:
    """
    Build-time gate. Fail-closed: returns (False, reason) unless EVERY condition for
    a defined-risk structure is met. Call this immediately before sending the order.

    legs — ordered list of {"role": "long"|"short", "symbol": str}
    """
    spec = STRUCTURE_REGISTRY.get(structure_type)
    if spec is None:
        return False, f"'{structure_type}' is not an allowed structure (default-deny)"
    if max_loss is None or not math.isfinite(max_loss) or max_loss <= 0:
        return False, f"max_loss is not a bounded positive number ({max_loss!r}) — refusing to send"
    roles = tuple(leg.get("role") for leg in legs)
    if roles != spec["legs"]:
        return False, f"leg roles {roles} don't match the {structure_type} spec {spec['legs']}"
    if any(leg.get("role") == "short" for leg in legs) and not spec["shorts_covered"]:
        return False, f"{structure_type} would leave an uncovered short — banned"
    return True, "ok"


def open_structure_leg_symbols(open_trades: list[dict]) -> set[str]:
    """
    Every option symbol that is a leg of an OPEN multi-leg structure. A trade is
    multi-leg when it carries a short_symbol (the presence of a short leg means its
    risk is defined by a paired long — they must be resolved together).
    """
    legs: set[str] = set()
    for t in open_trades:
        if t.get("short_symbol"):
            for sym in (t.get("long_symbol"), t.get("short_symbol")):
                if sym:
                    legs.add(sym)
    return legs


def leg_action_allowed(symbol: str, open_trades: list[dict]) -> tuple[bool, str]:
    """
    Action-time gate. A single leg of an open structure may not be acted on alone:
    closing/cancelling it could strand the other leg (e.g. leave a short naked).
    The only legal resolution is closing the whole structure as one order.
    """
    if symbol in open_structure_leg_symbols(open_trades):
        return False, (f"{symbol} is a leg of an open multi-leg structure — resolve the "
                       f"structure as a unit, never this leg alone")
    return True, "ok"
