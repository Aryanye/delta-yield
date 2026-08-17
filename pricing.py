"""Black-76 implied volatility and greeks for Indian stock options.

Why Black-76 and not Black-Scholes-on-spot: NSE stock options are European and
the natural forward is the same-expiry single-stock future, which already
embeds the market's dividend and carry assumptions. Discounting the future
avoids having to guess a dividend yield per stock -- guessing that would put a
systematic error straight into the delta, which is the axis this whole scanner
is organised around.

Delta returned here is delta with respect to the FUTURE (the standard
practitioner convention, and the correct hedge ratio against the future).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config

IST = timezone(timedelta(hours=5, minutes=30))
SECONDS_PER_YEAR = 365.0 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def time_to_expiry(expiry: date, now: Optional[datetime] = None) -> float:
    """Year fraction to the 15:30 IST expiry instant. Zero once expired."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    expiry_dt = datetime(
        expiry.year, expiry.month, expiry.day,
        config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE, tzinfo=IST,
    )
    return max((expiry_dt - now).total_seconds(), 0.0) / SECONDS_PER_YEAR


def black76_price(F: float, K: float, T: float, sigma: float,
                  opt_type: str, r: float = config.RISK_FREE_RATE) -> float:
    """Undiscounted-forward Black-76 option price."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        intrinsic = max(F - K, 0.0) if opt_type == "CE" else max(K - F, 0.0)
        return intrinsic * math.exp(-r * max(T, 0.0))
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc = math.exp(-r * T)
    if opt_type == "CE":
        return disc * (F * norm_cdf(d1) - K * norm_cdf(d2))
    return disc * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


@dataclass
class Greeks:
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]     # per calendar day, per unit of underlying
    vega: Optional[float]      # per 1 vol point (1%)
    status: str                # ok | below_intrinsic | above_max_iv | no_price | expired


def implied_vol(price: float, F: float, K: float, T: float, opt_type: str,
                r: float = config.RISK_FREE_RATE) -> tuple:
    """Invert Black-76 for sigma by bisection. Returns (iv, status).

    Bisection rather than Newton: price is monotonic in sigma so bisection
    cannot diverge, and vega collapses for deep-OTM contracts, which is exactly
    where Newton becomes unstable -- and deep-OTM is most of this chain.
    """
    if T <= 0:
        return None, "expired"
    if price is None or price <= 0 or F <= 0 or K <= 0:
        return None, "no_price"

    disc = math.exp(-r * T)
    intrinsic = disc * (max(F - K, 0.0) if opt_type == "CE" else max(K - F, 0.0))
    # A quote below intrinsic value is arbitrage or (far more likely) a stale
    # or crossed book. No real IV exists; flag rather than silently clamp.
    if price <= intrinsic + 1e-9:
        return None, "below_intrinsic"

    hi_price = black76_price(F, K, T, config.IV_MAX, opt_type, r)
    if price >= hi_price:
        return config.IV_MAX, "above_max_iv"

    lo, hi = config.IV_MIN, config.IV_MAX
    if price <= black76_price(F, K, T, lo, opt_type, r):
        return config.IV_MIN, "ok"

    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if black76_price(F, K, T, mid, opt_type, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi), "ok"


def greeks(price: float, F: float, K: float, T: float, opt_type: str,
           r: float = config.RISK_FREE_RATE) -> Greeks:
    """Full greek set implied from a traded price."""
    iv, status = implied_vol(price, F, K, T, opt_type, r)
    if iv is None or T <= 0:
        return Greeks(None, None, None, None, None, status)

    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * iv * iv * T) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    disc = math.exp(-r * T)
    nd1 = norm_pdf(d1)

    if opt_type == "CE":
        delta = disc * norm_cdf(d1)
        price_model = disc * (F * norm_cdf(d1) - K * norm_cdf(d2))
    else:
        delta = -disc * norm_cdf(-d1)
        price_model = disc * (K * norm_cdf(-d2) - F * norm_cdf(-d1))

    gamma = disc * nd1 / (F * iv * sqrt_t)
    vega = F * disc * nd1 * sqrt_t / 100.0
    theta = (r * price_model - disc * F * nd1 * iv / (2 * sqrt_t)) / 365.0

    return Greeks(iv=iv, delta=delta, gamma=gamma, theta=theta, vega=vega, status=status)


def bucket_delta(abs_delta: float) -> Optional[float]:
    """Snap |delta| to the nearest heatmap bucket, or None if it fits none."""
    best, best_dist = None, config.BUCKET_TOLERANCE
    for b in config.DELTA_BUCKETS:
        dist = abs(abs_delta - b)
        if dist <= best_dist:
            best, best_dist = b, dist
    return best


if __name__ == "__main__":
    # Round-trip check: price -> IV -> price must recover the input, and a
    # known put/call parity relation must hold.
    F, K, T = 1000.0, 1050.0, 30 / 365.0
    for sigma in (0.15, 0.30, 0.60):
        for ot in ("CE", "PE"):
            p = black76_price(F, K, T, sigma, ot)
            g = greeks(p, F, K, T, ot)
            assert abs(g.iv - sigma) < 1e-5, (sigma, g.iv, ot)
            print(f"{ot} sigma={sigma:.2f} price={p:8.3f} iv={g.iv:.6f} "
                  f"delta={g.delta:+.4f} theta/day={g.theta:+.4f} vega={g.vega:.4f}")
    c = black76_price(F, K, T, 0.3, "CE")
    p = black76_price(F, K, T, 0.3, "PE")
    parity = c - p - math.exp(-config.RISK_FREE_RATE * T) * (F - K)
    print(f"put-call parity residual: {parity:.2e}")
    assert abs(parity) < 1e-9
    print("delta bucketing:", [(d, bucket_delta(d)) for d in (0.048, 0.21, 0.335, 0.62)])
    print("ALL PRICING CHECKS PASSED")
