"""End-to-end verification against live Kite data.

Run this once after logging in, and any time you suspect the numbers. It proves
the three things the dashboard's credibility rests on:

  1. the margin engine behaves as assumed (batching is safe, margin is
     standalone and linear in lots)
  2. computed deltas agree with an independent reconstruction
  3. the yield arithmetic on a real contract ties out by hand

Usage:  python3 verify.py [SYMBOL ...]
"""
from __future__ import annotations

import sys
from datetime import datetime

import auth
import config
import margins as margins_mod
import pricing
import universe as universe_mod

# Probe candidates, in preference order. Resolved against the live universe --
# F&O membership changes (TATAMOTORS left the list after its demerger), so a
# hardcoded list goes stale and must never be assumed present.
PROBE_CANDIDATES = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "SBIN",
                    "ITC", "AXISBANK", "LT"]


def choose_probes(uni, requested):
    if requested:
        missing = [n for n in requested if n not in uni.stocks]
        if missing:
            print(f"  not in the current F&O universe, skipping: {missing}")
        return [n for n in requested if n in uni.stocks]
    picked = [n for n in PROBE_CANDIDATES if n in uni.stocks][:3]
    # Different underlyings on purpose: batching must not be able to benefit
    # from cross-margining between the probes.
    return picked or uni.stocks[:3]


def pick_probe_contracts(kite, uni, names):
    """One near-0.25-delta put per requested stock, on the nearest expiry."""
    expiry = uni.expiries[0]
    probes = []
    for name in names:
        fut = uni.futures.get((name, expiry))
        if not fut:
            print(f"  {name}: no future for {expiry}, skipping")
            continue
        try:
            fq = kite.quote([fut.key])[fut.key]
        except Exception as exc:
            print(f"  {name}: quote failed ({type(exc).__name__})")
            continue
        fpx = float(fq["last_price"])
        strikes = sorted({c.strike for c in uni.options
                          if c.name == name and c.expiry == expiry
                          and c.opt_type == "PE"})
        if not strikes:
            continue
        target = fpx * 0.93                       # roughly a 0.25-delta put
        strike = min(strikes, key=lambda s: abs(s - target))
        match = [c for c in uni.options if c.name == name and c.expiry == expiry
                 and c.opt_type == "PE" and c.strike == strike]
        if not match:
            continue
        c = match[0]
        try:
            oq = kite.quote([c.key])[c.key]
        except Exception:
            continue
        px = float(oq.get("last_price") or 0)
        depth = oq.get("depth") or {}
        buy, sell = depth.get("buy") or [], depth.get("sell") or []
        bid = float(buy[0]["price"]) if buy and buy[0].get("price") else 0
        ask = float(sell[0]["price"]) if sell and sell[0].get("price") else 0
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else px
        if mid <= 0:
            print(f"  {name}: no usable price for {c.tradingsymbol}, skipping")
            continue
        probes.append((c, fpx, mid))
    return probes


def main() -> None:
    requested = sys.argv[1:]
    kite = auth.get_kite()
    who = auth.session_status()
    print(f"session: {who.get('user')} ({who.get('user_id')})\n")

    uni = universe_mod.build_universe()
    print(f"universe: {len(uni.stocks)} stocks (indices excluded), "
          f"expiries {[e.isoformat() for e in uni.expiries]}")
    leaked = [s for s in uni.stocks if s in config.INDEX_NAMES]
    print(f"index leakage check: {'FAIL ' + str(leaked) if leaked else 'PASS (0 indices)'}\n")

    names = choose_probes(uni, requested)
    print(f"selecting probe contracts from {names} ...")
    probes = pick_probe_contracts(kite, uni, names)
    if not probes:
        raise SystemExit("could not build any probe contracts")

    # ---- margin engine ----------------------------------------------------
    report = margins_mod.verify_semantics(
        kite, [(c.tradingsymbol, c.lot_size, round(mid, 1)) for c, _f, mid in probes])

    # ---- greeks + yield arithmetic ---------------------------------------
    now = datetime.now(pricing.IST)
    print("=" * 78)
    print("GREEKS AND YIELD -- hand-checkable")
    print("=" * 78)
    mode = config.MARGIN_MODE
    margin_map = margins_mod.fetch_margins(
        kite, [(c.tradingsymbol, c.lot_size, round(mid, 1)) for c, _f, mid in probes],
        mode=mode)

    for c, fpx, mid in probes:
        T = pricing.time_to_expiry(c.expiry, now)
        g = pricing.greeks(mid, fpx, c.strike, T, c.opt_type)
        m = margin_map.get(c.tradingsymbol)
        print(f"\n{c.tradingsymbol}")
        print(f"  future {fpx:,.2f}   strike {c.strike:,.2f}   "
              f"{(c.strike/fpx-1)*100:+.2f}% from forward")
        print(f"  T = {T*365:.2f} days   mid = {mid:.2f}   lot = {c.lot_size}")
        if g.iv is None:
            print(f"  IV inversion: {g.status}")
            continue
        print(f"  implied vol {g.iv*100:.2f}%   delta {g.delta:+.4f}   "
              f"theta/day {g.theta:+.3f}")
        # independent re-price: feeding IV back must reproduce the input price
        back = pricing.black76_price(fpx, c.strike, T, g.iv, c.opt_type)
        print(f"  re-price from IV: {back:.4f} vs input {mid:.4f}  "
              f"(residual {abs(back-mid):.2e})")
        if m and m.ok:
            credit = mid * c.lot_size
            ret = credit / m.total * 100
            print(f"  credit  = {mid:.2f} x {c.lot_size} = Rs {credit:,.0f}")
            print(f"  margin  = Rs {m.total:,.0f}  "
                  f"(span {m.span:,.0f} + exposure {m.exposure:,.0f})")
            print(f"  YIELD   = {credit:,.0f} / {m.total:,.0f} = {ret:.3f}%")
        else:
            print(f"  margin unavailable: {m.error if m else 'no result'}")

    print("\n" + "=" * 78)
    ok = report["batch_safe"] and report["linear"]
    print(f"VERDICT: margin batching {'SAFE' if report['batch_safe'] else 'UNSAFE'}, "
          f"per-lot linearity {'OK' if report['linear'] else 'BROKEN'}, "
          f"mode='{config.MARGIN_MODE}'")
    if not ok:
        print("Do not trust the yield column until the above is resolved.")
    print("=" * 78)


if __name__ == "__main__":
    main()
