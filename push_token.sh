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
