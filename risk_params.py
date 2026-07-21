"""
risk_params.py — the human-set variables: what to trade, how much, and the limits.

This is the ONLY file in this kit whose values are yours and that kit upgrades never
touch. Everything else — config.py's loading/overlay mechanism, strategy logic in
agent.py, the broker wrapper, risk checks, etc. — is structural and updates freely as
the platform ships fixes. These numbers don't, until you change them.

Read every line. Change values to match your own strategy and risk tolerance.

DISCLAIMER: This is a software template, not investment advice.
"""

# ── Strategy mode ──────────────────────────────────────────────────────────────
# "equity"         — buy/sell stocks
# "premium_buyer"  — buy calls/puts directionally
# "spreads"        — debit spreads (bull call / bear put)
STRATEGY_MODE: str = "equity"

# ── Watchlist ──────────────────────────────────────────────────────────────────
# Grouped by sector. Add or remove tickers freely. Sectors the NETWORK has flagged are
# advisory (weighed in AI ranking, not skipped); only YOUR own MANUAL_BLOCKED_SECTORS
# (below) are hard-skipped.
WATCHLIST: dict[str, list[str]] = {
    "Technology":             ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMD", "TSLA", "NFLX", "PLTR", "SMCI", "MSTR", "COIN", "RKLB", "HOOD", "MARA"],
    "Energy":                 ["XOM", "CVX", "COP", "SLB", "HAL", "OXY"],
    "Financials":             ["JPM", "BAC", "GS", "MS", "WFC", "C", "COF"],
    "Healthcare":             ["UNH", "JNJ", "ABT", "LLY", "MRK", "PFE", "AMGN"],
    "Industrials":            ["CAT", "DE", "HON", "GE", "LMT", "BA", "UPS"],
    "Consumer Discretionary": ["AMZN", "HD", "NKE", "SBUX", "TGT", "WMT", "MELI"],
}

# ── Position sizing ────────────────────────────────────────────────────────────
MAX_POSITIONS:       int   = 30     # max concurrent open positions (increased for high activity)
MAX_POSITION_PCT:    float = 0.01   # 1% of portfolio per equity trade (smaller size avoids BP exhaustion)
MAX_OPTION_PCT:      float = 0.02   # 2% per single-leg options trade
MAX_SPREAD_PCT:      float = 0.02   # 2% per spread (max loss = debit paid)
MAX_NEW_PER_CYCLE:   int   = 10     # cap new positions opened in one session (forces instant activity)

# ── Stop loss / take profit ────────────────────────────────────────────────────
# Reward:risk must not be negative-expectancy by construction -- 4% stop vs 2%
# target needed >66% win rate just to break even before any edge. 6% target
# against the same 4% stop is 1.5:1 reward:risk, a defensible default floor.
EQUITY_STOP_LOSS_PCT:   float = 0.04   # exit equity if down 4% (widened to avoid quick shakeouts)
OPTION_STOP_LOSS_PCT:   float = 0.50   # exit option if down 50% of premium paid
EQUITY_TAKE_PROFIT_PCT: float = 0.06   # exit equity at 6% gain — 1.5:1 reward:risk vs the 4% stop
TAKE_PROFIT_PCT:        float = 1.00   # options: exit at 100% gain on premium (2x paid)

# ── Trailing stop (all instruments) ────────────────────────────────────────────
# Once a position gains TRIGGER_PCT, the stop trails DISTANCE_PCT below the
# highest price seen since entry. Locks in gains on reversals without capping upside.
# Equities: the old 1%/1% default fired on routine intraday chop and cut winners
# at ~+2% while stops ran to -4% — fleet trade logs showed symmetric $win/$loss
# despite the 6/4 target/stop. 4% trigger / 2% trail lets a winner actually run.
# Options use wider distances (volatile premium, theta decay would fire too early).
TRAILING_STOP_ENABLED:              bool  = True
TRAILING_STOP_TRIGGER_PCT:          float = 0.04   # equities: activate at 4% gain
TRAILING_STOP_DISTANCE_PCT:         float = 0.02   # equities: trail 2% below HWM
OPTION_TRAILING_STOP_TRIGGER_PCT:   float = 0.20   # options: activate at 20% premium gain
OPTION_TRAILING_STOP_DISTANCE_PCT:  float = 0.20   # options: trail 20% below HWM premium

# ── Naked long options gate ────────────────────────────────────────────────────
# Single-leg long calls/puts (STRATEGY_MODE "premium_buyer") are gated OFF
# fleet-wide by default: they are the fleet's dominant loss source (theta decay +
# directional coin-flips compound). Spreads are unaffected (defined max loss).
# Set True only if you genuinely want naked long premium exposure.
ALLOW_NAKED_LONG_OPTIONS: bool = False

# ── Options DTE window ─────────────────────────────────────────────────────────
MIN_DTE: int = 21    # < 21 DTE: gamma risk spikes
MAX_DTE: int = 45    # > 45 DTE: too much premium at risk for too long
OPTION_EXIT_DTE: int = 7   # monitor closes single-leg options at <= this many DTE regardless of P&L (0 disables)

# ── Options delta targeting ────────────────────────────────────────────────────
MIN_DELTA: float = 0.20    # below this: lottery ticket (lowered for more leverage/excitement)
MAX_DELTA: float = 0.50    # above this: just trade the stock

# ── Beta filter ───────────────────────────────────────────────────────────────
# Candidates with realized beta > this are filtered out as bullish entries in
# range_bound regimes. Computed live from 40-day price bars vs SPY.
HIGH_BETA_THRESHOLD: float = 1.8

# ── IV Rank ────────────────────────────────────────────────────────────────────
MAX_IV_RANK_TO_BUY: float = 30.0   # don't buy when IV is expensive

# ── Risk-free rate (2026-07-21) ─────────────────────────────────────────────────
# Input to greeks.py's local Black-Scholes/IV solver -- neither this account's
# Alpaca tier nor its EODHD tier return real options Greeks (confirmed 403 on
# EODHD's UnicornBay options add-on, not currently subscribed), so Greeks are
# computed locally from live bid/ask instead. Greeks aren't very sensitive to
# small errors here -- update occasionally (e.g. roughly track the 3-month
# T-bill yield), not something that needs day-to-day tuning.
RISK_FREE_RATE: float = 0.045

# ── Spreads ────────────────────────────────────────────────────────────────────
MAX_SPREAD_DEBIT_PCT:  float = 0.33   # max debit as % of spread width
EARNINGS_BLACKOUT_DAYS: int = 5       # enforced for options/spreads when the network ticker brief supplies days_to_earnings (until then: dormant)

# ── Portfolio-level Greeks budget (2026-07-21) ─────────────────────────────────
# Every check above is single-position -- five individually-compliant same-
# direction bets can still stack into an aggregate delta that moves like 5x
# leveraged exposure on one directional call. This caps the WHOLE options book,
# not just each new trade. Table stakes in every systematic options methodology
# (Cboe's published index rules included); was missing entirely before this.
# None = disabled (default) -- set both to opt in. Expressed as % of equity,
# consistent with MAX_OPTION_PCT/MAX_SPREAD_PCT above.
MAX_PORTFOLIO_DELTA_PCT: float | None = None   # e.g. 0.15 = net delta-dollar exposure capped at 15% of equity
MAX_PORTFOLIO_VEGA_PCT:  float | None = None   # e.g. 0.05 = net vega-dollar exposure capped at 5% of equity

# ── Volatility-adaptive structure selection (2026-07-21) ───────────────────────
# Cboe's own Volatility-Managed PutWrite Index rotates from cash-secured-puts
# (low vol) toward an iron-condor-shaped defined-risk structure as VIX
# percentile rises -- this is the kit's equivalent: STRATEGY_MODE
# "premium_buyer" rotates to "spreads" for the session once the VIXY-proxy vol
# percentile (risk.py's pick_vol_adaptive_strategy_mode()) crosses the
# threshold below. Only ever tightens risk, never loosens it automatically --
# "equity" and "spreads" modes are untouched regardless of vol regime.
# Disabled by default; existing installs unaffected until turned on.
VOL_ADAPTIVE_STRUCTURE_ENABLED: bool = False
VOL_REGIME_HIGH_PCTL: float = 70.0   # VIXY-proxy percentile at/above which premium_buyer rotates to spreads

# ── Network rules ──────────────────────────────────────────────────────────────
# Blocked sectors are populated from Agentberg at runtime — no need to set here.
# Add permanent manual blocks if you want to avoid certain sectors regardless.
MANUAL_BLOCKED_SECTORS: list[str] = []

# Trust dial: adopt the network's loss-consensus blocked sectors as BINDING for
# this agent (default: advisory only — the network informs, you decide).
NETWORK_BLOCKED_BINDING: bool = False

# Anti-hedge dial: refuse entries opposite to a position already held in the same
# ticker (long + short simultaneously nets to ~zero exposure while paying the
# spread twice). Leave False if you run deliberate pairs/hedge strategies.
BLOCK_OPPOSITE_POSITIONS: bool = False

# Regimes to sit out entirely. "bear" means no new longs.
BLOCKED_REGIMES: list[str] = []
