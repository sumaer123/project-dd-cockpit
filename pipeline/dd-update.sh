#!/bin/bash
# Project DD — THE one-shot update. Auth (only if needed) → local → scores → cloud.
#
# This is the whole protocol in one command. It probes all three credentials
# concurrently and opens a browser login ONLY for the dead ones, then runs the
# full pipeline at full speed. When everything is already live it adds ~2s and
# goes straight through, so it is safe to make this the default entry point.
#
#   ./dd-update.sh            # auth-if-needed, then refresh, then verify cloud
#   ./dd-update.sh --no-wait  # skip the cloud-publish wait (returns ~15s sooner)
#
# The AE consumption forecasts and quota targets are pulled LIVE inside this run
# (refresh.py: "CP forecasts"/"CP targets" from the ConsumptionPlan app's objects)
# and asserted afterwards by ddmodules' "Consumption forecast" check. There is no
# separate forecast step and nothing to type by hand — if that check ever reads
# EMPTY, the app has fallen back to the frozen manual numbers.
#
# NOT for launchd. The scheduled job calls refresh.py directly, without --auth, so an
# unattended run never pops a browser window — it fails honestly instead.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3)"
# Cloud settings come from config/territory.json (via ddconfig.py).
APP_NAME="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import ddconfig as c; print(c.CLOUD_APP_NAME if c.CLOUD_ENABLED else "")' "$ROOT/pipeline")"
APP_URL="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import ddconfig as c; print(c.CLOUD_APP_URL)' "$ROOT/pipeline")"
WAIT=1; [ "${1:-}" = "--no-wait" ] && WAIT=0

# Remember where the deploy log ends BEFORE the run. Waiting for "a DEPLOYED
# line dated today" is not enough — an earlier refresh the same day already left
# one, so the wait would fall straight through and report a stale deploy as ours.
DLOG="$ROOT/cloud/.deploy/AUTO_DEPLOY.log"
before=$(wc -l < "$DLOG" 2>/dev/null || echo 0)

"$PY" "$ROOT/pipeline/refresh.py" --auth
rc=$?
[ $rc -ne 0 ] && { echo "refresh FAILED (exit $rc) — local data.js untouched"; exit $rc; }
[ $WAIT -eq 0 ] && exit 0
[ -z "$APP_NAME" ] && { echo "cloud mirror disabled in config — local cockpit refreshed"; exit 0; }

# The cloud publish is detached, so wait for THIS run's deploy line rather than
# any deploy line — a stale SUCCEEDED from an earlier run must never read as ours.
echo "waiting for cloud publish…"
landed=0
for _ in $(seq 1 40); do
  # only lines appended AFTER this run started count as this run's deploy
  if tail -n "+$((before + 1))" "$DLOG" 2>/dev/null \
     | grep -q "DEPLOYED $APP_NAME"; then
    landed=1; break
  fi
  # a skip is terminal too — stop waiting instead of burning the full 120s
  if tail -n "+$((before + 1))" "$DLOG" 2>/dev/null \
     | grep -qE "SKIP|FAIL|unchanged"; then
    landed=2; break
  fi
  sleep 3
done

echo "--- cloud ---"
case $landed in
  1) echo "cloud: DEPLOYED (this run)";;
  2) echo "cloud: publish skipped/failed this run — see log below";;
  *) echo "cloud: no deploy line after 120s — publish may still be running";;
esac
tail -n "+$((before + 1))" "$DLOG" 2>/dev/null | tail -3
[ -n "$APP_URL" ] && curl -s -o /dev/null -w "gate: HTTP %{http_code}\n" --max-time 25 "$APP_URL"
for f in data.js app.js styles.css; do
  if diff -q "$ROOT/app/$f" "$ROOT/cloud/$f" >/dev/null 2>&1; then
    echo "parity: $f OK"
  else
    echo "parity: $f DIFFERS  <-- cloud is not a mirror of local"
  fi
done
grep -o '"lastRefresh"[^,]*' "$ROOT/cloud/data.js" 2>/dev/null | head -1
