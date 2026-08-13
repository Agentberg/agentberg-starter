"""
trailing.py — ATR-scaled trailing-stop math for real broker-side stop management.

Pure functions, no state, no network calls. check_positions() in agent.py owns
the state (current_stop_price/high_water_mark/entry_atr in memory.py) and the
"only ever tighten, never loosen" gate; this module just computes what the stop
*should* be given entry/HWM/ATR.

Ported 2026-08-13 from jeeboo's strategy/sizing.py::compute_atr()/
trailing_stop_price() (itself ported from minig's execution.py::
trail_equity_stops(), built 2026-08-05, live-tested) -- the kit's own
trailing-stop was reactive-only (Python compares price vs. HWM, calls
close_position() if triggered) with NO resting order on Alpaca's book, so a
process outage between polls left positions with zero trailing protection at
all. jeeboo's approach instead PATCHes the entry bracket's real stop-loss
order price as HWM improves, so the broker enforces it even if the local
process is briefly down. See agent.py's _trail_stops() for the caller side.
"""
from __future__ import annotations


def compute_atr(bars: list[dict], period: int = 14) -> float | None:
    """Average True Range over the trailing `period` bars. None if there's not
    enough history — callers fall back to a fixed stop/target in that case."""
    if not bars or len(bars) < period + 1:
        return None
    trs = []
    prev_close = bars[0].get("c")
    for b in bars[1:]:
        h, l, c = b.get("h"), b.get("l"), b.get("c")
        if h is None or l is None or prev_close is None:
            prev_close = c
            continue
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = c
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def trailing_stop_price(entry_price: float, hwm: float, entry_atr: float | None,
                         is_short: bool = False,
                         k_activate: float = 1.5, k_trail: float = 2.0,
                         fallback_activate_pct: float = 0.04, fallback_trail_pct: float = 0.02,
                         ) -> float | None:
    """ATR-scaled trailing stop: a flat % trail is inside normal daily noise for
    a high-ATR name and stops out on ordinary chop (confirmed live in jeeboo,
    2026-08-12: RTX/DIS/NDSN all stopped out prematurely under a flat 2% trail,
    giving back $518 combined vs. holding to the actual exit).

    `hwm` is the FAVORABLE extreme since entry regardless of direction — highest
    price for a long, lowest for a short (caller tracks this; see agent.py). The
    `is_short` branch mirrors every comparison: jeeboo's original version (this
    is ported from) is long-only by design (its README: "Equity-only,
    long-only"), so it never needed this -- the kit trades both directions and
    a naive port would compute a long-style floor-below-hwm stop for a short
    position, which is backwards.

    Returns None if not yet armed (hwm hasn't moved k_activate x ATR favorably
    from entry). Once armed, ALWAYS floors/ceilings the stop at entry_price
    (breakeven) — minig found live (OKTA, 2026-08-07) that k_trail(2.0) >
    k_activate(1.5) means the naive `hwm -/+ k_trail*atr` can sit past
    entry_price at the exact moment of arming, even though the position is
    unambiguously in profit. A trail that arms below breakeven defeats the
    point of arming it.

    Falls back to fallback_activate_pct/fallback_trail_pct (plain % of entry,
    not ATR) when entry_atr is unavailable — same degrade-gracefully pattern as
    everywhere else in this kit when a data dependency is missing.

    Caller must apply the "only ever tighten, never loosen" check against the
    currently-live stop before replacing it — this function is pure, it just
    computes what the stop *should* be given the current entry/HWM/ATR."""
    if entry_price <= 0 or hwm <= 0:
        return None
    favorable_move = (entry_price - hwm) if is_short else (hwm - entry_price)
    if entry_atr and entry_atr > 0:
        armed = favorable_move >= k_activate * entry_atr
        if not armed:
            return None
        raw = hwm + k_trail * entry_atr if is_short else hwm - k_trail * entry_atr
        return round(min(raw, entry_price) if is_short else max(raw, entry_price), 2)
    # No ATR history — fall back to plain % of entry price, same shape.
    armed = (favorable_move / entry_price) >= fallback_activate_pct
    if not armed:
        return None
    raw = hwm * (1 + fallback_trail_pct) if is_short else hwm * (1 - fallback_trail_pct)
    return round(min(raw, entry_price) if is_short else max(raw, entry_price), 2)
