"""Render a self-contained, shareable snapshot of the dashboard.

The published page is the SAME UI as the live dashboard -- the identical HTML,
CSS and JS -- with the data baked in as `window.__SNAPSHOT__` instead of being
fetched from the local server. The dashboard detects that global and rebuilds
every aggregation client-side, so a person opening the shared link gets the
same heatmap, filters, sorting and drill-down, just frozen at one instant.

Output is written for the Artifact publisher, which supplies its own
<!doctype>/<head>/<body> wrapper, so those tags are stripped here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
import db
import pricing
import queries

TEMPLATE = config.BASE_DIR / "static" / "dashboard.html"

# Only the fields the UI actually reads -- keeps the payload small enough to
# publish comfortably.
ROW_FIELDS = [
    "tradingsymbol", "name", "expiry", "dte", "strike", "opt_type", "lot_size",
    "spot", "future", "bid", "ask", "mid", "ltp", "spread_pct", "oi", "oi_lots",
    "volume", "iv", "delta", "abs_delta", "delta_bucket", "margin", "span",
    "exposure", "credit", "return_pct", "liq_flag", "px_status", "quality",
]
ROUND = {"spot": 2, "future": 2, "bid": 2, "ask": 2, "mid": 2, "ltp": 2,
         "spread_pct": 2, "iv": 2, "delta": 4, "abs_delta": 4, "margin": 0,
         "span": 0, "exposure": 0, "credit": 0, "return_pct": 3, "oi_lots": 1,
         "strike": 2}


def _trim(row: dict) -> dict:
    out = {}
    for f in ROW_FIELDS:
        v = row.get(f)
        if isinstance(v, float) and f in ROUND:
            v = round(v, ROUND[f])
        out[f] = v
    return out


def _strategy_payload() -> dict:
    """{"NAME|stance": [structures]} for the published page, which has no
    server to price margins with."""
    out, ts = {}, None
    for row in db.read_strategies():
        try:
            out[f"{row['name']}|{row['stance']}"] = json.loads(row["payload"])
        except Exception:
            continue
        ts = max(ts or "", row.get("computed_at") or "")
    if ts:
        out["_computed_at"] = ts
    return out


def build_snapshot(session_ok: Optional[bool] = None) -> dict:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM latest WHERE return_pct IS NOT NULL "
            "ORDER BY return_pct DESC")]
        unds = [dict(r) for r in conn.execute("SELECT * FROM underlyings")]
    summary = queries.summary()
    stocks = sorted({r["name"] for r in rows})
    with db.connect() as conn:
        meta = {r["name"]: dict(r) for r in conn.execute(
            "SELECT name, company, spot, prev_close, day_change, day_pct "
            "FROM stock_meta")}
    return {
        "summary": summary,
        "rows": [_trim(r) for r in rows],
        "underlyings": [
            {"name": u["name"], "expiry": u["expiry"],
             "spot": round(u["spot"], 2) if u["spot"] else None,
             "future": round(u["future"], 2) if u["future"] else None,
             "basis_pct": round(u["basis_pct"], 3) if u["basis_pct"] else None,
             "lot_size": u["lot_size"]} for u in unds],
        "stocks": [{
            "name": s,
            "company": (meta.get(s) or {}).get("company") or "",
            "spot": (meta.get(s) or {}).get("spot"),
            "day_change": (meta.get(s) or {}).get("day_change"),
            "day_pct": (meta.get(s) or {}).get("day_pct"),
        } for s in stocks],
        "watchlist": db.watchlist_get(),
        "events": queries.events_upcoming(days=45),
        "strategies": _strategy_payload(),
        "generated_at": datetime.now(pricing.IST).isoformat(timespec="seconds"),
        # Lets the published page tell the truth about its own freshness.
        "session_ok": session_ok,
        "data_ts": (summary.get("snapshot") or {}).get("ts"),
        "stale_after_minutes": config.STALE_AFTER_MINUTES,
        "operator_url": f"http://{config.SERVER_HOST}:{config.SERVER_PORT}/",
    }


def render(out_path: Optional[Path] = None,
           session_ok: Optional[bool] = None,
           fragment: bool = False) -> Path:
    """Write the self-contained page.

    fragment=True strips the document scaffolding, because the Artifact
    publisher wraps the content in its own <!doctype>/<head>/<body>.

    fragment=False (the default, used for Vercel) emits a COMPLETE document.
    That distinction matters more than it looks: a fragment served directly has
    no doctype (quirks mode) and, fatally for a phone, no viewport meta -- so
    mobile browsers assume a ~980px desktop viewport, zoom out, and the mobile
    media queries never match.
    """
    out_path = Path(out_path) if out_path else (config.BASE_DIR / "snapshot.html")
    html = TEMPLATE.read_text()
    snap = build_snapshot(session_ok=session_ok)

    if not snap["rows"]:
        raise SystemExit("No data to publish -- run a collection cycle first.")

    # The published page re-applies the dashboard's DEFAULT filters client-side.
    # If an exported row is missing a field those filters test, every row gets
    # dropped and the page renders empty -- which is exactly what shipped when
    # `quality` was added to the UI but not to ROW_FIELDS. Fail loudly here
    # rather than publishing a blank grid.
    required = ["quality", "delta_bucket", "return_pct", "abs_delta",
                "liq_flag", "opt_type", "name"]
    sample = snap["rows"][0]
    missing = [f for f in required if f not in sample]
    if missing:
        raise SystemExit(
            f"Export is missing field(s) the dashboard filters on: {missing}. "
            f"Add them to ROW_FIELDS.")

    default_visible = sum(
        1 for r in snap["rows"]
        if r.get("quality") == "ok" and r.get("delta_bucket") is not None
        and r.get("return_pct") is not None)
    if default_visible == 0:
        raise SystemExit(
            "Export would render an EMPTY page under the dashboard's default "
            "filters (quality='ok'). Refusing to publish.")

    # The published page rebuilds per-stock context client-side, so anything the
    # server computes in SQL has to be exported explicitly or it silently
    # vanishes from the shared copy -- that is how the day-movement column went
    # missing from the public heatmap while working locally.
    stock_fields = ["name", "company", "spot", "day_change", "day_pct"]
    missing_stock = [f for f in stock_fields if f not in (snap["stocks"] or [{}])[0]]
    if missing_stock:
        raise SystemExit(
            f"Export is missing per-stock field(s) the UI renders: "
            f"{missing_stock}. Add them to build_snapshot()['stocks'].")
    with_move = sum(1 for s in snap["stocks"] if s.get("day_pct") is not None)
    if with_move == 0:
        raise SystemExit(
            "No stock carries a day movement -- the public heatmap would show "
            "prices with no change. Refusing to publish.")
    print(f"  day movement present for {with_move}/{len(snap['stocks'])} stocks")

    # Embed as JSON.parse("...") rather than a bare object literal. A megabytes-
    # sized object literal has to go through the full JS parser; JSON.parse takes
    # a much faster path, which matters on a 4 MB payload over a phone connection.
    inner = json.dumps(snap, default=str, separators=(",", ":"))
    blob = "JSON.parse(" + json.dumps(inner) + ")"
    # </script> inside the string would close the tag early; \/ is a valid escape
    # in a JS string literal and decodes to the same character.
    blob = blob.replace("</", "<\\/")

    # Strip the document scaffolding the Artifact publisher provides itself.
    body = html
    body = re.sub(r"<!doctype html>\s*", "", body, flags=re.I)
    body = re.sub(r"</?html[^>]*>\s*", "", body, flags=re.I)
    body = re.sub(r"</?head[^>]*>\s*", "", body, flags=re.I)
    body = re.sub(r"</?body[^>]*>\s*", "", body, flags=re.I)
    body = re.sub(r'<meta[^>]*>\s*', "", body, flags=re.I)

    # The login modal and its button are server-only controls. They are inert in
    # a snapshot (all wiring is behind `if(!EMB)`), but shipping a public page
    # containing a Kite sign-in prompt is confusing at best, so remove the
    # markup outright rather than relying on it staying hidden.
    body = re.sub(r'<div class="modal" id="loginModal">.*?</div></div>', "",
                  body, flags=re.S)
    body = re.sub(r'<button class="btn" id="loginBtn"[^>]*>.*?</button>', "",
                  body, flags=re.S)

    stamp = snap["generated_at"]
    body = body.replace(
        '<span class="sub">NSE F&amp;O · Stocks</span>',
        f'<span class="sub">NSE F&amp;O · Stocks · snapshot {stamp[:16].replace("T", " ")} IST</span>')

    injected = f'<script>window.__SNAPSHOT__={blob};</script>\n<script>\n"use strict";'
    body = body.replace('<script>\n"use strict";', injected, 1)

    if not fragment:
        # Complete, phone-correct document for direct serving (Vercel).
        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0c0e11">
<meta name="color-scheme" content="dark">
<meta name="description" content="Yield on margin per delta across NSE F&amp;O stocks.">
<meta name="robots" content="noindex">
</head>
<body>
{body}
</body>
</html>"""

    out_path.write_text(body)

    # A ~100-byte marker beside the page. The page polls THIS, not itself:
    # re-fetching an 800 KB payload every 30s just to read a timestamp would be
    # absurd, so the check costs almost nothing and can therefore be frequent.
    try:
        (out_path.parent / "version.json").write_text(json.dumps({
            "data_ts": snap.get("data_ts"),
            "generated_at": snap.get("generated_at"),
            "session_ok": snap.get("session_ok"),
        }))
    except Exception:
        pass

    size_mb = out_path.stat().st_size / 1e6
    print(f"snapshot written: {out_path}")
    print(f"  {len(snap['rows']):,} contracts · {len(snap['stocks'])} stocks · {size_mb:.2f} MB")
    if size_mb > 15:
        print("  WARNING: approaching the 16 MB artifact limit")
    return out_path


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if a != "--fragment"]
    render(args[0] if args else None, fragment="--fragment" in sys.argv)


def live_deployment_age_minutes() -> Optional[float]:
    """Minutes since the last READY production deployment, via the Vercel API.

    Used by the Mac to decide whether the cloud runner has gone quiet. Querying
    the API costs one small JSON request; downloading the 5 MB page to read its
    timestamp would not be affordable every cycle.
    """
    import os, urllib.request, time as _time
    env = config.load_env()
    token = os.environ.get("VERCEL_TOKEN") or env.get("VERCEL_TOKEN")
    org = os.environ.get("VERCEL_ORG_ID") or env.get("VERCEL_ORG_ID")
    proj = os.environ.get("VERCEL_PROJECT_ID") or env.get("VERCEL_PROJECT_ID")
    if not (token and org and proj):
        return None
    url = (f"https://api.vercel.com/v6/deployments?projectId={proj}"
           f"&teamId={org}&target=production&state=READY&limit=1")
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        deployments = data.get("deployments") or []
        if not deployments:
            return None
        created_ms = deployments[0].get("ready") or deployments[0].get("created")
        return (_time.time() - created_ms / 1000.0) / 60.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Automated deployment
# ---------------------------------------------------------------------------
def deploy(log=print, session_ok: Optional[bool] = None) -> dict:
    """Rebuild the public snapshot and push it to Vercel.

    Returns {"ok": bool, "url": str, "error": str}. Never raises -- a failed
    deploy must not take down the collector that feeds the local dashboard.
    """
    import subprocess
    try:
        config.PUBLISH_DIR.mkdir(exist_ok=True)
        render(config.PUBLISH_DIR / "index.html", session_ok=session_ok,
               fragment=False)
    except SystemExit as exc:
        return {"ok": False, "error": f"snapshot build refused: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        proc = subprocess.run(
            ["vercel", "deploy", "--prod", "--yes"],
            cwd=str(config.PUBLISH_DIR), capture_output=True, text=True,
            timeout=config.PUBLISH_TIMEOUT,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "vercel CLI not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "vercel deploy timed out"}

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return {"ok": False, "error": " / ".join(tail) or "vercel deploy failed"}
    return {"ok": True, "url": config.PUBLISH_URL}
