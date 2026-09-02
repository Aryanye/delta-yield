#!/bin/bash
# Push today's Kite access token up to the GitHub Actions secret.
#
# This is what lets the cloud collector run while your Mac is asleep. Note what
# does NOT travel: your Zerodha password, your 2FA, and your api_secret all stay
# on this machine. Only the access token goes up, and Zerodha expires it every
# morning, so the blast radius is one trading day.
set -e
cd "$(dirname "$0")"

TOKEN=$(python3 -c "
import json, config
p = config.TOKEN_PATH
print(json.loads(p.read_text())['access_token'] if p.exists() else '')
" 2>/dev/null)

if [ -z "$TOKEN" ]; then
  echo "No cached Kite token found. Sign in first:"
  echo "  open http://127.0.0.1:8777/   (or run: python3 auth.py)"
  exit 1
fi

REPO=$(git config --get remote.origin.url 2>/dev/null | sed 's#.*github.com[:/]##; s#\.git$##')
if [ -z "$REPO" ]; then echo "No GitHub remote configured."; exit 1; fi

printf '%s' "$TOKEN" | gh secret set KITE_ACCESS_TOKEN --repo "$REPO"
echo "Pushed today's Kite token to $REPO."
echo "The cloud collector will keep the public dashboard live until it expires tomorrow."

# ---- also hand the token to the Vercel live-quote function ----------------
# The published page re-quotes what it shows every 15s through api/live.js,
# which reads KITE_ACCESS_TOKEN from the project's env. Env changes apply on
# the next deploy, and the cloud loop deploys every 5 minutes.
python3 - <<'PY'
import json, urllib.request, config
env = config.load_env()
tok, org, proj = env.get("VERCEL_TOKEN"), env.get("VERCEL_ORG_ID"), env.get("VERCEL_PROJECT_ID")
if not (tok and org and proj):
    print("Vercel env not updated: VERCEL_* missing from .env"); raise SystemExit(0)
kite_tok = json.loads(config.TOKEN_PATH.read_text())["access_token"]
def upsert(key, value):
    body = json.dumps({"key": key, "value": value, "type": "encrypted",
                       "target": ["production"]}).encode()
    req = urllib.request.Request(
        f"https://api.vercel.com/v10/projects/{proj}/env?teamId={org}&upsert=true",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()
try:
    upsert("KITE_API_KEY", env["KITE_API_KEY"])       # static; harmless to repeat
    upsert("KITE_ACCESS_TOKEN", kite_tok)              # rolls daily
    print("Updated Kite env on Vercel (live ticks pick it up on the next deploy).")
except Exception as exc:
    print(f"Vercel env update failed: {type(exc).__name__}: {str(exc)[:80]}")
PY
