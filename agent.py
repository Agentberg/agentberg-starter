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

from __future__ import annotations

import datetime
import os
import sys
import time

import character
import config as cfg
import knowledge
import memory
import migrations
import risk
import structures
from agentberg import AgentbergClient
from alpaca import AlpacaClient
from llm import rank_candidates

# Clients — constructed once at import time, reused across calls
_alpaca    = AlpacaClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, cfg.ALPACA_BASE_URL)
_agentberg = AgentbergClient(cfg.AGENTBERG_URL, cfg.AGENT_ID)

# Populated in run_daily(); read by _vote_outcome() at trade close
_finding_ticker_map: dict[str, str] = {}


def _phone_home() -> None:
    """Fire-once anonymous activation ping. Identifies that this kit was run without
    any PII — just a random UUID tied to this install, timestamp, and platform.
    Writes .kit_phonehome after success so it never fires again."""
    sentinel = os.path.join(os.path.dirname(__file__), ".kit_phonehome")
    if os.path.exists(sentinel):
        return
    id_file = os.path.join(os.path.dirname(__file__), ".kit_id")
    if os.path.exists(id_file):
        with open(id_file) as f:
            kit_id = f.read().strip()
    else:
        import uuid
        kit_id = str(uuid.uuid4())
        with open(id_file, "w") as f:
            f.write(kit_id)
    try:
        _agentberg.phone_home(kit_id=kit_id, source="agent_first_run", platform=sys.platform)
        open(sentinel, "w").close()
    except Exception:
        pass


def _ensure_registered():
    """One-time: claim our id on the network. Retries once on network error so a brief
    outage at startup doesn't leave the agent permanently unregistered.
    If it was already taken, the network assigns a UNIQUE variant — we persist it to
    .agent_id and adopt it. After that, config.py loads the confirmed id automatically."""
    idfile = os.path.join(os.path.dirname(__file__), ".agent_id")
    if os.path.exists(idfile):
        return  # already registered
    for attempt in (1, 2):
        try:
            res = _agentberg.register(cfg.AGENT_ID)
            confirmed = res.get("agent_id", cfg.AGENT_ID)
            with open(idfile, "w") as f:
                f.write(confirmed)
            if res.get("reassigned"):
                print(f"    [register] ⚠  {res.get('message', '')}")
                cfg.AGENT_ID = confirmed
                _agentberg.agent_id = confirmed
            else:
                print(f"    [register] id '{confirmed}' is yours on Agentberg")
            return
        except Exception as e:
            if attempt == 1:
                print(f"    [register] network error ({type(e).__name__}) — retrying in 3s…")
                time.sleep(3)
            else:
                print(f"    [register] ⚠  could not reach Agentberg — running unregistered")
                print(f"    [register]    findings won't appear on the network until this resolves")
                print(f"    [register]    restart the agent to retry registration")


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
    voided = 0
    for t in open_trades:
        legs = [s for s in (t.get("long_symbol"), t.get("short_symbol")) if s] or [t["symbol"]]
        if any(s in held for s in legs):
            continue   # still open at the broker

        # Entry order never filled — phantom open. Void it, never publish.
        if not _alpaca.was_entry_filled(t.get("order_id")):
            memory.void_trade(t["id"])
            voided += 1
            continue

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
        if t.get("network_trade_id"):
            _agentberg.close_trade(t["network_trade_id"], pnl=pnl, pnl_pct=pnl_pct, exit_reason="manual")
        reconciled += 1
    if reconciled:
        print(f"[reconcile] Closed {reconciled} trade(s) from broker truth (server-side/offline exits)")
    if voided:
        print(f"[reconcile] Voided {voided} phantom trade(s) — entry order never filled")


def run_session():
    """
    Full trading cycle. Call once at market open and once at close.
    """
    migrations.run()
    memory.init_db()
    _phone_home()
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
        # if health_label == "stressed":
        #     position_size_override = cfg.MAX_POSITION_PCT * 0.5
        #     print(f"    [RISK OVERRIDE] Health STRESSED — position size halved to {position_size_override:.1%}")
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

    # Build finding ticker map: {ticker: finding_id} (highest-weight finding per ticker)
    global _finding_ticker_map
    finding_ticker_map: dict[str, str] = {}
    finding_tickers_data = _agentberg.get_finding_tickers()
    for item in finding_tickers_data:
        for t in item.get("tickers", []):
            if t not in finding_ticker_map:  # already sorted weight DESC
                finding_ticker_map[t] = str(item["finding_id"])
    _finding_ticker_map = finding_ticker_map
    if finding_ticker_map:
        print(f"    Finding ticker queue: {len(finding_ticker_map)} ticker(s) from {len(finding_tickers_data)} network finding(s)")

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

    # Pull the rest of the network's intelligence to leverage in ranking — rotation and
    # narrative skill packs beyond /skills/core. All advisory; the agent weighs, never obeys.
    rotation  = _agentberg.get_skill("rotation") or {}
    narrative = _agentberg.get_skill("narrative") or {}
    network_signals = {
        "brief": brief,
        "entry_signals": entry_signals,
        "alerts": alerts,
        "rotation": rotation,
        "narrative": narrative.get("summary") if isinstance(narrative, dict) else narrative,
    }

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

    # Mark watchlist candidates that appear in the finding ticker queue
    for c in candidates:
        if c["ticker"] in finding_ticker_map:
            c["from_finding_id"] = finding_ticker_map[c["ticker"]]

    # Add network-sourced tickers not already in candidates (cap: top 20 from queue, up to 10 new)
    candidate_tickers = {c["ticker"] for c in candidates}
    added_from_network = 0
    for fk_ticker, fk_finding_id in list(finding_ticker_map.items())[:20]:
        if fk_ticker in candidate_tickers or added_from_network >= 10:
            continue
        fk_bars = _alpaca.get_bars(fk_ticker, timeframe="1Day", limit=40)
        if len(fk_bars) < 2:
            continue
        fk_close = float(fk_bars[-1]["c"])
        fk_prev  = float(fk_bars[-2]["c"])
        fk_chg   = (fk_close - fk_prev) / fk_prev
        fk_dir   = "bullish" if fk_chg > 0.003 else ("bearish" if fk_chg < -0.003 else None)
        if not fk_dir:
            continue
        candidates.append({
            "ticker":          fk_ticker,
            "sector":          "Network",
            "direction":       fk_dir,
            "price":           fk_close,
            "day_change":      fk_chg,
            "from_finding_id": fk_finding_id,
        })
        print(f"    CANDIDATE {fk_ticker} [Network finding]: {fk_dir} {fk_chg:+.2%} @ ${fk_close:.2f}")
        candidate_tickers.add(fk_ticker)
        added_from_network += 1

    print(f"    {len(candidates)} candidate(s) before enrichment")

    # ── Step 3a: Enrich candidates with agentberg ticker intelligence ──────────
    # Each candidate gets the network's collective verdict: trade stats from all
    # agents (WR, net P&L, count) + related findings count. Attached as
    # network_intel dict — flows directly into the ranking prompt so the LLM sees
    # what the whole network has experienced with this ticker before it decides.
    if candidates:
        enriched = 0
        for c in candidates:
            brief = _agentberg.get_ticker_brief(c["ticker"])
            if brief:
                c["network_intel"] = {
                    "verdict":       brief["ticker_stats"]["verdict"],
                    "network_wr":    brief["ticker_stats"]["win_rate"],
                    "network_pnl":   brief["ticker_stats"]["net_pnl"],
                    "trade_count":   brief["ticker_stats"]["trade_count"],
                    "findings_count": len(brief.get("findings", [])),
                }
                enriched += 1
        print(f"    Enriched {enriched}/{len(candidates)} candidates with network ticker intel")

    print(f"    {len(candidates)} candidate(s) before LLM filter")

    # ── Step 3b: LLM ranking with self-reflection ─────────────────────────────
    # The agent reviews its own track record before ranking — this is the reflection
    # loop: win rates, sector P&L, last 5 closed trades vs their thesis. Without this,
    # every ranking call starts blind, unable to improve toward the operator's goals.
    performance_context = {
        "stats":   memory.get_summary_stats(days=90),
        "sectors": memory.get_sector_performance(days=90),
        "recent":  memory.get_recent_trades(limit=10),
    }
    candidates = rank_candidates(candidates, regime, risk_level, health_label,
                                 network_blocked, network_signals, performance_context)
    candidates = candidates[:cfg.MAX_NEW_PER_CYCLE]

    # ── Step 3c: Send heartbeat (telemetry) ────────────────────────────────────
    # Report kit version, universe size, and available candidates for diagnostics
    try:
        import json
        with open(os.path.join(os.path.dirname(__file__), "kit_manifest.json")) as f:
            manifest = json.load(f)
            kit_version = manifest.get("version")
    except Exception:
        kit_version = None

    universe_size = sum(len(v) for v in cfg.WATCHLIST.values())
    try:
        _agentberg.send_heartbeat(
            kit_version=kit_version,
            universe_size=universe_size,
            candidates_count_after_filters=len(candidates),
        )
    except Exception as e:
        print(f"    [heartbeat] failed ({e})")

    # ── Step 4: Execute ────────────────────────────────────────────────────────
    print(f"[4] Executing {len(candidates)} trade(s) ({mode})...")
    executed = []

    # Guard: tickers already held at the broker (catches restart-within-window re-entry)
    held_tickers: set[str] = {p["symbol"] for p in positions}
    # Guard: tickers ordered this session (catches duplicate candidates across sectors)
    traded_this_session: set[str] = set()

    for c in candidates:
        ticker    = c["ticker"]
        sector    = c["sector"]
        direction = c["direction"]

        if ticker in held_tickers or ticker in traded_this_session:
            print(f"    SKIP {ticker}: already have open position or already ordered this session")
            continue

        # Trade rationale (PRIVATE to the operator) — assembled from the REAL signal +
        # the AI's recorded reason, captured NOW so it can't be hallucinated after the
        # outcome is known. Recorded with the trade; reviewed via `python journal.py`.
        thesis = f"{direction} {ticker} [{sector}] — {c.get('day_change', 0):+.1%} momentum"
        if c.get("reason"):
            thesis += f"; AI: {c['reason']}"
        expected_pct = cfg.EQUITY_TAKE_PROFIT_PCT if mode == "equity" else cfg.TAKE_PROFIT_PCT
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
                # Use live snapshot price for sizing and bracket levels —
                # bar close is yesterday's price; stop off a stale price
                # is wrong and can misplace the bracket by several percent.
                live_price = _alpaca.get_live_price(ticker)
                if live_price is None:
                    print(f"    [warn] live price fetch failed for {ticker} — using bar close ${c['price']:.2f}")
                    live_price = c["price"]
                qty          = max(1, int(pos_value / live_price))
                side         = "buy" if direction == "bullish" else "sell"
                stop_price        = round(live_price * (1 - cfg.EQUITY_STOP_LOSS_PCT), 2) if side == "buy" else None
                take_profit_price = round(live_price * (1 + cfg.EQUITY_TAKE_PROFIT_PCT), 2) if side == "buy" else None
                order      = _alpaca.submit_order(ticker, qty, side,
                                stop_loss_price=stop_price, take_profit_price=take_profit_price)
                net_open   = _agentberg.open_trade(
                    ticker=ticker, trade_type="long_stock" if direction == "bullish" else "short_stock",
                    entry_date=datetime.date.today().isoformat(),
                    finding_ids=[c["from_finding_id"]] if c.get("from_finding_id") else None,
                    sector=sector, entry_price=live_price,
                    execution_env="paper" if cfg.ALPACA_PAPER else "live",
                )
                trade_id   = memory.record_trade_open(ticker, sector, live_price, qty,
                                signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct,
                                network_trade_id=net_open.get("trade_id") if net_open else None)
                print(f"    ORDER {ticker}: {side} ×{qty} @ ~${live_price:.2f}  stop=${stop_price or 'none'}  tp=${take_profit_price or 'none'}")
                executed.append({**c, "qty": qty, "order_id": order["id"], "memory_id": trade_id})
                traded_this_session.add(ticker)
                held_tickers.add(ticker)
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
                net_open = _agentberg.open_trade(
                    ticker=ticker, trade_type=f"long_{option_type}",
                    entry_date=datetime.date.today().isoformat(),
                    finding_ids=[c["from_finding_id"]] if c.get("from_finding_id") else None,
                    sector=sector, entry_price=limit_price,
                    execution_env="paper" if cfg.ALPACA_PAPER else "live",
                )
                trade_id = memory.record_trade_open(ticker, sector, limit_price, 1, trade_type=f"long_{option_type}",
                                signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct,
                                long_symbol=contract["symbol"],
                                network_trade_id=net_open.get("trade_id") if net_open else None)
                print(f"    ORDER {ticker} {option_type.upper()} {contract['expiration_date']} ${contract['strike_price']} δ={delta:.2f} @ ${limit_price:.2f}")
                executed.append({**c, "symbol": contract["symbol"], "premium": limit_price, "memory_id": trade_id})
                traded_this_session.add(ticker)
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
                net_open = _agentberg.open_trade(
                    ticker=ticker, trade_type="spread",
                    entry_date=datetime.date.today().isoformat(),
                    finding_ids=[c["from_finding_id"]] if c.get("from_finding_id") else None,
                    sector=sector, entry_price=net_debit,
                    execution_env="paper" if cfg.ALPACA_PAPER else "live",
                )
                trade_id = memory.record_trade_open(ticker, sector, net_debit, 1, trade_type=f"{option_type}_spread",
                                signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct,
                                long_symbol=buy_leg["symbol"], short_symbol=sell_leg["symbol"],
                                multiplier=100, order_id=order.get("id"),
                                network_trade_id=net_open.get("trade_id") if net_open else None)
                print(f"    SPREAD {ticker} {option_type.upper()} ${float(buy_leg['strike_price']):.0f}/${float(sell_leg['strike_price']):.0f} debit=${net_debit:.2f}")
                executed.append({**c, "memory_id": trade_id, "net_debit": net_debit})
                traded_this_session.add(ticker)
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
        positions_closed=memory.count_closed_today(),
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

    # ── Step 9: Kit version check — mandatory Cat 0/A must be adopted for fleet consistency ──
    try:
        upd = knowledge.check_kit_update(_agentberg)
        if upd.get("status") == "update_available":
            mandatory = upd.get("mandatory_changes", [])
            optional = upd.get("optional_changes", [])
            print(f"[9] Kit update: v{upd['current']} → v{upd['latest']}")
            if mandatory:
                print(f"    !! MANDATORY ({len(mandatory)} change(s) — Cat 0/A — network participation + safe plumbing):")
                for entry in mandatory:
                    for item in entry.get("added", [])[:2]:
                        print(f"       + v{entry.get('version','?')}: {item}")
                print("       Adopt these now — UPGRADING.md fast path (agentberg upgrade --auto for Cat 0).")
            if optional:
                print(f"    -- Optional ({len(optional)} change(s) — Cat B/C — review before adopting):")
                for entry in optional[:3]:
                    for item in entry.get("added", [])[:1]:
                        print(f"       + v{entry.get('version','?')}: {item}")
    except Exception as e:
        print(f"[9] Update check skipped ({e})")

    stats = memory.get_summary_stats()
    print(f"[done] {len(executed)} orders placed | All-time: {stats['total_trades']} trades, "
          f"{stats['win_rate']:.0%} WR, ${stats['net_pnl']:+,.2f} P&L")

    # ── Reflection — am I moving toward the operator's goal? ──────────────────
    recent_stats = memory.get_summary_stats(days=14)
    if recent_stats["total_trades"] >= 3:
        losing_sectors = memory.get_losing_sectors(min_trades=3, max_wr=0.40)
        winning_sectors = memory.get_winning_sectors(min_trades=3, min_wr=0.60)
        print(f"[reflect] Last 14 days: {recent_stats['win_rate']:.0%} WR, ${recent_stats['net_pnl']:+,.0f} P&L")
        if winning_sectors:
            print(f"[reflect] Edge confirmed: {', '.join(winning_sectors)}")
        if losing_sectors:
            print(f"[reflect] Consistent losers (consider excluding): {', '.join(losing_sectors)}")


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
            if trade.get("network_trade_id"):
                _agentberg.close_trade(trade["network_trade_id"], pnl=net_pl_dollars, pnl_pct=net_pct, exit_reason=reason)
            _vote_outcome(trade, net_pl_dollars)
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
        profit_threshold = cfg.EQUITY_TAKE_PROFIT_PCT if asset_class == "us_equity" else cfg.TAKE_PROFIT_PCT

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
    trade = next((t for t in open_trades if t["symbol"] == symbol or t.get("long_symbol") == symbol), None)
    if not trade:
        return
    pnl_dollars = (trade.get("entry_price") or 0) * (trade.get("qty") or 0) * pnl_pct
    memory.record_trade_close(trade["id"], exit_price=0, pnl=pnl_dollars, pnl_pct=pnl_pct, exit_reason=reason)
    print(f"    [journal] {symbol} closed {pnl_pct:+.1%} ({reason}) — review with `python journal.py`")
    if trade.get("network_trade_id"):
        _agentberg.close_trade(trade["network_trade_id"], pnl=pnl_dollars, pnl_pct=pnl_pct, exit_reason=reason)
    _vote_outcome(trade, pnl_dollars)


def _vote_outcome(trade: dict, pnl_dollars: float):
    """Vote on any active network findings that apply to this closed trade.

    Sector: if the trade's sector had an active block finding, vote on it.
    Ticker: if the trade's symbol has a network finding, vote on that too.
    Loss → upvote (finding was right); win → downvote (finding may be wrong).
    """
    vote = "up" if pnl_dollars < 0 else "down"

    # 1. Sector finding
    sector = trade.get("sector")
    if sector:
        blocked_map = _agentberg.get_blocked_sectors()
        sector_finding_id = blocked_map.get(sector)
        if sector_finding_id:
            _agentberg.cast_vote(sector_finding_id, vote)
            print(f"    [vote] {vote}voted {sector} sector_failure (finding {sector_finding_id})")

    # 2. Ticker finding
    ticker = trade.get("symbol")
    if ticker and ticker in _finding_ticker_map:
        ticker_finding_id = _finding_ticker_map[ticker]
        _agentberg.cast_vote(ticker_finding_id, vote)
        print(f"    [vote] {vote}voted {ticker} ticker finding (finding {ticker_finding_id})")


def _maybe_publish(blocked_sectors: list[str], regime: str | None):
    """Contribute to the network. Two independent paths:

      1. TRADES — publish-all. Every closed trade goes up exactly once, with its real
         P&L from the ledger. No threshold, no daily gate: max collaboration is the
         design, and publishing is what unlocks higher network tiers (a non-publisher
         stays Tier 0 and sees only weak CLAIMED findings).
      2. FINDINGS — interpretive sector claims, quality-gated (≥5 trades, decisive WR)
         and published at most once per day. Thresholds belong to findings, not trades.
    """
    print("[5] Contributing to Agentberg...")
    published = 0

    # ── 1. Trades — publish ALL closed trades exactly once, with real P&L ──────────
    unpublished = memory.get_unpublished_closed_trades()
    for t in unpublished:
        result = _agentberg.add_trade(
            finding_id=None,
            ticker=t["symbol"],
            trade_type=t.get("trade_type") or "long_stock",
            entry_date=(t.get("opened_at") or "")[:10],
            exit_date=(t.get("closed_at") or "")[:10],
            pnl=t.get("pnl") or 0.0,
            pnl_pct=t.get("pnl_pct") or 0.0,
            exit_reason=t.get("exit_reason") or "closed",
            spy_regime=regime,
            execution_env="paper" if cfg.ALPACA_PAPER else "live",
        )
        if result:
            memory.mark_trade_published(t["id"])
            published += 1
    if unpublished:
        print(f"    Trades published: {published}/{len(unpublished)}")

    # ── 2. Findings — interpretive, quality-gated, once per day ────────────────────
    if not memory.was_published_today("sector_findings"):
        sector_perf = memory.get_sector_performance()
        findings = 0
        for s in sector_perf:
            sector = s["sector"]
            if not sector or s["trade_count"] < 5:
                continue
            if s["win_rate"] >= 0.70:
                category, verb = "trade_result", "performing well"
            elif s["win_rate"] <= 0.30:
                category, verb = "sector_failure", "failing"
            else:
                continue
            result = _agentberg.publish_finding(
                category=category,
                claim=f"{sector} sector {verb} — {s['win_rate']:.0%} WR over {s['trade_count']} trades, net P&L ${s['net_pnl']:+,.2f}",
                trade_count=s["trade_count"],
                win_rate=s["win_rate"],
                conditions={"spy_regime": regime, "sector": sector},
            )
            if result:
                findings += 1
        memory.mark_published("sector_findings")
        published += findings
        print(f"    Findings published: {findings}")

    print(f"    Total contributed this session: {published}")


if __name__ == "__main__":
    run_session()
