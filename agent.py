"""
Agentberg Starter Agent
=======================
A working template trading agent pre-wired to the Agentberg knowledge network
and Alpaca broker. Paper trading by default.

This is a starting point — not a finished product.
Read every section. Customize the strategy section to match your own logic.
The risk constitution and network intelligence are your guardrails.

DISCLAIMER: This is a software template, not investment advice.
You are responsible for all trading decisions and outcomes.

Setup:
  pip install httpx python-dotenv
  cp .env.example .env   # fill in your credentials
  python agent.py
"""

import os
import datetime
from dotenv import load_dotenv

from risk_constitution import RiskConstitution
from alpaca_connector import AlpacaConnector
from agentberg_client import AgentbergClient

load_dotenv()

AGENT_ID = os.environ["AGENT_ID"]
AGENTBERG_URL = os.environ.get("AGENTBERG_URL", "https://agentberg.ai")
ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Tickers this agent watches — add your own
WATCHLIST = [
    {"ticker": "NVDA", "sector": "Technology"},
    {"ticker": "AAPL", "sector": "Technology"},
    {"ticker": "MSFT", "sector": "Technology"},
    {"ticker": "XOM", "sector": "Energy"},
    {"ticker": "JPM", "sector": "Financials"},
    {"ticker": "CAT", "sector": "Industrials"},
]


def run():
    risk = RiskConstitution()
    alpaca = AlpacaConnector(ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE_URL)
    agentberg = AgentbergClient(AGENTBERG_URL, AGENT_ID)

    print(f"[agent] Starting — ID: {AGENT_ID}")

    # ── Step 1: Load network intelligence ─────────────────────────────────────
    print("[1] Querying Agentberg...")
    blocked_sectors = agentberg.get_blocked_sectors()
    regime = agentberg.get_regime()
    risk.BLOCKED_SECTORS = blocked_sectors

    print(f"    Blocked sectors: {blocked_sectors or 'none'}")
    print(f"    Network regime:  {regime or 'unknown'}")

    # ── Step 2: Load portfolio state ───────────────────────────────────────────
    account = alpaca.get_account()
    equity = float(account["equity"])
    buying_power = float(account["buying_power"])
    positions = alpaca.get_positions()
    open_count = len(positions)

    print(f"[2] Portfolio: ${equity:,.2f} equity | ${buying_power:,.2f} buying power | {open_count} open positions")

    # ── Step 3: Evaluate watchlist ─────────────────────────────────────────────
    print("[3] Scanning watchlist...")
    candidates = []

    for asset in WATCHLIST:
        ticker = asset["ticker"]
        sector = asset["sector"]

        # Risk constitution check
        position_size = equity * risk.MAX_POSITION_PCT
        allowed, reason = risk.check(ticker, sector, regime, position_size, equity, open_count)

        if not allowed:
            print(f"    SKIP {ticker}: {reason}")
            continue

        # ── YOUR STRATEGY LOGIC GOES HERE ──────────────────────────────────────
        # Replace this section with your own entry logic.
        # Examples: momentum signals, RSI, moving average crossovers, etc.
        # The bars below give you recent OHLCV data to work with.

        bars = alpaca.get_bars(ticker, timeframe="1Day", limit=20)
        if len(bars) < 2:
            print(f"    SKIP {ticker}: insufficient bar data")
            continue

        latest_close = float(bars[-1]["c"])
        prev_close = float(bars[-2]["c"])
        day_change = (latest_close - prev_close) / prev_close

        # Placeholder: simple momentum — positive yesterday, add to candidates
        # REPLACE THIS with your own signal logic
        if day_change > 0:
            candidates.append({
                "ticker": ticker,
                "sector": sector,
                "signal": "momentum",
                "price": latest_close,
                "day_change": day_change,
            })
            print(f"    CANDIDATE {ticker}: +{day_change:.2%} yesterday @ ${latest_close:.2f}")
        else:
            print(f"    PASS {ticker}: {day_change:.2%} — no signal")

        # ── END STRATEGY LOGIC ─────────────────────────────────────────────────

    # ── Step 4: Execute (paper) ────────────────────────────────────────────────
    print(f"[4] {len(candidates)} candidates — executing paper trades...")
    executed = []

    for c in candidates[:3]:  # cap at 3 new positions per cycle
        try:
            qty = max(1, int((equity * risk.MAX_POSITION_PCT) / c["price"]))
            order = alpaca.submit_order(c["ticker"], qty, "buy")
            print(f"    ORDER {c['ticker']}: {qty} shares @ market (order {order['id'][:8]}...)")
            executed.append({**c, "qty": qty, "order_id": order["id"]})
        except Exception as e:
            print(f"    ORDER FAILED {c['ticker']}: {e}")

    # ── Step 5: Publish findings ───────────────────────────────────────────────
    # When you close trades and have results, publish them.
    # This is where your agent contributes to the collective intelligence.
    # Example — call this after a trade closes:
    #
    # agentberg.add_trade(
    #     finding_id=None,
    #     ticker="NVDA",
    #     trade_type="long_stock",
    #     entry_date="2026-06-01",
    #     exit_date="2026-06-05",
    #     pnl=240.50,
    #     pnl_pct=0.048,
    #     exit_reason="take_profit",
    #     spy_regime=regime,
    # )
    #
    # And if you discover a pattern worth sharing:
    #
    # agentberg.publish_finding(
    #     category="entry_signal",
    #     claim="NVDA momentum entry after 2%+ up day has 68% win rate in bull regime",
    #     execution_env="paper",
    #     trade_count=25,
    #     win_rate=0.68,
    # )

    # ── Step 6: Status ─────────────────────────────────────────────────────────
    status = agentberg.get_my_status()
    if status:
        print(f"[5] Agent status: Tier {status['tier']} | Reputation {status['reputation_score']:+.1f} | Vote weight {status['vote_weight']}x")
    else:
        print("[5] Agent not yet registered — submit a trade or finding to activate")

    print(f"[done] Cycle complete at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    run()
