"""
Risk Constitution
-----------------
The rules your agent follows. Read every line.
Change the values to match your own risk tolerance.
This is not financial advice — these are mechanical limits you define and own.
"""


class RiskConstitution:

    # Maximum share of portfolio in any single position (0.05 = 5%)
    MAX_POSITION_PCT: float = 0.05

    # Stop loss threshold — exit if position drops this much (0.02 = 2%)
    STOP_LOSS_PCT: float = 0.02

    # Maximum simultaneous open positions
    MAX_OPEN_POSITIONS: int = 10

    # Execution environment — "paper" until you are ready and have tested
    ALLOWED_EXEC_ENV: str = "paper"

    # Sectors this agent will never trade regardless of signal
    # Agentberg populates this from the network on startup — you can add your own
    BLOCKED_SECTORS: list[str] = []

    # Regimes this agent will not trade in
    # "bear" means no new long positions when network consensus is bear market
    BLOCKED_REGIMES: list[str] = ["bear"]

    def check(
        self,
        ticker: str,
        sector: str,
        regime: str | None,
        position_value: float,
        portfolio_equity: float,
        open_positions: int,
    ) -> tuple[bool, str]:
        """Returns (allowed, reason). Always call this before submitting an order."""

        if sector in self.BLOCKED_SECTORS:
            return False, f"{sector} blocked by network consensus"

        if regime and regime in self.BLOCKED_REGIMES:
            return False, f"Regime '{regime}' is blocked — no new longs"

        if open_positions >= self.MAX_OPEN_POSITIONS:
            return False, f"At max positions ({self.MAX_OPEN_POSITIONS})"

        if portfolio_equity > 0:
            pct = position_value / portfolio_equity
            if pct > self.MAX_POSITION_PCT:
                return False, f"Position {pct:.1%} exceeds {self.MAX_POSITION_PCT:.1%} limit"

        return True, "ok"
