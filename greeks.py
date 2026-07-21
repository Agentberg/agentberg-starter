"""
greeks.py — Black-Scholes option pricing, Greeks, and implied-volatility solver.

Pure stdlib (`math` only, no numpy/scipy) — this kit deliberately keeps heavy
deps lazy/optional elsewhere (signals.py's yfinance/pandas import, e.g.), and
Greeks/IV for a handful of open positions once per session doesn't need a
numerics library.

Exists because neither this account's Alpaca tier nor its EODHD tier
(Fundamentals only — options is a separate paid UnicornBay marketplace add-on,
confirmed 403 Forbidden live 2026-07-21 against the real production key) return
delta/gamma/theta/vega/IV for options at all. Alpaca's options SNAPSHOT
endpoint does return live bid/ask on this tier though (confirmed live) — real
market data. This module turns a live quote into Greeks the same way any
options desk would from first principles: solve for the implied vol that
reproduces the quoted price, then read Greeks off that same Black-Scholes
surface. Self-consistent by construction (the Greeks match the market price
that produced them), not a third-party approximation layered on top of it.

DISCLAIMER: standard European Black-Scholes. Real listed US equity options are
American-style (early exercise possible) — BS slightly misprices early-
exercise value, most relevant for deep ITM puts on non-dividend names and
options on dividend payers near ex-div. Close enough for a portfolio-level
risk BUDGET (this module's actual use in risk.py/alpaca.py), not precise
enough for pricing an actual trade — use the broker's own fill, never this
module's price(), for execution decisions.
"""
from __future__ import annotations

import math

Greeks = dict  # {"delta": float, "gamma": float, "theta": float, "vega": float, "rho": float}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        raise ValueError(f"S, K, T, sigma must all be positive (got S={S}, K={K}, T={T}, sigma={sigma})")
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def price(S: float, K: float, T: float, r: float, sigma: float, option_type: str = "call") -> float:
    """Black-Scholes theoretical price. T in years, r and sigma as decimals
    (0.045 = 4.5%, 0.30 = 30% vol), option_type 'call' or 'put'."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if option_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def greeks(S: float, K: float, T: float, r: float, sigma: float, option_type: str = "call") -> Greeks:
    """delta (unitless, [-1,1]), gamma (delta per $1 underlying move), theta
    (dollars per calendar day, already negative for a long position's normal
    decay), vega (dollars per 1 vol POINT i.e. sigma+0.01 — matches the
    convention Alpaca's own greeks field uses, confirmed against a live
    contract during this build), rho (dollars per 1% rate move)."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * pdf_d1 * math.sqrt(T) / 100
    if option_type == "call":
        delta = _norm_cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
        rho = K * T * math.exp(-r * T) * _norm_cdf(d2) / 100
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365
        rho = -K * T * math.exp(-r * T) * _norm_cdf(-d2) / 100
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


def implied_volatility(
    market_price: float, S: float, K: float, T: float, r: float,
    option_type: str = "call", low: float = 0.001, high: float = 5.0,
    tol: float = 1e-4, max_iter: int = 100,
) -> float | None:
    """Bisection solve for the sigma that reproduces `market_price`. Bisection
    over Newton-Raphson on purpose — guaranteed to converge given a valid
    bracket (Black-Scholes price is monotonic in sigma), no derivative or
    starting-point sensitivity to worry about for a once-per-session batch
    call on a handful of positions. Returns None if no solution exists in
    [low, high] — a market price outside the no-arbitrage bounds a real IV
    could produce (stale/crossed quote), not "IV happens to be extreme"."""
    if T <= 0 or S <= 0 or K <= 0 or market_price <= 0:
        return None
    try:
        price_low = price(S, K, T, r, low, option_type) - market_price
        price_high = price(S, K, T, r, high, option_type) - market_price
    except ValueError:
        return None
    if price_low * price_high > 0:
        return None
    for _ in range(max_iter):
        mid = (low + high) / 2
        try:
            diff = price(S, K, T, r, mid, option_type) - market_price
        except ValueError:
            return None
        if abs(diff) < tol:
            return round(mid, 4)
        if (price_low < 0) == (diff < 0):
            low, price_low = mid, diff
        else:
            high = mid
    return round((low + high) / 2, 4)
