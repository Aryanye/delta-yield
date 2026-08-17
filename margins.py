"""Exact per-lot short-option margin via Kite's /margins/orders endpoint.

This deliberately does NOT model SPAN. Zerodha's own margin engine is queried
for each contract, so the number on the dashboard is the number that will
actually be blocked in the account -- including exchange margin revisions,
per-stock risk add-ons and any additional margin, none of which a local SPAN
approximation would track.

Accuracy notes:
  * quantity is set to exactly one lot, so `total` is margin per lot.
  * span + exposure + additional are stored separately so the split is visible.

On which endpoint to use -- this matters more than it looks. Kite documents
/margins/orders as computing margins "considering the existing positions and
open orders". For a scanner that is wrong: if you already hold a position in a
stock, its margin would come back netted against that holding, and the stock
would look artificially cheap next to its peers. The basket endpoint accepts
consider_positions=False, which is the standalone number a fresh short would
actually block.

MODE is therefore resolved empirically at runtime by verify_semantics() rather
than assumed -- see collector.resolve_margin_mode().
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import config


@dataclass
class MarginResult:
    tradingsymbol: str
    total: float
    span: float
    exposure: float
    additional: float
    ok: bool
    error: str = ""


def _order_payload(tradingsymbol: str, quantity: int, price: float) -> dict:
    """One SELL order request for the margin endpoint."""
    return {
        "exchange": "NFO",
        "tradingsymbol": tradingsymbol,
        "transaction_type": "SELL",
        "variety": "regular",
        "product": "NRML",       # carry/overnight -- the correct product for
                                 # holding a short option to expiry
        "order_type": "LIMIT",
        "quantity": int(quantity),
        "price": float(price),
        "trigger_price": 0.0,
    }


def _call(kite, payload: List[dict], mode: str):
    """Dispatch one batch to the chosen margin endpoint."""
    if mode == "basket":
        resp = kite.basket_order_margins(payload, consider_positions=False)
        # The basket response nests the per-order breakdown under "orders";
        # "final" is the netted whole-basket figure, which is NOT what we want.
        if isinstance(resp, dict):
            return resp.get("orders") or []
        return resp
    return kite.order_margins(payload)


def _disjoint_batches(requests_in: List[tuple], underlying_of, batch_size: int):
    """Yield batches containing AT MOST ONE contract per underlying.

    Measured on live data: putting several strikes of the same stock in one
    margin call makes the engine treat them as a portfolio and hand back
    cross-margin benefit -- FORCEMOT strikes came back up to 5.2% cheaper when
    batched with their siblings than when priced alone. Different underlyings
    do NOT net against each other (verified), so round-robining across stocks
    keeps batches large while every figure stays standalone.
    """
    queues: Dict[str, List[tuple]] = {}
    for req in requests_in:
        queues.setdefault(underlying_of(req[0]), []).append(req)

    while any(queues.values()):
        batch = []
        for name in list(queues.keys()):
            if not queues[name]:
                del queues[name]
                continue
            batch.append(queues[name].pop())
            if len(batch) >= batch_size:
                break
        if batch:
            yield batch


def fetch_margins(kite, requests_in: List[tuple], log=print,
                  mode: str = "basket", underlying_of=None) -> Dict[str, MarginResult]:
    """Fetch per-lot SELL margin for many contracts.

    requests_in: list of (tradingsymbol, lot_size, limit_price).
    mode: "basket" (standalone, ignores your open positions) or "orders".
    underlying_of: callable tradingsymbol -> underlying name. Required to keep
        same-stock contracts out of the same batch; without it every contract
        would be priced alone, which is correct but ~50x slower.
    Returns {tradingsymbol: MarginResult}.
    """
    out: Dict[str, MarginResult] = {}
    batch_size = config.MARGIN_BATCH
    underlying_of = underlying_of or (lambda ts: ts)
    batches = list(_disjoint_batches(requests_in, underlying_of, batch_size))
    total_batches = len(batches)

    for batch_no, chunk in enumerate(batches, 1):
        payload = [_order_payload(ts, lot, px) for ts, lot, px in chunk]

        result = None
        for attempt in range(config.MAX_RETRIES):
            try:
                result = _call(kite, payload, mode)
                break
            except Exception as exc:
                wait = 0.5 * (2 ** attempt)
                if attempt == config.MAX_RETRIES - 1:
                    log(f"  margin batch {batch_no}/{total_batches} failed: "
                        f"{type(exc).__name__}: {str(exc)[:120]}")
                else:
                    time.sleep(wait)

        if result is None:
            for ts, _lot, _px in chunk:
                out[ts] = MarginResult(ts, 0, 0, 0, 0, ok=False, error="api_error")
            continue

        # The response is positional: entry i corresponds to payload i. Guard
        # against a short response rather than mis-assigning margins.
        if len(result) != len(chunk):
            log(f"  margin batch {batch_no}: expected {len(chunk)} rows, "
                f"got {len(result)} -- marking batch unusable")
            for ts, _lot, _px in chunk:
                out[ts] = MarginResult(ts, 0, 0, 0, 0, ok=False, error="length_mismatch")
            continue

        for (ts, _lot, _px), row in zip(chunk, result):
            returned = row.get("tradingsymbol") or ts
            if returned != ts:
                out[ts] = MarginResult(ts, 0, 0, 0, 0, ok=False, error="symbol_mismatch")
                continue
            total = float(row.get("total") or 0)
            out[ts] = MarginResult(
                tradingsymbol=ts,
                total=total,
                span=float(row.get("span") or 0),
                exposure=float(row.get("exposure") or 0),
                additional=float(row.get("additional") or 0),
                ok=total > 0,
                error="" if total > 0 else "zero_margin",
            )

        time.sleep(config.MARGIN_RATE_SLEEP)

    return out


def _total(row) -> float:
    return float((row or {}).get("total") or 0)


def verify_semantics(kite, probes: List[tuple], log=print) -> dict:
    """Prove -- not assume -- how the margin endpoints behave on this account.

    probes: list of (tradingsymbol, lot_size, price), ideally 3+ contracts on
    DIFFERENT underlyings so that batching cannot benefit from cross-margining.

    Checks, in order of how badly getting them wrong would corrupt the output:
      1. batch vs solo   -- does putting N contracts in one call change any
                            individual margin? If yes, batching is unsafe and
                            the whole per-lot premise breaks.
      2. orders vs basket-- does /margins/orders net against open positions?
                            If the two disagree, basket(consider_positions=False)
                            is the standalone truth.
      3. linear in lots  -- 2 lots must cost 2x 1 lot for a per-lot number to
                            mean anything.
    """
    report = {"probes": [], "batch_safe": True, "modes_agree": True,
              "linear": True, "recommended_mode": "basket"}

    solo_orders, solo_basket = {}, {}
    for ts, lot, px in probes:
        try:
            solo_orders[ts] = _total(kite.order_margins([_order_payload(ts, lot, px)])[0])
        except Exception as exc:
            log(f"  order_margins failed for {ts}: {type(exc).__name__}: {str(exc)[:90]}")
        try:
            resp = kite.basket_order_margins(
                [_order_payload(ts, lot, px)], consider_positions=False)
            orders = resp.get("orders") if isinstance(resp, dict) else resp
            solo_basket[ts] = _total(orders[0]) if orders else 0.0
        except Exception as exc:
            log(f"  basket_order_margins failed for {ts}: "
                f"{type(exc).__name__}: {str(exc)[:90]}")
        time.sleep(config.MARGIN_RATE_SLEEP)

    # (1) same contracts, one combined call
    payload = [_order_payload(ts, lot, px) for ts, lot, px in probes]
    batched_orders, batched_basket = {}, {}
    try:
        rows = kite.order_margins(payload)
        for (ts, _l, _p), row in zip(probes, rows):
            batched_orders[ts] = _total(row)
    except Exception as exc:
        log(f"  batched order_margins failed: {type(exc).__name__}")
    try:
        resp = kite.basket_order_margins(payload, consider_positions=False)
        rows = resp.get("orders") if isinstance(resp, dict) else resp
        for (ts, _l, _p), row in zip(probes, rows or []):
            batched_basket[ts] = _total(row)
        report["basket_final"] = _total((resp or {}).get("final")) if isinstance(resp, dict) else None
    except Exception as exc:
        log(f"  batched basket_order_margins failed: {type(exc).__name__}")

    log("\n" + "=" * 78)
    log("MARGIN ENGINE VERIFICATION")
    log("=" * 78)
    log(f"{'contract':<22}{'orders solo':>14}{'basket solo':>14}"
        f"{'orders batch':>14}{'basket batch':>14}")
    for ts, lot, px in probes:
        so, sb = solo_orders.get(ts, 0), solo_basket.get(ts, 0)
        bo, bb = batched_orders.get(ts, 0), batched_basket.get(ts, 0)
        log(f"{ts:<22}{so:>14,.0f}{sb:>14,.0f}{bo:>14,.0f}{bb:>14,.0f}")
        drift_batch = abs(bb - sb) / sb if sb else 0
        drift_mode = abs(sb - so) / sb if sb else 0
        if drift_batch > 0.005:
            report["batch_safe"] = False
        if drift_mode > 0.005:
            report["modes_agree"] = False
        report["probes"].append({
            "symbol": ts, "lot_size": lot, "orders_solo": so, "basket_solo": sb,
            "orders_batch": bo, "basket_batch": bb,
            "batch_drift_pct": round(drift_batch * 100, 3),
            "mode_drift_pct": round(drift_mode * 100, 3),
        })

    # (3) linearity on the first probe
    ts, lot, px = probes[0]
    try:
        resp = kite.basket_order_margins(
            [_order_payload(ts, lot * 2, px)], consider_positions=False)
        rows = resp.get("orders") if isinstance(resp, dict) else resp
        two = _total(rows[0]) if rows else 0
        one = solo_basket.get(ts, 0)
        report["two_lot_total"] = two
        report["linear"] = bool(one and abs(two - 2 * one) / (2 * one) < 0.02)
        log(f"\nlinearity: 1 lot {one:,.0f}  ->  2 lots {two:,.0f}  "
            f"(expected {2*one:,.0f})   linear={report['linear']}")
    except Exception as exc:
        log(f"  linearity probe failed: {type(exc).__name__}")

    log(f"\nbatching safe (batch == solo)        : {report['batch_safe']}")
    log(f"orders == basket(no positions)      : {report['modes_agree']}")
    if not report["modes_agree"]:
        log("  -> /margins/orders is netting against your open positions;")
        log("     using basket(consider_positions=False) for standalone margin.")
    report["recommended_mode"] = "basket"
    log(f"mode selected                       : {report['recommended_mode']}")
    log("=" * 78 + "\n")
    return report
