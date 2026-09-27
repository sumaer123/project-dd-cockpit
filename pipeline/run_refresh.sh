#!/bin/bash
# Project DD — cron/manual wrapper. Sets PATH so the CLIs resolve under a minimal env.
# stdout goes to launchd.out.log, NOT refresh.log (refresh.py owns and rotates that).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$DIR/refresh.py" >> "$DIR/launchd.out.log" 2>&1
