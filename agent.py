"""
agent.py — Strategy logic only.

One function per concern:
  run_session()       — full cycle: query → filter → scan → rank → execute → report
  check_positions()   — stop-loss / take-profit monitor (called by scheduler every 5 min)

All parameters live in config.py.
All SQL lives in memory.py.
API calls live in agentberg.py and alpaca.py.

DISCLAIMER: This is a software template, not investment advice.
You are responsible for all trading decisions and outcomes.
"""

import datetime

import character
import config as cfg
import knowledge
import memory
import risk
import structures
from agentberg import AgentbergClient
from alpaca import AlpacaClient
from llm import rank_candidates

# Clients — constructed once at import time, reused across calls
_alpaca    = AlpacaClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, cfg.ALPACA_BASE_URL)
_agentberg = AgentbergClient(cfg.AGENTBERG_URL, cfg.AGENT_ID)


def _ensure_registered():
    """One-time: claim our id on the network. If it was already taken, the network
    assigns a UNIQUE one — we persist it to .agent_id, adopt it for this session, and
    tell the operator. After that, config.py loads the confirmed id automatically."""
    import os
    idfile = os.path.join(os.path.dirname(__file__), ".agent_id")
    if os.path.exists(idfile):
        return  # already registered
    try:
        res = _agentberg.register(cfg.AGENT_ID)
    except Exception as e:
        print(f"    [register] skipped ({e}) — using '{cfg.AGENT_ID}'")
        return
    confirmed = res.get("agent_id", cfg.AGENT_ID)
    with open(idfile, "w") as f:
        f.write(confirmed)
    if res.get("reassigned"):
        print(f"    [register] ⚠  {res.get('message', '')}")
        cfg.AGENT_ID = confirmed          # adopt it for the rest of this session
        _agentberg.agent_id = confirmed
    else:
        print(f"    [register] id '{confirmed}' is yours")


def reconcile_ledger():
    """
    Broker is the source of truth — the local ledger is only a cache.

    Bracket/OTO stops fire at Alpaca even when this app is off, and check_positions()
    only records exits IT performs. So any trade still 'open' locally but no longer
    held at the broker was closed server-side and never recorded. Left uncorrected,
    those phantom-open losers silently drop out of the win-rate denominator and we
    publish inflated findings to the whole network and cast outcome votes from drifted
    data. Run this BEFORE any publish or vote, every session.
    """
    open_trades = memory.get_open_trades()
    if not open_trades:
        return
    held = _alpaca.get_position_symbols()
    reconciled = 0
    for t in open_trades:
        legs = [s for s in (t.get("long_symbol"), t.get("short_symbol")) if s] or [t["symbol"]]
        if any(s in held for s in legs):
            continue   # still open at the broker

        long_sym = t.get("long_symbol") or t["symbol"]
        fill      = _alpaca.get_last_fill(long_sym, side="sell")
        exit_price = float(fill.get("filled_avg_price") or 0) if fill else 0.0
        entry = t.get("entry_price") or 0
        qty   = t.get("qty") or 0
        mult  = t.get("multiplier") or 1
        if exit_price and entry:
            pnl     = (exit_price - entry) * qty * mult
            pnl_pct = (exit_price - entry) / entry
        else:
            pnl, pnl_pct = 0.0, 0.0
        memory.record_trade_close(
            t["id"], exit_price=exit_price, pnl=pnl, pnl_pct=pnl_pct,
            exit_reason="reconciled_broker",
        )
        reconciled += 1
    if reconciled:
        print(f"[reconcile] Closed {reconciled} trade(s) from broker truth (server-side/offline exits)")


def run_session():
    """
    Full trading cycle. Call once at market open and once at close.
    """
    memory.init_db()
    mode = cfg.STRATEGY_MODE
    print(f"\n[agent] {datetime.datetime.now():%Y-%m-%d %H:%M} | ID: {cfg.AGENT_ID} | Mode: {mode}")
    if not character.is_set():
        print("    [setup] No character yet — ask the human the onboarding questions "
              "(see AGENTS.md) or run `python setup.py`. Using kit defaults until then.")
    else:
        print(f"    [character] {character.summary()}")
    guide = _agentberg.get_guide()
    if guide:
        print(f"    [playbook] Agentberg Playbook v{guide.get('version','?')} loaded — "
              f"the network informs, you decide ({cfg.AGENTBERG_URL}/guide)")
    _ensure_registered()

    # ── Reconcile FIRST — rebuild close state from the broker before any publish/vote
    print("[reconcile] Syncing local ledger with broker...")
    reconcile_ledger()

    # ── Step 0: Skills — regime, risk calendar, market health ─────────────────
    print("[0] Loading skills...")
    skills = _agentberg.get_skills()

    regime        = None
    risk_level    = "unknown"
    health_label  = "unknown"
    position_size_override = None

    if skills:
        skill_regime = skills.get("regime", {})
        skill_risk   = skills.get("risk_calendar", {})
        skill_health = skills.get("health", {})

        regime       = skill_regime.get("regime")
        risk_level   = skill_risk.get("risk_level", "unknown")
        health_label = skill_health.get("health_label", "unknown")

        print(f"    Regime:  {regime or 'unknown'} — {skill_regime.get('strategy_favored', '')}")
        print(f"    Risk:    {risk_level.upper()} — {skill_risk.get('verdict', '')}")
        print(f"    Health:  {health_label.upper()} — {skill_health.get('verdict', '')}")

        for flag in skill_health.get("flags", []):
            print(f"    ⚠ {flag}")
        for ev in [e for e in skill_risk.get("events", []) if e.get("impact") == "high"]:
            print(f"    ⚠ HIGH-IMPACT EVENT: {ev['date']} — {ev['event']}")

        # Halve position size when market health is stressed
        if health_label == "stressed":
            position_size_override = cfg.MAX_POSITION_PCT * 0.5
            print(f"    [RISK OVERRIDE] Health STRESSED — position size halved to {position_size_override:.1%}")
    else:
        print("    [WARNING] Skills unavailable — continuing with network intelligence only")

    effective_position_pct = position_size_override or cfg.MAX_POSITION_PCT

    # ── Step 1: Network intelligence ──────────────────────────────────────────
    print("[1] Querying Agentberg network...")
    network_blocked_map = _agentberg.get_blocked_sectors()          # {sector: finding_id}
    network_regime      = _agentberg.get_regime()
    # Agentberg INFORMS, it does not DECIDE. Network blocked-sectors are ADVISORY —
    # passed into AI ranking so the agent weighs them, never a hard skip. Only the
    # operator's OWN blocks bind (that's the human deciding, not the network).
    network_blocked = list(network_blocked_map.keys())              # advisory
    blocked_sectors = list(cfg.MANUAL_BLOCKED_SECTORS)              # binding (operator's rule)

    # Skills regime is more current than network consensus
    if not regime:
        regime = network_regime

    print(f"    Your blocks (binding):    {blocked_sectors or 'none'}")
    print(f"    Network flags (advisory): {network_blocked or 'none'}")
    print(f"    Regime:  {regime or 'unknown'}")

    entry_signals = _agentberg.get_entry_signals()
    if entry_signals:
        top = entry_signals[0]
        print(f"    Network entry signal (weight {top.get('weight', '?')}x): {top.get('claim', '')[:80]}")

    brief = _agentberg.get_network_brief(regime=regime)
    if brief:
        verdict  = brief.get("verdict", "amber").upper()
        wr       = brief.get("network_win_rate")
        pnl      = brief.get("cumulative_pnl", 0)
        conf     = brief.get("confidence", 0)
        wr_str   = f"{wr:.0%}" if wr is not None else "n/a"
        print(f"    Network brief: {verdict} (confidence {conf:.0%}) | WR {wr_str} | Network P&L ${pnl:+,.0f}")

    alerts = _agentberg.get_consensus_alerts()
    for alert in alerts:
        print(f"    ⚠ CONSENSUS ALERT: {alert['sector']} — {alert['agent_count']} agents, "
              f"${alert['cumulative_loss']:,.0f} cumulative loss")
        _agentberg.ack_alert(alert["alert_id"])

    # ── Step 2: Portfolio state ────────────────────────────────────────────────
    account = _alpaca.get_account()
    equity        = float(account["equity"])
    buying_power  = float(account["buying_power"])
    positions     = _alpaca.get_positions()
    open_count    = len(positions)

    print(f"[2] Portfolio: ${equity:,.2f} equity | ${buying_power:,.2f} BP | {open_count} open positions")

    # ── Step 3: Scan watchlist ─────────────────────────────────────────────────
    print(f"[3] Scanning {sum(len(v) for v in cfg.WATCHLIST.values())} tickers ({mode} mode)...")
    candidates = []

    for sector, tickers in cfg.WATCHLIST.items():
        if sector in blocked_sectors:
            print(f"    SKIP {sector}: blocked by your own rules")
            continue

        for ticker in tickers:
            bars = _alpaca.get_bars(ticker, timeframe="1Day", limit=40)
            if len(bars) < 2:
                continue

            latest_close = float(bars[-1]["c"])
            prev_close   = float(bars[-2]["c"])
            day_change   = (latest_close - prev_close) / prev_close

            # ── YOUR SIGNAL LOGIC GOES HERE ────────────────────────────────────
            # Replace the placeholder below with your own entry signal.
            # Examples: RSI, SMA crossover, volume spike, breakout pattern.
            # Return a direction: "bullish", "bearish", or None to skip.

            direction = None   # replace with your signal

            # Momentum signal — 0.3% threshold (loosened from 1% to catch range-bound moves)
            if day_change > 0.003:
                direction = "bullish"
            elif day_change < -0.003:
                direction = "bearish"

            # ── END SIGNAL LOGIC ───────────────────────────────────────────────

            if not direction:
                continue

            candidates.append({
                "ticker":     ticker,
                "sector":     sector,
                "direction":  direction,
                "price":      latest_close,
                "day_change": day_change,
            })
            print(f"    CANDIDATE {ticker} [{sector}]: {direction} {day_change:+.2%} @ ${latest_close:.2f}")

    print(f"    {len(candidates)} candidate(s) before LLM filter")

    # ── Step 3b: LLM ranking (optional) ───────────────────────────────────────
    candidates = rank_candidates(candidates, regime, risk_level, health_label, network_blocked)
    candidates = candidates[:cfg.MAX_NEW_PER_CYCLE]

    # ── Step 4: Execute ────────────────────────────────────────────────────────
    print(f"[4] Executing {len(candidates)} trade(s) ({mode})...")
    executed = []

    for c in candidates:
        ticker    = c["ticker"]
        sector    = c["sector"]
        direction = c["direction"]

        # Trade rationale (PRIVATE to the operator) — assembled from the REAL signal +
        # the AI's recorded reason, captured NOW so it can't be hallucinated after the
        # outcome is known. Recorded with the trade; reviewed via `python journal.py`.
        thesis = f"{direction} {ticker} [{sector}] — {c.get('day_change', 0):+.1%} momentum"
        if c.get("reason"):
            thesis += f"; AI: {c['reason']}"
        expected_pct = cfg.TAKE_PROFIT_PCT
        stop_pct = cfg.EQUITY_STOP_LOSS_PCT if mode == "equity" else cfg.OPTION_STOP_LOSS_PCT
        signal = {"day_change": c.get("day_change"), "direction": direction}

        if mode == "equity":
            pos_value = equity * effective_position_pct
            allowed, reason = risk.check_equity(
                ticker, sector, regime, blocked_sectors, pos_value, equity, open_count
            )
            if not allowed:
                print(f"    SKIP {ticker}: {reason}")
                continue
            try:
                qty        = max(1, int(pos_value / c["price"]))
                side       = "buy" if direction == "bullish" else "sell"
                stop_price = c["price"] * (1 - cfg.EQUITY_STOP_LOSS_PCT) if side == "buy" else None
                order      = _alpaca.submit_order(ticker, qty, side, stop_loss_price=stop_price)
                trade_id   = memory.record_trade_open(ticker, sector, c["price"], qty,
                                signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct)
                print(f"    ORDER {ticker}: {side} ×{qty} @ ~${c['price']:.2f}  stop=${stop_price:.2f if stop_price else 'none'}")
                executed.append({**c, "qty": qty, "order_id": order["id"], "memory_id": trade_id})
                open_count += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker}: {e}")

        elif mode == "premium_buyer":
            option_type = "call" if direction == "bullish" else "put"
            iv_rank     = _alpaca.get_iv_rank(ticker)
            contracts   = _alpaca.find_option_contracts(
                ticker, option_type,
                min_dte=cfg.MIN_DTE, max_dte=cfg.MAX_DTE,
                min_delta=cfg.MIN_DELTA, max_delta=cfg.MAX_DELTA,
            )
            if not contracts:
                print(f"    SKIP {ticker}: no contracts in DTE/delta range")
                continue

            contract    = contracts[0]
            greeks      = contract.get("greeks") or {}
            delta       = float(greeks.get("delta", 0))
            dte         = (datetime.date.fromisoformat(contract["expiration_date"]) - datetime.date.today()).days
            bid         = float(contract.get("bid_price") or 0)
            ask         = float(contract.get("ask_price") or 0)
            if bid == 0 and ask == 0:
                print(f"    SKIP {ticker}: no bid/ask")
                continue
            limit_price = round((bid + ask) / 2, 2)

            allowed, reason = risk.check_option(
                ticker, sector, regime, blocked_sectors, equity, open_count,
                premium=limit_price, dte=dte, delta=delta, iv_rank=iv_rank,
            )
            if not allowed:
                print(f"    SKIP {ticker} {option_type}: {reason}")
                continue
            try:
                order    = _alpaca.submit_option_single(contract["symbol"], qty=1, side="buy", limit_price=limit_price)
                trade_id = memory.record_trade_open(ticker, sector, limit_price, 1, trade_type=f"long_{option_type}",
                                signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct)
                print(f"    ORDER {ticker} {option_type.upper()} {contract['expiration_date']} ${contract['strike_price']} δ={delta:.2f} @ ${limit_price:.2f}")
                executed.append({**c, "symbol": contract["symbol"], "premium": limit_price, "memory_id": trade_id})
                open_count += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker}: {e}")

        elif mode == "spreads":
            option_type   = "call" if direction == "bullish" else "put"
            buy_contracts = _alpaca.find_option_contracts(ticker, option_type, min_dte=cfg.MIN_DTE, max_dte=cfg.MAX_DTE, min_delta=0.35, max_delta=0.50)
            sell_contracts = _alpaca.find_option_contracts(ticker, option_type, min_dte=cfg.MIN_DTE, max_dte=cfg.MAX_DTE, min_delta=0.15, max_delta=0.30)
            if not buy_contracts or not sell_contracts:
                print(f"    SKIP {ticker}: couldn't build spread")
                continue

            buy_leg  = buy_contracts[0]
            sell_leg = next((s for s in sell_contracts if s["expiration_date"] == buy_leg["expiration_date"]), sell_contracts[0])
            buy_ask  = float(buy_leg.get("ask_price") or 0)
            sell_bid = float(sell_leg.get("bid_price") or 0)
            net_debit     = round(buy_ask - sell_bid, 2)
            spread_width  = abs(float(buy_leg["strike_price"]) - float(sell_leg["strike_price"]))
            dte           = (datetime.date.fromisoformat(buy_leg["expiration_date"]) - datetime.date.today()).days

            allowed, reason = risk.check_spread(
                ticker, sector, regime, blocked_sectors, equity, open_count,
                net_debit=net_debit, spread_width=spread_width, dte=dte,
            )
            if not allowed:
                print(f"    SKIP {ticker} spread: {reason}")
                continue

            # Build-time gate (structures.py): fail-closed structural check before any
            # order is sent. Refuses unknown structures or any whose max_loss isn't a
            # bounded positive number — naked/ratio legs can't get past this.
            ok, why = structures.validate_structure(
                "debit_vertical", max_loss=net_debit * 100,
                legs=[{"role": "long", "symbol": buy_leg["symbol"]},
                      {"role": "short", "symbol": sell_leg["symbol"]}],
            )
            if not ok:
                print(f"    SKIP {ticker} spread: structure gate — {why}")
                continue
            try:
                order    = _alpaca.submit_option_spread(buy_leg["symbol"], sell_leg["symbol"], qty=1, net_debit=net_debit)
                trade_id = memory.record_trade_open(ticker, sector, net_debit, 1, trade_type=f"{option_type}_spread",
                                signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct,
                                long_symbol=buy_leg["symbol"], short_symbol=sell_leg["symbol"],
                                multiplier=100, order_id=order.get("id"))
                print(f"    SPREAD {ticker} {option_type.upper()} ${float(buy_leg['strike_price']):.0f}/${float(sell_leg['strike_price']):.0f} debit=${net_debit:.2f}")
                executed.append({**c, "memory_id": trade_id, "net_debit": net_debit})
                open_count += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker} spread: {e}")

    # ── Step 5: Publish findings (once per day) ────────────────────────────────
    _maybe_publish(blocked_sectors, regime)

    # ── Step 6: Write session to memory ───────────────────────────────────────
    memory.record_session(
        portfolio_value=equity,
        buying_power=buying_power,
        blocked_sectors=blocked_sectors,
        candidates_found=len(candidates),
        positions_opened=len(executed),
        positions_closed=0,   # updated by check_positions()
        session_pnl=0,        # calculated from closed trades
        regime=regime,
    )

    # ── Step 7: Agent reputation ───────────────────────────────────────────────
    status = _agentberg.get_my_status()
    if status:
        print(f"[7] Status: Tier {status['tier']} | Reputation {status['reputation_score']:+.1f} | Vote weight {status['vote_weight']}x | Votes cast {status.get('votes_cast', 0)}")

    # ── Step 8: Weekly knowledge upload (capabilities + verified metrics) ───────
    # No-ops outside this agent's upload window; shares capabilities, never alpha.
    try:
        result = knowledge.maybe_upload(_agentberg, cfg.AGENT_ID)
        if result.get("status") == "accepted":
            print(f"[8] Uploaded weekly knowledge for {result['iso_week']}")
    except Exception as e:
        print(f"[8] Knowledge upload skipped ({e})")

    # ── Step 9: Pull-to-review — surface a newer kit version, never auto-apply ──
    try:
        upd = knowledge.check_kit_update(_agentberg)
        if upd.get("status") == "update_available":
            print(f"[9] Kit update available: v{upd['latest']} (you have v{upd['current']}) — review before adopting:")
            for entry in upd["changes"]:
                for item in entry.get("added", []):
                    print(f"      + {item}")
            print("      Adopt via UPGRADING.md — copy reviewed files in; never auto-apply to a live agent.")
    except Exception as e:
        print(f"[9] Update check skipped ({e})")

    stats = memory.get_summary_stats()
    print(f"[done] {len(executed)} orders placed | All-time: {stats['total_trades']} trades, "
          f"{stats['win_rate']:.0%} WR, ${stats['net_pnl']:+,.2f} P&L")


def check_positions():
    """
    Stop-loss and take-profit monitor. Called every 5 minutes by scheduler.
    Does NOT open new positions — only closes based on P&L thresholds.

    Spreads are resolved as a unit: a debit spread's two legs are closed together
    (one mleg order), never per-leg. Closing the long leg alone trips Alpaca's
    "uncovered contract" reject, and judging the short leg on its own P&L stops out
    healthy spreads. So we resolve spreads first from the local ledger, then handle
    whatever single positions remain.
    """
    positions = _alpaca.get_positions()
    if not positions:
        return
    pos_by_symbol = {p["symbol"]: p for p in positions}
    handled: set = set()
    open_trades = memory.get_open_trades()
    # Action-time gate (structures.py): every symbol that is a leg of an open
    # multi-leg structure. None of these may be closed standalone (see single loop).
    structure_legs = structures.open_structure_leg_symbols(open_trades)

    # ── Spreads first (two-leg, closed atomically) ─────────────────────────────
    for trade in open_trades:
        long_sym  = trade.get("long_symbol")
        short_sym = trade.get("short_symbol")
        if not short_sym:
            continue   # not a spread
        long_pos  = pos_by_symbol.get(long_sym)
        short_pos = pos_by_symbol.get(short_sym)
        if not long_pos or not short_pos:
            continue   # legs not both live (reconcile handles vanished spreads)

        qty  = trade.get("qty") or 1
        mult = trade.get("multiplier") or 100
        net_pl_dollars = float(long_pos.get("unrealized_pl", 0)) + float(short_pos.get("unrealized_pl", 0))
        cost_dollars   = (trade.get("entry_price") or 0) * mult * qty
        net_pct        = (net_pl_dollars / cost_dollars) if cost_dollars else 0.0

        reason = None
        if net_pct <= -cfg.OPTION_STOP_LOSS_PCT:
            reason = "stop_loss"
        elif net_pct >= cfg.TAKE_PROFIT_PCT:
            reason = "take_profit"
        if not reason:
            handled.update([long_sym, short_sym])
            continue

        net_credit = (trade.get("entry_price") or 0) + net_pl_dollars / (mult * qty)
        print(f"[monitor] {reason.upper()} SPREAD {trade['symbol']} ({long_sym}/{short_sym}): "
              f"net {net_pct:.1%} — closing both legs")
        try:
            _alpaca.submit_option_spread_close(long_sym, short_sym, qty=qty, net_credit=net_credit)
            exit_price = round((trade.get("entry_price") or 0) + net_pl_dollars / (mult * qty), 2)
            memory.record_trade_close(trade["id"], exit_price=exit_price, pnl=net_pl_dollars,
                                      pnl_pct=net_pct, exit_reason=reason)
            print(f"    [journal] {trade['symbol']} closed {net_pct:+.1%} ({reason}) — review with `python journal.py`")
            _vote_sector_outcome(trade, net_pl_dollars)
        except Exception as e:
            print(f"[monitor] Spread close failed {trade['symbol']}: {e}")
        handled.update([long_sym, short_sym])

    # ── Single positions (equity + single-leg options) ─────────────────────────
    for pos in positions:
        symbol = pos["symbol"]
        if symbol in handled:
            continue
        # Action-time gate: never close one leg of an open structure on its own. A
        # half-live spread (one leg vanished) would otherwise be stopped out here on
        # its standalone P&L, stranding the other leg — the naked-leg bug.
        if symbol in structure_legs:
            print(f"[monitor] SKIP {symbol}: leg of an open structure — never closed alone")
            continue
        unrealised_pnl_pct = float(pos.get("unrealized_plpc", 0))
        asset_class = pos.get("asset_class", "")
        stop_threshold   = -cfg.EQUITY_STOP_LOSS_PCT if asset_class == "us_equity" else -cfg.OPTION_STOP_LOSS_PCT
        profit_threshold = cfg.TAKE_PROFIT_PCT

        reason = None
        if unrealised_pnl_pct <= stop_threshold:
            reason = "stop_loss"
        elif unrealised_pnl_pct >= profit_threshold:
            reason = "take_profit"
        if not reason:
            continue

        print(f"[monitor] {reason.upper()} {symbol}: {unrealised_pnl_pct:.1%} — closing")
        try:
            _alpaca.close_position(symbol)
            _record_close(symbol, reason, unrealised_pnl_pct)
        except Exception as e:
            print(f"[monitor] Close failed {symbol}: {e}")


def _record_close(symbol: str, reason: str, pnl_pct: float):
    open_trades = memory.get_open_trades()
    trade = next((t for t in open_trades if t["symbol"] == symbol), None)
    if not trade:
        return
    pnl_dollars = (trade.get("entry_price") or 0) * (trade.get("qty") or 0) * pnl_pct
    memory.record_trade_close(trade["id"], exit_price=0, pnl=pnl_dollars, pnl_pct=pnl_pct, exit_reason=reason)
    # Transparency to the operator — the recorded thesis is now checked against reality.
    print(f"    [journal] {symbol} closed {pnl_pct:+.1%} ({reason}) — review with `python journal.py`")
    _vote_sector_outcome(trade, pnl_dollars)


def _vote_sector_outcome(trade: dict, pnl_dollars: float):
    """Vote on the sector_failure finding that blocked (or didn't block) this sector.
    Loss in a blocked sector → upvote (block was right); win → downvote (block may be wrong)."""
    sector = trade.get("sector")
    if not sector:
        return
    blocked_map = _agentberg.get_blocked_sectors()
    finding_id  = blocked_map.get(sector)
    if finding_id:
        vote = "up" if pnl_dollars < 0 else "down"
        _agentberg.cast_vote(finding_id, vote)
        print(f"    [vote] {vote}voted {sector} sector_failure (finding {finding_id})")


def _maybe_publish(blocked_sectors: list[str], regime: str | None):
    """Publish sector findings once per day based on local memory performance."""
    if memory.was_published_today("sector_findings"):
        print("[5] Findings already published today — skipping")
        return

    print("[5] Publishing findings to Agentberg...")
    sector_perf = memory.get_sector_performance()
    published = 0

    for s in sector_perf:
        sector = s["sector"]
        if not sector or s["trade_count"] < 5:
            continue

        if s["win_rate"] >= 0.70:
            result = _agentberg.publish_finding(
                category="trade_result",
                claim=f"{sector} sector performing well — {s['win_rate']:.0%} WR over {s['trade_count']} trades, net P&L ${s['net_pnl']:+,.2f}",
                trade_count=s["trade_count"],
                win_rate=s["win_rate"],
                conditions={"spy_regime": regime, "sector": sector},
            )
            if result:
                published += 1

        elif s["win_rate"] <= 0.30:
            result = _agentberg.publish_finding(
                category="sector_failure",
                claim=f"{sector} sector failing — {s['win_rate']:.0%} WR over {s['trade_count']} trades, net P&L ${s['net_pnl']:+,.2f}",
                trade_count=s["trade_count"],
                win_rate=s["win_rate"],
                conditions={"spy_regime": regime, "sector": sector},
            )
            if result:
                published += 1

    # Publish recent closed trades from Alpaca — once per day (yesterday's only,
    # separate gate to prevent re-publishing the same orders every day).
    if not memory.was_published_today("recent_trades"):
        closed_orders = _alpaca.get_recent_closed_orders(limit=50, days=1)
        trade_published = 0
        for order in closed_orders:
            ticker    = order.get("symbol", "")
            filled_at = (order.get("filled_at") or "")[:10]
            if not ticker or not filled_at:
                continue
            _agentberg.add_trade(
                finding_id=None,
                ticker=ticker,
                trade_type="long_stock",
                entry_date=(order.get("submitted_at") or filled_at)[:10],
                exit_date=filled_at,
                pnl=0.0,
                pnl_pct=0.0,
                exit_reason="manual",
                spy_regime=regime,
                execution_env="paper" if cfg.ALPACA_PAPER else "live",
            )
            trade_published += 1
        if trade_published:
            memory.mark_published("recent_trades")
        published += trade_published

    if published > 0:
        memory.mark_published("sector_findings")
    print(f"    Published {published} finding(s) / trade(s)")


if __name__ == "__main__":
    run_session()
