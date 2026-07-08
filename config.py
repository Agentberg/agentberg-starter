"""
config.py — loads risk_params.py, then applies your character overlay and any
learned guidance overrides on top.

This file is kit mechanism, not values — it updates freely with the kit (Cat 0/A).
Your actual numbers (watchlist, position sizing, stops, DTE/delta windows, etc.)
live in risk_params.py, which the kit's own upgrades never touch. Edit that file
to change your strategy/risk tolerance, not this one.

DISCLAIMER: This is a software template, not investment advice.
"""
import os
from dotenv import load_dotenv

load_dotenv()

from risk_params import *  # noqa: F401,F403 -- your numbers, re-exported here

# Copy the mutable structures so the overlay logic below never mutates
# risk_params' own module-level objects via the shared reference a star-import
# creates -- WATCHLIST in particular is written to in place further down.
WATCHLIST = {k: list(v) for k, v in WATCHLIST.items()}
MANUAL_BLOCKED_SECTORS = list(MANUAL_BLOCKED_SECTORS)
BLOCKED_REGIMES = list(BLOCKED_REGIMES)

# ── Identity ───────────────────────────────────────────────────────────────────
AGENT_ID       = os.environ["AGENT_ID"]                          # unique name on Agentberg network
# Once registered, the network may have handed us a UNIQUE id (if our chosen one was
# taken). That confirmed id is persisted in .agent_id and takes precedence so our
# reputation and findings stay ours. See agent.py _ensure_registered().
_ID_FILE = os.path.join(os.path.dirname(__file__), ".agent_id")
if os.path.exists(_ID_FILE):
    _confirmed = open(_ID_FILE).read().strip()
    if _confirmed:
        AGENT_ID = _confirmed
AGENTBERG_URL  = os.environ.get("AGENTBERG_URL", "https://agentberg.ai")

# ── Broker credentials ─────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
if not ALPACA_PAPER and "paper" in ALPACA_BASE_URL.lower():
    raise EnvironmentError(
        "ALPACA_PAPER=false but ALPACA_BASE_URL still points to paper-api — "
        "set ALPACA_BASE_URL to the live endpoint or revert ALPACA_PAPER."
    )

# ── Character overlay ──────────────────────────────────────────────────────────
# If onboarding is complete (character.json), apply the operator's persona ON TOP of
# the defaults above. Anything the human deferred keeps the kit default. The agent
# operates by this until the human asks to change it. See character.py / setup.py.
try:
    import character as _character
    _c = _character.load()
except Exception:
    _c = {}

if _c:
    _instr = _c.get("instruments")
    if _instr == "equity":
        STRATEGY_MODE = "equity"
    elif _instr in ("options", "both"):
        STRATEGY_MODE = "premium_buyer"

    if _c.get("max_loss_per_trade_pct") is not None:
        EQUITY_STOP_LOSS_PCT = float(_c["max_loss_per_trade_pct"]) / 100.0
    if _c.get("take_profit_pct") is not None:
        EQUITY_TAKE_PROFIT_PCT = float(_c["take_profit_pct"]) / 100.0
    if _c.get("max_position_pct") is not None:
        MAX_POSITION_PCT = float(_c["max_position_pct"]) / 100.0
    if _c.get("max_positions") is not None:
        MAX_POSITIONS = int(_c["max_positions"])
    if _c.get("trade_in_bear") is True:
        BLOCKED_REGIMES = [r for r in BLOCKED_REGIMES if r != "bear"]

    # Never-trade list: sector names become blocked sectors; everything else is
    # treated as a ticker and removed from the watchlist entirely.
    for _item in _c.get("must_exclude", []):
        _s = _item.strip()
        if not _s:
            continue
        if _s.title() in WATCHLIST:
            MANUAL_BLOCKED_SECTORS = list(set(MANUAL_BLOCKED_SECTORS + [_s.title()]))
        else:
            for _sec in WATCHLIST:
                WATCHLIST[_sec] = [t for t in WATCHLIST[_sec] if t.upper() != _s.upper()]

    # Always-watch tickers the human insisted on.
    _incl = [x.strip().upper() for x in _c.get("must_include", []) if x.strip()]
    if _incl:
        WATCHLIST["Preferred"] = sorted(set(WATCHLIST.get("Preferred", []) + _incl))


# ── Guidance overrides (learned, not hand-set) ──────────────────────────────────
# Applied on top of the defaults + character overlay above — this is the agent's own
# prior APPLY decision (from run_guidance_cycle()/evaluate_guidance() in agent.py, or
# postcar peer guidance once it writes the same file shape), not a human edit and not
# the network deciding for the agent. The network only informs; this file only exists
# because the agent's own LLM already judged a specific change worth making — this
# block is the one piece that makes that decision actually take effect, instead of
# only landing in guidance_overrides.json as an audit trail nothing reads back.
#
# Safety: only applies to names that are ALREADY real config constants defined above
# (an LLM-suggested "param" is a free-form guess, not guaranteed to match anything —
# e.g. the guidance-eval prompt's own example, MOMENTUM_THRESHOLD, isn't a real
# constant in this file). Unknown params are skipped and logged, never silently
# created as new globals. Value is coerced to the current constant's type; a
# coercion failure skips that one override rather than raising. Any error in this
# whole block is swallowed — a malformed overrides file must never block startup.
_OVERRIDES_FILE = os.path.join(os.path.dirname(__file__), "guidance_overrides.json")


def _coerce_like(current, value):
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(float(value))
    if isinstance(current, float):
        return float(value)
    return value


try:
    import json as _json

    with open(_OVERRIDES_FILE) as _f:
        _applied = _json.load(_f).get("applied", [])

    # Keep only the latest entry per param (a param may have been overridden more
    # than once over time) — entries are appended in chronological order, so the
    # last occurrence for a given param is the most recent.
    _latest: dict[str, dict] = {}
    for _entry in _applied:
        _p = _entry.get("param")
        if _p:
            _latest[_p] = _entry

    for _param, _entry in _latest.items():
        if _param not in globals() or not _param.isupper():
            print(f"    [guidance] skipping unknown override param: {_param}")
            continue
        _current = globals()[_param]
        try:
            _new_value = _coerce_like(_current, _entry.get("value"))
        except (TypeError, ValueError):
            print(f"    [guidance] skipping override {_param}: value doesn't match expected type")
            continue
        globals()[_param] = _new_value
        print(f"    [guidance] applied override: {_param} {_current!r} -> {_new_value!r} "
              f"({str(_entry.get('rationale', ''))[:60]})")
except FileNotFoundError:
    pass
except Exception as _e:
    print(f"    [guidance] overrides load failed, using defaults ({_e})")
