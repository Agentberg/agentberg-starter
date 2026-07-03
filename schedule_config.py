"""
schedule_config.py — your session schedule. Kit upgrades never touch this file.

Everything else in scheduler.py -- the main loop, holiday/weekend detection,
heartbeat, crash recovery -- is structural mechanism with zero human-set values
and updates freely with the kit (Cat 0/A). These values are yours.

Read every line. Change to match your own strategy.
"""

import datetime

# ── Session schedule ────────────────────────────────────────────────────────
SESSION_TIMES = [
    datetime.time(9, 35),    # morning — after opening volatility
    datetime.time(12, 0),    # midday  — lunch-hour momentum
    datetime.time(15, 50),   # close   — before EOD
]

# ── Position monitoring & market hours ──────────────────────────────────────
MONITOR_INTERVAL_SECS = 300   # position check cadence (seconds)
MARKET_OPEN  = datetime.time(9, 30)
MARKET_CLOSE = datetime.time(16, 0)
