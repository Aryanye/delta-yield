#!/bin/bash
# Start the Delta Yield scanner for the trading day.
cd "$(dirname "$0")"
if ! python3 -c "import auth,sys; sys.exit(0 if auth.session_status().get('ok') else 1)" 2>/dev/null; then
  echo "Kite session expired -- logging in..."
  python3 auth.py || exit 1
fi
exec python3 server.py
