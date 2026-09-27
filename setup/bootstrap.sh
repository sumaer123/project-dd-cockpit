#!/bin/bash
# Project DD — one-command setup on a fresh Mac. Safe to re-run.
#   1. checks / installs prerequisites (Homebrew, python3, sf CLI, databricks CLI)
#   2. runs setup/configure.py  (discovers YOUR role + team from Salesforce)
#   3. first data pull          (pipeline/refresh.py --auth)
#   4. daily schedule           (setup/install_schedule.py)
#   5. optional cloud mirror    (setup/create_cloud_app.py)
#   6. opens the cockpit
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
step(){ printf "\n\033[1m━━ %s\033[0m\n" "$1"; }
ok(){ printf "  ✓ %s\n" "$1"; }

step "1/6 · Prerequisites"
if ! command -v brew >/dev/null 2>&1; then
  echo "  Homebrew is missing. Install it first (one line, from https://brew.sh):"
  echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi
ok "brew $(brew --version | head -1 | awk '{print $2}')"
command -v python3 >/dev/null 2>&1 || brew install python
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' || { echo "  python3 $PYV is too old (need 3.9+): brew install python"; exit 1; }
ok "python3 $PYV"
command -v sf >/dev/null 2>&1 || { echo "  installing Salesforce CLI…"; brew install sf; }
ok "sf $(sf --version 2>/dev/null | awk '{print $1}' | cut -d/ -f2)"
if ! command -v databricks >/dev/null 2>&1; then
  echo "  installing Databricks CLI…"; brew tap databricks/tap && brew install databricks
fi
ok "$(databricks --version)"

step "2/6 · Configure your territory (Salesforce role + team discovery)"
python3 "$ROOT/setup/configure.py" "$@" || exit 1

step "3/6 · First data pull"
python3 "$ROOT/pipeline/refresh.py" --auth || { echo "  refresh failed — see $ROOT/pipeline/refresh.log"; exit 1; }

step "4/6 · Daily auto-refresh"
read -r -p "  Install the daily auto-refresh (launchd)? [Y/n] " a
[[ "${a:-Y}" =~ ^[Yy] ]] && python3 "$ROOT/setup/install_schedule.py"

step "5/6 · Cloud mirror"
if python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);import ddconfig as c;sys.exit(0 if c.CLOUD_ENABLED else 1)' "$ROOT/pipeline"; then
  read -r -p "  Create + deploy the Databricks App now? [Y/n] " a
  [[ "${a:-Y}" =~ ^[Yy] ]] && python3 "$ROOT/setup/create_cloud_app.py"
else
  echo "  cloud mirror disabled in config — skipped (re-run setup/configure.py to enable)"
fi

step "6/6 · Open the cockpit"
open "$ROOT/Open Project DD.command"
echo "  Done. Day-to-day: double-click 'Update Project DD.command' whenever you want fresh data."
