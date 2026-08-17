"""Mechanical option-structure construction from the live chain.

This is a CALCULATOR, not a recommender. You pick the stock and the directional
stance; it assembles the standard structures for that stance out of real strikes
at real prices, and reports what they actually pay and what they can actually
lose. It does not rank them as "best", does not tell you what to trade, and has
no view on the underlying.

Every number is measured, not modelled:
  * leg prices are the same bid-ask mids the scanner uses
  * margin is Zerodha's own netted basket margin for the exact multi-leg
    position -- which is the whole point of a spread, and is why a defined-risk
    structure can show a far better return on margin than its naked leg
  * payoff, breakevens and max loss are evaluated numerically on a price grid,
    so ratio structures are handled the same way as vertical ones
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config
import db
import margins as margins_mod

# Target deltas for each leg. Chosen to be conventional, not clever.
STANCES = {
    "bullish": "You expect the stock to rise, or at least not fall much.",
    "bearish": "You expect the stock to fall, or at least not rise much.",
    "neutral": "You expect the stock to stay in a range.",
}


def _round_tick(price: float) -> float:
    """NFO options trade in 5-paise ticks; the margin API wants a valid price."""
    if not price or price <= 0:
        return 0.0
    return round(round(price / config.TICK_SIZE) * config.TICK_SIZE, 2)


@dataclass
class Leg:
    tradingsymbol: str
    opt_type: str          # CE | PE
    strike: float
    price: float           # per unit
    qty_lots: int          # negative = short
    delta: float
    iv: Optional[float] = None
    liq_flag: str = ""


@dataclass
class Structure:
    key: str
    name: str
    stance: str
    rationale: str
    legs: List[Leg] = field(default_factory=list)
    lot_size: int = 0
    net_credit: float = 0.0        # rupees, positive = you receive
    max_profit: Optional[float] = None
    max_loss: Optional[float] = None   # None = theoretically unbounded
    breakevens: List[float] = field(default_factory=list)
    margin: Optional[float] = None
    return_on_margin: Optional[float] = None
    net_delta: float = 0.0
    warnings: List[str] = field(default_factory=list)


def _pick(rows: List[dict], opt_type: str, target_delta: float,
          exclude: Optional[set] = None) -> Optional[dict]:
    """Closest tradeable strike to a target absolute delta."""
    exclude = exclude or set()
    cands = [r for r in rows
             if r["opt_type"] == opt_type
             and r.get("abs_delta") is not None
             and r.get("mid")
             and r["tradingsymbol"] not in exclude
             and r.get("quality") == "ok"]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(r["abs_delta"] - target_delta))


def _payoff(legs: List[Leg], spot: float, lot: int) -> Dict:
    """Numeric expiry payoff. Handles ratios and wings without special cases."""
    lo, hi = spot * 0.30, spot * 2.20
    steps = 2400
    grid = [lo + (hi - lo) * i / steps for i in range(steps + 1)]

    def value_at(px: float) -> float:
        total = 0.0
        for lg in legs:
            intrinsic = (max(px - lg.strike, 0.0) if lg.opt_type == "CE"
                         else max(lg.strike - px, 0.0))
            # qty_lots is signed; premium paid/received is the opposite sign.
            total += lg.qty_lots * lot * (intrinsic - lg.price)
        return total

    values = [value_at(p) for p in grid]
    max_profit = max(values)
    min_value = min(values)

    # An unhedged short tail keeps losing as the grid extends, so treat the
    # extremes as unbounded rather than quoting a number that depends on where
    # we happened to stop the grid.
    left_open = values[0] < values[1] < values[2]
    right_open = values[-1] < values[-2] < values[-3]
    max_loss = None if (left_open or right_open) else min_value

    breakevens = []
    for i in range(len(grid) - 1):
        a, b = values[i], values[i + 1]
        if (a < 0 <= b) or (a > 0 >= b):
            span = b - a
            t = 0.0 if span == 0 else (0 - a) / span
            breakevens.append(round(grid[i] + t * (grid[i + 1] - grid[i]), 2))
    return {"max_profit": max_profit, "max_loss": max_loss,
            "breakevens": breakevens[:4]}


def _build(key: str, name: str, stance: str, rationale: str,
           specs: List[tuple], rows: List[dict], lot: int,
           spot: float) -> Optional[Structure]:
    """specs: list of (opt_type, target_delta, qty_lots)."""
    legs: List[Leg] = []
    used: set = set()
    for opt_type, target, qty in specs:
        row = _pick(rows, opt_type, target, used)
        if not row:
            return None
        used.add(row["tradingsymbol"])
        legs.append(Leg(
            tradingsymbol=row["tradingsymbol"], opt_type=opt_type,
            strike=row["strike"], price=row["mid"], qty_lots=qty,
            delta=row["delta"], iv=row.get("iv"), liq_flag=row.get("liq_flag", ""),
        ))

    # Two legs landing on the same strike is a degenerate structure.
    if len({(l.opt_type, l.strike) for l in legs}) != len(legs):
        return None

    net_credit = sum(-l.qty_lots * lot * l.price for l in legs)
    pay = _payoff(legs, spot, lot)
    st = Structure(
        key=key, name=name, stance=stance, rationale=rationale, legs=legs,
        lot_size=lot, net_credit=net_credit,
        max_profit=pay["max_profit"], max_loss=pay["max_loss"],
        breakevens=pay["breakevens"],
        net_delta=sum(l.qty_lots * l.delta for l in legs),
    )
    thin = [l.tradingsymbol for l in legs if l.liq_flag == "red"]
    if thin:
        st.warnings.append(f"Thin book on {', '.join(thin)} — the mid may not be fillable.")
    return st


def candidates(name: str, expiry: str, stance: str,
               rows: Optional[List[dict]] = None) -> List[Structure]:
    """Assemble the conventional structures for a stance."""
    if rows is None:
        with db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM latest WHERE name = ? AND expiry = ?", (name, expiry))]
    rows = [r for r in rows if r.get("mid") and r.get("abs_delta") is not None]
    if not rows:
        return []
    lot = rows[0]["lot_size"]
    spot = rows[0].get("future") or rows[0].get("spot")
    if not spot:
        return []

    out: List[Structure] = []
    if stance == "bullish":
        out += [
            _build("short_put", "Short put", stance,
                   "Collects premium and profits if the stock holds above the strike. "
                   "Undefined risk below it.",
                   [("PE", 0.25, -1)], rows, lot, spot),
            _build("bull_put_spread", "Bull put spread", stance,
                   "Sells a put and buys a further-out one as a floor. Caps both the "
                   "credit and the loss, and the netted margin is far smaller.",
                   [("PE", 0.30, -1), ("PE", 0.12, 1)], rows, lot, spot),
            _build("put_ratio_spread", "Put ratio spread (1x2)", stance,
                   "Buys one nearer put and sells two further out. Usually opens for a "
                   "credit and pays most if the stock drifts down to the short strike, "
                   "but the extra short leg leaves risk open below.",
                   [("PE", 0.40, 1), ("PE", 0.20, -2)], rows, lot, spot),
        ]
    elif stance == "bearish":
        out += [
            _build("short_call", "Short call", stance,
                   "Collects premium and profits if the stock stays below the strike. "
                   "Undefined risk above it.",
                   [("CE", 0.25, -1)], rows, lot, spot),
            _build("bear_call_spread", "Bear call spread", stance,
                   "Sells a call and buys a further-out one as a cap. Defined risk and "
                   "much lighter margin than the naked call.",
                   [("CE", 0.30, -1), ("CE", 0.12, 1)], rows, lot, spot),
            _build("call_ratio_spread", "Call ratio spread (1x2)", stance,
                   "Buys one nearer call and sells two further out. Pays most if the "
                   "stock drifts up to the short strike; risk stays open above it.",
                   [("CE", 0.40, 1), ("CE", 0.20, -2)], rows, lot, spot),
        ]
    else:
        out += [
            _build("short_strangle", "Short strangle", stance,
                   "Sells both wings. Maximum credit, undefined risk on both sides.",
                   [("PE", 0.16, -1), ("CE", 0.16, -1)], rows, lot, spot),
            _build("iron_condor", "Iron condor", stance,
                   "A strangle with both tails bought back. Defined risk both ways and "
                   "a fraction of the margin.",
                   [("PE", 0.20, -1), ("PE", 0.08, 1),
                    ("CE", 0.20, -1), ("CE", 0.08, 1)], rows, lot, spot),
            _build("short_straddle", "Short straddle", stance,
                   "Sells the at-the-money call and put. The largest credit and the "
                   "largest sensitivity to a move.",
                   [("PE", 0.50, -1), ("CE", 0.50, -1)], rows, lot, spot),
        ]
    return [s for s in out if s]


def price_margins(kite, structures: List[Structure], log=print) -> List[Structure]:
    """Attach Zerodha's netted basket margin to each structure.

    Netting is CORRECT here, unlike in the scanner: these legs really are one
    position, and the hedge benefit is real margin you will not have to post.
    """
    for st in structures:
        payload = []
        for lg in st.legs:
            payload.append({
                "exchange": "NFO", "tradingsymbol": lg.tradingsymbol,
                "transaction_type": "SELL" if lg.qty_lots < 0 else "BUY",
                "variety": "regular", "product": "NRML", "order_type": "LIMIT",
                "quantity": int(abs(lg.qty_lots) * st.lot_size),
                "price": _round_tick(lg.price),
                "trigger_price": 0.0,
            })
        try:
            resp = kite.basket_order_margins(payload, consider_positions=False)
            final = (resp or {}).get("final") or {}
            total = float(final.get("total") or 0)
            st.margin = total or None
            if st.margin and st.net_credit:
                st.return_on_margin = st.net_credit / st.margin * 100
        except Exception as exc:
            log(f"  margin failed for {st.key}: {type(exc).__name__}: {str(exc)[:70]}")
    return structures


def target_stocks(limit: int = 60, event_days: int = 25) -> List[str]:
    """Which stocks are worth pre-pricing, in priority order.

    Pricing every stock would be ~1,900 margin calls; this keeps it to the ones
    a person would actually open: anything with a dated event, whatever is on
    the watch list, then the highest-yielding names so the tab is useful even
    when no events are near.
    """
    picks: List[str] = []

    def add(names):
        for n in names:
            if n not in picks:
                picks.append(n)

    with db.connect() as conn:
        confirmed = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM events WHERE confidence='confirmed' "
            "AND date(event_date) >= date('now','localtime') "
            "AND date(event_date) <= date('now','localtime', ?) "
            "ORDER BY event_date", (f"+{event_days} days",))]
        add(confirmed)
        add(db.watchlist_get())
        add([r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM events WHERE confidence='estimated' "
            "AND date(event_date) >= date('now','localtime') "
            "AND date(event_date) <= date('now','localtime', ?) "
            "ORDER BY event_date", (f"+{event_days} days",))])
        add([r[0] for r in conn.execute(
            "SELECT name FROM latest WHERE return_pct IS NOT NULL AND quality='ok' "
            "GROUP BY name ORDER BY MAX(return_pct) DESC LIMIT 30")])
    return picks[:limit]


def precompute(kite, expiry: str, names: Optional[List[str]] = None,
               limit: int = 60, log=print, budget_seconds: float = 240.0) -> dict:
    """Build and price structures for the priority stocks, caching each result.

    Time-boxed: this runs off the critical path, but it still shares the Kite
    rate limit with the collector, so it stops cleanly when the budget is spent
    rather than running into the next data cycle.
    """
    import time as _time
    started = _time.time()
    names = names or target_stocks(limit=limit)
    if not names:
        return {"stocks": 0, "rows": 0}

    with db.connect() as conn:
        by_stock: Dict[str, List[dict]] = {}
        for r in conn.execute(
                "SELECT * FROM latest WHERE expiry = ?", (expiry,)):
            row = dict(r)
            by_stock.setdefault(row["name"], []).append(row)

    written, done = [], 0
    for name in names:
        if _time.time() - started > budget_seconds:
            log(f"  strategy precompute stopped at budget after {done} stocks")
            break
        rows = by_stock.get(name)
        if not rows:
            continue
        for stance in ("bullish", "neutral", "bearish"):
            sts = candidates(name, expiry, stance, rows=rows)
            if not sts:
                continue
            price_margins(kite, sts, log=lambda m: None)
            written.append({
                "name": name, "expiry": expiry, "stance": stance,
                "payload": _json.dumps([to_dict(x) for x in sts]),
            })
        done += 1

    db.write_strategies(written)
    log(f"strategies: priced {len(written)} stance-sets across {done} stocks "
        f"in {_time.time()-started:.0f}s")
    return {"stocks": done, "rows": len(written)}


def to_dict(st: Structure) -> dict:
    return {
        "key": st.key, "name": st.name, "stance": st.stance,
        "rationale": st.rationale, "lot_size": st.lot_size,
        "net_credit": round(st.net_credit, 2),
        "max_profit": None if st.max_profit is None else round(st.max_profit, 2),
        "max_loss": None if st.max_loss is None else round(st.max_loss, 2),
        "breakevens": st.breakevens,
        "margin": None if st.margin is None else round(st.margin, 2),
        "return_on_margin": None if st.return_on_margin is None
                            else round(st.return_on_margin, 3),
        "net_delta": round(st.net_delta, 4),
        "warnings": st.warnings,
        "legs": [{
            "tradingsymbol": l.tradingsymbol, "opt_type": l.opt_type,
            "strike": l.strike, "price": round(l.price, 2),
            "qty_lots": l.qty_lots, "delta": round(l.delta, 4),
            "iv": None if l.iv is None else round(l.iv, 2),
            "liq_flag": l.liq_flag,
            "action": "SELL" if l.qty_lots < 0 else "BUY",
        } for l in st.legs],
    }
