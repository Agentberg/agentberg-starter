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
import json as _json
import os
import sys
import time

import character
import config as cfg
import interconnect
import knowledge
import memory
import migrations
import risk
import structures
from agentberg import AgentbergClient
from alpaca import AlpacaClient
import llm as _llm
from llm import rank_candidates, rank_candidates_v2, session_stance, trade_decision, evaluate_guidance

_SESSION_STATE = os.path.join("logs", "session_state.json")




def _pre_allocate(primaries: list[dict], total_risk_usd: float, min_conviction: float = 0.60) -> dict:
    """Conviction-weighted capital split across primary candidates.

    Scores are squared before normalising so high-conviction candidates get
    meaningfully more capital than marginal ones (agy feedback: uniform weights
    on clustered 0.5x scores defeat the purpose of conviction scoring).
    """
    if not primaries or total_risk_usd <= 0:
        return {}
    eligible = [c for c in primaries if c.get("conviction", 0) >= min_conviction]
    if not eligible:
        eligible = primaries
    scores = {c["ticker"]: (c.get("conviction", 0.5) ** 2) for c in eligible}
    total  = sum(scores.values()) or 1.0
    return {ticker: total_risk_usd * (score / total) for ticker, score in scores.items()}


def _write_session_state(result: str) -> None:
    try:
        os.makedirs("logs", exist_ok=True)
        with open(_SESSION_STATE, "w") as _f:
            _json.dump({"result": result, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}, _f)
    except Exception:
        pass


def _compute_intraday_signals(ticker: str) -> dict | None:
    """Fetch today's 15-min bars and compute intraday RSI(14), VWAP, and distance
    to 20-day high. Returns None on insufficient data (e.g. pre-market, weekend).
    All values are informational — passed into LLM context, never hard-gated."""
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        bars_15m = _alpaca.get_bars(ticker, timeframe="15Min", limit=40)
        # Keep only today's bars
        today_bars = [b for b in bars_15m if b["t"][:10] == today]
        if len(today_bars) < 6:
            return None

        # Intraday RSI(14) — use close prices from 15-min bars
        closes = [float(b["c"]) for b in today_bars]
        if len(closes) < 2:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        avg_gain = sum(gains) / len(deltas) if deltas else 0
        avg_loss = sum(losses) / len(deltas) if deltas else 0
        intraday_rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss else 100.0

        # VWAP — (sum of typical_price × volume) / sum(volume)
        tp_vol = sum(((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3) * float(b["v"])
                     for b in today_bars)
        total_vol = sum(float(b["v"]) for b in today_bars)
        vwap = tp_vol / total_vol if total_vol else closes[-1]

        # Distance to 20-day high (using daily bars already cached at scan time)
        daily_bars = _alpaca.get_bars(ticker, timeframe="1Day", limit=22)
        if daily_bars:
            high_20d = max(float(b["h"]) for b in daily_bars[-20:])
            pct_from_high = (closes[-1] - high_20d) / high_20d  # negative = below high
        else:
            high_20d = None
            pct_from_high = None

        return {
            "intraday_rsi":    round(intraday_rsi, 1),
            "intraday_vwap":   round(vwap, 2),
            "price_vs_vwap":   round((closes[-1] - vwap) / vwap * 100, 2),  # % above/below VWAP
            "pct_from_20d_high": round(pct_from_high * 100, 2) if pct_from_high is not None else None,
            "bars_today":      len(today_bars),
        }
    except Exception as exc:
        return None


def _compute_beta(stock_bars: list, spy_bars: list) -> float:
    """Compute realized beta vs SPY from daily close bars. Returns 0.0 on insufficient data."""
    stock_closes = {b["t"][:10]: float(b["c"]) for b in stock_bars}
    spy_closes   = {b["t"][:10]: float(b["c"]) for b in spy_bars}
    dates = sorted(set(stock_closes) & set(spy_closes))
    if len(dates) < 10:
        return 0.0
    stock_rets = [(stock_closes[dates[i]] - stock_closes[dates[i-1]]) / stock_closes[dates[i-1]]
                  for i in range(1, len(dates))]
    spy_rets   = [(spy_closes[dates[i]]   - spy_closes[dates[i-1]])   / spy_closes[dates[i-1]]
                  for i in range(1, len(dates))]
    n = len(stock_rets)
    spy_mean   = sum(spy_rets) / n
    stock_mean = sum(stock_rets) / n
    cov     = sum((stock_rets[i] - stock_mean) * (spy_rets[i] - spy_mean) for i in range(n)) / (n - 1)
    var_spy = sum((spy_rets[i] - spy_mean) ** 2 for i in range(n)) / (n - 1)
    return cov / var_spy if var_spy else 0.0


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
    reconciled = registered = voided = 0
    for t in open_trades:
        legs = [s for s in (t.get("long_symbol"), t.get("short_symbol")) if s] or [t["symbol"]]
        order_id = t.get("order_id")

        if any(s in held for s in legs):
            # Still open at the broker. Network registration is deferred from
            # open-time to here (see the trade-open comment in run_session()) —
            # register now if the entry is confirmed filled and we haven't yet.
            if not t.get("network_trade_id") and _alpaca.was_entry_filled(order_id):
                finding_ids = _json.loads(t["finding_ids"]) if t.get("finding_ids") else None
                try:
                    net_open = _agentberg.open_trade(
                        ticker=t["symbol"], trade_type=t.get("trade_type", "long_stock"),
                        entry_date=(t.get("opened_at") or "")[:10],
                        finding_ids=finding_ids,
                        execution_env="paper" if cfg.ALPACA_PAPER else "live",
                        sector=t.get("sector"), entry_price=t.get("entry_price"),
                        entry_regime=t.get("entry_regime"), entry_beta=t.get("entry_beta"),
                        entry_iv=t.get("entry_iv"), entry_dte=t.get("entry_dte"),
                        network_aligned=bool(t.get("network_aligned")),
                        network_signal=t.get("network_signal"),
                        macro_window=bool(t.get("macro_window")),
                    )
                    if net_open and net_open.get("trade_id"):
                        memory.update_network_trade_id(t["id"], net_open["trade_id"])
                        registered += 1
                except Exception as e:
                    print(f"[net] open_trade {t['symbol']} failed ({e})")
            continue   # still open at the broker

        if not t.get("network_trade_id"):
            # Entry order reached a genuinely terminal non-fill state (canceled/
            # expired/rejected/suspended/done_for_day) — phantom open, void it,
            # never publish, no network call (nothing was ever registered).
            # "Not filled yet" alone is NOT the same as terminal — a still-live
            # order (new/accepted/pending/partially_filled) is left alone below
            # instead of being voided prematurely (2026-07-06 fix — voiding on
            # not-yet-filled voided orders that later filled for real, leaving
            # phantom server registrations and orphaned real positions with no
            # local record at all).
            if _alpaca.entry_order_terminal_unfilled(order_id):
                memory.void_trade(t["id"])
                voided += 1
                continue
            if not _alpaca.was_entry_filled(order_id):
                continue  # still pending, not held, not terminal — wait, don't guess

        long_sym = t.get("long_symbol") or t["symbol"]
        is_short = "short" in (t.get("trade_type") or "")
        # Closing fill is a BUY for a short position (buy to cover), a SELL for a
        # long one -- confirmed bug: this was hardcoded to "sell" regardless of
        # direction, so short-position reconciliation could never find its own
        # exit fill (it only matches the entry side instead).
        fill      = _alpaca.get_last_fill(long_sym, side="buy" if is_short else "sell")
        exit_price = float(fill.get("filled_avg_price") or 0) if fill else 0.0
        entry = t.get("entry_price") or 0
        qty   = t.get("qty") or 0
        mult  = t.get("multiplier") or 1
        sign  = -1 if is_short else 1   # qty is always stored positive; direction lives here
        if exit_price and entry:
            pnl     = (exit_price - entry) * qty * mult * sign
            pnl_pct = (exit_price - entry) / entry * sign
        else:
            pnl, pnl_pct = 0.0, 0.0
        memory.record_trade_close(
            t["id"], exit_price=exit_price, pnl=pnl, pnl_pct=pnl_pct,
            exit_reason="reconciled_broker",
            exit_order_id=fill.get("id") if fill else None,
            closed_at=fill.get("filled_at") if fill else None,
            exit_commission=float(fill.get("commission") or 0) if fill else 0.0,
        )
        if t.get("network_trade_id"):
            _agentberg.close_trade(t["network_trade_id"], pnl=pnl, pnl_pct=pnl_pct,
                                   exit_price=exit_price or None, exit_reason="manual",
                                   exit_date=datetime.date.today().isoformat())
        reconciled += 1
    if registered:
        print(f"[reconcile] Registered {registered} newly-confirmed trade(s) with the network")
    if reconciled:
        print(f"[reconcile] Closed {reconciled} trade(s) from broker truth (server-side/offline exits)")
    if voided:
        print(f"[reconcile] Voided {voided} phantom trade(s) — entry order reached a terminal non-fill state")


def eod_reconcile(days: int = 30):
    """
    End-of-day: correct every broker-verifiable field against Alpaca's confirmed
    fills — entry AND exit side, not just price/qty, across a rolling window
    (default 30 days), not just today.

    record_trade_open()/record_trade_close() record price, qty, and timestamps
    from the order-SUBMIT response — if the fill hadn't posted yet, those are
    pre-trade/pre-fill estimates, never corrected until now. reconcile_ledger()
    (every session) already fixes trades closed server-side while the app was
    down; this fixes drift on trades the app itself opened/closed, using the
    order_id / exit_order_id already stored at entry/exit:
      - entry_price, qty, opened_at (real fill time), entry_commission
      - exit_price, pnl, pnl_pct, closed_at (real fill time), exit_commission
    The window is rolling, not "today only", so a day the reconcile job never
    ran (agent down, network outage) still gets caught up and corrected on the
    next run instead of that drift going uncorrected forever. Called once per
    day, right after market close (scheduler.py), already-tallied trades are
    cheap no-ops (single get_order() lookup, no write unless drifted).
    """
    trades = memory.get_trades_opened_since(days)
    entry_fixed = exit_fixed = 0
    for t in trades:
        # ── Entry side ───────────────────────────────────────────────────────
        if t.get("order_id"):
            order = _alpaca.get_order(t["order_id"])
            if order and order.get("status") == "filled":
                broker_price = float(order.get("filled_avg_price") or 0)
                broker_qty   = int(float(order.get("filled_qty") or 0))
                local_price  = t.get("entry_price") or 0
                local_qty    = t.get("qty") or 0
                if broker_price and broker_qty and (
                    abs(broker_price - local_price) > 0.01 or broker_qty != local_qty
                ):
                    memory.correct_trade_entry(
                        t["id"], broker_price, broker_qty,
                        opened_at=order.get("filled_at") or t.get("opened_at"),
                        entry_commission=float(order.get("commission") or 0),
                    )
                    print(f"[eod] {t['symbol']}: corrected entry ${local_price:.2f}×{local_qty} "
                          f"→ ${broker_price:.2f}×{broker_qty} (broker truth)")
                    entry_fixed += 1

        # ── Exit side ────────────────────────────────────────────────────────
        if t.get("status") == "closed" and t.get("exit_order_id"):
            order = _alpaca.get_order(t["exit_order_id"])
            if order and order.get("status") == "filled":
                broker_exit = float(order.get("filled_avg_price") or 0)
                local_exit  = t.get("exit_price") or 0
                if broker_exit and abs(broker_exit - local_exit) > 0.01:
                    entry = t.get("entry_price") or 0
                    qty   = t.get("qty") or 0
                    mult  = t.get("multiplier") or 1
                    sign  = -1 if "short" in (t.get("trade_type") or "") else 1
                    pnl     = (broker_exit - entry) * qty * mult * sign
                    pnl_pct = (broker_exit - entry) / entry * sign if entry else 0.0
                    memory.correct_trade_exit(
                        t["id"], broker_exit, pnl, pnl_pct,
                        closed_at=order.get("filled_at") or t.get("closed_at"),
                        exit_commission=float(order.get("commission") or 0),
                    )
                    print(f"[eod] {t['symbol']}: corrected exit ${local_exit:.2f} "
                          f"→ ${broker_exit:.2f} (broker truth)")
                    exit_fixed += 1

    if entry_fixed or exit_fixed:
        print(f"[eod] Corrected {entry_fixed} entr{'y' if entry_fixed == 1 else 'ies'}, "
              f"{exit_fixed} exit(s) against broker")
    else:
        print("[eod] Ledger tallies with broker — no drift found")


def run_session():
    """
    Full trading cycle. Call once at market open and once at close.
    """
    _write_session_state("in_progress")
    # init_db() first: on a truly fresh install there's no agent.db yet, so
    # migrations.run() would no-op (it only ALTERs an existing table) and
    # init_db()'s own CREATE TABLE doesn't carry every column memory.py's
    # functions expect (e.g. entry_regime) — this order guarantees the table
    # exists before migrations tries to extend it.
    memory.init_db()
    migrations.run()
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

    # ── PostCar peer guidance (written by postcar/postcar_check.py sidecar) ──────
    # New kit writes .postcar_guidance (no extension) inside postcar/, not the
    # agent root -- old kit wrote .postcar_guidance.md at the agent root, so
    # this path never matched either convention until now.
    _peer_guidance = ""
    try:
        from pathlib import Path as _Path
        _pg = _Path("postcar/.postcar_guidance")
        if _pg.exists():
            _peer_guidance = _pg.read_text(encoding="utf-8").strip()
            if _peer_guidance:
                print(f"    [postcar] peer guidance available ({len(_peer_guidance)} chars)")
    except Exception:
        pass

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

    # ── Step 0b: Catalog sync — thesis-driven skill discovery ─────────────────
    # Build session thesis from live config + current regime, sync catalog from
    # server, match locally, fetch content for newly relevant thesis/commodity
    # skills. All non-binding: flows into LLM context as advisory intelligence.
    print("[0b] Syncing skill catalog...")
    session_thesis = knowledge.build_session_thesis(cfg.STRATEGY_MODE, cfg.WATCHLIST, regime)
    catalog_skills: dict = {}
    try:
        catalog_result = knowledge.sync_catalog(_agentberg, session_thesis)
        catalog_skills = catalog_result.get("fetched_skills") or {}
        matched_count  = len(catalog_result.get("matched") or [])
        new_ids        = catalog_result.get("newly_discovered") or []
        sectors_abbr   = ", ".join(session_thesis["sectors"][:3])
        suffix         = "..." if len(session_thesis["sectors"]) > 3 else ""
        print(f"    Thesis: {session_thesis['strategy']} | sectors: {sectors_abbr}{suffix}")
        print(f"    Catalog: {matched_count} relevant skill(s) matched | {len(catalog_skills)} fetched into context")
        if new_ids:
            print(f"    New to catalog since last sync: {', '.join(new_ids[:5])}")
    except Exception as e:
        print(f"    [catalog] failed ({e}) — continuing without catalog skills")

    # universe-sp500 is fetched directly by ID, not via the matched/top-N path
    # above -- sync_catalog()'s to_fetch deliberately excludes anything already
    # covered by a dedicated fetch (same reason rotation/narrative below use
    # their own get_skill() call instead of competing for the top-5 cap).
    try:
        universe_skill = _agentberg.get_catalog_skill("universe-sp500")
        if universe_skill:
            catalog_skills["universe-sp500"] = universe_skill
    except Exception as e:
        print(f"    [catalog] universe-sp500 fetch failed ({e})")

    # Open/overdue commitments made when acting on peer guidance (see
    # interconnect.process_postcar_guidance()) -- confirmed live 2026-07-07:
    # postcar's own overdue check only ever logged to its sidecar log file,
    # never reaching this decision loop, so a stated "I will trim this
    # position by <date>" never actually got seen again once the date passed.
    # Advisory only: surfaced into network_signals below so the LLM sees its
    # own overdue promises, never auto-enforced.
    open_commitments: list = []
    try:
        open_commitments = interconnect.get_open_commitments()
        if open_commitments:
            overdue = [c for c in open_commitments if c.get("status") == "overdue"]
            print(f"    Commitments: {len(open_commitments)} open ({len(overdue)} overdue)")
            for c in overdue[:5]:
                print(f"      OVERDUE (due {c.get('due_date')}): {c.get('action','')[:100]}")
    except Exception as e:
        print(f"    [commitments] failed ({e})")

    # ── Step 0c: Intelligence snapshot ────────────────────────────────────────
    # Pre-computed signal from the server (15-min cache). Four enrichments:
    # finding velocity (momentum), regime win rates, tier-2+ agent consensus,
    # and network trend (7d vs 30d). All advisory — flows into LLM context.
    print("[0c] Fetching intelligence snapshot...")
    intelligence_snapshot: dict = {}
    try:
        snap = _agentberg.get_intelligence_snapshot(regime=regime)
        if snap:
            intelligence_snapshot = snap
            trend = snap.get("network_trend", {})
            velocity = snap.get("finding_velocity", [])
            consensus = snap.get("top_agent_consensus", [])
            wr_7d  = trend.get("win_rate_7d")
            wr_30d = trend.get("win_rate_30d")
            direction = ""
            if wr_7d is not None and wr_30d is not None:
                direction = " ↑ improving" if wr_7d > wr_30d else " ↓ declining"
            wr_str = f"{wr_7d:.0%} (7d) vs {wr_30d:.0%} (30d){direction}" if wr_7d is not None else "n/a"
            rising = [v for v in velocity if v.get("momentum") == "rising"]
            print(f"    Network trend: WR {wr_str} | {len(rising)} finding(s) gaining votes | {len(consensus)} tier-2+ consensus signal(s)")
    except Exception as e:
        print(f"    [0c] intelligence snapshot failed ({e}) — continuing")

    # ── Step 0d: Attribution report — push 30-day summary to network ──────────
    # Agent computes own breakdown locally (zero server compute), pushes summary.
    # Server afternoon job cross-compares all agents → synthetic fleet findings.
    _session_macro_window = risk_level == "high"  # fallback; overridden by Step 0e
    print("[0d] Pushing attribution report...")
    try:
        _attr_report = memory.compute_attribution(window_days=30)
        if _attr_report and _attr_report.get("total_trades", 0) > 0:
            _agentberg.push_attribution_report(_attr_report)
            print(f"    Attribution: {_attr_report['total_trades']} trades | "
                  f"WR {_attr_report['win_rate']:.0%} | "
                  f"network-aligned P&L ${_attr_report['network_aligned_pnl']:+,.0f}")
        else:
            print("    No closed trades in window — skipping attribution push")
    except Exception as e:
        print(f"    [0d] attribution push failed ({e}) — continuing")

    # ── Step 0e: Macro calendar — session sizing posture from real event dates ─
    # FOMC, CPI, NFP, PCE within 7 days → macro_window=True → reduce sizing.
    # Replaces the risk_level=="high" heuristic with actual BLS/Fed calendar data.
    print("[0e] Checking macro calendar...")
    try:
        _macro = _agentberg.get_macro_calendar()
        if _macro:
            _session_macro_window = _macro.get("macro_window", False)
            _days_to_event = _macro.get("days_to_next_high_impact")
            _next_event    = _macro.get("next_high_impact_event", "")
            if _session_macro_window:
                print(f"    MACRO WINDOW ACTIVE — {_next_event} in {_days_to_event}d. Sizing reduced.")
            else:
                print("    No high-impact events in 7-day window. Normal sizing.")
        else:
            print("    [0e] macro calendar unavailable — using risk_level fallback")
    except Exception as e:
        print(f"    [0e] macro calendar failed ({e}) — using risk_level fallback")

    # ── Step 0f: L1 Session Stance ────────────────────────────────────────────
    # One LLM call: regime + risk + health + 30d track record → session_stance.
    # Sets risk_budget (fraction of equity to deploy) and max_concurrent (slots).
    # Shapes everything downstream without touching candidate-level logic.
    print("[0f] Computing session stance (L1)...")
    _l1_perf = memory.get_summary_stats(days=30)
    stance = session_stance(
        regime=regime, risk_level=risk_level, health_label=health_label,
        performance_stats=_l1_perf, character_brief=character.persona_brief(),
    )
    print(f"    Stance: {stance['stance'].upper()} | budget: {stance['risk_budget']:.0%} "
          f"| max: {stance['max_concurrent']} slot(s) | focus: {stance['focus']}")
    if stance.get("forbidden_sectors"):
        print(f"    L1 forbidden: {', '.join(stance['forbidden_sectors'])}")
    if stance.get("trusted_sectors"):
        print(f"    L1 trusted:   {', '.join(stance['trusted_sectors'])}")

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

    # G-05: Network coverage — which sectors have active agents and collective signal strength.
    # Data only: shows where network intelligence is rich vs sparse. Agent decides how to weight.
    coverage = _agentberg.get_network_coverage()
    if coverage:
        covered = [s for s in coverage.get("sectors", []) if s["coverage"] in ("high", "medium")]
        blind = [s for s in coverage.get("sectors", []) if s["coverage"] in ("low", "none")]
        if covered:
            print(f"    Network coverage: {len(covered)} sector(s) well-covered, {len(blind)} sparse/blind")

    network_signals = {
        "brief":                 brief,
        "entry_signals":         entry_signals,
        "alerts":                alerts,
        "rotation":              rotation,
        "narrative":             narrative.get("summary") if isinstance(narrative, dict) else narrative,
        "catalog_skills":        catalog_skills,
        "network_coverage":      coverage,
        "intelligence_snapshot": intelligence_snapshot,
        "open_commitments":      open_commitments,
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
    _funnel_sector = sum(len(v) for s, v in cfg.WATCHLIST.items() if s not in blocked_sectors)

    spy_bars = _alpaca.get_bars("SPY", timeframe="1Day", limit=40)

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
                "beta":       _compute_beta(bars, spy_bars),
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
            "beta":            _compute_beta(fk_bars, spy_bars),
        })
        print(f"    CANDIDATE {fk_ticker} [Network finding]: {fk_dir} {fk_chg:+.2%} @ ${fk_close:.2f}")
        candidate_tickers.add(fk_ticker)
        added_from_network += 1

    # ── Step 3 (cont): Pre-market movers + social heat injection ─────────────
    # Tickers from intelligence_snapshot (STEP 0c) that aren't already candidates.
    # Pre-market movers: significant gap before open → real signal.
    # Social heat: high StockTwits volume with bullish/bearish tilt → crowd signal.
    # Both go through STEP 3a enrichment + 3a.5 hard filter + 3b LLM ranking.
    # Sector from server response (pre-tagged) — ensures all checks apply.
    _injected_market = 0
    _MAX_PM   = 5   # cap: pre-market movers
    _MAX_SOC  = 5   # cap: social heat

    def _try_inject(ticker, sector_hint, source_tag):
        nonlocal _injected_market
        if ticker in candidate_tickers:
            return
        if sector_hint and sector_hint in blocked_sectors:
            return
        try:
            bars = _alpaca.get_bars(ticker, timeframe="1Day", limit=40)
            if len(bars) < 2:
                return
            latest_close = float(bars[-1]["c"])
            prev_close   = float(bars[-2]["c"])
            day_change   = (latest_close - prev_close) / prev_close
            direction    = "bullish" if day_change > 0.003 else ("bearish" if day_change < -0.003 else None)
            if not direction:
                return
            candidates.append({
                "ticker":      ticker,
                "sector":      sector_hint or "Unknown",
                "direction":   direction,
                "price":       latest_close,
                "day_change":  day_change,
                "beta":        _compute_beta(bars, spy_bars),
                "source":      source_tag,
            })
            candidate_tickers.add(ticker)
            print(f"    CANDIDATE {ticker} [{source_tag}]: {direction} {day_change:+.2%} @ ${latest_close:.2f}")
            _injected_market += 1
        except Exception:
            pass

    pm_added = 0
    for mover in (intelligence_snapshot.get("premarket_movers") or []):
        if pm_added >= _MAX_PM:
            break
        t = mover.get("ticker", "")
        if not t or t in candidate_tickers:
            continue
        _try_inject(t, mover.get("sector"), "premarket")
        pm_added += 1

    soc_added = 0
    for item in (intelligence_snapshot.get("social_heat") or []):
        if soc_added >= _MAX_SOC:
            break
        t = item.get("ticker", "")
        if not t or t in candidate_tickers:
            continue
        sent = item.get("stocktwits_sentiment", "")
        if sent not in ("bullish", "bearish", "leaning_bullish", "leaning_bearish"):
            continue  # skip neutral — no directional signal
        _try_inject(t, item.get("sector"), "social_heat")
        soc_added += 1

    if _injected_market:
        print(f"    Injected {_injected_market} candidate(s) from pre-market/social heat signals")

    # ── Step 3 (cont): Network universe injection (S&P 500) ──────────────────
    # Optional depth beyond your own WATCHLIST, sourced from the catalog's
    # universe-sp500 skill (see knowledge.py::extract_universe_tickers()).
    # Capped and clearly source-tagged -- this only adds scan candidates, it
    # never touches risk_params.py or your own sector/sizing rules.
    _MAX_NETWORK_UNIVERSE = 100
    network_universe = knowledge.extract_universe_tickers(catalog_skills)
    if network_universe:
        own_tickers = candidate_tickers | {t for v in cfg.WATCHLIST.values() for t in v}
        universe_added = 0
        for sector, tickers in network_universe.items():
            if sector in blocked_sectors:
                continue
            for ticker in tickers:
                if universe_added >= _MAX_NETWORK_UNIVERSE:
                    break
                if ticker in own_tickers:
                    continue
                own_tickers.add(ticker)
                _try_inject(ticker, sector, "network_universe")
                universe_added += 1
            if universe_added >= _MAX_NETWORK_UNIVERSE:
                break
        if universe_added:
            print(f"    Scanned {universe_added} additional candidate(s) from network S&P 500 universe (capped at {_MAX_NETWORK_UNIVERSE})")

    print(f"    {len(candidates)} candidate(s) before enrichment")
    _funnel_momentum = len(candidates)

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
                    "verdict":              brief["ticker_stats"]["verdict"],
                    "network_wr":           brief["ticker_stats"]["win_rate"],
                    "network_pnl":          brief["ticker_stats"]["net_pnl"],
                    "trade_count":          brief["ticker_stats"]["trade_count"],
                    "findings_count":       len(brief.get("findings", [])),
                    # Pre-market data (yfinance, pre-computed server-side)
                    "premarket_chg_pct":    brief.get("premarket_chg_pct"),
                    "premarket_direction":  brief.get("premarket_direction"),
                    # StockTwits community sentiment (pre-computed server-side)
                    "stocktwits_sentiment": brief.get("stocktwits_sentiment"),
                    "stocktwits_bull_pct":  brief.get("stocktwits_bull_pct"),
                }
                enriched += 1
        print(f"    Enriched {enriched}/{len(candidates)} candidates with network ticker intel")

    # ── Step 3a.1: Intraday signal enrichment (15-min bars) ───────────────────────
    # Fetch today's 15-min bars for each candidate and attach intraday RSI, VWAP,
    # and distance-to-20d-high. Informational only — flows into LLM ranking context.
    # Silent on failure (pre-market, weekend, API error) — candidate is not dropped.
    if candidates:
        intraday_enriched = 0
        for c in candidates:
            sig = _compute_intraday_signals(c["ticker"])
            if sig:
                c["intraday"] = sig
                intraday_enriched += 1
        if intraday_enriched:
            print(f"    Intraday signals (15-min RSI/VWAP/high) added for {intraday_enriched}/{len(candidates)} candidates")

    # ── Step 3a.5: Pre-LLM hard filter — high-beta bullish in range_bound ────────
    # Drop candidates whose realized beta vs SPY exceeds the threshold before the LLM
    # sees them. Beta is computed from the same 40-day bars already fetched — no extra
    # API calls. Threshold lives in config.py so operators can tune it.
    if regime == "range_bound":
        _before = len(candidates)
        candidates = [
            c for c in candidates
            if not (c.get("direction") == "bullish"
                    and c.get("beta", 0) > cfg.HIGH_BETA_THRESHOLD)
        ]
        _dropped = _before - len(candidates)
        if _dropped:
            print(f"    [hard-filter] dropped {_dropped} high-beta bullish candidate(s) "
                  f"(beta > {cfg.HIGH_BETA_THRESHOLD}) in range_bound regime")

    print(f"    {len(candidates)} candidate(s) before LLM filter")
    _funnel_beta = len(candidates)

    # ── Step 3b: L2 Ranking — conviction scores + primary/buffer split ────────
    # Candidates are ranked into two lists:
    #   primaries: top max_concurrent slots — capital will be pre-allocated to these
    #   buffer:    50% excess candidates — inherit a dropped primary's slot at execution
    # Each candidate gets a conviction score (0.0-1.0) used for proportional allocation.
    performance_context = {
        "stats":   memory.get_summary_stats(days=90),
        "sectors": memory.get_sector_performance(days=90),
        "recent":  memory.get_recent_trades(limit=10),
    }
    l2_result = rank_candidates_v2(
        candidates, stance["max_concurrent"], regime, risk_level, health_label,
        network_blocked, network_signals, performance_context,
        forbidden_sectors=stance.get("forbidden_sectors", []),
        trusted_sectors=stance.get("trusted_sectors", []),
        focus=stance.get("focus"),
        l1_stance=stance.get("stance"),
    )
    primaries = l2_result["primaries"]
    buffer    = l2_result["buffer"]
    _funnel_llm = len(primaries)

    # ── Step 3b.5: Pre-allocation — conviction-weighted capital split ──────────
    # Allocate BEFORE any L3 call so queue position never biases capital access.
    # Buffer candidates do NOT receive pre-allocation — they inherit a dropped
    # primary's slot and its fixed dollar amount at execution time.
    total_risk_usd = equity * stance["risk_budget"]
    alloc_map = _pre_allocate(primaries, total_risk_usd)
    print(f"    Pre-alloc: ${total_risk_usd:,.0f} risk budget | "
          f"{len(primaries)} primary slot(s) + {len(buffer)} buffer")
    for ticker_a, amt_a in alloc_map.items():
        print(f"    Alloc {ticker_a}: ${amt_a:,.0f}")

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
    hb = None
    try:
        hb = _agentberg.send_heartbeat(
            kit_version=kit_version,
            universe_size=universe_size,
            candidates_count_after_filters=_funnel_llm,
            filter_funnel={
                "after_sector":   _funnel_sector,
                "after_momentum": _funnel_momentum,
                "after_beta":     _funnel_beta,
                "after_llm":      _funnel_llm,
            },
            llm_provider=_llm.active_provider_name(),
        )
        if hb and hb.get("anomaly"):
            label = hb["anomaly_label"]
            detail = hb["anomaly_detail"]
            print(f"    [heartbeat] ⚠ {label}: {detail}")
            _agentberg.report_issue(
                trap_name="FILTER_ANOMALY",
                concern=f"{label}: {detail}",
                severity="high",
                diagnostics={
                    "anomaly_label": label,
                    "anomaly_detail": detail,
                    "after_sector": _funnel_sector,
                    "after_momentum": _funnel_momentum,
                    "after_beta": _funnel_beta,
                    "after_llm": _funnel_llm,
                    "universe_size": universe_size,
                },
                kit_version=kit_version,
            )
    except Exception as e:
        print(f"    [heartbeat] failed ({e})")

    # ── Guidance cycle: triggered when inbox has unread messages ──────────────
    if hb and hb.get("inbox_pending"):
        try:
            inbox_messages = _agentberg.get_inbox()
            if inbox_messages:
                run_guidance_cycle(inbox_messages)
        except Exception as e:
            print(f"    [guidance] failed ({e})")

    # ── Trap: zero candidates after all filters ────────────────────────────────
    if _funnel_llm == 0:
        recent = memory.get_session_history(days=7)
        consecutive = 0
        for s in recent:
            if s.get("candidates_found", 1) == 0:
                consecutive += 1
            else:
                break
        if consecutive >= 2:
            print(f"    [trap] ZERO_CANDIDATES — {consecutive} consecutive sessions with 0 candidates. Filing support case.")
            _agentberg.report_issue(
                trap_name="SCANNER_ZERO_CANDIDATES_CONSECUTIVE",
                concern=f"0 candidates returned for {consecutive} consecutive sessions",
                severity="high",
                diagnostics={
                    "regime": regime,
                    "universe_size": universe_size,
                    "after_sector": _funnel_sector,
                    "after_momentum": _funnel_momentum,
                    "after_beta": _funnel_beta,
                    "after_llm": _funnel_llm,
                    "consecutive_sessions": consecutive,
                },
                kit_version=kit_version,
            )

    # ── Step 4: Execute — work queue with L3 per-candidate decisions ─────────
    # Work queue:  primaries carry conviction-weighted pre-allocated amounts.
    # Buffer queue: 50% excess candidates — fill dropped primary slots once
    #               (no cascading fills: _is_buffer_fill guards infinite loops).
    _candidates_total = len(primaries) + len(buffer)
    print(f"[4] Executing — {len(primaries)} primary slot(s) + {len(buffer)} buffer ({mode})...")
    executed: list = []

    held_tickers: set[str] = {p["symbol"] for p in positions}
    traded_this_session: set[str] = set()

    _work_queue = [
        dict(c, _alloc_usd=alloc_map.get(c["ticker"], 0), _is_buffer_fill=False)
        for c in primaries
    ]
    _buf_queue   = list(buffer)
    _slots_opened = 0

    while _work_queue:
        c         = _work_queue.pop(0)
        ticker    = c["ticker"]
        sector    = c["sector"]
        direction = c["direction"]
        alloc_usd = c.get("_alloc_usd", 0)
        is_buffer = c.get("_is_buffer_fill", False)
        _rank_pos = _slots_opened + 1

        def _pull_buffer():
            if _buf_queue and not is_buffer:
                _b = dict(_buf_queue.pop(0))
                # Buffer candidate gets its own C²-proportional share — not the primary's budget.
                # Inheriting the primary's larger allocation over-funds a low-conviction trade.
                primary_sq = max(c.get("conviction", 0.75) ** 2, 0.01)
                buffer_sq  = _b.get("conviction", 0.58) ** 2
                _b["_alloc_usd"]      = alloc_usd * (buffer_sq / primary_sq)
                _b["_is_buffer_fill"] = True
                _work_queue.insert(0, _b)

        if ticker in held_tickers or ticker in traded_this_session:
            print(f"    SKIP {ticker}: already held / ordered this session")
            _pull_buffer()
            continue

        _trade_fids: list[str] = []
        if c.get("from_finding_id"):
            _trade_fids.append(str(c["from_finding_id"]))
        if sector != "Network":
            _sector_fid = network_blocked_map.get(sector)
            if _sector_fid and _sector_fid not in _trade_fids:
                _trade_fids.append(str(_sector_fid))
        trade_finding_ids = _trade_fids or None

        thesis = f"{direction} {ticker} [{sector}] — {c.get('day_change', 0):+.1%} momentum"
        if c.get("reason"):
            thesis += f"; AI: {c['reason']}"
        expected_pct = cfg.EQUITY_TAKE_PROFIT_PCT if mode == "equity" else cfg.TAKE_PROFIT_PCT
        stop_pct     = cfg.EQUITY_STOP_LOSS_PCT   if mode == "equity" else cfg.OPTION_STOP_LOSS_PCT
        signal       = {"day_change": c.get("day_change"), "direction": direction}

        if mode == "equity":
            # ── L3: per-candidate trade decision (fixed pre-allocated budget) ──
            l3 = trade_decision(
                c, alloc_usd, regime, character.persona_brief(),
                focus=stance.get("focus"), l1_stance=stance.get("stance"),
            )
            if l3.get("_l3_failed"):
                # LLM failure (not a deliberate skip) — halt execution and alert
                print(f"    [trap] L3_EXECUTION_FAILURE on {ticker} — halting session, alerting operator")
                _agentberg.report_issue(
                    trap_name="L3_EXECUTION_FAILURE",
                    concern=f"L3 LLM failure on {ticker}: {l3.get('reason', 'unknown error')}",
                    severity="critical",
                    diagnostics={
                        "ticker":  ticker,
                        "sector":  sector,
                        "alloc_usd": alloc_usd,
                        "regime":  regime,
                        "reason":  l3.get("reason", ""),
                    },
                    kit_version=kit_version,
                )
                break  # halt — do not attempt remaining candidates
            if not l3.get("execute", False):
                print(f"    L3 SKIP {ticker}: {l3.get('reason', '')} — pulling buffer")
                _pull_buffer()
                continue

            size_usd   = min(l3.get("size_usd") or alloc_usd, alloc_usd)
            stop_pct   = l3.get("stop_pct",   cfg.EQUITY_STOP_LOSS_PCT)
            target_pct = l3.get("target_pct", cfg.EQUITY_TAKE_PROFIT_PCT)

            allowed, reason = risk.check_equity(
                ticker, sector, regime, blocked_sectors, size_usd, equity, open_count
            )
            if not allowed:
                print(f"    SKIP {ticker}: {reason}")
                continue
            try:
                live_price = _alpaca.get_live_price(ticker)
                if live_price is None:
                    print(f"    [warn] live price fetch failed for {ticker} — using bar close ${c['price']:.2f}")
                    live_price = c["price"]
                qty               = max(1, int(size_usd / live_price))
                side              = "buy" if direction == "bullish" else "sell"
                stop_price        = round(live_price * (1 - stop_pct),   2) if side == "buy" else round(live_price * (1 + stop_pct),   2)
                take_profit_price = round(live_price * (1 + target_pct), 2) if side == "buy" else round(live_price * (1 - target_pct), 2)
                order    = _alpaca.submit_order(ticker, qty, side,
                               stop_loss_price=stop_price, take_profit_price=take_profit_price,
                               base_price=live_price)
                # Use Alpaca's actual fill price; fall back to pre-order snapshot only if not yet filled
                entry_price = float(order.get("filled_avg_price") or 0) or live_price
                # Network registration is deferred to reconcile_ledger()'s fill-
                # confirmed path (next session), not done here — registering
                # before the order is confirmed filled left phantom "open" trades
                # on the server forever whenever an order later expired unfilled
                # (void_trade() never un-registers, local-only by design). See
                # memory.update_network_trade_id()'s docstring.
                trade_id = memory.record_trade_open(
                    ticker, sector, entry_price, qty,
                    trade_type="long_stock" if direction == "bullish" else "short_stock",
                    signal_data=signal, thesis=thesis,
                    expected_pct=expected_pct, stop_pct=stop_pct,
                    order_id=order.get("id"), network_trade_id=None,
                    entry_regime=regime, entry_beta=c.get("beta"),
                    network_aligned=bool(trade_finding_ids),
                    network_signal=direction, macro_window=_session_macro_window,
                    candidates_ranked=_candidates_total, rank_position=_rank_pos,
                    entry_commission=float(order.get("commission") or 0),
                    finding_ids=trade_finding_ids,
                )
                print(f"    ORDER {ticker}: {side} ×{qty} @ ~${live_price:.2f}  "
                      f"stop=${stop_price or 'none'}  tp=${take_profit_price or 'none'}  "
                      f"alloc=${size_usd:,.0f}  conviction={c.get('conviction', 0):.0%}")
                executed.append({**c, "qty": qty, "order_id": order["id"], "memory_id": trade_id})
                traded_this_session.add(ticker)
                held_tickers.add(ticker)
                open_count   += 1
                _slots_opened += 1
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
                # Use Alpaca's actual fill price; fall back to the limit price only if not yet filled
                entry_price = float(order.get("filled_avg_price") or 0) or limit_price
                # Network registration deferred to reconcile_ledger() — see the
                # long/short stock branch above for why.
                trade_id = memory.record_trade_open(
                    ticker, sector, entry_price, 1, trade_type=f"long_{option_type}",
                    signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct,
                    long_symbol=contract["symbol"], order_id=order.get("id"),
                    network_trade_id=None,
                    entry_regime=regime, entry_beta=c.get("beta"),
                    entry_iv=iv_rank, entry_dte=dte,
                    network_aligned=bool(trade_finding_ids),
                    network_signal=direction, macro_window=_session_macro_window,
                    candidates_ranked=_candidates_total, rank_position=_rank_pos,
                    entry_commission=float(order.get("commission") or 0),
                    finding_ids=trade_finding_ids,
                )
                print(f"    ORDER {ticker} {option_type.upper()} {contract['expiration_date']} "
                      f"${contract['strike_price']} δ={delta:.2f} @ ${entry_price:.2f}")
                executed.append({**c, "symbol": contract["symbol"], "premium": entry_price, "memory_id": trade_id})
                traded_this_session.add(ticker)
                open_count   += 1
                _slots_opened += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker}: {e}")

        elif mode == "spreads":
            option_type    = "call" if direction == "bullish" else "put"
            buy_contracts  = _alpaca.find_option_contracts(ticker, option_type, min_dte=cfg.MIN_DTE, max_dte=cfg.MAX_DTE, min_delta=0.35, max_delta=0.50)
            sell_contracts = _alpaca.find_option_contracts(ticker, option_type, min_dte=cfg.MIN_DTE, max_dte=cfg.MAX_DTE, min_delta=0.15, max_delta=0.30)
            if not buy_contracts or not sell_contracts:
                print(f"    SKIP {ticker}: couldn't build spread")
                continue

            buy_leg      = buy_contracts[0]
            sell_leg     = next((s for s in sell_contracts if s["expiration_date"] == buy_leg["expiration_date"]), sell_contracts[0])
            buy_ask      = float(buy_leg.get("ask_price") or 0)
            sell_bid     = float(sell_leg.get("bid_price") or 0)
            net_debit    = round(buy_ask - sell_bid, 2)
            spread_width = abs(float(buy_leg["strike_price"]) - float(sell_leg["strike_price"]))
            dte          = (datetime.date.fromisoformat(buy_leg["expiration_date"]) - datetime.date.today()).days

            allowed, reason = risk.check_spread(
                ticker, sector, regime, blocked_sectors, equity, open_count,
                net_debit=net_debit, spread_width=spread_width, dte=dte,
            )
            if not allowed:
                print(f"    SKIP {ticker} spread: {reason}")
                continue

            ok, why = structures.validate_structure(
                "debit_vertical", max_loss=net_debit * 100,
                legs=[{"role": "long",  "symbol": buy_leg["symbol"]},
                      {"role": "short", "symbol": sell_leg["symbol"]}],
            )
            if not ok:
                print(f"    SKIP {ticker} spread: structure gate — {why}")
                continue
            try:
                order    = _alpaca.submit_option_spread(buy_leg["symbol"], sell_leg["symbol"], qty=1, net_debit=net_debit)
                # Use Alpaca's actual net fill price; fall back to the target debit only if not yet filled
                entry_price = float(order.get("filled_avg_price") or 0) or net_debit
                # Network registration deferred to reconcile_ledger() — see the
                # long/short stock branch above for why. trade_type stores as
                # "{option_type}_spread"; open_trade()'s own _TYPE_MAP normalizes
                # call_spread/put_spread -> "spread" when reconcile registers it.
                trade_id = memory.record_trade_open(
                    ticker, sector, entry_price, 1, trade_type=f"{option_type}_spread",
                    signal_data=signal, thesis=thesis, expected_pct=expected_pct, stop_pct=stop_pct,
                    long_symbol=buy_leg["symbol"], short_symbol=sell_leg["symbol"],
                    multiplier=100, order_id=order.get("id"),
                    network_trade_id=None,
                    entry_regime=regime, entry_beta=c.get("beta"),
                    entry_dte=dte, network_aligned=bool(trade_finding_ids),
                    network_signal=direction, macro_window=_session_macro_window,
                    candidates_ranked=_candidates_total, rank_position=_rank_pos,
                    entry_commission=float(order.get("commission") or 0),
                    finding_ids=trade_finding_ids,
                )
                print(f"    SPREAD {ticker} {option_type.upper()} "
                      f"${float(buy_leg['strike_price']):.0f}/${float(sell_leg['strike_price']):.0f} debit=${net_debit:.2f}")
                executed.append({**c, "memory_id": trade_id, "net_debit": net_debit})
                traded_this_session.add(ticker)
                open_count   += 1
                _slots_opened += 1
            except Exception as e:
                print(f"    ORDER FAILED {ticker} spread: {e}")

    # ── Step 5: Publish findings (once per day) ────────────────────────────────
    _maybe_publish(blocked_sectors, regime)

    # ── Step 6: Write session to memory ───────────────────────────────────────
    memory.record_session(
        portfolio_value=equity,
        buying_power=buying_power,
        blocked_sectors=blocked_sectors,
        candidates_found=_candidates_total,
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

    # ── Step 9: Kit version check — Cat 0/A auto-applied; Cat B/C flagged for review ──
    _kit_upgraded = False
    _KIT_AUTOUPGRADE_SENTINEL = os.path.join(os.path.dirname(__file__), ".kit_autoupgrade_check")
    def _autoupgrade_due() -> bool:
        try:
            return (time.time() - float(open(_KIT_AUTOUPGRADE_SENTINEL).read().strip())) >= 86400
        except Exception:
            return True
    try:
        upd = knowledge.check_kit_update(_agentberg)
        if upd.get("status") == "update_available":
            mandatory = upd.get("mandatory_changes", [])
            optional = upd.get("optional_changes", [])
            print(f"[9] Kit update: v{upd['current']} → v{upd['latest']}")
            if mandatory and _autoupgrade_due():
                print(f"    [auto-upgrade] {len(mandatory)} Cat 0/A change(s) — applying now…")
                try:
                    import subprocess as _sub
                    _r = _sub.run(
                        [sys.executable, os.path.join(os.path.dirname(__file__), "upgrade.py"), "--no-restart"],
                        capture_output=True, text=True, timeout=180,
                        cwd=os.path.dirname(__file__) or ".",
                    )
                    if _r.returncode == 0:
                        print(f"    [auto-upgrade] Done → v{upd['latest']}. Restarting after session.")
                        _kit_upgraded = True
                        open(_KIT_AUTOUPGRADE_SENTINEL, "w").write(str(time.time()))
                    else:
                        print(f"    [auto-upgrade] FAILED — {(_r.stderr or _r.stdout)[:200]}")
                        print("       Manual fallback: python3 upgrade.py")
                except Exception as ue:
                    print(f"    [auto-upgrade] error ({ue}) — run python3 upgrade.py manually")
            elif mandatory:
                print(f"    !! MANDATORY ({len(mandatory)} change(s) — Cat 0/A):")
                for entry in mandatory:
                    for item in entry.get("added", [])[:2]:
                        print(f"       + v{entry.get('version','?')}: {item}")
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

        # G-05: Push voluntary sector reflection to network — sector names only, no alpha.
        # Feeds the network coverage map so agents know where collective data is strong or sparse.
        if losing_sectors or winning_sectors:
            today = datetime.date.today().isoformat()
            result = _agentberg.push_reflection(
                session_date=today,
                weak_sectors=losing_sectors,
                strong_sectors=winning_sectors,
            )
            if result:
                print(f"[reflect] Sector signal pushed to network (weak: {len(losing_sectors)}, strong: {len(winning_sectors)})")

    _write_session_state("ok")

    # If Cat 0/A upgrade was applied this session, send a heartbeat to signal
    # the new version is live, then explicitly restart the scheduler so the
    # new code is loaded. Works with or without the run.sh watchdog.
    if _kit_upgraded:
        try:
            _new_ver = None
            with open(os.path.join(os.path.dirname(__file__), "kit_manifest.json")) as _mf:
                _new_ver = json.load(_mf).get("version")
            _agentberg.send_heartbeat(
                kit_version=_new_ver,
                universe_size=universe_size,
                candidates_count_after_filters=0,
                filter_funnel={},
            )
            print(f"[restart] Heartbeat sent (v{_new_ver}) — restarting scheduler.")
        except Exception as _hbe:
            print(f"[restart] Heartbeat failed ({_hbe}) — restarting anyway.")
        try:
            from pathlib import Path as _Path
            from upgrade import _restart_scheduler as _do_restart
            _do_restart(_Path(os.path.dirname(os.path.abspath(__file__))))
        except Exception as _re:
            print(f"[restart] Explicit restart failed ({_re}) — watchdog will recover.")
        sys.exit(0)


_CLOSE_FAIL_COUNTS: dict[str, int] = {}
_CLOSE_FAIL_ALERTED: set[str] = set()


def _close_with_retry(key: str, close_fn, retries: int = 2, delay: float = 2.0):
    """Retry a broker close call; escalate to the operator on repeated cross-cycle failure.

    A bare try/except-and-continue around a close order leaves a stopped-out position
    open indefinitely if the order keeps getting rejected (PDT rule, no buying power,
    extended hours) — the failure sits in scheduler.log and nobody sees it. Retry
    absorbs transient rejects; the cross-cycle counter catches the case where the
    position simply won't close, and fires a critical support trap (visible in Slack
    and the admin dashboard) instead of failing silently forever.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            order = close_fn()
            _CLOSE_FAIL_COUNTS.pop(key, None)
            _CLOSE_FAIL_ALERTED.discard(key)
            return order
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(delay)
    _CLOSE_FAIL_COUNTS[key] = _CLOSE_FAIL_COUNTS.get(key, 0) + 1
    fail_count = _CLOSE_FAIL_COUNTS[key]
    if fail_count >= 3 and key not in _CLOSE_FAIL_ALERTED:
        _CLOSE_FAIL_ALERTED.add(key)
        try:
            _agentberg.report_issue(
                trap_name="STOP_LOSS_CLOSE_FAILURE",
                concern=f"Position {key} has failed to close for {fail_count} consecutive "
                        f"monitor cycles: {last_exc}",
                severity="critical",
                diagnostics={
                    "symbol": key,
                    "consecutive_failures": fail_count,
                    "last_error": str(last_exc),
                },
            )
        except Exception as report_exc:
            print(f"[monitor] Failed to report close-failure issue for {key}: {report_exc}")
    raise last_exc


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
            close_order = _close_with_retry(
                trade["symbol"],
                lambda: _alpaca.submit_option_spread_close(long_sym, short_sym, qty=qty, net_credit=net_credit),
            )
            exit_price = round((trade.get("entry_price") or 0) + net_pl_dollars / (mult * qty), 2)
            memory.record_trade_close(trade["id"], exit_price=exit_price, pnl=net_pl_dollars,
                                      pnl_pct=net_pct, exit_reason=reason,
                                      exit_order_id=close_order.get("id"),
                                      exit_commission=float(close_order.get("commission") or 0))
            print(f"    [journal] {trade['symbol']} closed {net_pct:+.1%} ({reason}) — review with `python journal.py`")
            if trade.get("network_trade_id"):
                _agentberg.close_trade(trade["network_trade_id"], pnl=net_pl_dollars, pnl_pct=net_pct,
                                       exit_price=exit_price or None, exit_reason=reason,
                                       exit_date=datetime.date.today().isoformat())
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

        # ── Trailing stop (all instruments) ────────────────────────────────────
        # Tracks the highest price seen since entry. Once the position is up
        # TRIGGER_PCT, the stop trails DISTANCE_PCT below that high — locking in
        # gains on reversals without capping upside. Equities use tight distances
        # (1%); options use wider ones (20%) to survive normal premium volatility.
        if cfg.TRAILING_STOP_ENABLED:
            is_equity = asset_class == "us_equity"
            trigger_pct  = cfg.TRAILING_STOP_TRIGGER_PCT if is_equity else cfg.OPTION_TRAILING_STOP_TRIGGER_PCT
            distance_pct = cfg.TRAILING_STOP_DISTANCE_PCT if is_equity else cfg.OPTION_TRAILING_STOP_DISTANCE_PCT
            current_price = float(pos.get("current_price", 0))
            trade = next((t for t in open_trades
                          if t.get("long_symbol") == symbol or t["symbol"] == symbol), None)
            if trade and current_price > 0:
                hwm = float(trade.get("high_water_mark") or trade.get("entry_price") or current_price)
                if current_price > hwm:
                    hwm = current_price
                    memory.update_high_water_mark(trade["id"], hwm)
                if unrealised_pnl_pct >= trigger_pct:
                    trail_stop = hwm * (1 - distance_pct)
                    if current_price <= trail_stop:
                        print(f"[monitor] TRAILING STOP {symbol}: "
                              f"${current_price:.2f} below trail ${trail_stop:.2f} "
                              f"(HWM ${hwm:.2f}, up {unrealised_pnl_pct:.1%})")
                        try:
                            close_order = _close_with_retry(symbol, lambda: _alpaca.close_position(symbol))
                            _record_close(symbol, "trailing_stop", unrealised_pnl_pct, close_order=close_order)
                        except Exception as e:
                            print(f"[monitor] Trailing stop close failed {symbol}: {e}")
                        continue

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
            close_order = _close_with_retry(symbol, lambda: _alpaca.close_position(symbol))
            _record_close(symbol, reason, unrealised_pnl_pct, close_order=close_order)
        except Exception as e:
            print(f"[monitor] Close failed {symbol}: {e}")


def run_guidance_cycle(inbox_messages: list[dict]) -> None:
    """
    GUIDANCE CYCLE (CYCLE 3): Evaluate Agentberg guidance messages and apply warranted changes.

    Triggered automatically by run_session() when heartbeat returns inbox_pending=True.
    Each message is evaluated against 4 parameters before any change is applied:
      1. Validity   — is the thesis logically coherent and evidence-backed?
      2. Credibility — sender type × evidence tier × reputation score
      3. Alignment  — does it fit this agent's goals, risk tolerance, and character?
      4. Risk       — scope of change, reversibility, paper vs live
    """
    if not inbox_messages:
        return

    print(f"\n[guidance] {len(inbox_messages)} message(s) from Agentberg:")
    for msg in inbox_messages:
        subject = (msg.get("subject") or "(no subject)")[:60]
        sender_type = msg.get("sender_type", "platform")
        print(f"    • [{sender_type}] from {msg.get('sender_id', '?')}: {subject}")

    char_brief = character.brief() if character.is_set() else ""
    try:
        track_record = {"stats": memory.get_summary_stats(days=90)}
    except Exception:
        track_record = {}

    verdicts = evaluate_guidance(inbox_messages, char_brief, track_record)

    message_ids_to_ack: list[str] = []
    for msg, verdict in zip(inbox_messages, verdicts or []):
        msg_id = msg.get("message_id", "?")
        decision = verdict.get("decision", "DEFER")
        reasoning = verdict.get("reasoning", "")
        changes = verdict.get("suggested_changes") or []

        v = verdict.get("validity_score", "?")
        c = verdict.get("credibility_score", "?")
        a = verdict.get("alignment_score", "?")
        r = verdict.get("risk_score", "?")
        subject = (msg.get("subject") or "message")[:50]

        print(f"\n    [{decision}] {subject}")
        print(f"    Validity:{v} Credibility:{c} Alignment:{a} Risk:{r}")
        print(f"    Reasoning: {reasoning}")

        if decision == "APPLY" and changes:
            print(f"    Applying {len(changes)} change(s):")
            _apply_guidance_changes(changes)
            message_ids_to_ack.append(msg_id)

        elif decision == "ASK":
            question = verdict.get("follow_up_question", "")
            if question:
                sender_id = msg.get("sender_id", "platform")
                try:
                    _agentberg.send_inbox_reply(
                        recipient_id=sender_id,
                        subject=f"Re: {subject}",
                        body=question,
                        in_reply_to=msg_id,
                    )
                    print(f"    Follow-up sent to {sender_id}: {question[:100]}")
                except Exception as e:
                    print(f"    ASK reply failed ({e})")
            # Do NOT ACK — message stays pending until answer arrives

        else:
            message_ids_to_ack.append(msg_id)

    if message_ids_to_ack:
        try:
            _agentberg.ack_inbox(message_ids_to_ack)
            print(f"\n[guidance] ACKed {len(message_ids_to_ack)} message(s)")
        except Exception as e:
            print(f"[guidance] ACK failed ({e})")


def _apply_guidance_changes(changes: list[dict]) -> None:
    """Write APPLY decisions to guidance_overrides.json for audit and next-session awareness."""
    import json as _json_inner
    overrides_path = os.path.join(os.path.dirname(__file__), "guidance_overrides.json")
    try:
        with open(overrides_path) as f:
            existing = _json_inner.load(f)
    except Exception:
        existing = {"applied": []}

    for change in changes:
        param = change.get("param", "")
        suggested = change.get("suggested", "")
        rationale = change.get("rationale", "")
        if not param or suggested == "":
            continue
        print(f"        {param}: {change.get('current', '?')} → {suggested} ({rationale[:60]})")
        existing["applied"].append({
            "param": param,
            "current": change.get("current"),
            "value": suggested,
            "rationale": rationale,
            "applied_at": datetime.datetime.utcnow().isoformat(),
        })

    try:
        with open(overrides_path, "w") as f:
            _json_inner.dump(existing, f, indent=2)
        print(f"        → saved to guidance_overrides.json ({len(changes)} change(s))")
    except Exception as e:
        print(f"        → save failed ({e})")


def _record_close(symbol: str, reason: str, pnl_pct: float, close_order: dict | None = None):
    open_trades = memory.get_open_trades()
    trade = next((t for t in open_trades if t["symbol"] == symbol or t.get("long_symbol") == symbol), None)
    if not trade:
        return
    entry_price = trade.get("entry_price") or 0
    qty = trade.get("qty") or 0
    mult = trade.get("multiplier") or 1
    # qty is always stored positive; direction lives in trade_type. pnl_pct (the
    # incoming param) is already direction-correct -- it comes from Alpaca's own
    # unrealized_plpc -- but a dollar pnl computed directly from (exit-entry) needs
    # the same sign flip applied, or a short's dollar pnl comes out backwards.
    sign = -1 if "short" in (trade.get("trade_type") or "") else 1
    # Use Alpaca's actual fill price from the close order; fall back to computing from entry + pnl_pct
    exit_price = float(close_order.get("filled_avg_price") or 0) if close_order else 0.0
    if not exit_price and entry_price:
        exit_price = round(entry_price * (1 + pnl_pct * sign), 4)
    pnl_dollars = (exit_price - entry_price) * qty * mult * sign if (exit_price and entry_price) else entry_price * qty * mult * pnl_pct
    memory.record_trade_close(
        trade["id"], exit_price=exit_price, pnl=pnl_dollars, pnl_pct=pnl_pct, exit_reason=reason,
        exit_order_id=close_order.get("id") if close_order else None,
        closed_at=close_order.get("filled_at") if close_order else None,
        exit_commission=float(close_order.get("commission") or 0) if close_order else 0.0,
    )
    print(f"    [journal] {symbol} closed {pnl_pct:+.1%} @ ${exit_price:.2f} ({reason})")
    if trade.get("network_trade_id"):
        _agentberg.close_trade(trade["network_trade_id"], pnl=pnl_dollars, pnl_pct=pnl_pct,
                               exit_price=exit_price or None, exit_reason=reason,
                               exit_date=datetime.date.today().isoformat())
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
