"""Read-side queries that back the dashboard API."""
from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Dict, List, Optional

import config
import db
import pricing


def market_status(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(pricing.IST)
    open_h, open_m = (int(x) for x in config.MARKET_OPEN.split(":"))
    end_h, end_m = (int(x) for x in config.MARKET_END.split(":"))
    is_weekday = now.weekday() in config.TRADE_WEEKDAYS
    within = dtime(open_h, open_m) <= now.time() <= dtime(end_h, end_m)
    return {
        "open": bool(is_weekday and within),
        "weekday": is_weekday,
        "now": now.isoformat(timespec="seconds"),
        "session": f"{config.MARKET_OPEN}-{config.MARKET_END} IST",
    }


def summary() -> dict:
    snap = db.last_snapshot()
    with db.connect() as conn:
        expiries = [r[0] for r in conn.execute(
            "SELECT DISTINCT expiry FROM latest ORDER BY expiry")]
        stocks = conn.execute("SELECT COUNT(DISTINCT name) FROM latest").fetchone()[0]
        band = conn.execute(
            "SELECT COUNT(*) FROM latest WHERE in_band = 1 AND return_pct IS NOT NULL "
            "AND quality = 'ok'").fetchone()[0]
        best = conn.execute(
            "SELECT name, tradingsymbol, return_pct, abs_delta, expiry, opt_type "
            "FROM latest WHERE return_pct IS NOT NULL AND quality = 'ok' "
            "ORDER BY return_pct DESC LIMIT 1").fetchone()
        med = conn.execute(
            "SELECT AVG(return_pct) FROM latest WHERE return_pct IS NOT NULL "
            "AND quality = 'ok'").fetchone()[0]
        qual = {r[0]: r[1] for r in conn.execute(
            "SELECT quality, COUNT(*) FROM latest WHERE in_band = 1 "
            "AND return_pct IS NOT NULL GROUP BY quality")}
    return {
        "snapshot": snap,
        "expiries": expiries,
        "stocks": stocks,
        "band_contracts": band,
        "avg_return_pct": med,
        "best": dict(best) if best else None,
        "quality_counts": qual,
        "market": market_status(),
        "delta_band": [config.DELTA_MIN, config.DELTA_MAX],
        "buckets": config.DELTA_BUCKETS,
        "refresh_seconds": config.REFRESH_SECONDS,
    }


def heatmap(side: str = "PE", expiry: Optional[str] = None,
            liquidity: str = "all", quality: str = "ok") -> dict:
    """Best-representative return for every (stock, delta bucket).

    For each bucket the contract whose |delta| sits CLOSEST TO THE BUCKET
    CENTRE is chosen -- not the highest-returning contract in the tolerance
    window, which would systematically pick the edge of the window and overstate
    the yield at that delta.
    """
    where = ["return_pct IS NOT NULL", "delta_bucket IS NOT NULL", "in_band = 1"]
    params: List = []
    # Default to trustworthy prices only. Rows priced off a stale print produce
    # spectacular fake yields that would otherwise own the top of the heatmap.
    if quality == "ok":
        where.append("quality = 'ok'")
    if side in ("CE", "PE"):
        where.append("opt_type = ?")
        params.append(side)
    if expiry:
        where.append("expiry = ?")
        params.append(expiry)
    if liquidity == "green":
        where.append("liq_flag = 'green'")
    elif liquidity == "tradeable":
        where.append("liq_flag IN ('green','amber')")

    sql = (f"SELECT name, tradingsymbol, expiry, dte, strike, opt_type, abs_delta, "
           f"delta_bucket, return_pct, margin, credit, mid, iv, oi, oi_lots, "
           f"lot_size, liq_flag, spread_pct, future, quality "
           f"FROM latest WHERE {' AND '.join(where)}")

    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]

    best: Dict[tuple, dict] = {}
    for r in rows:
        key = (r["name"], r["delta_bucket"])
        dist = abs(r["abs_delta"] - r["delta_bucket"])
        cur = best.get(key)
        if cur is None or dist < cur["_dist"]:
            best[key] = dict(r, _dist=dist)

    stocks: Dict[str, dict] = {}
    for (name, bucket), r in best.items():
        entry = stocks.setdefault(name, {"name": name, "buckets": {}})
        r.pop("_dist", None)
        entry["buckets"][f"{bucket:.2f}"] = r

    with db.connect() as conn:
        move = {r["name"]: dict(r) for r in conn.execute(
            "SELECT name, company, spot, prev_close, day_change, day_pct "
            "FROM stock_meta")}
    out = list(stocks.values())
    for s in out:
        m = move.get(s["name"]) or {}
        s["company"] = m.get("company")
        s["spot"] = m.get("spot")
        s["day_change"] = m.get("day_change")
        s["day_pct"] = m.get("day_pct")
        vals = [b["return_pct"] for b in s["buckets"].values()]
        s["avg_return"] = sum(vals) / len(vals) if vals else None
        s["max_return"] = max(vals) if vals else None
        s["coverage"] = len(vals)
        lots = [b["lot_size"] for b in s["buckets"].values()]
        s["lot_size"] = lots[0] if lots else None
        margins = [b["margin"] for b in s["buckets"].values() if b.get("margin")]
        s["min_margin"] = min(margins) if margins else None
    out.sort(key=lambda s: (s["avg_return"] is None, -(s["avg_return"] or 0)))
    return {"side": side, "expiry": expiry, "buckets": config.DELTA_BUCKETS,
            "stocks": out}


def watchlist_heatmap(side: str = "PE", expiry: Optional[str] = None,
                      liquidity: str = "all", quality: str = "ok") -> dict:
    """Same computation as heatmap(), narrowed to the saved watch list.

    Deliberately reuses heatmap() rather than duplicating the bucket-selection
    logic -- two implementations of "closest strike to this delta" would drift.
    """
    names = set(db.watchlist_get())
    data = heatmap(side=side, expiry=expiry, liquidity=liquidity, quality=quality)
    data["stocks"] = [s for s in data["stocks"] if s["name"] in names]
    data["watchlist"] = sorted(names)
    data["missing"] = sorted(names - {s["name"] for s in data["stocks"]})
    return data


def top(limit: int = 300, side: Optional[str] = None,
        expiry: Optional[str] = None, dmin: float = config.DELTA_MIN,
        dmax: float = config.DELTA_MAX, liquidity: str = "all",
        min_oi_lots: float = 0, name: Optional[str] = None,
        sort: str = "return_pct", quality: str = "ok") -> List[dict]:
    where = ["return_pct IS NOT NULL", "abs_delta BETWEEN ? AND ?"]
    params: List = [dmin, dmax]
    if quality == "ok":
        where.append("quality = 'ok'")
    if side in ("CE", "PE"):
        where.append("opt_type = ?")
        params.append(side)
    if expiry:
        where.append("expiry = ?")
        params.append(expiry)
    if name:
        where.append("name = ?")
        params.append(name)
    if liquidity == "green":
        where.append("liq_flag = 'green'")
    elif liquidity == "tradeable":
        where.append("liq_flag IN ('green','amber')")
    if min_oi_lots:
        where.append("oi_lots >= ?")
        params.append(min_oi_lots)

    sort_col = sort if sort in {
        "return_pct", "abs_delta", "margin", "credit", "iv", "oi_lots", "dte",
    } else "return_pct"
    params.append(int(limit))

    sql = (f"SELECT * FROM latest WHERE {' AND '.join(where)} "
           f"ORDER BY {sort_col} DESC LIMIT ?")
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def chain(name: str, expiry: Optional[str] = None) -> dict:
    where = ["name = ?"]
    params: List = [name]
    if expiry:
        where.append("expiry = ?")
        params.append(expiry)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM latest WHERE {' AND '.join(where)} "
            f"ORDER BY expiry, strike, opt_type", params)]
        und = [dict(r) for r in conn.execute(
            "SELECT * FROM underlyings WHERE name = ? ORDER BY expiry", (name,))]
    return {"name": name, "rows": rows, "underlyings": und}


def events_upcoming(days: int = 45, confidence: Optional[str] = None,
                    names: Optional[List[str]] = None) -> List[dict]:
    """Upcoming events, nearest first, annotated with days-to-event."""
    where = ["date(event_date) >= date('now','localtime')",
             "date(event_date) <= date('now','localtime', ?)"]
    params: List = [f"+{int(days)} days"]
    if confidence:
        where.append("confidence = ?")
        params.append(confidence)
    if names:
        where.append(f"name IN ({','.join('?' for _ in names)})")
        params.extend(names)
    sql = ("SELECT e.*, COALESCE(m.company,'') company, m.spot, m.day_pct, "
           "CAST(julianday(e.event_date) - julianday('now','localtime') AS INTEGER) days_away "
           "FROM events e LEFT JOIN stock_meta m ON m.name = e.name "
           f"WHERE {' AND '.join(where)} "
           "ORDER BY date(e.event_date), e.confidence DESC, e.name")
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    # NSE reports the same filing through several endpoints, so one results
    # announcement can arrive as a board-meeting row AND an event-calendar row.
    # Collapse on (stock, date, type) first, then drop the generic
    # "board meeting" row when a specific event for that stock and date is
    # already present -- otherwise one earnings date shows up three times.
    rank = {"earnings": 0, "dividend": 1, "buyback": 1, "split": 1,
            "bonus": 1, "rights": 1, "restructure": 2, "fundraise": 3}
    by_type, out = set(), []
    for r in sorted(rows, key=lambda x: (x["event_date"],
                                         rank.get(x["event_type"], 5),
                                         0 if x["confidence"] == "confirmed" else 1)):
        key = (r["name"], r["event_date"], r["event_type"])
        if key in by_type:
            continue
        by_type.add(key)
        out.append(r)

    specific = {(r["name"], r["event_date"]) for r in out
                if r["event_type"] not in ("board_meeting", "other")}
    return [r for r in out
            if r["event_type"] not in ("board_meeting", "other")
            or (r["name"], r["event_date"]) not in specific]


def events_for(name: str, days: int = 120) -> List[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT *, CAST(julianday(event_date) - julianday('now','localtime') "
            "AS INTEGER) days_away FROM events WHERE name = ? "
            "AND date(event_date) >= date('now','localtime') "
            "AND date(event_date) <= date('now','localtime', ?) "
            "ORDER BY date(event_date)", (name, f"+{int(days)} days"))]


def expiries_list() -> List[str]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT expiry FROM latest ORDER BY expiry")]


def stock_list() -> List[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT l.name, COUNT(*) n, MAX(l.return_pct) best, "
            "COALESCE(m.company, '') company, m.spot, m.prev_close, "
            "m.day_change, m.day_pct "
            "FROM latest l LEFT JOIN stock_meta m ON m.name = l.name "
            "GROUP BY l.name ORDER BY l.name")]


def bucket_history(name: str, bucket: float, side: str = "PE",
                   hours: int = 24) -> List[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT ts, AVG(return_pct) return_pct, AVG(iv) iv, AVG(margin) margin "
            "FROM history WHERE name = ? AND delta_bucket = ? AND opt_type = ? "
            "AND ts >= datetime('now', ?) GROUP BY ts ORDER BY ts",
            (name, bucket, side, f"-{hours} hours"))]
