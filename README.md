# Delta Yield — NSE F&O stock option scanner

**Location:** `~/Trading/fno_delta_yield`
(symlinked from `~/Desktop/Claude/Finance/fno_delta_yield` for convenience).

It lives outside `~/Desktop` deliberately: macOS TCC blocks background
LaunchAgents from reading Desktop/Documents/Downloads, and an agent gets a bare
`Operation not permitted` with no way to prompt for consent. Moving the project
was the fix — the alternative was granting Full Disk Access to `python3`, which
would hand every Python script you ever run access to your whole disk.

Answers one question across every F&O **stock** (indices excluded): *at a given
delta, which stock pays the most premium per rupee of margin?*

For every strike it computes, live:

```
yield % = (bid-ask mid × lot size) / (exact margin blocked for one lot) × 100
```

Both sides of that ratio are measured, not modelled. The premium is the live
mid; the margin comes from Zerodha's own margin engine, so it includes SPAN,
exposure, exchange margin revisions and per-stock add-ons.

## Quick start

```bash
./run_daily.sh
```

Checks the Kite session, logs you in if it has expired, then serves the
dashboard at http://127.0.0.1:8777. It refreshes every 5 minutes while the
market is open and writes each snapshot to SQLite; outside market hours it
idles and re-checks every minute.

## Daily login — as automatic as it can honestly be

Zerodha expires API tokens every morning. **The sign-in itself cannot be
automated**: it needs your Zerodha password and 2FA, and any script that stored
those and typed them for you would be handing your broker credentials to a
program. Everything *around* it is automated:

1. **The server starts itself** each weekday at 09:00 (launchd job, below).
2. If the token is dead it **opens the Kite sign-in page automatically** and
   raises a macOS notification.
3. You sign in — the one step that is yours.
4. Kite redirects straight back to the server, which **captures the token from
   the URL**, swaps the session in live and kicks off a refresh. No copying,
   no pasting, no restart.

For step 4, set the app's redirect URL **once** at
<https://developers.kite.trade/apps> to:

```
http://127.0.0.1:8777/kite/callback
```

Without that, the flow still works — the dashboard shows a **Log in to Kite**
button and you paste the redirect URL into the box instead.

### Auto-start on weekday mornings

Already installed and running. To reinstall after editing the plist:

```bash
cp ~/Trading/fno_delta_yield/com.aryan.deltayield.plist ~/Library/LaunchAgents/ && launchctl unload ~/Library/LaunchAgents/com.aryan.deltayield.plist 2>/dev/null; launchctl load ~/Library/LaunchAgents/com.aryan.deltayield.plist
```

Starts the server at 09:00 Mon–Fri and restarts it if it dies mid-session.
Stop it with `launchctl unload ~/Library/LaunchAgents/com.aryan.deltayield.plist`.

The plist sets an explicit `PATH` (Homebrew + nvm + system). launchd does not
run a login shell and this machine has no bash profile, so without that the
data cycles would run but every Vercel deploy would fail with
"vercel CLI not found".

## Where it runs

The published dashboard is produced by **GitHub Actions**, not by this Mac. The
scheduled workflow checks out the repo, collects the full chain from Kite,
prices the structures, renders the snapshot and deploys it to Vercel — so the
public link keeps updating with the laptop asleep, shut, or switched off.

Repo: <https://github.com/Aryanye/delta-yield> · workflow `.github/workflows/collect.yml`

The Mac still runs the local dashboard at `127.0.0.1:8777` for your own use. It
just no longer publishes (`PUBLISH_ENABLED = False`) — two publishers would race
each other and double-spend the Vercel deploy budget.

### The daily token, and what stays local

Zerodha still expires the token every morning, and the cloud cannot do an
interactive 2FA login. So the two halves join like this:

1. You sign in **on your Mac** as before — password, 2FA and `api_secret` never
   leave this machine.
2. The server then runs `push_token.sh` automatically, which stores **only the
   derived access token** as the `KITE_ACCESS_TOKEN` GitHub secret.
3. The cloud collector uses that token for the rest of the day.

If a scheduled run fails — nearly always because the token rolled over — you get
a Telegram message with a link to the run log telling you to sign in.

You can also push it by hand at any time:

```bash
./push_token.sh
```

### If the page updates slowly, check the token secret first

The most likely cause is that the **cloud is running yesterday's Kite token**.
It then fails every cycle, the Mac's gap-filler becomes the only publisher, and
the cadence stretches to the gap-fill interval instead of 5 minutes.

```bash
python3 -c "import json,config;print(json.loads(config.TOKEN_PATH.read_text())['created_at'])"
gh api /repos/Aryanye/delta-yield/actions/secrets/KITE_ACCESS_TOKEN --jq .updated_at
```

If the secret is older than the local token, run `./push_token.sh`. The server
now watches the cached token and relays within 60 seconds of any change, so this
should not recur — but it is the first thing to check.

### The Vercel deploy token

The cloud runner needs a **personal API token** (`vcp_…`), created at
<https://vercel.com/account/tokens> and stored as the `VERCEL_TOKEN` secret.

Do not reuse the token from `~/Library/Application Support/com.vercel.cli/auth.json`:
that is a short-lived OAuth *session* token (`vca_…`) which the CLI refreshes
locally, and `vercel deploy --token` rejects it. `cloud_cycle.py` now detects
that prefix by name and says so.

The preflight probes the **project** endpoint, not `/v2/user` — a team-scoped
token has no user-level access and 404s there even when it can deploy perfectly
well.

### Why the page went stale, and the redundancy that fixes it

GitHub's scheduled workflows are **queued at low priority and dropped under
load**. On a nominal 30-minute schedule the observed gaps were 18, 37, 50 and
then 76 minutes — two runs skipped outright, leaving the page frozen for over an
hour. Nothing in this code was at fault; the runs never started.

Two changes:

1. **Cron moved off `:00`/`:30`** to `7,22,37,52`, the least contended minutes.
2. **The Mac publishes again, as a gap-filler.** Before each publish it asks the
   Vercel API how old the live deployment is and only deploys if the cloud has
   gone quiet (`PUBLISH_GAPFILL_MINUTES`, default 11). Two independent
   publishers, and they never double-publish. If the probe itself fails it
   errs toward publishing, so a broken check can't leave the page frozen.

Between them, the page stays current whenever *either* GitHub or your Mac is
working — which in practice is always.

For a **guaranteed** 5-minute cadence with no dependence on GitHub's scheduler
at all, the repo needs to be public: that unlocks unlimited Actions minutes, and
a single long-running job can then loop internally instead of relying on cron.

### Why 5 minutes, and not less

Two independent hard limits, either of which alone rules out a 1-minute refresh:

| Limit | Number |
|---|---|
| A full sweep against Kite's **1 req/s** quote limit (59 batches + 170 margin batches) | **~155 s** per cycle |
| **Vercel Hobby: 100 deployments/day**. Market hours are 375 min | 5 min = 75/day · 3 min = 125/day · 1 min = 375/day |

So the floor is ~3 minutes physically and 5 minutes practically. Going faster
would need Vercel Pro *and* a restructured collector — and the page is ~800 KB
gzipped, so a 1-minute poll would cost a phone ~48 MB/hour for data that moves
very little in 60 seconds.

### Cost and cadence

Actions minutes are the constraint, not money:

| Cadence | Minutes/month | Private repo (2,000 free) | Public repo |
|---|---|---|---|
| every 5 min | ~9,150 | ~$57/mo over | free |
| **every 30 min** | ~1,525 | **free** | free |

The repo is **private** and the schedule is **every 30 minutes**, which stays
inside the free tier. Public repos get unlimited Actions minutes, so if you make
this repo public you can drop the cron to `*/5 3-10 * * 1-5` and get 5-minute
refreshes for nothing. Nothing secret lives in the repo — credentials are all
encrypted Actions secrets — but that is your call to make.

Scheduled workflows on GitHub are **best effort**: under load they can start
5–20 minutes late. The page always states its own age, so a late run reads as
stale rather than passing for current.

## The Mac-only caveat that no longer applies

Everything — the Kite polling, the SQLite writes, the Vercel deploys — happens
on this machine. **If the Mac sleeps, the public page freezes** at whatever was
last deployed. There is no server in the cloud doing this for you.

The LaunchAgent now runs the server under `caffeinate -i -s`, which blocks idle
and system sleep for as long as the server is running. That covers "I walked
away and it dozed off".

It does **not** cover closing the lid. Clamshell sleep cannot be overridden by
`caffeinate`; macOS only stays awake with the lid shut when on AC power with an
external display attached. Observed on 2026-08-14: deploys ran exactly 5 minutes
apart from 13:42 to 14:12, then stopped dead for 41 minutes when the lid closed
at 14:16.

So, for the shared link to stay live through the session:

| Situation | Result |
|---|---|
| Lid open, plugged in or not | Updates every 5 min |
| Lid open, machine idle | Updates — `caffeinate` holds it awake |
| **Lid closed** | **Frozen until you reopen it** |
| Lid closed, AC + external display | Updates (macOS stays awake) |
| Mac shut down | Frozen |

The page is honest about this: after 12 minutes without fresh data during market
hours it shows the red **DATA IS FROZEN** banner with the exact age, so a stale
page never passes as a live one.

If you need it live with the laptop shut, the collector has to move to something
always-on (a small VPS or a Pi). That is a real change — the Kite login redirect
would have to point at that host instead of `127.0.0.1` — but nothing else in
the design would need to move.

## The public link

**https://delta-yield.vercel.app** — public, no login, safe to send to anyone.
Market data only: no account, position or credential information, and the Kite
login controls are stripped from the public build.

It **redeploys itself automatically** after every successful 5-minute cycle
during market hours, so the shared link tracks the local dashboard. Deploys run
on their own thread, so a slow Vercel build never delays a data cycle.

`./publish_public.sh` forces an immediate rebuild and deploy.

### Refresh on the public page

The page carries a **Refresh** button that reloads with a cache-busting query
string, so it always fetches the newest deploy rather than whatever the browser
or CDN had. The age pill next to the title is colour-coded — green under 8
minutes, amber under 20, red beyond — and its tooltip gives the exact capture
time.

It also pulls the newest deploy on its own while the market is open, but only
when the page has been idle for 45 seconds and no drawer or dialog is open, so
it never yanks the view out from under you mid-read.

### Timing, and why it slipped

Three separate bugs made the public page lag the local one; all are fixed:

1. The scheduler slept a full `REFRESH_SECONDS` **after** each cycle, making the
   real period `cycle + 300s` (~8.6 min). It now sleeps the remainder measured
   from cycle start.
2. Publishing was chained behind the strategy precompute, so a slow precompute
   swallowed the deploy. Publishing is now throttled on the wall clock only, and
   `PUBLISH_INTERVAL_SECONDS` (210) deliberately sits **below** the cycle period
   — above it, a cycle finishing slightly early loses its publish and the page
   lands on every other cycle.
3. The precompute ran concurrently with the collector and both hammer the same
   10 req/s Kite margin endpoint. They throttled each other badly enough to
   stretch a 140s cycle to **579s**. The precompute now takes `RUN_LOCK`, so it
   only runs in the gap between cycles, with a budget sized to fit.

### When the session dies

The public page cannot refresh itself, so it says so rather than quietly
serving old numbers as if they were current. If the Kite session expires (or
the feed stalls while the market is open) the page shows a red **DATA IS
FROZEN** banner with the exact age of the data, the header reads **FEED
STOPPED**, and one final deploy is pushed so that banner actually reaches
viewers.

It deliberately does **not** put a Kite sign-in prompt on the public page. That
URL is shareable, and a public page asking for broker credentials is a phishing
pattern regardless of intent. The banner instead links to your own local
dashboard, which is where the real sign-in lives — a dead link for anyone else,
one click for you.

You are told out of band instead:

- a **Telegram message** to your own chat (reuses the intraday_strangle bot)
- a macOS notification, and the Kite sign-in page opens automatically
- a second Telegram message confirming when the feed is back

Test the Telegram path with `python3 notify.py`. Disable it with
`TELEGRAM_ENABLED = False` in `config.py`.

### The deployment budget

Vercel's Hobby plan allows **100 deployments per day**. Market hours are 375
minutes, so a 5-minute cadence costs **75 deploys/day** — under the cap, but
with little room for anything else. `config.PUBLISH_DAILY_BUDGET` (default 85)
is a hard stop: once reached the publisher backs off for the rest of the day
and says so in the log, rather than burning limits you may want elsewhere.

To buy headroom, raise `PUBLISH_INTERVAL_SECONDS` (600 = 38 deploys/day).

## Verify the numbers

```bash
python3 verify.py
```

Proves batching doesn't distort margins, that margin is standalone and linear
in lots, that IV round-trips back to the input price, and walks the yield
arithmetic on real contracts so you can check it by hand.

## Finding a stock

The heatmap has a **Find** box that matches on ticker *or* company name, and
completes as you type. NSE company names are abbreviated, so the matcher lets
each word you type prefix-match a word in the name — "oracle financial" finds
OFSS, "tata motors passenger" finds TMPV. Colloquial names are aliased too:
"dominos" finds JUBLFOOD, "jockey" finds PAGEIND, "royal enfield" finds
EICHERMOT.

Expiry defaults to the **nearest** expiry everywhere, since mixing expiries
compares premiums earned over different numbers of days.

Each row also shows the underlying's **live price and move for the day** in
rupees and percent. This is the context that explains the ranking: the
top-yielding names are usually the ones that just moved hard, because that is
what lifted their implied vol.

## Watch List

A second heatmap limited to stocks you choose. Add by ticker or company name
(same matcher as the main search), remove with the × on each chip.

The list lives in SQLite on the local dashboard, so it survives restarts and is
baked into every published snapshot. On the shared link — which has no server —
edits fall back to `localStorage` and stay in that browser, so the page is still
useful on a phone without pretending it can write to your Mac.

Colour on this tab is **absolute yield**, not a within-column rank: a handful of
stocks is too small a sample to rank against itself.

## Event calendar

Built from NSE corporate filings and refreshed once a day as part of a cycle.
Two clearly separated tiers, because conflating them would be worse than having
no calendar:

| Tier | Source | Trust |
|---|---|---|
| **Confirmed** | NSE board-meeting intimations and event calendar | Exact. But companies only file 1–3 weeks ahead, so this list is short and fills in as results season approaches. |
| **Estimated** | Projected ~91 days after the company's own last filed result | A guess with a wide error bar. Useful for "is an event roughly inside this expiry", never for "trade it on that date". |

Events appear at the top of **Opportunities**, flagged when they fall *before*
the selected expiry — which is the thing that matters to a seller, since an
event inside the expiry is already priced into the premium being collected.

## Structure explorer

Tap any event card to build the conventional structures for a stance you pick
(bullish / bearish / neutral): short legs, vertical spreads, ratio spreads,
strangles, condors, straddles.

Everything is measured, not modelled — legs are real strikes at live mids, and
**margin is Zerodha's netted basket figure for the whole position**. That is
what makes the defined-risk versions look so different: on ALKEM a bull put
spread showed 12.0% on margin against 8.8% for the naked short put, purely
because the netted margin is ₹50k instead of ₹85k. Payoff, breakevens and max
loss are evaluated numerically across a price grid, so ratios are handled the
same way as verticals, and an open tail is reported as **Unbounded** rather than
a number that depends on where the grid stopped.

**This is a calculator, not a recommender.** You choose the stock and the
stance; it does the arithmetic. It expresses no view on any stock and does not
rank structures as "best".

Structures are **pre-priced during collection**, on a background thread that
never delays a data cycle, and baked into the published snapshot — so the
Opportunities tab is fully live on the shared link, phone included, with no
server behind it. Roughly 44 stocks × 3 stances takes ~50s.

Which stocks get priced, in priority order (capped at
`STRATEGY_STOCK_LIMIT`, default 60):

1. anything with a **confirmed** event in the next 25 days
2. your **watch list**
3. stocks with an **estimated** event in that window
4. the **highest-yielding** names, so the tab is useful even with no events near

Event cards outside that set are dimmed and say so rather than dead-ending on a
tap. Each card carries a **structures** badge when priced structures exist.

## On a phone

The published link is laid out for mobile: the heatmap keeps its table (ten
delta columns are genuinely tabular) with the stock column pinned and the grid
scrolling under it, while record-style tables become two-column cards. Filters
collapse behind a toggle, and the desktop layout above 760px is untouched.

## How the numbers are built

| Quantity | Method |
|---|---|
| Premium | bid-ask **mid**; falls back to LTP only when one side is missing |
| Margin | Kite `basket_order_margins(consider_positions=False)`, 1 lot, SELL, NRML |
| Delta, IV | Black-76 inverted from the mid against the **same-expiry future** |
| Universe | NSE F&O stocks only — every index hard-excluded |
| Expiries | nearest two monthly expiries |
| Delta band | 0.05–0.50 absolute (full chain still stored in `latest`) |

**Why the future, not spot.** The same-expiry future already embeds the
market's dividend and carry view. Using spot would require guessing a dividend
yield per stock, and that guess would land straight in the delta — the axis the
whole scanner is organised around.

**Why basket margins.** `/margins/orders` nets against positions you already
hold, which would make stocks you're already short look artificially cheap.
The basket endpoint with `consider_positions=False` returns the standalone
margin a fresh short would actually block.

## Reading it honestly

- A high yield is **compensation for risk**, not free money. Stocks at the top
  are usually there because implied vol is high — which is exactly why the
  option may finish in the money.
- Compare **within** a delta column, where breach probability is roughly
  matched. Comparing across columns compares different risks.
- Yield is **gross of costs** and not annualised, so a far-month row earns its
  premium over more days than a near-month one.
- Liquidity is **flagged, never hidden**: 🟢 good, 🟡 thin, 🔴 wide spread or
  almost no OI. After market close everything flags red — depth is empty, so
  prices fall back to LTP.

## Files

| File | Role |
|---|---|
| `config.py` | every tunable; `config.json` overrides it |
| `universe.py` | instruments dump → stocks-only universe, lot sizes, expiries |
| `pricing.py` | Black-76 IV inversion and greeks |
| `margins.py` | batched exact margin fetch + semantics verification |
| `collector.py` | one refresh cycle |
| `db.py` | SQLite schema (`latest` = full chain, `history` = in-band over time) |
| `queries.py` | read-side aggregations behind the API |
| `server.py` | dashboard + API + market-hours scheduler |
| `publish.py` | self-contained shareable snapshot |
| `verify.py` | live correctness checks |

## Data model

- **`latest`** — full option chain for every stock, wiped and rewritten each
  cycle. The complete current picture.
- **`history`** — in-band rows only, appended every cycle, pruned after 30
  days. Lets you see how yield at a given delta evolves.
- **`underlyings`** — spot, future and basis per stock and expiry.
