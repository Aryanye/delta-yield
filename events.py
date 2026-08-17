"""Corporate event calendar for the F&O universe, built from NSE filings.

What is honestly available, and what is not:

  CONFIRMED  NSE publishes board-meeting intimations and an event calendar, but
             companies only file them roughly one to three weeks ahead. So the
             confirmed calendar is accurate and sparse -- typically a handful of
             F&O names at any moment, filling in as the quarter's results
             season approaches.

  ESTIMATED  For everything else the next results date is projected from the
             company's own filing history (~91 days after its last quarterly
             result). This is a guess with a wide error bar and is labelled as
             such everywhere it surfaces. It is useful for "is an event roughly
             inside this expiry", never for "trade this on that date".

Never conflate the two: a fabricated-looking precise date for an unannounced
result would be worse than no date at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

import config
import db

NSE_HOME = "https://www.nseindia.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Purpose text -> our event type. Order matters: first match wins.
TYPE_RULES = [
    (r"financial result|quarterly result|audited result|unaudited result", "earnings"),
    (r"buy\s*back|buyback", "buyback"),
    (r"dividend", "dividend"),
    (r"bonus", "bonus"),
    (r"split|sub-?division", "split"),
    (r"right", "rights"),
    (r"amalgamation|merger|demerger|scheme of arrangement", "restructure"),
    (r"fund rais|preferential|qip|debenture", "fundraise"),
    (r"board meeting", "board_meeting"),
]


def classify(purpose: str) -> str:
    text = (purpose or "").lower()
    for pattern, label in TYPE_RULES:
        if re.search(pattern, text):
            return label
    return "other"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{NSE_HOME}/companies-listing/corporate-filings-board-meetings",
    })
    s.get(NSE_HOME, timeout=20)          # prime the cookie jar
    return s


def _parse_nse_date(value: str) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip().split(" ")[0]
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def fetch_confirmed(fno: set, days_ahead: int = 90, log=print) -> List[dict]:
    """Board meetings + event calendar, filtered to the F&O universe."""
    out: List[dict] = []
    today = date.today()
    to = today + timedelta(days=days_ahead)
    fmt = lambda d: d.strftime("%d-%m-%Y")
    s = _session()

    try:
        url = (f"{NSE_HOME}/api/corporate-board-meetings?index=equities"
               f"&from_date={fmt(today)}&to_date={fmt(to)}")
        for row in s.get(url, timeout=30).json():
            sym = row.get("bm_symbol")
            if sym not in fno:
                continue
            when = _parse_nse_date(row.get("bm_date"))
            if not when:
                continue
            purpose = row.get("bm_purpose") or row.get("bm_desc") or ""
            out.append({
                "name": sym, "event_date": when.isoformat(),
                "event_type": classify(purpose), "purpose": purpose.strip()[:200],
                "detail": (row.get("bm_desc") or "").strip()[:300],
                "source": "nse_board_meeting", "confidence": "confirmed",
            })
    except Exception as exc:
        log(f"  board meetings fetch failed: {type(exc).__name__}: {str(exc)[:80]}")

    try:
        url = (f"{NSE_HOME}/api/event-calendar?index=equities"
               f"&from_date={fmt(today)}&to_date={fmt(to)}")
        for row in s.get(url, timeout=30).json():
            sym = row.get("symbol")
            if sym not in fno:
                continue
            when = _parse_nse_date(row.get("date"))
            if not when:
                continue
            purpose = row.get("purpose") or ""
            out.append({
                "name": sym, "event_date": when.isoformat(),
                "event_type": classify(purpose), "purpose": purpose.strip()[:200],
                "detail": (row.get("bm_desc") or "").strip()[:300],
                "source": "nse_event_calendar", "confidence": "confirmed",
            })
    except Exception as exc:
        log(f"  event calendar fetch failed: {type(exc).__name__}: {str(exc)[:80]}")

    return out


def fetch_estimated(fno: set, confirmed: List[dict], log=print) -> List[dict]:
    """Project the next results date from each company's own filing history."""
    have_earnings = {e["name"] for e in confirmed if e["event_type"] == "earnings"}
    out: List[dict] = []
    today = date.today()
    try:
        s = _session()
        url = (f"{NSE_HOME}/api/corporates-financial-results"
               f"?index=equities&period=Quarterly")
        rows = s.get(url, timeout=40).json()
    except Exception as exc:
        log(f"  historical results fetch failed: {type(exc).__name__}")
        return out

    latest: Dict[str, date] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym not in fno or sym in have_earnings:
            continue
        when = _parse_nse_date(row.get("broadCastDate"))
        if when and when > latest.get(sym, date(1970, 1, 1)):
            latest[sym] = when

    for sym, last in latest.items():
        # Indian companies file quarterly; ~91 days is the modal gap. Roll
        # forward until the projection is in the future, so a stale history
        # still yields a sensible next date.
        nxt = last + timedelta(days=91)
        while nxt < today:
            nxt += timedelta(days=91)
        out.append({
            "name": sym, "event_date": nxt.isoformat(), "event_type": "earnings",
            "purpose": "Quarterly results (projected)",
            "detail": f"Projected ~91 days after the last filed result on "
                      f"{last.isoformat()}. NSE has not published a board "
                      f"meeting date yet.",
            "source": "projected_from_history", "confidence": "estimated",
        })
    return out


def refresh(fno: set, log=print, force: bool = False) -> dict:
    """Rebuild the events table. Cheap enough to run once a day."""
    if not force and not _is_stale():
        return {"skipped": True, "reason": "fetched recently"}

    confirmed = fetch_confirmed(fno, log=log)
    estimated = fetch_estimated(fno, confirmed, log=log)
    rows = confirmed + estimated
    if not rows:
        log("  no events fetched -- keeping the previous calendar")
        return {"ok": False, "rows": 0}

    db.write_events(rows)
    n_conf = sum(1 for r in rows if r["confidence"] == "confirmed")
    log(f"events: {n_conf} confirmed + {len(rows)-n_conf} projected across "
        f"{len({r['name'] for r in rows})} stocks")
    return {"ok": True, "rows": len(rows), "confirmed": n_conf}


def _is_stale(max_age_hours: int = 20) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(fetched_at) FROM events").fetchone()
    if not row or not row[0]:
        return True
    try:
        age = datetime.now() - datetime.fromisoformat(row[0])
        return age > timedelta(hours=max_age_hours)
    except Exception:
        return True


if __name__ == "__main__":
    import universe
    db.init()
    u = universe.build_universe()
    print(refresh(set(u.stocks), force=True))
    with db.connect() as c:
        for r in c.execute(
            "SELECT name,event_date,event_type,confidence,purpose FROM events "
            "ORDER BY event_date LIMIT 12"):
            print(f"  {r['name']:<12} {r['event_date']}  {r['event_type']:<14}"
                  f"{r['confidence']:<10} {r['purpose'][:44]}")
