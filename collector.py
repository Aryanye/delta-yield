"""One refresh cycle: quotes -> greeks -> exact margins -> return -> SQLite."""
from __future__ import annotations

import math
import time
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import config
import db
import events as events_mod
import margins as margins_mod
import pricing
import universe as universe_mod


def _round_tick(price: float) -> float:
    if price is None or price <= 0:
        return 0.0
    return round(round(price / config.TICK_SIZE) * config.TICK_SIZE, 2)


def _chunk(seq: List, size: int):
    for i in range(0, len(seq), size):
        yield seq[i: i + size]


class AuthExpired(RuntimeError):
    """The Kite session died. Nothing downstream can succeed."""


def fetch_quotes(kite, keys: List[str], log=print, label: str = "") -> Dict[str, dict]:
    """Batched /quote with pacing. Kite allows 500 instruments at 1 req/sec.

    A dead token makes every batch fail identically. Retrying and continuing
    turns an auth failure into a silent empty result, so it is raised instead.
    """
    out: Dict[str, dict] = {}
    batches = list(_chunk(keys, config.QUOTE_BATCH))
    for i, batch in enumerate(batches, 1):
        for attempt in range(config.MAX_RETRIES):
            try:
                out.update(kite.quote(batch))
                break
            except Exception as exc:
                name = type(exc).__name__
                if name in ("TokenException", "PermissionException"):
                    raise AuthExpired(f"{name}: {str(exc)[:120]}") from exc
                if attempt == config.MAX_RETRIES - 1:
                    log(f"  quote batch {i}/{len(batches)} ({label}) failed: "
                        f"{name}: {str(exc)[:100]}")
                else:
                    time.sleep(1.0 * (attempt + 1))
        if i < len(batches):
            time.sleep(config.QUOTE_RATE_SLEEP)
    return out


def _best_bid_ask(quote: dict) -> Tuple[float, float]:
    depth = (quote or {}).get("depth") or {}
    buy = depth.get("buy") or []
    sell = depth.get("sell") or []
    bid = float(buy[0]["price"]) if buy and buy[0].get("price") else 0.0
    ask = float(sell[0]["price"]) if sell and sell[0].get("price") else 0.0
    return bid, ask


def _liquidity_flag(spread_pct: Optional[float], oi_lots: float,
                    volume: int) -> str:
    """green | amber | red. Nothing is hidden -- this only colours the row."""
    if spread_pct is None:
        return "red"
    if spread_pct > config.SPREAD_BAD_PCT or oi_lots < config.OI_BAD_LOTS:
        return "red"
    if (spread_pct > config.SPREAD_WARN_PCT
            or oi_lots < config.OI_WARN_LOTS
            or volume < config.MIN_VOLUME_WARN):
        return "amber"
    return "green"


def collect(kite, uni: Optional[universe_mod.Universe] = None, log=print) -> dict:
    started = time.time()
    now = datetime.now(pricing.IST)
    ts = now.isoformat(timespec="seconds")

    uni = uni or universe_mod.build_universe()
    log(f"universe: {len(uni.stocks)} stocks, "
        f"expiries {[e.isoformat() for e in uni.expiries]}, "
        f"{len(uni.options)} option contracts")

    snapshot_id = db.start_snapshot(ts)

    # ---- 1. underlying + forward -------------------------------------------
    fut_keys = [f.key for f in uni.futures.values()]
    eq_keys = [f"NSE:{name}" for name in uni.stocks]
    log(f"fetching {len(fut_keys)} futures + {len(eq_keys)} spot quotes ...")
    base_quotes = fetch_quotes(kite, fut_keys + eq_keys, log, "underlying")

    future_px: Dict[Tuple[str, date], float] = {}
    spot_px: Dict[str, float] = {}
    spot_move: Dict[str, dict] = {}
    for (name, expiry), fut in uni.futures.items():
        q = base_quotes.get(fut.key)
        if q and q.get("last_price"):
            future_px[(name, expiry)] = float(q["last_price"])
    for name in uni.stocks:
        q = base_quotes.get(f"NSE:{name}")
        if q and q.get("last_price"):
            ltp = float(q["last_price"])
            spot_px[name] = ltp
            ohlc = q.get("ohlc") or {}
            prev_close = float(ohlc.get("close") or 0)
            # Kite returns net_change as 0 on this endpoint, so the day move is
            # derived from the previous close in the OHLC block instead.
            change = (ltp - prev_close) if prev_close > 0 else None
            spot_move[name] = {
                "prev_close": prev_close or None,
                "day_change": change,
                "day_pct": (change / prev_close * 100) if prev_close > 0 else None,
                "day_open": float(ohlc.get("open") or 0) or None,
                "day_high": float(ohlc.get("high") or 0) or None,
                "day_low": float(ohlc.get("low") or 0) or None,
            }

    log(f"  got {len(future_px)} future prices, {len(spot_px)} spot prices")

    # Without forwards there is nothing to price. Abort before touching the
    # database so the previous good snapshot survives.
    if not future_px:
        db.finish_snapshot(snapshot_id, status="error", n_contracts=0,
                           note="no underlying quotes returned")
        raise RuntimeError(
            "no future prices returned -- aborting this cycle and keeping the "
            "previous snapshot")

    underlying_rows = []
    for (name, expiry), fpx in future_px.items():
        spx = spot_px.get(name)
        underlying_rows.append({
            "name": name, "expiry": expiry.isoformat(), "spot": spx, "future": fpx,
            "basis_pct": ((fpx / spx - 1) * 100) if spx else None,
            "lot_size": uni.lot_size.get((name, expiry)), "ts": ts,
        })
    db.write_underlyings(underlying_rows)
    # Corporate events change daily at most, and NSE can be flaky. Refresh
    # opportunistically and never let it break a data cycle.
    try:
        events_mod.refresh(set(uni.stocks), log=log)
    except Exception as exc:
        log(f"  event refresh skipped: {type(exc).__name__}: {str(exc)[:70]}")

    db.write_stock_meta([
        dict({"name": n, "company": uni.company.get(n, ""),
              "spot": spot_px.get(n), "ts": ts}, **spot_move.get(n, {}))
        for n in uni.stocks])

    # ---- 2. pre-filter strikes by moneyness --------------------------------
    candidates = []
    for c in uni.options:
        fpx = future_px.get((c.name, c.expiry))
        if not fpx or fpx <= 0:
            continue
        if abs(c.strike / fpx - 1.0) <= config.MONEYNESS_WINDOW:
            candidates.append(c)
    log(f"strike pre-filter: {len(candidates)} of {len(uni.options)} contracts "
        f"within +/-{config.MONEYNESS_WINDOW:.0%} of forward")

    # ---- 3. option quotes ---------------------------------------------------
    opt_keys = [c.key for c in candidates]
    log(f"fetching option quotes in {math.ceil(len(opt_keys)/config.QUOTE_BATCH)} "
        f"batches ...")
    opt_quotes = fetch_quotes(kite, opt_keys, log, "options")
    log(f"  got {len(opt_quotes)} option quotes")

    # ---- 4. greeks ----------------------------------------------------------
    rows: List[dict] = []
    priced = 0
    for c in candidates:
        q = opt_quotes.get(c.key)
        if not q:
            continue
        fpx = future_px.get((c.name, c.expiry))
        bid, ask = _best_bid_ask(q)
        ltp = float(q.get("last_price") or 0)
        oi = int(q.get("oi") or 0)
        volume = int(q.get("volume") or 0)

        # Mid is the honest mark. LTP goes stale for hours on illiquid strikes,
        # and a stale LTP would feed a wrong IV, a wrong delta and a wrong
        # return -- so mid wins whenever a two-sided market exists.
        if bid > 0 and ask > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid if mid > 0 else None
            px_source = "mid"
        elif ltp > 0:
            mid = ltp
            spread_pct = None
            px_source = "ltp_only"
        else:
            continue

        T = pricing.time_to_expiry(c.expiry, now)
        g = pricing.greeks(mid, fpx, c.strike, T, c.opt_type)
        if g.delta is None:
            px_status = g.status
            abs_delta = None
        else:
            px_status = px_source if g.status == "ok" else g.status
            abs_delta = abs(g.delta)
            priced += 1

        dte = (c.expiry - now.date()).days
        oi_lots = (oi / c.lot_size) if c.lot_size else 0
        in_band = (abs_delta is not None
                   and config.DELTA_MIN <= abs_delta <= config.DELTA_MAX)

        rows.append({
            "tradingsymbol": c.tradingsymbol, "name": c.name,
            "expiry": c.expiry.isoformat(), "dte": dte, "strike": c.strike,
            "opt_type": c.opt_type, "lot_size": c.lot_size,
            "spot": spot_px.get(c.name), "future": fpx,
            "bid": bid or None, "ask": ask or None, "mid": mid, "ltp": ltp or None,
            "spread_pct": (spread_pct * 100) if spread_pct is not None else None,
            "oi": oi, "oi_lots": round(oi_lots, 1), "volume": volume,
            "iv": (g.iv * 100) if g.iv is not None else None,
            "delta": g.delta, "abs_delta": abs_delta,
            "gamma": g.gamma, "theta": g.theta, "vega": g.vega,
            "delta_bucket": pricing.bucket_delta(abs_delta) if abs_delta else None,
            "margin": None, "span": None, "exposure": None,
            "credit": None, "return_pct": None,
            "liq_flag": _liquidity_flag(spread_pct, oi_lots, volume),
            "px_status": px_status,
            "quality": None,          # filled by _assign_quality
            "in_band": 1 if in_band else 0,
        })

    _assign_quality(rows, log)

    log(f"greeks: {priced} contracts priced, "
        f"{sum(r['in_band'] for r in rows)} inside the "
        f"{config.DELTA_MIN}-{config.DELTA_MAX} delta band")

    _report_truncation(rows, uni, future_px, log)

    # ---- 5. exact margins for in-band contracts -----------------------------
    band_rows = [r for r in rows if r["in_band"]]
    margin_requests = [
        (r["tradingsymbol"], r["lot_size"], _round_tick(r["mid"])) for r in band_rows
    ]
    log(f"fetching exact SPAN+exposure margins for {len(margin_requests)} "
        f"contracts in {math.ceil(len(margin_requests)/config.MARGIN_BATCH)} batches ...")
    symbol_to_name = {r["tradingsymbol"]: r["name"] for r in band_rows}
    margin_map = margins_mod.fetch_margins(
        kite, margin_requests, log, mode=config.MARGIN_MODE,
        underlying_of=lambda ts: symbol_to_name.get(ts, ts))

    margined = 0
    for r in band_rows:
        m = margin_map.get(r["tradingsymbol"])
        if not m or not m.ok:
            continue
        credit = r["mid"] * r["lot_size"]
        r["margin"] = m.total
        r["span"] = m.span
        r["exposure"] = m.exposure
        r["credit"] = credit
        r["return_pct"] = (credit / m.total * 100.0) if m.total > 0 else None
        margined += 1

    log(f"  {margined} contracts priced with exact broker margin")

    # ---- 6. persist ---------------------------------------------------------
    if not rows:
        db.finish_snapshot(snapshot_id, status="error", n_contracts=0,
                           note="no priced contracts")
        raise RuntimeError("cycle produced no rows -- previous snapshot kept")
    db.write_latest(rows)
    hist = [dict(r, snapshot_id=snapshot_id, ts=ts)
            for r in band_rows if r.get("return_pct") is not None]
    db.write_history(hist)

    duration = time.time() - started
    db.finish_snapshot(
        snapshot_id, n_stocks=len({r["name"] for r in rows}),
        n_contracts=len(rows), n_priced=priced, n_margined=margined,
        duration_sec=round(duration, 1), status="ok",
        note=f"expiries={','.join(e.isoformat() for e in uni.expiries)}",
    )

    log(f"cycle complete in {duration:.1f}s "
        f"({len(rows)} rows stored, {margined} with return)")

    return {
        "snapshot_id": snapshot_id, "ts": ts, "rows": len(rows),
        "priced": priced, "margined": margined, "duration_sec": duration,
        "stocks": len({r["name"] for r in rows}),
    }


def _assign_quality(rows: List[dict], log) -> None:
    """Mark rows whose PRICE cannot be trusted, independently of liquidity.

    Liquidity flags answer "could I get filled?". This answers a harder
    question: "is this premium even real?" They are not the same, and conflating
    them is how a stale print becomes a headline number.

    A contract with no live two-sided book falls back to its last traded price,
    which may be hours or days old. Fed through Black-76 that stale print
    produces a nonsense IV, a nonsense delta, and a spectacular fake yield --
    FORCEMOT 23000CE showed 190% IV and a 205% yield off a dead print.

    Two independent tests:
      no_book     -- no two-sided quote, so the premium is a stale print
      iv_outlier  -- IV wildly detached from that stock's own ATM vol for the
                     same expiry; the smile can reach ~2x ATM at the wings, so
                     3x is a deliberately conservative bar that catches only
                     genuinely broken prices
    """
    atm_iv: Dict[tuple, float] = {}
    best_dist: Dict[tuple, float] = {}
    for r in rows:
        if r["iv"] is None or r["px_status"] != "mid" or not r["future"]:
            continue
        key = (r["name"], r["expiry"])
        dist = abs(r["strike"] / r["future"] - 1.0)
        if dist < best_dist.get(key, 1e9):
            best_dist[key] = dist
            atm_iv[key] = r["iv"]

    counts = {"ok": 0, "no_book": 0, "iv_outlier": 0, "unpriced": 0}
    for r in rows:
        if r["iv"] is None:
            r["quality"] = "unpriced"
        elif r["px_status"] != "mid":
            r["quality"] = "no_book"
        else:
            ref = atm_iv.get((r["name"], r["expiry"]))
            if ref and (r["iv"] > 3.0 * ref or r["iv"] < ref / 3.0):
                r["quality"] = "iv_outlier"
            else:
                r["quality"] = "ok"
        counts[r["quality"]] += 1

    band = [r for r in rows if r["in_band"]]
    good = sum(1 for r in band if r["quality"] == "ok")
    log(f"price quality: {counts['ok']} ok, {counts['no_book']} no live book, "
        f"{counts['iv_outlier']} IV outliers, {counts['unpriced']} unpriced "
        f"({good}/{len(band)} in-band rows have a trustworthy price)")


def _report_truncation(rows: List[dict], uni, future_px: Dict, log) -> None:
    """Warn only about truncation WE caused.

    A group whose most-OTM quoted strike still has |delta| > DELTA_MIN has an
    unreachable low-delta tail -- but there are two very different reasons:

      (a) our moneyness window filtered strikes out  -> our bug, must report
      (b) NSE simply lists no strikes that far OTM   -> nothing to fix

    Case (b) is by far the common one (304 of 315 groups on the live chain), so
    reporting both together would cry wolf and train the reader to ignore a
    warning that sometimes matters.
    """
    worst: Dict[tuple, float] = {}
    for r in rows:
        if r["abs_delta"] is None:
            continue
        key = (r["name"], r["expiry"], r["opt_type"])
        worst[key] = min(worst.get(key, 1.0), r["abs_delta"])
    short_tail = {k for k, v in worst.items() if v > config.DELTA_MIN}
    if not short_tail:
        return

    # Which groups did our own filter actually remove strikes from?
    we_excluded = set()
    for c in uni.options:
        fpx = future_px.get((c.name, c.expiry))
        if not fpx:
            continue
        if abs(c.strike / fpx - 1.0) > config.MONEYNESS_WINDOW:
            we_excluded.add((c.name, c.expiry.isoformat(), c.opt_type))

    our_fault = sorted(short_tail & we_excluded)
    exchange = len(short_tail) - len(our_fault)
    if our_fault:
        log(f"  WARNING: moneyness window truncated the {config.DELTA_MIN} tail "
            f"for {len(our_fault)} group(s): {our_fault[:4]} -- widen "
            f"config.MONEYNESS_WINDOW")
    if exchange:
        log(f"  ({exchange} groups simply have no strikes listed that far OTM "
            f"-- nothing to fix)")


if __name__ == "__main__":
    import auth
    db.init()
    collect(auth.get_kite())
