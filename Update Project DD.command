#!/bin/bash
# Double-click: the one-shot update. Re-authenticates only the dead logins
# (browser opens), refreshes all data, republishes the cloud mirror, verifies.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/pipeline/dd-update.sh"
