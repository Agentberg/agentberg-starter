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


def tightening_trail_stop_price(entry_price: float, hwm: float, shares: float,
                                 is_short: bool = False, giveback_start: float = 250.0,
                                 decay_per_dollar: float = 0.3, floor_dollars: float = 100.0,
                                 ) -> float | None:
    """Profit-tightening $ trailing stop (2026-08-26, ported from jeeboo's
    strategy/sizing.py, same day) — replaces the flat always-on $100 trail
    below. Same "always-on from entry, no activation delay, ratchets toward
    entry only" shape as trailing_stop_price(), but the $ giveback allowed
    from the high-water mark shrinks as banked profit grows instead of
    staying constant:

        profit_dollars = max(0, (hwm - entry_price) * shares)          # long
        profit_dollars = max(0, (entry_price - hwm) * shares)          # short
        giveback = clamp(giveback_start - decay_per_dollar * profit_dollars,
                          floor_dollars, giveback_start)

    Day-1 / loss-side stop is always entry_price ∓ giveback_start/shares
    (flat, unchanged, by construction — profit_dollars is 0 whenever the
    position hasn't moved favorably past entry). floor_dollars is
    deliberately the SAME value as trailing_stop_price()'s default
    trail_dollars=100.0: strict generalization, identical to the old rule
    once a trade is deep enough in profit, wider only in the shallow-profit
    zone where the old flat rule was clipping trades near breakeven on
    ordinary noise.

    Backtested long-only (no-lookahead, 5-min bars, same 289-trade dataset
    as the fixed-$100 validation — see agentberg memory
    finding_tightening_trail_beats_fixed100_2026-08-26.md):
    giveback_start=250/decay=0.3/floor=100 settled $9,089.95 vs fixed-$100's
    $4,606.05. Short-side formula mirrors trailing_stop_price()'s existing
    is_short handling but has not itself been separately backtested — same
    standing caveat, one ~2-month window, revisit if forward performance
    diverges."""
    if entry_price <= 0 or hwm <= 0 or shares <= 0:
        return None
    giveback_start_per_share = giveback_start / shares
    profit_dollars = max(0.0, (entry_price - hwm) * shares) if is_short else max(0.0, (hwm - entry_price) * shares)
    giveback = max(floor_dollars, min(giveback_start, giveback_start - decay_per_dollar * profit_dollars))
    giveback_per_share = giveback / shares
    day1 = entry_price + giveback_start_per_share if is_short else entry_price - giveback_start_per_share
    trailed = hwm + giveback_per_share if is_short else hwm - giveback_per_share
    return round(min(day1, trailed) if is_short else max(day1, trailed), 2)


def trailing_stop_price(entry_price: float, hwm: float, shares: float,
                         is_short: bool = False, trail_dollars: float = 100.0,
                         ) -> float | None:
    """Fixed-$ trailing stop, always-on from entry (no activation delay) — replaces
    the prior ATR-scaled version (ported from jeeboo's strategy/sizing.py, same
    2026-08-24 change). Backtest across 289 home-fleet closed trades found the
    ATR trail's SETTLED (actually-stopped-out) performance was -$19,285.14, worse
    than a flat $100 fixed trail's +$10,202.51 on the same trades/window (see
    agentberg memory finding_trailing_stop_policy_backtest_2026-08-24.md — one
    ~2-month window, not a validated forward rule).

    `hwm` is the FAVORABLE extreme since entry regardless of direction — highest
    price for a long, lowest for a short (caller tracks this; see agent.py).

    trail_dollars is a fixed $ amount of TOTAL POSITION drawdown tolerated from
    the high-water mark, divided by share count to get the per-share trail
    distance — so a $50k position and a $3k position both get the same $100
    give-back budget from their own peak, not the same price-per-share distance.
    Day-1 stop (before any favorable move) uses the same formula as the ongoing
    trail — no separate activation gate, matches exactly what was backtested.
    Ratchets toward entry only (caller applies "only ever tighten, never loosen"
    against the currently-live stop, same as before)."""
    if entry_price <= 0 or hwm <= 0 or shares <= 0:
        return None
    trail_per_share = trail_dollars / shares
    day1 = entry_price + trail_per_share if is_short else entry_price - trail_per_share
    trailed = hwm + trail_per_share if is_short else hwm - trail_per_share
    return round(min(day1, trailed) if is_short else max(day1, trailed), 2)
