#!/bin/bash
# Double-click: starts the local refresh helper (for the in-app "Refresh now"
# button) if it isn't running, then opens the cockpit in your default browser.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
pgrep -f "pipeline/refresh_server.py" >/dev/null 2>&1 || \
  nohup python3 "$DIR/pipeline/refresh_server.py" >/dev/null 2>&1 &
sleep 1
open "$DIR/app/index.html"
