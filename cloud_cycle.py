"""One collection cycle for a headless runner (GitHub Actions).

The local dashboard is a long-lived server; CI is the opposite -- a fresh,
stateless container per run. So this does exactly one pass and exits:

    market-hours check -> collect -> price structures -> render -> deploy

Deliberate differences from the local server:

  * **No interactive login.** It authenticates from KITE_API_KEY plus a
    KITE_ACCESS_TOKEN handed in as a secret. Your api_secret is never needed
    here and never leaves your Mac -- only the token, which Zerodha expires
    every morning anyway.
  * **No persistent database.** `latest` is rebuilt from scratch every cycle,
    which is all the published page needs. The watch list is the one piece of
    real state, so it is read from watchlist.json in the repo.
  * **Fails loudly.** A CI run that silently publishes nothing is worse than a
    red build, so anything unexpected exits non-zero.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
import db
import pricing
import publish
import queries
import strategies
import universe as universe_mod
from kiteconnect import KiteConnect

WATCHLIST_FILE = config.BASE_DIR / "watchlist.json"


def log(msg: str) -> None:
    print(f"[{datetime.now(pricing.IST).strftime('%H:%M:%S')}] {msg}", flush=True)


def kite_from_env() -> KiteConnect:
    api_key = os.environ.get("KITE_API_KEY")
    token = os.environ.get("KITE_ACCESS_TOKEN")
    if not api_key or not token:
        raise SystemExit("KITE_API_KEY and KITE_ACCESS_TOKEN must be set")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    profile = kite.profile()          # fails fast if the token died overnight
    log(f"kite session ok for {profile.get('user_name')} ({profile.get('user_id')})")
    return kite


def load_watchlist() -> None:
    """Seed the ephemeral DB with the watch list committed to the repo."""
    if not WATCHLIST_FILE.exists():
        return
    try:
        names = json.loads(WATCHLIST_FILE.read_text())
        if isinstance(names, list) and names:
            db.watchlist_set([str(n).upper() for n in names])
            log(f"watch list: {len(names)} stocks from watchlist.json")
    except Exception as exc:
        log(f"could not read watchlist.json: {type(exc).__name__}")


def deploy_to_vercel() -> None:
    token = os.environ.get("VERCEL_TOKEN")
    if not token:
        raise SystemExit("VERCEL_TOKEN not set")
    # VERCEL_ORG_ID / VERCEL_PROJECT_ID in the environment make the CLI target
    # the existing project without a committed .vercel directory.
    proc = subprocess.run(
        ["npx", "--yes", "vercel@latest", "deploy", "--prod", "--yes",
         "--token", token],
        cwd=str(config.PUBLISH_DIR), capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise SystemExit("vercel deploy failed:\n" + "\n".join(tail))
    log(f"deployed to {config.PUBLISH_URL}")


def check_vercel_token() -> None:
    """Validate the deploy token BEFORE doing five minutes of collection.

    A bad token used to surface only at the very end, after the whole chain had
    been fetched and priced -- wasting the run and the Kite rate limit. Note the
    token must be a Vercel *personal API token*; the `vca_` value the local CLI
    stores is a short-lived OAuth session token and is rejected by --token.
    """
    import urllib.request
    token = os.environ.get("VERCEL_TOKEN", "")
    if not token:
        raise SystemExit("VERCEL_TOKEN secret is not set")
    if token.startswith("vca_"):
        raise SystemExit(
            "VERCEL_TOKEN looks like a CLI session token (vca_...). Create a "
            "personal API token at https://vercel.com/account/tokens and set it "
            "with:  gh secret set VERCEL_TOKEN --repo Aryanye/delta-yield")
    # Probe the PROJECT, not /v2/user: a team-scoped token has no user-level
    # access and 404s there even when it is perfectly able to deploy.
    org = os.environ.get("VERCEL_ORG_ID", "")
    proj = os.environ.get("VERCEL_PROJECT_ID", "")
    if not org or not proj:
        raise SystemExit("VERCEL_ORG_ID and VERCEL_PROJECT_ID must be set")
    req = urllib.request.Request(
        f"https://api.vercel.com/v9/projects/{proj}?teamId={org}",
        headers={"Authorization": f"Bearer {token}"})
    try:
        info = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as exc:
        raise SystemExit(
            f"VERCEL_TOKEN cannot reach the target project ({exc}). Create a "
            f"personal API token scoped to the team at "
            f"https://vercel.com/account/tokens")
    log(f"vercel token ok for project {info.get('name')}")


def main() -> int:
    force = "--force" in sys.argv
    mkt = queries.market_status()
    if not mkt["open"] and not force:
        log(f"market closed ({mkt['now']}) — nothing to do")
        return 0

    check_vercel_token()      # fail in seconds, not after a full collection
    db.init()
    load_watchlist()

    kite = kite_from_env()
    uni = universe_mod.build_universe()
    log(f"universe: {len(uni.stocks)} stocks, {len(uni.options)} contracts")

    from collector import collect
    result = collect(kite, uni, log=log)
    log(f"collected {result['rows']} rows, {result['margined']} with margin")

    expiry = (queries.expiries_list() or [None])[0]
    if expiry and os.environ.get("SKIP_STRATEGIES") != "1":
        strategies.precompute(
            kite, expiry, log=log,
            limit=int(os.environ.get("STRATEGY_LIMIT", config.STRATEGY_STOCK_LIMIT)),
            budget_seconds=float(os.environ.get("STRATEGY_BUDGET", 120)))

    config.PUBLISH_DIR.mkdir(exist_ok=True)
    publish.render(config.PUBLISH_DIR / "index.html", session_ok=True, fragment=False)
    # vercel.json lives in the repo and is copied alongside index.html so the
    # deployed site keeps its no-cache headers.
    src = config.BASE_DIR / "vercel.site.json"
    if src.exists():
        (config.PUBLISH_DIR / "vercel.json").write_text(src.read_text())

    deploy_to_vercel()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"::error::cycle failed: {type(exc).__name__}: {exc}")
        sys.exit(1)
