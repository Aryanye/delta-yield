"""Dashboard HTTP server + market-hours refresh scheduler.

Single process: a threaded stdlib HTTP server for the UI/API, plus a background
thread that runs a collection cycle every REFRESH_SECONDS while the market is
open. No external web framework, so there is nothing to install and nothing to
keep in sync.

Binds to 127.0.0.1 by default -- this serves your live broker data, so it is
local-only unless you deliberately change SERVER_HOST.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import auth
import collector
import config
import db
import notify
import pricing
import publish
import queries
import strategies
import universe as universe_mod

STATIC_DIR = config.BASE_DIR / "static"

STATE = {
    "status": "idle",          # idle | running | ok | error
    "last_run": None,
    "last_error": None,
    "last_result": None,
    "next_run": None,
    "log": [],
    "publish": {"last_ok": None, "last_error": None, "count_today": 0,
                "day": None, "url": config.PUBLISH_URL, "status": "idle"},
    "auth_expired": False,
    "auth_nudged": False,
}
# RLock, not Lock: helpers such as log() take this lock internally, so any code
# that logs while holding it would self-deadlock and wedge every HTTP handler.
STATE_LOCK = threading.RLock()
RUN_LOCK = threading.Lock()
PUBLISH_LOCK = threading.Lock()
STRATEGY_LOCK = threading.Lock()


def log(msg: str) -> None:
    line = f"[{datetime.now(pricing.IST).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with STATE_LOCK:
        STATE["log"].append(line)
        del STATE["log"][:-200]


def run_cycle(kite, uni) -> None:
    """Run one collection cycle, guarded so two never overlap."""
    if kite is None:
        log("no Kite session -- run `python3 auth.py` then hit Refresh")
        with STATE_LOCK:
            STATE.update(status="error", last_error="No Kite session. Run: python3 auth.py")
        return
    if not RUN_LOCK.acquire(blocking=False):
        log("refresh already in progress -- skipping this trigger")
        return
    try:
        with STATE_LOCK:
            STATE["status"] = "running"
        result = collector.collect(kite, uni, log=log)
        with STATE_LOCK:
            STATE.update(status="ok", last_run=result["ts"], last_result=result,
                         last_error=None, auth_expired=False, auth_nudged=False)
    except collector.AuthExpired as exc:
        log(f"KITE SESSION EXPIRED: {exc}")
        log("previous snapshot preserved -- sign in to resume")
        nudge = False
        with STATE_LOCK:
            STATE.update(status="error", last_error=f"Kite session expired: {exc}",
                         auth_expired=True)
            if not STATE["auth_nudged"]:
                STATE["auth_nudged"] = True
                nudge = True
        # Everything below runs once per expiry, not on every failed cycle.
        if nudge:
            try:
                auth.notify_and_open(
                    auth.login_url(),
                    "Kite session expired - sign in to resume the scanner")
                log("opened the Kite sign-in page")
            except Exception:
                pass
            if notify.session_expired(log=log):
                log("telegram alert sent")
            # Push one more deploy so the PUBLIC page shows a stale banner
            # rather than quietly serving yesterday's numbers as if current.
            if config.PUBLISH_ON_STALE:
                threading.Thread(
                    target=publish_public,
                    kwargs={"force": True, "session_ok": False},
                    daemon=True).start()
    except Exception as exc:
        tb = traceback.format_exc(limit=3)
        log(f"CYCLE FAILED: {type(exc).__name__}: {exc}")
        with STATE_LOCK:
            STATE.update(status="error", last_error=f"{type(exc).__name__}: {exc}\n{tb}")
    finally:
        RUN_LOCK.release()


def relay_token_to_cloud() -> None:
    """Hand today's Kite token to the cloud runner after a successful login.

    This is the join between the two halves: the interactive sign-in (password,
    2FA, api_secret) stays on this Mac, and only the derived access token -- good
    until tomorrow morning -- is pushed to the GitHub secret the scheduled
    collector reads. Without this the cloud goes dark the moment the token rolls.
    """
    script = config.BASE_DIR / "push_token.sh"
    if not script.exists():
        return
    try:
        proc = subprocess.run(["/bin/bash", str(script)], capture_output=True,
                              text=True, timeout=90)
        if proc.returncode == 0:
            log("relayed today's Kite token to the cloud runner")
        else:
            log(f"token relay failed: {(proc.stderr or proc.stdout).strip()[:120]}")
    except Exception as exc:
        log(f"token relay failed: {type(exc).__name__}: {str(exc)[:80]}")


def _token_fingerprint() -> str:
    """Hash of the cached access token, so we can tell when it changes."""
    import hashlib
    try:
        blob = json.loads(config.TOKEN_PATH.read_text())
        return hashlib.sha256(blob.get("access_token", "").encode()).hexdigest()
    except Exception:
        return ""


def token_relay_watcher() -> None:
    """Relay the token to the cloud whenever it changes, by ANY route.

    The relay used to hang off the dashboard's login handlers only. Sign in with
    `python3 auth.py` instead and the cloud kept the previous day's token: it
    died on TokenException, the Mac's gap-filler became the only publisher, and
    the page updated on the gap-fill interval rather than the real cadence.
    Watching the token file catches every path -- dashboard, terminal, or a
    token dropped in by hand.
    """
    marker = config.DATA_DIR / "last_relayed.txt"
    while True:
        try:
            fp = _token_fingerprint()
            if fp:
                last = marker.read_text().strip() if marker.exists() else ""
                if fp != last:
                    log("cached Kite token changed — relaying to the cloud")
                    relay_token_to_cloud()
                    marker.write_text(fp)
        except Exception as exc:
            log(f"token watcher error: {type(exc).__name__}: {str(exc)[:70]}")
        time.sleep(60)


def precompute_strategies(kite) -> None:
    """Price the structure menu in the background, off the data cycle.

    The published snapshot has no server to call, so structures must already be
    priced by the time it is built -- this is what makes the Opportunities tab
    work on the shared link.
    """
    if kite is None or not STRATEGY_LOCK.acquire(blocking=False):
        return
    try:
        # Take RUN_LOCK so this never overlaps a collection cycle. Both hammer
        # the same 10 req/s Kite order-margin endpoint, and when they ran
        # concurrently they throttled each other badly enough to stretch a 140s
        # cycle to 579s -- which is what wrecked the publish cadence.
        if not RUN_LOCK.acquire(blocking=False):
            return
        try:
            expiry = (queries.expiries_list() or [None])[0]
            if not expiry:
                return
            strategies.precompute(kite, expiry, log=log,
                                  limit=config.STRATEGY_STOCK_LIMIT,
                                  budget_seconds=config.STRATEGY_BUDGET_SECONDS)
        finally:
            RUN_LOCK.release()
    except Exception as exc:
        log(f"strategy precompute failed: {type(exc).__name__}: {str(exc)[:90]}")
    finally:
        STRATEGY_LOCK.release()


def maybe_publish(session_ok: bool = True) -> None:
    """Publish if the last successful deploy is older than the interval.

    Publishing is throttled on the WALL CLOCK rather than chained behind the
    cycle or the strategy precompute. Chaining it made deploys inherit every
    upstream delay -- a slow precompute simply swallowed the publish, and the
    public page silently fell 10+ minutes behind the local one.
    """
    # Gap-fill: defer to the cloud runner unless it has actually gone quiet.
    if getattr(config, "PUBLISH_MODE", "always") == "gapfill":
        age = publish.live_deployment_age_minutes()
        if age is not None and age < config.PUBLISH_GAPFILL_MINUTES:
            return
        if age is not None:
            log(f"live page is {age:.0f} min old — cloud runner is late, "
                f"publishing from here")

    last = None
    with STATE_LOCK:
        last = STATE["publish"].get("last_ok")
    if last:
        try:
            age = (datetime.now(pricing.IST)
                   - datetime.fromisoformat(last)).total_seconds()
            if age < config.PUBLISH_INTERVAL_SECONDS:
                return
        except Exception:
            pass
    publish_public(session_ok=session_ok)


def strategy_loop(httpd) -> None:
    """Re-price structures on their own timer, independent of publishing."""
    while True:
        try:
            if queries.market_status()["open"]:
                precompute_strategies(httpd.kite)
        except Exception as exc:
            log(f"strategy loop error: {type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(config.STRATEGY_INTERVAL_SECONDS)


def publish_public(force: bool = False, session_ok: bool = True) -> None:
    """Rebuild and deploy the public snapshot, respecting the daily budget."""
    if not config.PUBLISH_ENABLED and not force:
        return
    if not PUBLISH_LOCK.acquire(blocking=False):
        return                       # a deploy is already in flight
    try:
        today = datetime.now(pricing.IST).strftime("%Y-%m-%d")
        pending_log = None
        with STATE_LOCK:
            pub = STATE["publish"]
            if pub["day"] != today:          # new trading day, reset the budget
                pub.update(day=today, count_today=0)
            over_budget = (not force
                           and pub["count_today"] >= config.PUBLISH_DAILY_BUDGET)
            if over_budget:
                if pub["status"] != "budget_exhausted":
                    pending_log = (f"public publish paused: hit the daily budget "
                                   f"of {config.PUBLISH_DAILY_BUDGET} deploys "
                                   f"(Vercel Hobby allows 100/day)")
                pub["status"] = "budget_exhausted"
            else:
                pub["status"] = "publishing"
        if pending_log:
            log(pending_log)
        if over_budget:
            return

        result = publish.deploy(log=lambda m: None, session_ok=session_ok)

        with STATE_LOCK:
            pub = STATE["publish"]
            if result.get("ok"):
                pub["count_today"] += 1
                pub["last_ok"] = datetime.now(pricing.IST).isoformat(timespec="seconds")
                pub["last_error"] = None
                pub["status"] = "ok"
                msg = (f"published to {result['url']} "
                       f"({pub['count_today']}/{config.PUBLISH_DAILY_BUDGET} today)")
            else:
                pub["last_error"] = result.get("error")
                pub["status"] = "error"
                msg = f"public publish FAILED: {result.get('error')}"
        log(msg)
    finally:
        PUBLISH_LOCK.release()


def adopt_shared_token(httpd) -> bool:
    """Pick up a Kite login done elsewhere (motherbot, or any algo's bot).

    The scanner's own sign-in paths hot-swap httpd.kite themselves; this covers
    the case where somebody signed in somewhere else entirely, so the daily login
    only ever has to happen once.
    """
    current = getattr(httpd, "kite_token", None)
    try:
        found = auth.client_from_shared_token()
    except Exception as exc:
        log(f"shared-token check failed: {type(exc).__name__}: {exc}")
        return False
    if not found:
        return False
    kite, token = found
    if token == current and httpd.kite is not None:
        return False
    httpd.kite = kite
    httpd.kite_token = token
    with STATE_LOCK:
        STATE.update(auth_expired=False, auth_nudged=False)
    who = auth.session_status()
    log(f"adopted a shared Kite token — signed in as {who.get('user')}")
    return True


def scheduler(httpd, uni, force_once: bool = True) -> None:
    """Refresh every REFRESH_SECONDS during market hours.

    Reads httpd.kite on every tick rather than capturing it once, so a login
    performed from the dashboard takes effect on the very next cycle.
    """
    if force_once:
        log("running an initial cycle at startup ...")
        adopt_shared_token(httpd)
        run_cycle(httpd.kite, uni)

    while True:
        started = time.time()
        adopt_shared_token(httpd)
        mkt = queries.market_status()
        if mkt["open"]:
            run_cycle(httpd.kite, uni)
            # Deploy on a separate thread so a slow Vercel build never delays
            # the next data cycle.
            with STATE_LOCK:
                ok = STATE["status"] == "ok"
            if ok and config.PUBLISH_ENABLED:
                # Straight to publish. Structures are refreshed by their own
                # loop and picked up by whichever publish comes next, so at
                # worst they trail the prices by one cycle.
                threading.Thread(target=maybe_publish, daemon=True).start()

            # Sleep the REMAINDER of the interval, measured from when the cycle
            # started. Sleeping a full REFRESH_SECONDS *after* the work made the
            # real period cycle_duration + 300s -- with a ~215s sweep that is
            # ~8.6 minutes, not the 5 the dashboard advertises.
            elapsed = time.time() - started
            sleep_for = max(config.MIN_CYCLE_GAP_SECONDS,
                            config.REFRESH_SECONDS - elapsed)
            if elapsed > config.REFRESH_SECONDS:
                log(f"cycle took {elapsed:.0f}s, longer than the "
                    f"{config.REFRESH_SECONDS}s interval — running back to back")
            else:
                log(f"next cycle in {sleep_for:.0f}s "
                    f"(cycle {elapsed:.0f}s of a {config.REFRESH_SECONDS}s period)")
        else:
            # Outside market hours nothing moves; idle cheaply and re-check.
            sleep_for = 60
        with STATE_LOCK:
            STATE["next_run"] = (
                datetime.now(pricing.IST).timestamp() + sleep_for)
        time.sleep(sleep_for)


class Handler(BaseHTTPRequestHandler):
    server_version = "FnODeltaYield/1.0"

    def log_message(self, fmt, *args):    # silence per-request console spam
        pass

    # -- helpers ----------------------------------------------------------
    def handle_one_request(self):
        # A phone that backgrounds mid-download, or a reload that cancels an
        # in-flight 5 MB response, closes the socket under us. That is normal
        # client behaviour, not a server fault -- swallow it instead of dumping
        # a traceback per occurrence.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        try:
            self._send_inner(body, ctype, code)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_inner(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, default=str).encode(), "application/json", code)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if route in ("/", "/index.html"):
                return self._static("dashboard.html", "text/html; charset=utf-8")
            if route.startswith("/static/"):
                name = route[len("/static/"):]
                ctype = ("text/css" if name.endswith(".css")
                         else "application/javascript" if name.endswith(".js")
                         else "text/html; charset=utf-8" if name.endswith(".html")
                         else "text/plain")
                return self._static(name, ctype)

            if route == "/api/summary":
                data = queries.summary()
                with STATE_LOCK:
                    data["runner"] = {k: STATE[k] for k in
                                      ("status", "last_run", "last_error", "next_run")}
                    data["publish"] = dict(STATE["publish"])
                    data["auth_expired"] = STATE["auth_expired"]
                return self._json(data)
            if route == "/api/heatmap":
                return self._json(queries.heatmap(
                    side=q.get("side", "PE"), expiry=q.get("expiry") or None,
                    liquidity=q.get("liquidity", "all"),
                    quality=q.get("quality", "ok")))
            if route == "/api/top":
                return self._json(queries.top(
                    limit=int(q.get("limit", 300)), side=q.get("side") or None,
                    expiry=q.get("expiry") or None,
                    dmin=float(q.get("dmin", config.DELTA_MIN)),
                    dmax=float(q.get("dmax", config.DELTA_MAX)),
                    liquidity=q.get("liquidity", "all"),
                    min_oi_lots=float(q.get("min_oi_lots", 0)),
                    name=q.get("name") or None,
                    sort=q.get("sort", "return_pct"),
                    quality=q.get("quality", "ok")))
            if route == "/api/chain":
                if not q.get("name"):
                    return self._json({"error": "name required"}, 400)
                return self._json(queries.chain(q["name"], q.get("expiry") or None))
            if route == "/api/events":
                return self._json({"events": queries.events_upcoming(
                    days=int(q.get("days", 45)),
                    confidence=q.get("confidence") or None)})
            if route == "/api/strategies":
                name = (q.get("name") or "").upper()
                if not name:
                    return self._json({"error": "name required"}, 400)
                expiry = q.get("expiry") or (queries.expiries_list() or [None])[0]
                stance = q.get("stance", "neutral")
                if stance not in strategies.STANCES:
                    return self._json({"error": "bad stance"}, 400)
                cached = db.read_strategies(name=name, expiry=expiry)
                hit = next((c for c in cached if c["stance"] == stance), None)
                if hit:
                    return self._json({
                        "name": name, "expiry": expiry, "stance": stance,
                        "stance_desc": strategies.STANCES[stance],
                        "events": queries.events_for(name),
                        "computed_at": hit["computed_at"],
                        "structures": json.loads(hit["payload"])})

                sts = strategies.candidates(name, expiry, stance)
                if not sts:
                    return self._json({"name": name, "expiry": expiry,
                                       "stance": stance, "structures": []})
                # Margins need a live session; without one the structures are
                # still returned, just without a return-on-margin figure.
                if self.server.kite is not None:
                    strategies.price_margins(self.server.kite, sts, log=log)
                return self._json({
                    "name": name, "expiry": expiry, "stance": stance,
                    "stance_desc": strategies.STANCES[stance],
                    "events": queries.events_for(name),
                    "structures": [strategies.to_dict(x) for x in sts]})
            if route == "/api/watchlist":
                return self._json({"watchlist": db.watchlist_get()})
            if route == "/api/watchlist/heatmap":
                return self._json(queries.watchlist_heatmap(
                    side=q.get("side", "PE"), expiry=q.get("expiry") or None,
                    liquidity=q.get("liquidity", "all"),
                    quality=q.get("quality", "ok")))
            if route == "/api/stocks":
                return self._json(queries.stock_list())
            if route == "/api/history":
                return self._json(queries.bucket_history(
                    q.get("name", ""), float(q.get("bucket", 0.2)),
                    q.get("side", "PE"), int(q.get("hours", 24))))
            if route == "/kite/callback":
                # Zerodha redirects here after sign-in when the app's redirect
                # URL is set to http://127.0.0.1:8777/kite/callback. The token
                # is captured straight from the query string -- nothing to
                # copy, nothing to paste.
                token = q.get("request_token", "")
                if q.get("status") and q["status"] != "success":
                    body = self._login_result_page(
                        False, f"Kite reported status={q['status']}")
                    return self._send(body, "text/html; charset=utf-8")
                if not token:
                    return self._send(
                        self._login_result_page(False, "No request_token in the redirect"),
                        "text/html; charset=utf-8")
                try:
                    kite = auth.complete_login(token)
                    self.server.kite = kite
                    self.server.kite_token = getattr(kite, "access_token", None)
                    was_expired = False
                    with STATE_LOCK:
                        was_expired = STATE["auth_expired"]
                        STATE.update(auth_expired=False, auth_nudged=False)
                    threading.Thread(target=relay_token_to_cloud, daemon=True).start()
                    # Best-effort only: the sign-in has already succeeded and the
                    # token is on disk, so nothing here may turn it into an error.
                    if was_expired:
                        try:
                            notify.session_restored(
                                auth.session_status().get("user", ""), log=log)
                        except Exception as exc:
                            log(f"restore notice failed (login was fine): "
                                f"{type(exc).__name__}")
                    who = auth.session_status()
                    log(f"logged in as {who.get('user')} ({who.get('user_id')}) "
                        f"via redirect callback")
                    threading.Thread(target=run_cycle,
                                     args=(kite, self.server.universe),
                                     daemon=True).start()
                    return self._send(
                        self._login_result_page(True, who.get("user") or "you"),
                        "text/html; charset=utf-8")
                except Exception as exc:
                    log(f"callback login failed: {type(exc).__name__}: {exc}")
                    return self._send(
                        self._login_result_page(False, f"{type(exc).__name__}: {exc}"),
                        "text/html; charset=utf-8")

            if route == "/api/session":
                st = auth.session_status()
                try:
                    st["login_url"] = auth.login_url()
                except Exception:
                    st["login_url"] = None
                return self._json(st)
            if route == "/api/publish":
                with STATE_LOCK:
                    return self._json(dict(STATE["publish"]))
            if route == "/api/log":
                with STATE_LOCK:
                    return self._json({"log": list(STATE["log"])})
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/refresh":
            uni = self.server.universe
            adopt_shared_token(self.server)
            threading.Thread(target=run_cycle, args=(self.server.kite, uni),
                             daemon=True).start()
            return self._json({"triggered": True})

        if route == "/api/publish":
            threading.Thread(target=publish_public, kwargs={"force": True},
                             daemon=True).start()
            return self._json({"triggered": True})

        if route == "/api/watchlist":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"error": "bad request body"}, 400)
            action = payload.get("action")
            names = payload.get("names") or ([payload["name"]] if payload.get("name") else [])
            names = [str(n).strip().upper() for n in names if str(n).strip()]
            try:
                if action == "add":
                    wl = db.watchlist_add(names)
                elif action == "remove":
                    wl = db.watchlist_remove(names)
                elif action == "set":
                    wl = db.watchlist_set(names)
                elif action == "clear":
                    wl = db.watchlist_set([])
                else:
                    return self._json({"error": f"unknown action {action!r}"}, 400)
                log(f"watchlist {action}: {names or '-'} -> {len(wl)} stocks")
                return self._json({"watchlist": wl})
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        if route == "/api/login":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"ok": False, "error": "bad request body"}, 400)
            pasted = (payload.get("redirect_url") or "").strip()
            if not pasted:
                return self._json({"ok": False, "error": "no redirect URL supplied"}, 400)
            try:
                kite = auth.complete_login(pasted)
                with STATE_LOCK:
                    STATE.update(auth_expired=False, auth_nudged=False)
                threading.Thread(target=relay_token_to_cloud, daemon=True).start()
                # Hot-swap the client so the running scheduler picks it up
                # without a restart -- the whole point of logging in from here.
                self.server.kite = kite
                self.server.kite_token = getattr(kite, "access_token", None)
                who = auth.session_status()
                log(f"logged in as {who.get('user')} ({who.get('user_id')}) via dashboard")
                threading.Thread(target=run_cycle,
                                 args=(kite, self.server.universe), daemon=True).start()
                return self._json({"ok": True, "user": who.get("user"),
                                   "user_id": who.get("user_id")})
            except Exception as exc:
                log(f"dashboard login failed: {type(exc).__name__}: {exc}")
                return self._json({"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"}, 400)
        return self._json({"error": "not found"}, 404)

    def _login_result_page(self, ok: bool, detail: str) -> bytes:
        """Tiny page shown in the browser tab Kite redirected to."""
        dash = f"http://{config.SERVER_HOST}:{config.SERVER_PORT}/"
        if ok:
            title, colour, msg = ("Connected", "#3fb950",
                                  f"Signed in as {detail}. Refreshing data now.")
        else:
            title, colour, msg = ("Sign-in failed", "#f85149", detail)
        return (f"""<!doctype html><meta charset="utf-8">
<title>Delta Yield - {title}</title>
<style>body{{background:#0c0e11;color:#e7eaef;font:15px/1.6 -apple-system,
"Avenir Next",Helvetica,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;text-align:center}}
.c{{max-width:460px;padding:30px}}h1{{color:{colour};font-size:20px;
letter-spacing:.12em;text-transform:uppercase;margin:0 0 10px}}
a{{color:#e8a33d}}</style>
<div class="c"><h1>{title}</h1><p>{msg}</p>
<p><a href="{dash}">Back to the dashboard</a></p>
{'<script>setTimeout(()=>location.href=' + chr(34) + dash + chr(34) + ',1800)</script>' if ok else ''}
</div>""").encode()

    def _static(self, name: str, ctype: str) -> None:
        path = (STATIC_DIR / name).resolve()
        # Contain path traversal: the resolved file must stay inside STATIC_DIR.
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.exists():
            return self._json({"error": "not found"}, 404)
        self._send(path.read_bytes(), ctype)


def main() -> None:
    db.init()
    log("building universe from instruments dump ...")
    uni = universe_mod.build_universe()
    log(f"universe ready: {len(uni.stocks)} stocks, "
        f"{len(uni.options)} contracts, expiries "
        f"{[e.isoformat() for e in uni.expiries]}")

    # A dead token must not stop the dashboard from starting -- it should show
    # the problem instead. The collector simply no-ops until a session exists.
    try:
        kite = auth.get_kite()
        who = auth.session_status()
        log(f"kite session ok for {who.get('user')} ({who.get('user_id')})")
    except SystemExit as exc:
        kite = None
        log(f"NO KITE SESSION: {exc}")
        # Put the sign-in page in front of the user immediately rather than
        # waiting for them to notice a dead dashboard.
        try:
            url = auth.login_url()
            log("opening the Kite sign-in page ...")
            auth.notify_and_open(
                url, "Kite session expired - sign in to resume the scanner")
        except Exception:
            log("run `python3 auth.py` (or use the dashboard button) to sign in")

    httpd = ThreadingHTTPServer((config.SERVER_HOST, config.SERVER_PORT), Handler)
    httpd.kite = kite
    httpd.universe = uni

    threading.Thread(target=scheduler, args=(httpd, uni, kite is not None),
                     daemon=True).start()
    threading.Thread(target=strategy_loop, args=(httpd,), daemon=True).start()
    threading.Thread(target=token_relay_watcher, daemon=True).start()

    url = f"http://{config.SERVER_HOST}:{config.SERVER_PORT}"
    log(f"dashboard live at {url}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
