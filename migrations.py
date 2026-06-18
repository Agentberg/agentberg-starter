"""
migrations.py — Schema migrations for agent.db.

Called from agent.py before memory.init_db(), so the schema is always current
even if memory.py was skipped during a kit upgrade (Category C file). This file
has no kit imports — raw sqlite3 only, so it runs regardless of what else was
or wasn't upgraded.
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
]


def run() -> None:
    """Apply all pending column migrations. Safe to call every startup."""
    if not DB_PATH.exists():
        return  # no db yet — memory.init_db() will create the full schema
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
