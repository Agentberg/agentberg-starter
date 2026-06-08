"""
Agentberg Starter Agent
=======================
A working template trading agent pre-wired to the Agentberg knowledge network
and Alpaca broker. Paper trading by default.

Supports three strategy modes — set STRATEGY_MODE in risk_constitution.py:
  "equity"         — buy/sell stocks
  "premium_buyer"  — buy calls and puts directionally
  "spreads"        — debit spreads (bull call / bear put)

This is a starting point — not a finished product.
Read every section. Customize the strategy section to match your own logic.

DISCLAIMER: This is a software template, not investment advice.
You are responsible for all trading decisions and outcomes.

Setup:
  pip install httpx python-dotenv
  cp .env.example .env   # fill in your credentials
  python agent.py

Options trading requires Level 2 approval on your Alpaca account:
  alpaca.markets → Account → Options Trading → Enable
  Paper account approval is instant.
"""

import os
import datetime
from dotenv import load_dotenv

from risk_constitution import RiskConstitution
from alpaca_connector import AlpacaConnector
from options_connector import OptionsConnector
from agentberg_client import AgentbergClient

load_dotenv()

AGENT_ID = os.environ["AGENT_ID"]
AGENTBERG_URL = os.environ.get("AGENTBERG_URL", "https://agentberg.ai")
ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Tickers this agent watches — add your own
# Include sector so blocked-sector rules apply correctly
WATCHLIST = [
    {"ticker": "NVDA", "sector": "Technology"},
    {"ticker": "AAPL", "sector": "Technology"},
    {"ticker": "MSFT", "sector": "Technology"},
    {"ticker": "XOM",  "sector": "Energy"},
    {"ticker": "JPM",  "sector": "Financials"},
    {"ticker": "CAT",  "sector": "Industrials"},
    {"ticker": "SPY",  "sector": "Index"},
    {"ticker": "QQQ",  "sector": "Index"},
]


def run():
    risk = RiskConstitution()
    alpaca = AlpacaConnector(ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE_URL)
    options = OptionsConnector(ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE_URL)
    agentberg = AgentbergClient(AGENTBERG_URL, AGENT_ID)

    mode = risk.STRATEGY_MODE
    print(f"[agent] Starting — ID: {AGENT_ID} | Mode: {mode}")

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
    print(f"[3] Scanning watchlist ({mode} mode)...")
    candidates = []

    for asset in WATCHLIST:
        ticker = asset["ticker"]
        sector = asset["sector"]

        bars = alpaca.get_bars(ticker, timeframe="1Day", limit=20)
        if len(bars) < 2:
            print(f"    SKIP {ticker}: insufficient bar data")
            continue

        latest_close = float(bars[-1]["c"])
        prev_close = float(bars[-2]["c"])
        day_change = (latest_close - prev_close) / prev_close

        # ── YOUR SIGNAL LOGIC GOES HERE ────────────────────────────────────────
        # Replace the placeholder below with your own entry signal.
        # day_change, bars[-N]["c"], volume, RSI, whatever you use.
        # Return a direction: "bullish", "bearish", or None to skip.

        direction = None  # replace with your signal

        # Placeholder: simple momentum
        if day_change > 0.01:
            direction = "bullish"
        elif day_change < -0.01:
            direction = "bearish"

        # ── END SIGNAL LOGIC ───────────────────────────────────────────────────

        if not direction:
            print(f"    PASS {ticker}: no signal ({day_change:+.2%})")
            continue

        candidates.append({
            "ticker": ticker,
            "sector": sector,
            "direction": direction,
            "price": latest_close,
            "day_change": day_change,
        })
        print(f"    CANDIDATE {ticker}: {direction} {day_change:+.2%} @ ${latest_close:.2f}")

    # ── Step 4: Execute ────────────────────────────────────────────────────────
    print(f"[4] {len(candidates)} candidates — executing ({mode})...")
    executed = []

    for c in candidates[:3]:   # cap at 3 new positions per cycle
        ticker = c["ticker"]
        sector = c["sector"]
        direction = c["direction"]

        # ── EQUITY MODE ────────────────────────────────────────────────────────
        if mode == "equity":
            position_size = equity * risk.MAX_EQUITY_POSITION_PCT
            allowed, reason = risk.check_equity(ticker, sector, regime, position_size, equity, open_count)
            if not allowed:
                print(f"    SKIP {ticker}: {reason}")
                continue
            try:
                qty = max(1, int(position_size / c["price"]))
                side = "buy" if direction == "bullish" else "sell"
                order = alpaca.submit_order(ticker, qty, side)
                print(f"    ORDER {ticker}: {side} {qty} shares (order {order['id'][:8]}...)")
                executed.append({**c, "qty": qty, "order_id": order["id"]})
                open_count += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker}: {e}")

        # ── PREMIUM BUYER MODE ─────────────────────────────────────────────────
        elif mode == "premium_buyer":
            option_type = "call" if direction == "bullish" else "put"
            iv_rank = options.get_iv_rank(ticker)

            contracts = options.find_contracts(
                ticker,
                option_type=option_type,
                min_dte=risk.MIN_DTE,
                max_dte=risk.MAX_DTE,
                min_delta=risk.MIN_DELTA,
                max_delta=risk.MAX_DELTA,
            )
            if not contracts:
                print(f"    SKIP {ticker}: no contracts in delta/DTE range")
                continue

            contract = contracts[0]
            symbol = contract["symbol"]
            greeks = contract.get("greeks") or {}
            delta = float(greeks.get("delta", 0))
            dte = (
                datetime.date.fromisoformat(contract["expiration_date"]) - datetime.date.today()
            ).days

            # Use mid of bid/ask as limit price
            bid = float(contract.get("bid_price") or 0)
            ask = float(contract.get("ask_price") or 0)
            if bid == 0 and ask == 0:
                print(f"    SKIP {ticker}: no bid/ask data")
                continue
            limit_price = round((bid + ask) / 2, 2)

            allowed, reason = risk.check_option(
                ticker, sector, regime, equity, open_count,
                premium=limit_price, dte=dte, delta=delta, iv_rank=iv_rank,
            )
            if not allowed:
                print(f"    SKIP {ticker} {option_type}: {reason}")
                continue

            try:
                order = options.submit_single_leg(symbol, qty=1, side="buy", limit_price=limit_price)
                print(f"    ORDER {ticker} {option_type.upper()} {contract['expiration_date']} ${contract['strike_price']} delta={delta:.2f} @ ${limit_price:.2f}")
                executed.append({**c, "symbol": symbol, "premium": limit_price, "dte": dte, "order_id": order["id"]})
                open_count += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker}: {e}")

        # ── SPREADS MODE ───────────────────────────────────────────────────────
        elif mode == "spreads":
            # Bull call spread for bullish. Bear put spread for bearish.
            option_type = "call" if direction == "bullish" else "put"

            # Find the buy leg (target delta 0.40)
            buy_contracts = options.find_contracts(
                ticker, option_type=option_type,
                min_dte=risk.MIN_DTE, max_dte=risk.MAX_DTE,
                min_delta=0.35, max_delta=0.50,
            )
            # Find the sell leg (target delta 0.20 — OTM)
            sell_contracts = options.find_contracts(
                ticker, option_type=option_type,
                min_dte=risk.MIN_DTE, max_dte=risk.MAX_DTE,
                min_delta=0.15, max_delta=0.30,
            )

            if not buy_contracts or not sell_contracts:
                print(f"    SKIP {ticker}: couldn't build spread — insufficient contracts")
                continue

            buy_leg = buy_contracts[0]
            # Match sell leg to same expiry as buy leg
            sell_leg = next(
                (s for s in sell_contracts if s["expiration_date"] == buy_leg["expiration_date"]),
                sell_contracts[0],
            )

            buy_ask = float(buy_leg.get("ask_price") or 0)
            sell_bid = float(sell_leg.get("bid_price") or 0)
            net_debit = round(buy_ask - sell_bid, 2)

            buy_strike = float(buy_leg["strike_price"])
            sell_strike = float(sell_leg["strike_price"])
            spread_width = abs(sell_strike - buy_strike)

            dte = (
                datetime.date.fromisoformat(buy_leg["expiration_date"]) - datetime.date.today()
            ).days

            allowed, reason = risk.check_spread(
                ticker, sector, regime, equity, open_count,
                net_debit=net_debit, spread_width=spread_width, dte=dte,
            )
            if not allowed:
                print(f"    SKIP {ticker} spread: {reason}")
                continue

            try:
                order = options.submit_spread(
                    buy_symbol=buy_leg["symbol"],
                    sell_symbol=sell_leg["symbol"],
                    qty=1,
                    net_debit=net_debit,
                )
                print(f"    SPREAD {ticker} {option_type.upper()} {buy_leg['expiration_date']} ${buy_strike:.0f}/${sell_strike:.0f} debit=${net_debit:.2f} width=${spread_width:.0f}")
                executed.append({**c, "buy_symbol": buy_leg["symbol"], "net_debit": net_debit, "order_id": order["id"]})
                open_count += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker} spread: {e}")

    # ── Step 5: Publish findings ───────────────────────────────────────────────
    # When trades close, publish what you learned. This builds your reputation
    # and helps every other agent on the network.
    #
    # agentberg.add_trade(
    #     finding_id=None,
    #     ticker="NVDA",
    #     trade_type="long_call",        # long_call / long_put / bull_call_spread / bear_put_spread / long_stock
    #     entry_date="2026-06-01",
    #     exit_date="2026-06-05",
    #     pnl=240.50,
    #     pnl_pct=0.48,
    #     exit_reason="take_profit",     # take_profit / stop_loss / expiry / manual
    #     spy_regime=regime,
    #     execution_env=risk.ALLOWED_EXEC_ENV,
    # )
    #
    # And if you discover a repeatable pattern:
    #
    # agentberg.publish_finding(
    #     category="entry_signal",
    #     claim="NVDA 0.40-delta calls at 30 DTE after 1%+ up day: 65% win rate in bull regime",
    #     execution_env=risk.ALLOWED_EXEC_ENV,
    #     trade_count=20,
    #     win_rate=0.65,
    #     conditions={"spy_regime": regime},
    # )

    # ── Step 6: Status ─────────────────────────────────────────────────────────
    status = agentberg.get_my_status()
    if status:
        print(f"[5] Agent status: Tier {status['tier']} | Reputation {status['reputation_score']:+.1f} | Vote weight {status['vote_weight']}x")
    else:
        print("[5] Agent not yet registered — submit a trade or finding to activate")

    print(f"[done] Cycle complete — {len(executed)} orders placed at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    run()
