"""Kite session handling.

Kite access tokens expire every morning, so the flow is: log in once a day via
the browser, paste the redirect URL back, and the token is cached for the rest
of the trading day. The collector then runs headless off that cache.

Credentials are never written to disk by this module -- only the derived
access token is cached, in data/kite_token.json.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect

import config
import shared_token          # one daily Kite login shared with the live algos


def extract_request_token(text: str) -> str:
    """Accept a full redirect URL, a query string, or a bare token."""
    text = (text or "").strip()
    if "request_token=" in text:
        parsed = urlparse(text)
        found = parse_qs(parsed.query).get("request_token")
        if found:
            return found[0].strip()
        return text.split("request_token=", 1)[1].split("&", 1)[0].strip()
    return text


def _save_token(api_key: str, access_token: str) -> None:
    config.TOKEN_PATH.write_text(json.dumps({
        "api_key": api_key,
        "access_token": access_token,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))
    config.TOKEN_PATH.chmod(0o600)
    # Share it: one Kite app + one account = one token, so logging in here also
    # signs in the live algos (and vice versa — see the candidates in get_kite).
    shared_token.publish(api_key, access_token, source="deltayield")


def _load_token(api_key: str) -> Optional[str]:
    if not config.TOKEN_PATH.exists():
        return None
    try:
        blob = json.loads(config.TOKEN_PATH.read_text())
    except Exception:
        return None
    # A token minted for a different app is useless; force a fresh login.
    if blob.get("api_key") != api_key:
        return None
    return blob.get("access_token")


def get_kite(interactive: bool = False) -> KiteConnect:
    """Return an authenticated KiteConnect client.

    Tries the cached token first and validates it with a cheap profile call,
    because an expired token otherwise fails much later inside a data fetch.
    """
    env = config.load_env()
    api_key = env.get("KITE_API_KEY")
    api_secret = env.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit(
            "No Kite credentials found. Set KITE_API_KEY and KITE_API_SECRET in "
            f"{config.BASE_DIR / '.env'} (or export them)."
        )

    kite = KiteConnect(api_key=api_key)

    # Shared store first: it holds the most recent login done by ANY algo on this
    # machine, and Kite invalidates every token minted before it.
    for token in (shared_token.read(api_key), env.get("KITE_ACCESS_TOKEN"),
                  _load_token(api_key)):
        if not token:
            continue
        kite.set_access_token(token)
        try:
            kite.profile()
            _save_token(api_key, token)
            return kite
        except Exception:
            continue

    if not interactive:
        raise SystemExit(
            "Kite session expired or missing. Run:  python3 auth.py  to log in."
        )

    print("\nKite login required (tokens expire daily).")
    print("\n1) Open this URL in your browser and sign in:\n")
    print("   " + kite.login_url() + "\n")
    print("2) You will be redirected to a page that fails to load -- that is")
    print("   expected. Copy the WHOLE address bar URL and paste it below.\n")
    pasted = input("Redirect URL (or bare request_token): ")
    request_token = extract_request_token(pasted)
    if not request_token:
        raise SystemExit("No request_token found in what you pasted.")

    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]
    kite.set_access_token(access_token)
    _save_token(api_key, access_token)
    profile = kite.profile()
    print(f"\nLogged in as {profile.get('user_name')} ({profile.get('user_id')}).")
    print(f"Token cached to {config.TOKEN_PATH} -- valid until tomorrow morning.")

    # Hand it straight to the cloud runner. Without this, signing in from the
    # terminal left the cloud on yesterday's token.
    script = config.BASE_DIR / "push_token.sh"
    if script.exists():
        import subprocess
        try:
            r = subprocess.run(["/bin/bash", str(script)], capture_output=True,
                               text=True, timeout=90)
            print((r.stdout or r.stderr).strip()[:200])
        except Exception as exc:
            print(f"Could not relay the token to the cloud: {type(exc).__name__}")
    return kite


def client_from_shared_token() -> Optional[tuple]:
    """(KiteConnect, token) built from a token another algo published, or None.

    This is how a login done on the motherbot (or any algo's bot) reaches the
    running scanner: no browser round-trip, no restart — the caller just swaps
    the client in. Validated with one profile() call so a dead token is never
    installed over a live one.
    """
    env = config.load_env()
    api_key = env.get("KITE_API_KEY")
    if not api_key:
        return None
    token = shared_token.read(api_key)
    if not token:
        return None
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    try:
        kite.profile()
    except Exception:
        return None
    _save_token(api_key, token)
    return kite, token


def login_url() -> str:
    """The Kite sign-in URL for this app (no session needed to build it)."""
    env = config.load_env()
    if not env.get("KITE_API_KEY"):
        raise RuntimeError("No KITE_API_KEY configured")
    return KiteConnect(api_key=env["KITE_API_KEY"]).login_url()


def complete_login(pasted: str) -> KiteConnect:
    """Exchange a pasted redirect URL / request_token for a cached access token.

    The api_secret never leaves this machine -- the exchange happens locally and
    only the resulting access token is written to disk.
    """
    env = config.load_env()
    api_key, api_secret = env.get("KITE_API_KEY"), env.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("No Kite credentials configured")
    request_token = extract_request_token(pasted)
    if not request_token:
        raise ValueError("Could not find a request_token in what you pasted")
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    kite.set_access_token(data["access_token"])
    _save_token(api_key, data["access_token"])
    return kite


def notify_and_open(url: str, message: str) -> None:
    """Nudge the user to sign in: macOS notification + open the Kite page.

    This is the furthest the login can honestly be automated. Kite sign-in
    requires your password and 2FA; anything that stored those and typed them
    for you would be handing your broker credentials to a script, so this stops
    at putting the sign-in page in front of you.
    """
    import subprocess
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "Delta Yield"'],
            capture_output=True, timeout=10)
    except Exception:
        pass
    try:
        subprocess.run(["open", url], capture_output=True, timeout=10)
    except Exception:
        pass


def session_status() -> dict:
    """Non-raising probe used by the dashboard to show session health."""
    try:
        env = config.load_env()
        api_key = env.get("KITE_API_KEY")
        if not api_key:
            return {"ok": False, "reason": "no_credentials"}
        token = (shared_token.read(api_key) or env.get("KITE_ACCESS_TOKEN")
                 or _load_token(api_key))
        if not token:
            return {"ok": False, "reason": "no_token"}
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(token)
        profile = kite.profile()
        return {"ok": True, "user": profile.get("user_name"), "user_id": profile.get("user_id")}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__, "detail": str(exc)[:160]}


if __name__ == "__main__":
    get_kite(interactive=True)
    sys.exit(0)
