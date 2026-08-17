"""Out-of-band alerts.

The dashboard can only tell you the session died if you happen to be looking at
it. These channels reach you when you are not.

Telegram credentials are reused from the intraday_strangle bot (same personal
chat), so there is nothing new to set up. Messages go only to your own chat id.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import config

_TELEGRAM_SOURCES = [
    config.BASE_DIR / ".env",
    Path.home() / "Desktop" / "Claude" / "Finance" / "intraday_strangle" / ".env",
    Path.home() / "Desktop" / "Claude" / "Finance" / "tsl_engine" / ".env",
]


def _telegram_creds() -> Optional[tuple]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return token, chat
    for path in _TELEGRAM_SOURCES:
        if not path.exists():
            continue
        vals = {}
        try:
            content = path.read_text()
        except OSError:
            # macOS TCC denies a background LaunchAgent access to ~/Desktop, and
            # an unguarded read here once escaped all the way out of the Kite
            # sign-in handler as "Sign-in failed: PermissionError".
            continue
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
        if vals.get("TELEGRAM_BOT_TOKEN") and vals.get("TELEGRAM_CHAT_ID"):
            return vals["TELEGRAM_BOT_TOKEN"], vals["TELEGRAM_CHAT_ID"]
    return None


def telegram(message: str, log=print) -> bool:
    """Send a message to your own Telegram chat. Never raises."""
    if not config.TELEGRAM_ENABLED:
        return False
    creds = _telegram_creds()
    if not creds:
        return False
    token, chat = creds
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            log(f"telegram send failed: HTTP {resp.status_code}")
            return False
        return True
    except Exception as exc:
        log(f"telegram send failed: {type(exc).__name__}")
        return False


def session_expired(log=print) -> bool:
    dash = f"http://{config.SERVER_HOST}:{config.SERVER_PORT}/"
    return telegram(
        "<b>Delta Yield — Kite session expired</b>\n"
        "The scanner has stopped refreshing and the public link is frozen.\n\n"
        f"Sign in here to resume: {dash}\n"
        "(The sign-in page should have opened on your Mac already.)",
        log=log)


def session_restored(user: str = "", log=print) -> bool:
    return telegram(
        f"<b>Delta Yield — back online</b>\n"
        f"Signed in{(' as ' + user) if user else ''}. "
        f"Live refresh and public publishing have resumed.",
        log=log)


if __name__ == "__main__":
    ok = telegram("Delta Yield: notification test — you can ignore this.")
    print("sent" if ok else "not sent (no creds, or TELEGRAM_ENABLED is off)")
