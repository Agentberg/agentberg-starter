"""
risk.py — Risk check functions.

All tunable parameters live in config.py.
This module is pure logic — checks return (allowed: bool, reason: str).
"""
from __future__ import annotations

import config as cfg


def check_equity(
    ticker: str,
    sector: str,
    regime: str | None,
    blocked_sectors: list[str],
    position_value: float,
    portfolio_equity: float,
    open_positions: int,
) -> tuple[bool, str]:
    if sector in blocked_sectors:
        return False, f"{sector} blocked by your MANUAL_BLOCKED_SECTORS rule"
    if regime and regime in cfg.BLOCKED_REGIMES:
        return False, f"Regime '{regime}' blocked — no new longs"
    if open_positions >= cfg.MAX_POSITIONS:
        return False, f"At max positions ({cfg.MAX_POSITIONS})"
    if portfolio_equity > 0 and (position_value / portfolio_equity) > cfg.MAX_POSITION_PCT:
        return False, f"Position {position_value/portfolio_equity:.1%} exceeds {cfg.MAX_POSITION_PCT:.1%} limit"
    return True, "ok"


def check_option(
    ticker: str,
    sector: str,
    regime: str | None,
    blocked_sectors: list[str],
    portfolio_equity: float,
    open_positions: int,
    premium: float,
    dte: int,
    delta: float,
    iv_rank: float | None = None,
    days_to_earnings: int | None = None,
) -> tuple[bool, str]:
    # Fleet-wide gate on naked long premium (2026-07-17): single-leg long options
    # are the fleet's dominant loss source (negative expectancy at fleet level).
    # Gated by default; operators who genuinely want the exposure opt in by adding
    # ALLOW_NAKED_LONG_OPTIONS = True to risk_params.py. getattr keeps existing
    # installs (whose risk_params.py predates the flag) gated.
    if not getattr(cfg, "ALLOW_NAKED_LONG_OPTIONS", False):
        return False, ("Naked long options gated fleet-wide — set "
                       "ALLOW_NAKED_LONG_OPTIONS = True in risk_params.py to opt in")
    _blackout = getattr(cfg, "EARNINGS_BLACKOUT_DAYS", 0) or 0
    if days_to_earnings is not None and 0 <= days_to_earnings <= _blackout:
        return False, f"Earnings in {days_to_earnings}d — inside {_blackout}d blackout"
    if sector in blocked_sectors:
        return False, f"{sector} blocked by your MANUAL_BLOCKED_SECTORS rule"
    if regime and regime in cfg.BLOCKED_REGIMES:
        return False, f"Regime '{regime}' blocked — no new longs"
    if open_positions >= cfg.MAX_POSITIONS:
        return False, f"At max positions ({cfg.MAX_POSITIONS})"
    if dte < cfg.MIN_DTE:
        return False, f"{dte} DTE below minimum {cfg.MIN_DTE} — gamma risk too high"
    if dte > cfg.MAX_DTE:
        return False, f"{dte} DTE above maximum {cfg.MAX_DTE}"
    if not (cfg.MIN_DELTA <= abs(delta) <= cfg.MAX_DELTA):
        return False, f"Delta {delta:.2f} outside target range {cfg.MIN_DELTA}–{cfg.MAX_DELTA}"
    if iv_rank is not None and iv_rank > cfg.MAX_IV_RANK_TO_BUY:
        return False, f"IV Rank {iv_rank:.0f} too high — premium too expensive (max {cfg.MAX_IV_RANK_TO_BUY})"
    cost = premium * 100
    if portfolio_equity > 0 and (cost / portfolio_equity) > cfg.MAX_OPTION_PCT:
        return False, f"Premium cost {cost/portfolio_equity:.1%} exceeds {cfg.MAX_OPTION_PCT:.1%} limit"
    return True, "ok"


def check_portfolio_greeks(
    candidate_delta: float,
    candidate_vega: float,
    candidate_qty: int,
    portfolio_delta: float,
    portfolio_vega: float,
    portfolio_equity: float,
) -> tuple[bool, str]:
    """Aggregate delta/vega budget across the WHOLE options book, not just this
    one position. check_option()/check_spread() above only ever look at a
    single candidate's own numbers -- five individually-compliant same-
    direction bets can still stack into a portfolio net delta that moves like
    5x leveraged exposure on one directional call, which no single-position
    check can see. This is table stakes in every systematic options
    methodology (Cboe's published index rules included) and was missing here
    entirely before 2026-07-21.

    Deltas/vegas are PER-CONTRACT (Alpaca convention: delta in [-1,1], vega in
    $ per 1-vol-point per contract) -- multiplied by qty*100 to get dollar
    exposure, consistent with how MAX_OPTION_PCT above treats premium*100.

    Disabled until MAX_PORTFOLIO_DELTA_PCT/MAX_PORTFOLIO_VEGA_PCT are set in
    risk_params.py (getattr default None) -- existing installs unaffected
    until an operator opts in, same pattern as ALLOW_NAKED_LONG_OPTIONS."""
    if portfolio_equity <= 0:
        return True, "ok"  # can't evaluate a % cap without a real equity figure
    max_delta_pct = getattr(cfg, "MAX_PORTFOLIO_DELTA_PCT", None)
    max_vega_pct = getattr(cfg, "MAX_PORTFOLIO_VEGA_PCT", None)
    if max_delta_pct is None and max_vega_pct is None:
        return True, "ok"

    projected_delta = abs(portfolio_delta) + abs(candidate_delta) * candidate_qty * 100
    projected_vega = abs(portfolio_vega) + abs(candidate_vega) * candidate_qty * 100

    if max_delta_pct is not None:
        delta_pct = projected_delta / portfolio_equity
        if delta_pct > max_delta_pct:
            return False, (f"Portfolio delta would reach {delta_pct:.1%} of equity, "
                           f"over the {max_delta_pct:.1%} MAX_PORTFOLIO_DELTA_PCT cap")
    if max_vega_pct is not None:
        vega_pct = projected_vega / portfolio_equity
        if vega_pct > max_vega_pct:
            return False, (f"Portfolio vega would reach {vega_pct:.1%} of equity, "
                           f"over the {max_vega_pct:.1%} MAX_PORTFOLIO_VEGA_PCT cap")
    return True, "ok"


def pick_vol_adaptive_strategy_mode(base_mode: str, vol_percentile: float | None) -> tuple[str, str | None]:
    """Rotates STRATEGY_MODE toward the more defined-risk structure as market
    vol rises -- Cboe's own published Volatility-Managed PutWrite Index
    methodology does exactly this (full cash-secured-put in low-vol, rotating
    toward an iron-condor-shaped structure as VIX percentile climbs). This
    kit's equivalent rotation: premium_buyer -> spreads.

    Only ever makes the session MORE conservative, never less: "equity" mode
    is untouched (no options exposure to adapt in the first place), "spreads"
    stays "spreads" (already the defined-risk choice) -- there is no branch
    that loosens exposure automatically in a calm regime, on purpose.

    Disabled by default (VOL_ADAPTIVE_STRUCTURE_ENABLED, risk_params.py) --
    opt-in, same inert-until-configured pattern as check_portfolio_greeks()
    above. Returns (effective_mode, reason_or_None) -- log the reason when
    not None so a session that silently traded spreads instead of the
    configured premium_buyer mode is explainable after the fact."""
    if not getattr(cfg, "VOL_ADAPTIVE_STRUCTURE_ENABLED", False):
        return base_mode, None
    if base_mode != "premium_buyer" or vol_percentile is None:
        return base_mode, None
    high = getattr(cfg, "VOL_REGIME_HIGH_PCTL", 70)
    if vol_percentile >= high:
        return "spreads", (f"vol regime {vol_percentile:.0f}th percentile >= {high} — "
                           f"rotating premium_buyer to spreads for this session "
                           f"(Cboe PUTVM-style defined-risk rotation)")
    return base_mode, None


def check_spread(
    ticker: str,
    sector: str,
    regime: str | None,
    blocked_sectors: list[str],
    portfolio_equity: float,
    open_positions: int,
    net_debit: float,
    spread_width: float,
    dte: int,
    days_to_earnings: int | None = None,
) -> tuple[bool, str]:
    _blackout = getattr(cfg, "EARNINGS_BLACKOUT_DAYS", 0) or 0
    if days_to_earnings is not None and 0 <= days_to_earnings <= _blackout:
        return False, f"Earnings in {days_to_earnings}d — inside {_blackout}d blackout"
    if sector in blocked_sectors:
        return False, f"{sector} blocked by your MANUAL_BLOCKED_SECTORS rule"
    if regime and regime in cfg.BLOCKED_REGIMES:
        return False, f"Regime '{regime}' blocked — no new longs"
    if open_positions >= cfg.MAX_POSITIONS:
        return False, f"At max positions ({cfg.MAX_POSITIONS})"
    if dte < cfg.MIN_DTE:
        return False, f"{dte} DTE below minimum {cfg.MIN_DTE}"
    if dte > cfg.MAX_DTE:
        return False, f"{dte} DTE above maximum {cfg.MAX_DTE}"
    if spread_width > 0 and (net_debit / spread_width) > cfg.MAX_SPREAD_DEBIT_PCT:
        return False, f"Debit {net_debit/spread_width:.0%} of width exceeds {cfg.MAX_SPREAD_DEBIT_PCT:.0%} cap"
    max_loss = net_debit * 100
    if portfolio_equity > 0 and (max_loss / portfolio_equity) > cfg.MAX_SPREAD_PCT:
        return False, f"Max loss {max_loss/portfolio_equity:.1%} exceeds {cfg.MAX_SPREAD_PCT:.1%} limit"
    return True, "ok"
