"""
memory.py — All local SQLite reads and writes.

This is the agent's long-term memory. Every trade, every session, every
sector snapshot lives here. Strategy logic (agent.py) never touches SQL directly.

Database: agent.db (created automatically on first run)
"""
from __future__ import annotations

import json
import sqlite3
import datetime
from pathlib import Path

DB_PATH = Path("agent.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT    NOT NULL,
                sector        TEXT,
                trade_type    TEXT    DEFAULT 'long_stock',
                entry_price   REAL,
                exit_price    REAL,
                qty           INTEGER,
                pnl           REAL    DEFAULT 0,
                pnl_pct       REAL    DEFAULT 0,
                exit_reason   TEXT,
                status        TEXT    DEFAULT 'open',
                session_date  TEXT,
                opened_at     TEXT,
                closed_at     TEXT,
                signal_data   TEXT,
                -- Trade rationale (PRIVATE to the operator — never uploaded. See journal.py).
                -- Captured at decision time and held to, so it can't be hallucinated later.
                entry_thesis    TEXT,   -- the logic for entering, grounded in the real signal + AI reason
                expected_pct    REAL,   -- target % the agent was aiming for
                stop_pct        REAL,   -- the stop it set
                variance_pct    REAL,   -- actual minus expected, computed at close
                variance_reason TEXT,   -- grounded reason for the variance (from exit_reason + numbers)
                -- Broker-side identity, needed to reconcile & close spreads correctly:
                long_symbol     TEXT,        -- tradeable symbol of the long leg / equity / option
                short_symbol    TEXT,        -- short leg of a spread (NULL otherwise)
                multiplier      INTEGER DEFAULT 1,   -- 1 for equity, 100 for options
                order_id        TEXT,        -- Alpaca entry order id
                network_trade_id TEXT        -- Agentberg network trade id (for auto-vote on close)
            );
            -- signal_data: JSON blob for any signal metadata at entry
            -- e.g. {"rsi": 44.2, "sma_20": 182.5, "day_change": 0.013}

            CREATE TABLE IF NOT EXISTS sessions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date      TEXT    NOT NULL,
                session_time      TEXT,
                portfolio_value   REAL,
                buying_power      REAL,
                blocked_sectors   TEXT,
                candidates_found  INTEGER DEFAULT 0,
                positions_opened  INTEGER DEFAULT 0,
                positions_closed  INTEGER DEFAULT 0,
                session_pnl       REAL    DEFAULT 0,
                regime            TEXT,
                notes             TEXT
            );

            CREATE TABLE IF NOT EXISTS sector_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT    NOT NULL,
                sector       TEXT    NOT NULL,
                trade_count  INTEGER DEFAULT 0,
                win_count    INTEGER DEFAULT 0,
                net_pnl      REAL    DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS publish_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                log_date     TEXT    NOT NULL,
                category     TEXT,
                sector       TEXT,
                notes        TEXT
            );

            CREATE TABLE IF NOT EXISTS persisted_findings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                finding_id   TEXT    NOT NULL UNIQUE,
                category     TEXT,
                claim        TEXT,
                weight       REAL    DEFAULT 1.0,
                confidence   REAL    NOT NULL,
                sector       TEXT,
                conditions   TEXT,
                persisted_at TEXT    NOT NULL
            );
        """)
        # Migrate older agent.db files to add the rationale columns.
        for col, typ in [("entry_thesis", "TEXT"), ("expected_pct", "REAL"),
                         ("stop_pct", "REAL"), ("variance_pct", "REAL"),
                         ("variance_reason", "TEXT"),
                         ("long_symbol", "TEXT"), ("short_symbol", "TEXT"),
                         ("multiplier", "INTEGER DEFAULT 1"), ("order_id", "TEXT"),
                         # network publish marker — set once a closed trade is sent to
                         # Agentberg, so every trade publishes exactly once (see agent.py).
                         ("published_at", "TEXT"),
                         # network trade id — stored at open, used to call close_trade() for auto-votes.
                         ("network_trade_id", "TEXT"),
                         # trailing stop — highest price seen since entry; updated in monitor
                         ("high_water_mark", "REAL"),
                         # broker's closing order id — captured whenever a close order is
                         # submitted, so eod_reconcile() can verify the exit fill later.
                         ("exit_order_id", "TEXT"),
                         # broker-reported commission, entry and exit side (0 for most
                         # equity/options accounts, but not assumed — read from the order).
                         ("entry_commission", "REAL DEFAULT 0"),
                         ("exit_commission", "REAL DEFAULT 0")]:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists


# ── Trade writes ───────────────────────────────────────────────────────────────

def record_trade_open(
    symbol: str,
    sector: str,
    entry_price: float,
    qty: int,
    trade_type: str = "long_stock",
    signal_data: dict | None = None,
    thesis: str | None = None,
    expected_pct: float | None = None,
    stop_pct: float | None = None,
    long_symbol: str | None = None,
    short_symbol: str | None = None,
    multiplier: int = 1,
    order_id: str | None = None,
    network_trade_id: str | None = None,
    entry_regime: str | None = None,
    entry_beta: float | None = None,
    entry_iv: float | None = None,
    entry_dte: int | None = None,
    network_aligned: bool = False,
    network_signal: str | None = None,
    macro_window: bool = False,
    candidates_ranked: int | None = None,
    rank_position: int | None = None,
    entry_commission: float = 0.0,
    finding_ids: list[str] | None = None,
) -> int:
    """
    Open a trade. Pass signal_data to record the entry signals, and the trade
    RATIONALE (private to the operator): `thesis` (why you entered, grounded in the
    real signal + AI reason), `expected_pct` (target), `stop_pct` (stop). These are
    recorded NOW and held to at close, so the rationale can't be hallucinated later.
    Pass network_trade_id when the trade was registered on Agentberg via open_trade()
    so close_trade() can be called on close for auto-voting on linked findings.
    Attribution context (entry_regime, entry_beta, etc.) is captured here at open —
    market context changes after close so it can't be reconstructed retroactively.

    Network registration is deferred until the entry order is confirmed filled
    (see reconcile_ledger()'s fill-confirmed path in agent.py) — pass
    network_trade_id=None here and finding_ids so reconcile_ledger() has what it
    needs to call open_trade() itself once the fill is confirmed.
    """
    if not thesis or not thesis.strip():
        print(f"[memory] WARNING: {symbol} opened with NO ENTRY THESIS — likely a backfill/recovery, not a normal decision-path entry. Flag for review.")
    elif not signal_data:
        print(f"[memory] WARNING: {symbol} opened with a thesis but NO signal_data — thesis cannot be grounded in real signals. Flag for review.")

    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (symbol, sector, trade_type, entry_price, qty, status, session_date,
                opened_at, signal_data, entry_thesis, expected_pct, stop_pct,
                long_symbol, short_symbol, multiplier, order_id, network_trade_id,
                entry_regime, entry_beta, entry_iv, entry_dte,
                network_aligned, network_signal, macro_window,
                candidates_ranked, rank_position, entry_commission, finding_ids)
               VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, sector, trade_type, entry_price, qty, today, now,
             json.dumps(signal_data) if signal_data else None,
             thesis, expected_pct, stop_pct,
             long_symbol or symbol, short_symbol, multiplier, order_id, network_trade_id,
             entry_regime, entry_beta, entry_iv, entry_dte,
             1 if network_aligned else 0, network_signal, 1 if macro_window else 0,
             candidates_ranked, rank_position, entry_commission,
             json.dumps(finding_ids) if finding_ids else None),
        )
        return cur.lastrowid


def update_network_trade_id(trade_id: int, network_trade_id: str) -> None:
    """Set network_trade_id after a deferred network registration succeeds — see
    reconcile_ledger()'s fill-confirmed path in agent.py. Registration is deferred
    until the entry order is confirmed filled so the server never learns about a
    trade that turns out to have never happened (the bug this replaces: open_trade()
    used to be called right after order submission — an order that later expired
    unfilled left a phantom 'open' registration on the server forever, since
    void_trade() is local-only by design)."""
    with _conn() as conn:
        conn.execute("UPDATE trades SET network_trade_id=? WHERE id=?", (network_trade_id, trade_id))


def _variance_reason(exit_reason: str, pnl_pct: float, expected_pct: float | None) -> str:
    """Grounded reason for the gap between expectation and outcome — from observable
    facts (exit_reason + the numbers), never free narration."""
    if exit_reason == "take_profit":
        return f"hit target as expected ({pnl_pct:+.1%})"
    if exit_reason == "stop_loss":
        return f"thesis invalidated — stopped out at {pnl_pct:+.1%}"
    tail = f" vs +{expected_pct:.0%} target" if expected_pct is not None else ""
    return f"exited via {exit_reason} at {pnl_pct:+.1%}{tail}"


def record_trade_close(trade_id: int, exit_price: float, pnl: float,
                       pnl_pct: float, exit_reason: str,
                       exit_order_id: str | None = None,
                       closed_at: str | None = None,
                       exit_commission: float = 0.0):
    """
    closed_at defaults to now (submit-time estimate) but should be overridden with
    the broker's own filled_at whenever the caller has it (reconcile_ledger,
    _record_close) — that's the real close time, not "when we happened to notice".
    exit_order_id is stored so eod_reconcile() can re-verify the fill later even
    when it wasn't confirmed synchronously at submit time.
    """
    now = closed_at or datetime.datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        row = conn.execute("SELECT expected_pct FROM trades WHERE id=?", (trade_id,)).fetchone()
        expected = row["expected_pct"] if row else None
        # Variance is COMPUTED from the recorded expectation vs the real outcome — the
        # numbers can't be confabulated, which is what keeps the journal honest.
        variance_pct = round(pnl_pct - expected, 4) if expected is not None else None
        variance_reason = _variance_reason(exit_reason, pnl_pct, expected)
        conn.execute(
            """UPDATE trades SET exit_price=?, pnl=?, pnl_pct=?, exit_reason=?,
               status='closed', closed_at=?, variance_pct=?, variance_reason=?,
               exit_order_id=?, exit_commission=? WHERE id=?""",
            (exit_price, pnl, pnl_pct, exit_reason, now, variance_pct, variance_reason,
             exit_order_id, exit_commission, trade_id),
        )


def get_journal(limit: int = 30) -> list[dict]:
    """Closed trades with their full rationale, newest first — for the human journal."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT symbol, sector, opened_at, closed_at, entry_thesis, expected_pct,
                      stop_pct, pnl, pnl_pct, exit_reason, variance_pct, variance_reason
               FROM trades WHERE status='closed' ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Session writes ─────────────────────────────────────────────────────────────

def record_session(portfolio_value: float, buying_power: float,
                   blocked_sectors: list[str], candidates_found: int,
                   positions_opened: int, positions_closed: int,
                   session_pnl: float, regime: str | None = None,
                   notes: str = ""):
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().strftime("%H:%M")
    with _conn() as conn:
        conn.execute(
            """INSERT INTO sessions
               (session_date, session_time, portfolio_value, buying_power,
                blocked_sectors, candidates_found, positions_opened,
                positions_closed, session_pnl, regime, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (today, now, portfolio_value, buying_power,
             json.dumps(blocked_sectors), candidates_found,
             positions_opened, positions_closed, session_pnl, regime, notes),
        )


def record_sector_snapshot(sector: str, trade_count: int, win_count: int, net_pnl: float):
    today = datetime.date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO sector_snapshots (session_date, sector, trade_count, win_count, net_pnl)
               VALUES (?, ?, ?, ?, ?)""",
            (today, sector, trade_count, win_count, net_pnl),
        )


# ── Publish gate ───────────────────────────────────────────────────────────────

def was_published_today(category: str, sector: str | None = None) -> bool:
    today = datetime.date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM publish_log WHERE log_date=? AND category=? AND (sector=? OR sector IS NULL)",
            (today, category, sector),
        ).fetchone()
    return row is not None


def mark_published(category: str, sector: str | None = None, notes: str = ""):
    today = datetime.date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO publish_log (log_date, category, sector, notes) VALUES (?, ?, ?, ?)",
            (today, category, sector, notes),
        )


# ── Reads / analytics ──────────────────────────────────────────────────────────

def get_summary_stats(days: int = 3650) -> dict:
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) total, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
                      SUM(pnl) net_pnl
               FROM trades WHERE status='closed' AND session_date >= ?""",
            (cutoff,),
        ).fetchone()
    total = row["total"] or 0
    wins  = row["wins"] or 0
    return {
        "total_trades": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total, 3) if total else 0,
        "net_pnl": round(row["net_pnl"] or 0, 2),
    }


def get_risk_metrics() -> dict | None:
    """
    Risk-adjusted track record for the Agentberg knowledge upload — NOT win rate.
    Expectancy (avg P&L/trade), profit factor, and max drawdown can't be reverse-
    engineered into a strategy, so they're safe to share. Returns None if there are
    no closed trades yet (nothing worth uploading).
    """
    with _conn() as conn:
        pnls = [
            r["pnl"] or 0
            for r in conn.execute("SELECT pnl FROM trades WHERE status='closed'").fetchall()
        ]
        equity = [
            r["portfolio_value"]
            for r in conn.execute(
                "SELECT portfolio_value FROM sessions "
                "WHERE portfolio_value IS NOT NULL ORDER BY id"
            ).fetchall()
        ]
    n = len(pnls)
    if n == 0:
        return None
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 3)
    else:
        profit_factor = 999.0 if gross_profit > 0 else 0.0
    # Max drawdown off the portfolio-value curve (peak-to-trough %).
    peak = None
    max_dd = 0.0
    for v in equity:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)
    return {
        "expectancy": round(sum(pnls) / n, 4),
        "profit_factor": profit_factor,
        "max_drawdown_pct": round(max_dd, 2),
        "sample_size": n,
    }


def get_sector_performance(days: int = 3650) -> list[dict]:
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT sector,
                      COUNT(*) trade_count,
                      SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
                      SUM(pnl) net_pnl
               FROM trades WHERE status='closed' AND session_date >= ?
               GROUP BY sector ORDER BY net_pnl DESC""",
            (cutoff,),
        ).fetchall()
    result = []
    for r in rows:
        total = r["trade_count"]
        result.append({
            "sector": r["sector"],
            "trade_count": total,
            "win_rate": round((r["wins"] or 0) / total, 3) if total else 0,
            "net_pnl": round(r["net_pnl"] or 0, 2),
        })
    return result


def get_recent_trades(limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_closed_today() -> int:
    """Trades closed today (by any path — monitor, reconcile, or session)."""
    today = datetime.date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n FROM trades WHERE status='closed' AND date(closed_at)=?",
            (today,),
        ).fetchone()
    return row["n"] or 0


def get_unpublished_closed_trades() -> list[dict]:
    """Every closed trade not yet sent to Agentberg, oldest first. The design is
    publish-all: each closed trade goes to the network exactly once, with its real
    P&L from the ledger (never a placeholder). Backfills anything missed while down."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, symbol, sector, trade_type, entry_price, exit_price, qty,
                      pnl, pnl_pct, exit_reason, opened_at, closed_at
               FROM trades
               WHERE status='closed' AND published_at IS NULL
               ORDER BY id ASC""",
        ).fetchall()
    return [dict(r) for r in rows]


def mark_trade_published(trade_id: int) -> None:
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute("UPDATE trades SET published_at=? WHERE id=?", (now, trade_id))


def void_trade(trade_id: int) -> None:
    """Mark a trade void — entry order never filled. Not published, not counted in stats."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            "UPDATE trades SET status='void', closed_at=?, exit_reason='entry_unfilled' WHERE id=?",
            (now, trade_id),
        )


def update_high_water_mark(trade_id: int, price: float) -> None:
    """Record the highest price seen since entry. Called by the position monitor each cycle."""
    with _conn() as conn:
        conn.execute("UPDATE trades SET high_water_mark=? WHERE id=?", (price, trade_id))


def get_trades_opened_since(days: int = 30) -> list[dict]:
    """Every trade opened in the last N days (open or already closed), for EOD
    broker reconciliation. Rolling window, not just today — so a missed day
    (agent down, network outage) still self-heals on the next run instead of
    that day's drift going uncorrected forever."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE session_date>=? AND status != 'void'", (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def correct_trade_entry(trade_id: int, entry_price: float, qty: int,
                        opened_at: str | None = None,
                        entry_commission: float = 0.0) -> None:
    """EOD correction: overwrite entry_price/qty/opened_at/commission with
    Alpaca's confirmed fill.

    record_trade_open() records these from the order-submit response — if the
    fill hadn't posted yet, that's a pre-trade snapshot, not broker truth.
    This is the only path that corrects it after the fact.
    """
    with _conn() as conn:
        if opened_at:
            conn.execute(
                "UPDATE trades SET entry_price=?, qty=?, opened_at=?, entry_commission=? WHERE id=?",
                (entry_price, qty, opened_at, entry_commission, trade_id),
            )
        else:
            conn.execute(
                "UPDATE trades SET entry_price=?, qty=?, entry_commission=? WHERE id=?",
                (entry_price, qty, entry_commission, trade_id),
            )


def correct_trade_exit(trade_id: int, exit_price: float, pnl: float, pnl_pct: float,
                       closed_at: str | None = None,
                       exit_commission: float = 0.0) -> None:
    """EOD correction: overwrite exit_price/pnl/pnl_pct/closed_at/commission with
    Alpaca's confirmed closing fill (see correct_trade_entry — same rationale,
    exit side). Recomputes variance_pct the same way record_trade_close() does,
    so a corrected exit still keeps the journal's variance honest."""
    with _conn() as conn:
        row = conn.execute("SELECT expected_pct FROM trades WHERE id=?", (trade_id,)).fetchone()
        expected = row["expected_pct"] if row else None
        variance_pct = round(pnl_pct - expected, 4) if expected is not None else None
        if closed_at:
            conn.execute(
                """UPDATE trades SET exit_price=?, pnl=?, pnl_pct=?, closed_at=?,
                   exit_commission=?, variance_pct=? WHERE id=?""",
                (exit_price, pnl, pnl_pct, closed_at, exit_commission, variance_pct, trade_id),
            )
        else:
            conn.execute(
                """UPDATE trades SET exit_price=?, pnl=?, pnl_pct=?,
                   exit_commission=?, variance_pct=? WHERE id=?""",
                (exit_price, pnl, pnl_pct, exit_commission, variance_pct, trade_id),
            )


def get_open_trades() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_history(days: int = 60) -> list[dict]:
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE session_date >= ? ORDER BY id DESC",
            (cutoff,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["blocked_sectors"] = json.loads(d.get("blocked_sectors") or "[]")
        except Exception:
            d["blocked_sectors"] = []
        result.append(d)
    return result


def get_winning_sectors(min_trades: int = 5, min_wr: float = 0.70) -> list[str]:
    rows = get_sector_performance()
    return [r["sector"] for r in rows if r["trade_count"] >= min_trades and r["win_rate"] >= min_wr]


def get_losing_sectors(min_trades: int = 5, max_wr: float = 0.30) -> list[str]:
    rows = get_sector_performance()
    return [r["sector"] for r in rows if r["trade_count"] >= min_trades and r["win_rate"] <= max_wr]


def get_win_rate(days: int = 30, sector: str | None = None) -> dict:
    """
    Rolling win rate. Optionally filter by sector.

    Usage:
        get_win_rate(days=30)                    # rolling 30-day
        get_win_rate(sector="Technology")         # sector-specific, all-time
        get_win_rate(days=14, sector="Energy")    # sector + window
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    params: list = [cutoff]
    sector_clause = ""
    if sector:
        sector_clause = "AND sector = ?"
        params.append(sector)
    with _conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
                       SUM(pnl) net_pnl
                FROM trades
               WHERE status='closed' AND session_date >= ? {sector_clause}""",
            params,
        ).fetchone()
    total = row["total"] or 0
    wins  = row["wins"] or 0
    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total, 3) if total else 0,
        "net_pnl": round(row["net_pnl"] or 0, 2),
        "days": days,
        "sector": sector,
    }


def compute_attribution(window_days: int = 30) -> dict:
    """
    Compute 30-day attribution breakdown from local SQLite for push to network.
    Groups closed trades by sector, regime, instrument type, exit reason, and
    network alignment. Used by agent Step 0d to push summary to /attribution/report.
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT sector, trade_type, exit_reason, entry_regime,
                      network_aligned, macro_window, pnl, pnl_pct
               FROM trades
               WHERE status='closed' AND closed_at >= ?""",
            (cutoff,),
        ).fetchall()

    if not rows:
        return {}

    def _agg(group):
        trades = len(group)
        wins = sum(1 for r in group if (r["pnl"] or 0) > 0)
        pnl = sum(r["pnl"] or 0 for r in group)
        return {"trades": trades, "wins": wins,
                "win_rate": round(wins / trades, 3) if trades else 0,
                "pnl": round(pnl, 2)}

    by_sector: dict = {}
    by_regime: dict = {}
    by_instrument: dict = {}
    by_exit_reason: dict = {}
    aligned: list = []
    ignored: list = []
    macro_losses = 0
    macro_total = 0

    for r in rows:
        sec = r["sector"] or "Unknown"
        reg = r["entry_regime"] or "unknown"
        instr = "equity" if r["trade_type"] in ("long_stock", "short_stock") else r["trade_type"] or "other"
        ex = r["exit_reason"] or "unknown"
        by_sector.setdefault(sec, []).append(r)
        by_regime.setdefault(reg, []).append(r)
        by_instrument.setdefault(instr, []).append(r)
        by_exit_reason.setdefault(ex, []).append(r)
        if r["network_aligned"]:
            aligned.append(r)
        else:
            ignored.append(r)
        if r["macro_window"]:
            macro_total += 1
            if (r["pnl"] or 0) < 0:
                macro_losses += 1

    total = len(rows)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    net_pnl = sum(r["pnl"] or 0 for r in rows)

    return {
        "window_days": window_days,
        "total_trades": total,
        "win_rate": round(wins / total, 3) if total else 0,
        "net_pnl": round(net_pnl, 2),
        "by_sector":      {k: _agg(v) for k, v in by_sector.items()},
        "by_regime":      {k: _agg(v) for k, v in by_regime.items()},
        "by_instrument":  {k: _agg(v) for k, v in by_instrument.items()},
        "by_exit_reason": {k: _agg(v) for k, v in by_exit_reason.items()},
        "network_aligned_pnl": round(sum(r["pnl"] or 0 for r in aligned), 2),
        "network_ignored_pnl": round(sum(r["pnl"] or 0 for r in ignored), 2),
        "macro_window_loss_rate": round(macro_losses / macro_total, 3) if macro_total else 0,
    }


def save_persisted_finding(
    finding_id: str,
    confidence: float,
    category: str = None,
    claim: str = None,
    weight: float = 1.0,
    sector: str = None,
    conditions: str = None,
) -> bool:
    """
    Persist a network finding to local SQLite at agent-chosen confidence.
    The agent decides when to call this — network never forces it.
    Upserts on finding_id: re-persisting at a new confidence replaces the old entry.
    Returns True on success.
    """
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO persisted_findings
                   (finding_id, category, claim, weight, confidence, sector, conditions, persisted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(finding_id) DO UPDATE SET
                   confidence   = excluded.confidence,
                   weight       = excluded.weight,
                   persisted_at = excluded.persisted_at""",
            (finding_id, category, claim, weight, confidence, sector, conditions, now),
        )
    return True


def get_persisted_findings(min_confidence: float = 0.0) -> list[dict]:
    """Return all locally persisted findings at or above the given confidence threshold."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT finding_id, category, claim, weight, confidence, sector, conditions, persisted_at
               FROM persisted_findings
               WHERE confidence >= ?
               ORDER BY confidence DESC, persisted_at DESC""",
            (min_confidence,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_portfolio_history(days: int = 60) -> list[dict]:
    """
    Session-by-session portfolio value — use for charting the equity curve.

    Returns one row per session, ordered oldest first.
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT session_date, session_time, portfolio_value,
                      buying_power, session_pnl, regime
               FROM sessions
               WHERE session_date >= ?
               ORDER BY id ASC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]
