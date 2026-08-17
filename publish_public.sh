#!/bin/bash
# Regenerate the public snapshot from the latest collected data and deploy it.
# Run this any time you want the shared link to show fresh numbers.
set -e
cd "$(dirname "$0")"

echo "==> building snapshot from the current database"
python3 publish.py site/index.html

echo "==> deploying to Vercel (production)"
cd site
vercel deploy --prod --yes 2>&1 | tail -3

echo ""
echo "Public link: https://delta-yield.vercel.app"
