#!/bin/bash
# Project DD — one-command setup on a fresh Mac. Safe to re-run.
#   1. checks prerequisites; installs missing ones with Homebrew when it is there
#   2. runs setup/configure.py  (discovers YOUR role + team from Salesforce)
#   3. first data pull          (pipeline/refresh.py --auth)
#   4. daily schedule           (setup/install_schedule.py)
#   5. optional cloud mirror    (setup/create_cloud_app.py)
#   6. opens the cockpit
# Any arguments go straight to configure.py, for example:
#   ./setup/bootstrap.sh --username you@databricks.com --no-cloud
# Homebrew is needed only when a tool is missing. Without admin rights, install the
# tools the no-admin way from the setup guide first; this script then finds them.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Homebrew (Apple Silicon + Intel) and the setup guide's no-admin install folders.
export PATH="$HOME/.local/bin:$HOME/.local/sf/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
step(){ printf "\n\033[1m━━ %s\033[0m\n" "$1"; }
ok(){ printf "  ✓ %s\n" "$1"; }
printf "Project DD kit %s\n" "$(cat "$ROOT/VERSION" 2>/dev/null || echo unknown)"

step "1/6 · Prerequisites"
need=""
python3 -c 'import sys' >/dev/null 2>&1 || need="$need python"
command -v sf >/dev/null 2>&1 || need="$need sf"
command -v databricks >/dev/null 2>&1 || need="$need databricks"
if [ -n "$need" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "  Missing:$need — and Homebrew is not installed."
    echo "  Either install Homebrew (needs an admin password), then re-run this script:"
    echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    echo "  or install the missing tools without admin rights (setup guide, Step A, no-admin path)."
    exit 1
  fi
  for t in $need; do
    case "$t" in
      python) echo "  installing Python…"; brew install python ;;
      sf) echo "  installing Salesforce CLI…"; brew install sf ;;
      databricks) echo "  installing Databricks CLI…"; brew tap databricks/tap && brew install databricks ;;
    esac
  done
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null \
  || { echo "  python3 $PYV is missing or too old (need 3.9+): brew install python, or xcode-select --install"; exit 1; }
ok "python3 $PYV"
command -v sf >/dev/null 2>&1 || { echo "  sf is still not on PATH (setup guide, Step A)"; exit 1; }
ok "sf $(sf --version 2>/dev/null | awk '{print $1}' | cut -d/ -f3)"
command -v databricks >/dev/null 2>&1 || { echo "  databricks is still not on PATH (setup guide, Step A)"; exit 1; }
DBV="$(databricks --version 2>/dev/null)"
case "$DBV" in
  "Databricks CLI v"*) ok "$DBV" ;;
  *) echo "  '$(command -v databricks)' is the old Python databricks-cli ($DBV). Remove it"
     echo "  (pip uninstall databricks-cli) and install the new CLI (setup guide, Step A)."; exit 1 ;;
esac

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
