"""
alpaca.py — Pure Alpaca broker wrapper. No strategy logic here.

Covers equities and options on the same account.
Paper trading by default — switch ALPACA_BASE_URL to live when ready.

Options require Level 2 approval on Alpaca:
  alpaca.markets → Account → Options Trading → Enable
  Paper account approval is instant.
"""

from __future__ import annotations

import datetime
import httpx


class AlpacaClient:

    def __init__(self, api_key: str, secret_key: str, base_url: str):
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._base = base_url.rstrip("/")
        self._data_base = "https://data.alpaca.markets"

    def _get(self, path: str, params: dict = None) -> dict | list:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{self._base}{path}", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()

    def _data_get(self, path: str, params: dict = None) -> dict | list:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{self._data_base}{path}", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        with httpx.Client(timeout=10) as c:
            r = c.post(f"{self._base}{path}", headers=self._headers, json=payload)
            r.raise_for_status()
            return r.json()

    def _delete(self, path: str) -> dict:
        with httpx.Client(timeout=10) as c:
            r = c.delete(f"{self._base}{path}", headers=self._headers)
            r.raise_for_status()
            return r.json()

    # ── Account ────────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        return self._get("/v2/account")

    # ── Positions ──────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        return self._get("/v2/positions")

    def get_equity_positions(self) -> list:
        return [p for p in self.get_positions() if p.get("asset_class") == "us_equity"]

    def get_option_positions(self) -> list:
        return [p for p in self.get_positions() if p.get("asset_class") == "us_option"]

    def close_position(self, symbol: str) -> dict:
        return self._delete(f"/v2/positions/{symbol}")

    # ── Market data ────────────────────────────────────────────────────────────

    def get_bars(self, ticker: str, timeframe: str = "1Day", limit: int = 40) -> list:
        # start is required — without it Alpaca may return only the most recent bar.
        # 2× buffer accounts for weekends and holidays in the lookback window.
        start = (datetime.date.today() - datetime.timedelta(days=limit * 2)).isoformat()
        data = self._data_get("/v2/stocks/bars", params={
            "symbols": ticker,
            "timeframe": timeframe,
            "limit": limit,
            "start": start,
        })
        return data.get("bars", {}).get(ticker, [])

    def get_snapshot(self, ticker: str) -> dict:
        return self._data_get(f"/v2/stocks/{ticker}/snapshot")

    # ── Equity orders ──────────────────────────────────────────────────────────

    def get_live_price(self, ticker: str) -> float | None:
        """Latest trade price from snapshot — use for order sizing and stop calc."""
        try:
            snap = self._data_get(f"/v2/stocks/{ticker}/snapshot")
            return float(
                snap.get("latestTrade", {}).get("p")
                or snap.get("latestQuote", {}).get("ap")
                or 0
            ) or None
        except Exception:
            return None

    def submit_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: float = None,
        stop_loss_price: float = None,
        take_profit_price: float = None,
    ) -> dict:
        payload = {
            "symbol": ticker,
            "qty": qty,
            "side": side,
            "type": order_type,
            "time_in_force": "day",
        }
        if limit_price is not None:
            payload["limit_price"] = str(limit_price)
        if stop_loss_price:
            # Bracket order — Alpaca requires BOTH stop_loss and take_profit.
            if take_profit_price is None:
                raise ValueError(f"Alpaca bracket orders require take_profit_price alongside stop_loss_price for {ticker}")
            payload["order_class"] = "bracket"
            payload["stop_loss"]   = {"stop_price": str(round(stop_loss_price, 2))}
            payload["take_profit"] = {"limit_price": str(round(take_profit_price, 2))}
        return self._post("/v2/orders", payload)

    def get_recent_closed_orders(self, limit: int = 50, days: int = 7) -> list:
        after = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        try:
            orders = self._get("/v2/orders", params={
                "status": "closed", "limit": limit,
                "after": after, "direction": "desc",
            })
            return [o for o in orders if o.get("filled_at")]
        except Exception:
            return []

    # ── Options ────────────────────────────────────────────────────────────────

    def find_option_contracts(
        self,
        ticker: str,
        option_type: str,
        min_dte: int = 21,
        max_dte: int = 45,
        min_delta: float = 0.30,
        max_delta: float = 0.50,
    ) -> list[dict]:
        """Find contracts matching DTE and delta targets, sorted by delta closest to range midpoint."""
        today = datetime.date.today()
        data = self._get("/v2/options/contracts", params={
            "underlying_symbols": ticker,
            "type": option_type,
            "expiration_date_gte": (today + datetime.timedelta(days=min_dte)).isoformat(),
            "expiration_date_lte": (today + datetime.timedelta(days=max_dte)).isoformat(),
            "limit": 100,
        })
        contracts = data if isinstance(data, list) else data.get("option_contracts", [])
        filtered = [
            c for c in contracts
            if min_delta <= abs(float((c.get("greeks") or {}).get("delta", 0))) <= max_delta
        ]
        mid = (min_delta + max_delta) / 2
        filtered.sort(key=lambda c: abs(abs(float((c.get("greeks") or {}).get("delta", 0))) - mid))
        return filtered

    def get_iv_rank(self, ticker: str) -> float | None:
        """IV rank 0-100. Buy premium when < 30. Returns None if unavailable."""
        try:
            snap = self._data_get(f"/v2/stocks/{ticker}/snapshot")
            iv = snap.get("impliedVolatility")
            hi = snap.get("impliedVolatilityHigh52Week")
            lo = snap.get("impliedVolatilityLow52Week")
            if iv and hi and lo and (hi - lo) > 0:
                return round(((iv - lo) / (hi - lo)) * 100, 1)
        except Exception:
            pass
        return None

    def submit_option_single(
        self, symbol: str, qty: int, side: str, limit_price: float,
        time_in_force: str = "day",
    ) -> dict:
        """Single-leg options order. Always limit — market orders get wide fills."""
        return self._post("/v2/orders", {
            "symbol": symbol, "qty": str(qty), "side": side,
            "type": "limit", "time_in_force": time_in_force,
            "limit_price": str(round(limit_price, 2)),
        })

    def submit_option_spread(
        self, buy_symbol: str, sell_symbol: str, qty: int, net_debit: float,
    ) -> dict:
        """Two-leg debit spread. net_debit = max you'll pay (buy leg - sell leg premium)."""
        return self._post("/v2/orders", {
            "type": "limit", "order_class": "mleg",
            "time_in_force": "day", "limit_price": str(round(net_debit, 2)),
            "legs": [
                {"symbol": buy_symbol,  "side": "buy",  "qty": str(qty), "position_intent": "bto"},
                {"symbol": sell_symbol, "side": "sell", "qty": str(qty), "position_intent": "sto"},
            ],
        })

    def submit_option_spread_close(
        self, long_symbol: str, short_symbol: str, qty: int, net_credit: float,
    ) -> dict:
        """
        Close a debit spread atomically as a single multi-leg order.

        Closing legs one at a time is the bug that breaks spreads: selling the
        long leg while the short is open trips Alpaca's "uncovered contract"
        reject, and evaluating the short leg standalone stops out a healthy
        spread. One mleg order closes both legs together — no naked exposure.

        net_credit = the minimum you'll accept to close (sell long, buy back short).
        """
        return self._post("/v2/orders", {
            "type": "limit", "order_class": "mleg",
            "time_in_force": "day", "limit_price": str(round(max(net_credit, 0.01), 2)),
            "legs": [
                {"symbol": long_symbol,  "side": "sell", "qty": str(qty), "position_intent": "stc"},
                {"symbol": short_symbol, "side": "buy",  "qty": str(qty), "position_intent": "btc"},
            ],
        })

    def get_position_symbols(self) -> set:
        """Set of symbols currently held — the broker's source of truth for what's open."""
        try:
            return {p["symbol"] for p in self.get_positions()}
        except Exception:
            return set()

    def get_order(self, order_id: str) -> dict | None:
        """Look up a single order by id. Returns None if not found or on error."""
        try:
            return self._get(f"/v2/orders/{order_id}")
        except Exception:
            return None

    def was_entry_filled(self, order_id: str | None) -> bool:
        """True if the entry order reached 'filled' status.
        Unknown/missing order_id returns True (safe default — don't void what we can't confirm)."""
        if not order_id:
            return True
        order = self.get_order(order_id)
        if order is None:
            return True
        return order.get("status") == "filled"

    # Terminal order states where the entry genuinely will never fill — distinct
    # from "new"/"accepted"/"pending_new"/"partially_filled"/etc., which are still
    # live and may yet fill. was_entry_filled()==False conflated "still pending"
    # with "genuinely dead", which is why reconcile_ledger() used to void orders
    # that were merely still working (not actually rejected/expired/cancelled) —
    # confirmed live 2026-07-06 on jeeboo (a fork of this kit's plumbing): orders
    # that later filled for real got voided prematurely, leaving phantom server
    # registrations and orphaned real positions with no local record at all.
    _TERMINAL_UNFILLED_STATUSES = frozenset({
        "canceled", "expired", "rejected", "suspended", "done_for_day",
    })

    def entry_order_terminal_unfilled(self, order_id: str | None) -> bool:
        """True only if the entry order reached a genuinely terminal non-fill
        state (won't ever fill from here). False for an order that's still live
        (new/accepted/pending/partially_filled) — those should be left alone,
        not voided, until they either fill or actually reach a terminal state."""
        if not order_id:
            return False
        order = self.get_order(order_id)
        if order is None:
            return False
        return order.get("status") in self._TERMINAL_UNFILLED_STATUSES

    def get_last_fill(self, symbol: str, side: str | None = None, days: int = 60) -> dict | None:
        """
        Most recent filled order for a symbol (optionally a given side), newest first.
        Used to reconcile a position that closed server-side (stop fired while app was
        off): the fill price here is the exit truth the local ledger never recorded.
        """
        after = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        try:
            orders = self._get("/v2/orders", params={
                "status": "closed", "symbols": symbol, "limit": 100,
                "after": after, "direction": "desc",
            })
        except Exception:
            return None
        for o in orders:
            if not o.get("filled_at"):
                continue
            if side and o.get("side") != side:
                continue
            return o
        return None
