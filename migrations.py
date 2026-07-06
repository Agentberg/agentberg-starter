"""
migrations.py — Schema migrations for agent.db.

Called from agent.py AFTER memory.init_db() (init_db creates the table if
it doesn't exist yet; this only ALTERs an existing one — it can't create
one). This keeps the schema current even if memory.py's own base schema was
skipped during a kit upgrade (Category C file). This file has no kit imports
— raw sqlite3 only, so it runs regardless of what else was or wasn't upgraded.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("agent.db")

# (column, type) — append new migrations here, oldest first. Never remove entries.
_MIGRATIONS = [
    # v2.1.0 — trade rationale + identity
    ("entry_thesis",    "TEXT"),
    ("expected_pct",    "REAL"),
    ("stop_pct",        "REAL"),
    ("variance_pct",    "REAL"),
    ("variance_reason", "TEXT"),
    ("long_symbol",     "TEXT"),
    ("short_symbol",    "TEXT"),
    ("multiplier",      "INTEGER DEFAULT 1"),
    ("order_id",        "TEXT"),
    # v2.1.0 — network publish marker
    ("published_at",    "TEXT"),
    # v2.5.1 — network trade id for auto-vote on close
    ("network_trade_id", "TEXT"),
    # v2.8.18 — attribution context captured at trade open
    ("entry_regime",      "TEXT"),
    ("entry_beta",        "REAL"),
    ("entry_iv",          "REAL"),
    ("entry_dte",         "INTEGER"),
    ("network_aligned",   "INTEGER DEFAULT 0"),
    ("network_signal",    "TEXT"),
    ("macro_window",      "INTEGER DEFAULT 0"),
    ("candidates_ranked", "INTEGER"),
    ("rank_position",     "INTEGER"),
    # v2.10.17 — EOD broker reconciliation (entry+exit price/qty/timestamp/commission)
    ("exit_order_id",    "TEXT"),
    ("entry_commission", "REAL DEFAULT 0"),
    ("exit_commission",  "REAL DEFAULT 0"),
    # v2.10.38 — deferred network registration (finding_ids must survive from
    # open-time to the later reconcile_ledger() call that actually registers)
    ("finding_ids",      "TEXT"),
]


def run() -> None:
    """Apply all pending column migrations. Safe to call every startup.

    Call memory.init_db() first — this only ALTERs an existing table, it
    can't create one. init_db()'s own schema doesn't cover every column in
    _MIGRATIONS (e.g. entry_regime), so calling this before init_db() on a
    fresh install leaves those columns missing and the first trade write
    crashes with "no column named ...".
    """
    if not DB_PATH.exists():
        return  # defensive guard — should be unreachable if init_db() ran first
    conn = sqlite3.connect(DB_PATH)
    try:
        for col, typ in _MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists, or trades table not yet created
    finally:
        conn.close()
