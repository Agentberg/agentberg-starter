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

# Pre-flight checks against Alpaca's own documented order rules
# (docs.alpaca.markets/docs/orders-at-alpaca), verified 2026-07-06 --
# catches violations locally with a clear message instead of a vague 422.
_VALID_BRACKET_TIF = {"day", "gtc"}  # Alpaca's own accepted values for bracket/OCO/OTO

# Stricter than Alpaca's own validation on purpose: "day" IS a technically valid
# TIF for a bracket order per Alpaca's rules (no rejection), which is exactly
# the footgun -- it silently expires the take-profit leg at market close and
# cancels its OCO stop-loss sibling right along with it, leaving the position
# completely unprotected with no error anywhere. Confirmed live 2026-07-06 on
# jeeboo (a fork of this kit's plumbing): two real positions (SNDK/STX) sat
# well past their own recorded stop_pct while still open -- their actual
# Alpaca order history showed take_profit "expired" and stop_loss "canceled"
# the same day they were entered, because this exact submit_order() hardcoded
# time_in_force: "day" unconditionally. This codebase never wants that, so
# it's banned here even though Alpaca itself would happily accept it.
_BANNED_BRACKET_TIF = {"day"}


def _price_decimals_ok(price: float) -> bool:
    """Alpaca: prices >= $1 allow max 2 decimals, prices < $1 allow max 4."""
    s = f"{price:.10f}".rstrip("0").rstrip(".")
    decimals = len(s.split(".")[1]) if "." in s else 0
    return decimals <= (2 if price >= 1 else 4)


def validate_bracket_order(side: str, stop_loss_price: float, take_profit_price: float,
                            time_in_force: str, base_price: float | None = None) -> None:
    """Raises ValueError with a specific reason if the bracket order violates a
    documented Alpaca rule, OR this codebase's own stricter safety rule (no day-TIF
    brackets, ever). Call before submitting -- cheap, no network call."""
    if time_in_force not in _VALID_BRACKET_TIF:
        raise ValueError(
            f"Bracket/OCO/OTO orders only support time_in_force in {_VALID_BRACKET_TIF} "
            f"per Alpaca's own docs, got {time_in_force!r}"
        )
    if time_in_force in _BANNED_BRACKET_TIF:
        raise ValueError(
            f"time_in_force={time_in_force!r} is valid per Alpaca but banned in this "
            f"codebase for brackets -- it silently expires protective legs at market "
            f"close with no error (confirmed live 2026-07-06). Use 'gtc'."
        )
    if side == "buy" and take_profit_price <= stop_loss_price:
        raise ValueError(f"Buy bracket: take_profit ({take_profit_price}) must be > stop_loss ({stop_loss_price})")
    if side == "sell" and take_profit_price >= stop_loss_price:
        raise ValueError(f"Sell bracket: take_profit ({take_profit_price}) must be < stop_loss ({stop_loss_price})")
    for label, price in (("stop_loss", stop_loss_price), ("take_profit", take_profit_price)):
        if not _price_decimals_ok(price):
            raise ValueError(f"{label} price {price} exceeds Alpaca's decimal precision (2dp >=$1, 4dp <$1)")
        if base_price is not None and abs(price - base_price) < 0.01:
            raise ValueError(f"{label} price {price} must be >= $0.01 from base price {base_price}")


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
        base_price: float = None,
    ) -> dict:
        """base_price (optional): the live reference price the caller computed
        stop/target from — enables validate_bracket_order()'s $0.01-minimum-
        distance check. Omit only when no such reference exists."""
        payload = {
            "symbol": ticker,
            "qty": qty,
            "side": side,
            "type": order_type,
            # Bracket orders MUST be "gtc" -- "day" silently kills protection,
            # see _BANNED_BRACKET_TIF above. Plain (non-bracket) orders keep "day".
            "time_in_force": "gtc" if stop_loss_price else "day",
        }
        if limit_price is not None:
            payload["limit_price"] = str(limit_price)
        if stop_loss_price:
            # Bracket order — Alpaca requires BOTH stop_loss and take_profit.
            if take_profit_price is None:
                raise ValueError(f"Alpaca bracket orders require take_profit_price alongside stop_loss_price for {ticker}")
            validate_bracket_order(side, stop_loss_price, take_profit_price,
                                    payload["time_in_force"], base_price=base_price)
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
        """True only if the entry order is CONFIRMED 'filled'.

        No order_id at all returns True (nothing to check against — matches the
        historical no-info default). But a real order_id whose lookup fails
        (timeout, rate limit, transient API error) returns False, not True — a
        failed lookup means "unknown," not "confirmed." Returning True here used
        to let reconcile_ledger() register an unconfirmed entry with the network
        on nothing more than an API blip, with no way to walk the registration
        back afterward (found live 2026-07-08 via the same bug reproducing on a
        fork of this plumbing). "Unknown" must retry, never silently pass as
        "yes" for something as consequential as a network trade registration."""
        if not order_id:
            return True
        order = self.get_order(order_id)
        if order is None:
            return False
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

    def get_last_fill(self, symbol: str, side: str | None = None, days: int = 60,
                       after: str | None = None) -> dict | None:
        """
        Most recent filled order for a symbol (optionally a given side), newest first.
        Used to reconcile a position that closed server-side (stop fired while app was
        off): the fill price here is the exit truth the local ledger never recorded.

        `after` (ISO date/datetime) restricts the search to orders filled on/after that
        point -- normally the trade's own entry time. Without it, this only matches by
        symbol+side, so a later, unrelated re-entry on the same symbol can be handed
        back as "the" exit fill for a trade it has nothing to do with.
        """
        window_start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        query_after = max(after[:10], window_start) if after else window_start
        try:
            orders = self._get("/v2/orders", params={
                "status": "closed", "symbols": symbol, "limit": 100,
                "after": query_after, "direction": "desc",
            })
        except Exception:
            return None
        for o in orders:
            if not o.get("filled_at"):
                continue
            if side and o.get("side") != side:
                continue
            if after and o["filled_at"] < after:
                continue
            return o
        return None
