"""Configuration for the FnO delta-yield scanner.

Single source of truth for every tunable. Edit the values here (or override via
config.json in the same directory) rather than scattering constants in code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "chain.sqlite"
TOKEN_PATH = DATA_DIR / "kite_token.json"

# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------
# Every index traded on NFO/BFO. These are hard-excluded: this scanner is
# stocks-only. Anything whose `name` matches one of these is dropped, and as a
# second line of defence we also require the underlying to have an NSE equity
# listing (see universe.build_universe).
INDEX_NAMES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "SENSEX50", "NIFTYIT", "NIFTYINFRA",
}

# How many monthly expiries to scan, nearest first. 2 = current + next.
NUM_EXPIRIES = 2

# --------------------------------------------------------------------------
# Delta band
# --------------------------------------------------------------------------
# Contracts outside this absolute-delta band are still stored (so the chain is
# complete in the DB) but are excluded from margin fetching and from the
# headline ranking, because they are not sensible short candidates.
DELTA_MIN = 0.05
DELTA_MAX = 0.50

# Buckets used by the heatmap view. Each contract is snapped to the nearest
# bucket centre within BUCKET_TOLERANCE.
DELTA_BUCKETS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
BUCKET_TOLERANCE = 0.025  # +/- around bucket centre

# Strikes further than this fraction from the future price are not even quoted.
# Measured on the live chain, a 0.45 window excluded only 183 of 23,696
# contracts (0.8%) -- it bought no meaningful speed while genuinely truncating
# the 0.05-delta tail for 11 (stock, expiry, side) groups. Widened to 0.90,
# which keeps a guard against pathological strike ladders while excluding
# nothing that could fall inside the delta band.
MONEYNESS_WINDOW = 0.90
TICK_SIZE = 0.05

# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------
RISK_FREE_RATE = 0.065        # used only for discounting; delta is F-based
MARKET_CLOSE_HOUR = 15        # expiry timestamp is 15:30 IST on expiry date
MARKET_CLOSE_MINUTE = 30
IV_MIN = 0.01
IV_MAX = 5.00

# --------------------------------------------------------------------------
# Liquidity — used for FLAGS ONLY. Nothing is hidden from the dashboard.
# --------------------------------------------------------------------------
SPREAD_WARN_PCT = 0.05        # bid-ask spread > 5% of mid  -> amber
SPREAD_BAD_PCT = 0.15         # > 15% of mid                -> red
OI_WARN_LOTS = 100            # OI below this many lots      -> amber
OI_BAD_LOTS = 20              # below this                   -> red
MIN_VOLUME_WARN = 100         # today's traded qty           -> amber

# --------------------------------------------------------------------------
# Refresh scheduling (IST)
# --------------------------------------------------------------------------
REFRESH_SECONDS = 300         # 5 minutes, measured cycle-start to cycle-start
MIN_CYCLE_GAP_SECONDS = 20    # breathing room if a cycle overruns the interval
MARKET_OPEN = "09:15"
MARKET_END = "15:30"
TRADE_WEEKDAYS = {0, 1, 2, 3, 4}   # Mon-Fri

# --------------------------------------------------------------------------
# API pacing (Kite published limits)
# --------------------------------------------------------------------------
QUOTE_BATCH = 400             # instruments per /quote call (limit 500)
QUOTE_RATE_SLEEP = 1.05       # /quote is 1 req/sec
MARGIN_BATCH = 50             # orders per margin call
# "basket" uses basket_order_margins(consider_positions=False) -> the standalone
# margin a fresh short would block, unaffected by positions you already hold.
# "orders" uses /margins/orders, which nets against existing positions.
# Run `python3 verify.py` to confirm the two agree on this account.
MARGIN_MODE = "basket"
MARGIN_RATE_SLEEP = 0.12      # order endpoints are 10 req/sec
MAX_RETRIES = 3

# --------------------------------------------------------------------------
# Public auto-publish (Vercel)
# --------------------------------------------------------------------------
# After each successful market-hours cycle the snapshot is rebuilt and deployed
# so the shared link stays current.
#
# Vercel's Hobby plan allows 100 deployments per DAY. Market hours are 375
# minutes, so a 5-minute cadence is 75 deploys -- under the cap but with little
# room for manual deploys. PUBLISH_DAILY_BUDGET is a hard stop: once hit, the
# publisher backs off for the rest of the day instead of failing noisily or
# eating into limits you may need elsewhere.
# TWO INDEPENDENT PUBLISHERS, so a stall in either one is invisible to you.
#
# The cloud runner is primary. But GitHub's scheduled workflows are queued at
# low priority and get dropped under load -- measured gaps of 18, 37, 50 and 76
# minutes on a nominal 30-minute schedule. Relying on it alone leaves the page
# frozen for over an hour.
#
# So the Mac publishes too, in "gapfill" mode: before each publish it asks the
# Vercel API how old the live deployment is, and only deploys if the cloud has
# gone quiet. Whichever machine is available covers the gap, and they never
# double-publish.
PUBLISH_ENABLED = True
PUBLISH_MODE = "gapfill"          # "always" | "gapfill" | "off"
PUBLISH_GAPFILL_MINUTES = 11      # publish only if the live page is older
# Must sit BELOW the cycle period, or a cycle finishing slightly early gets its
# publish throttled away and the public page lands on every OTHER cycle.
PUBLISH_INTERVAL_SECONDS = 210
PUBLISH_DAILY_BUDGET = 85
PUBLISH_DIR = BASE_DIR / "site"
PUBLISH_URL = "https://delta-yield.vercel.app"
PUBLISH_TIMEOUT = 240
# Publish once more right after the session dies, so the public page can carry
# an honest "data is frozen" banner instead of silently going stale.
PUBLISH_ON_STALE = True
# Consider the public snapshot stale after this many minutes of market time.
STALE_AFTER_MINUTES = 12

# How many stocks get their structure menu pre-priced each cycle, and how long
# that may take. Runs on a background thread, so it never delays a data cycle,
# but it shares the Kite rate limit -- hence the time box.
STRATEGY_STOCK_LIMIT = 60
# Must fit inside the idle gap between cycles (period minus cycle duration),
# because it now holds RUN_LOCK to avoid competing with the collector.
STRATEGY_BUDGET_SECONDS = 90
STRATEGY_INTERVAL_SECONDS = 600   # own timer, never in front of a publish

# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
# Reuses the intraday_strangle Telegram bot and your own chat id.
TELEGRAM_ENABLED = True

# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8777

# Credentials are read from the environment or a .env file. Never hardcode.
# This project lives outside ~/Desktop so that a background LaunchAgent can read
# it (macOS blocks agents from the protected Desktop/Documents/Downloads
# folders), so the sibling trading projects are referenced by absolute path
# rather than relative to BASE_DIR.
_FINANCE_DIR = Path.home() / "Desktop" / "Claude" / "Finance"
ENV_CANDIDATES = [
    BASE_DIR / ".env",
    _FINANCE_DIR / "intraday_strangle" / ".env",
    _FINANCE_DIR / "tsl_engine" / ".env",
]


def _load_overrides() -> dict:
    path = BASE_DIR / "config.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


# Apply config.json overrides onto module globals.
for _k, _v in _load_overrides().items():
    if _k.upper() in globals():
        globals()[_k.upper()] = _v


def _parse_env_file(path: Path) -> dict:
    out = {}
    try:
        content = path.read_text()
    except OSError:
        # Same TCC trap as notify.py: exists() can succeed where read() is denied.
        return out
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        # Also carry VERCEL_* and TELEGRAM_*: the freshness probe and the
        # alerting both read from the same .env, and filtering to KITE_* alone
        # silently returned no Vercel token.
        if val and key.startswith(("KITE_", "VERCEL_", "TELEGRAM_")):
            out[key] = val
    return out


def load_env() -> dict:
    """Read KITE_* credentials, preferring the real environment.

    api_key and api_secret must come from the *same* Kite app, so credentials
    are taken atomically from the first .env file that supplies a key/secret
    pair -- never merged field-by-field across files (the .env files in sibling
    project directories belong to different Kite apps).
    """
    env = {}
    if os.environ.get("KITE_API_KEY") and os.environ.get("KITE_API_SECRET"):
        env = {
            "KITE_API_KEY": os.environ["KITE_API_KEY"],
            "KITE_API_SECRET": os.environ["KITE_API_SECRET"],
        }
    else:
        for path in ENV_CANDIDATES:
            if not path.exists():
                continue
            parsed = _parse_env_file(path)
            if parsed.get("KITE_API_KEY") and parsed.get("KITE_API_SECRET"):
                env = parsed
                env["_source"] = str(path)
                break
    if os.environ.get("KITE_ACCESS_TOKEN"):
        env["KITE_ACCESS_TOKEN"] = os.environ["KITE_ACCESS_TOKEN"]
    return env


DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
