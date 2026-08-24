"""Cross-algo Kite access-token sharing.

One Kite app (api_key) + one Zerodha account = ONE access token. Every algo on
this machine can use the same one, so you only ever do the daily /login on one
Telegram bot: that bot publishes the token here, and the other algos adopt it on
their next refresh (engine loop / bot poll / process start).

Deliberately dependency-free and tiny so it can be dropped into any algo folder.

File: ~/.kite_shared/token.json (0600, dir 0700) — override with
$KITE_SHARED_TOKEN_FILE. Contents: {"api_key", "access_token", "ts", "source"}.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_PATH = Path.home() / ".kite_shared" / "token.json"
# Kite invalidates access tokens at ~06:00 IST each morning, so a token published
# before today's 06:00 boundary can never still be live — treat it as absent.
_EXPIRY_HOUR = 6


def token_path() -> Path:
    return Path(os.environ.get("KITE_SHARED_TOKEN_FILE") or DEFAULT_PATH)


def _session_start(now: datetime) -> datetime:
    """Most recent 06:00 IST boundary — tokens published before it are dead."""
    boundary = now.replace(hour=_EXPIRY_HOUR, minute=0, second=0, microsecond=0)
    return boundary if now >= boundary else boundary - timedelta(days=1)


def _load() -> dict | None:
    try:
        with open(token_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read(api_key: str, now: datetime | None = None) -> str | None:
    """Today's shared access token for `api_key`, or None if absent/stale/other app."""
    data = _load()
    if not data:
        return None
    if str(data.get("api_key") or "") != str(api_key or ""):
        return None            # a different Kite app — its token would not authenticate
    token = str(data.get("access_token") or "").strip()
    if not token:
        return None
    try:
        ts = datetime.fromtimestamp(float(data["ts"]), IST)
    except (KeyError, TypeError, ValueError, OSError):
        return None
    if ts < _session_start(now or datetime.now(IST)):
        return None            # published before this morning's expiry boundary
    return token


def publish(api_key: str, access_token: str, source: str = "") -> bool:
    """Share a freshly generated access token with every other algo. Best-effort:
    never let a sharing failure break the login that just succeeded."""
    if not api_key or not access_token:
        return False
    path = token_path()
    payload = {
        "api_key": str(api_key),
        "access_token": str(access_token),
        "ts": datetime.now(IST).timestamp(),
        "source": source or Path(__file__).resolve().parent.name,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        # atomic replace so a reader never sees a half-written token
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".token-")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def published_at(api_key: str | None = None) -> datetime | None:
    """When the shared token was written (for status messages), or None."""
    data = _load()
    if not data:
        return None
    if api_key is not None and str(data.get("api_key") or "") != str(api_key):
        return None
    try:
        return datetime.fromtimestamp(float(data["ts"]), IST)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def source(api_key: str | None = None) -> str | None:
    """Which algo published the shared token (for status messages)."""
    data = _load()
    if not data:
        return None
    if api_key is not None and str(data.get("api_key") or "") != str(api_key):
        return None
    return str(data.get("source") or "") or None
