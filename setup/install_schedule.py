#!/usr/bin/env python3
"""Install (or remove) the daily auto-refresh as a macOS launchd job.

Generates ~/Library/LaunchAgents/com.projectdd.<you>.refresh.plist from
config/territory.json (schedule.hour / schedule.minute) with the absolute paths
of THIS checkout, then loads it. The job runs pipeline/refresh.py WITHOUT --auth,
so it can never pop a browser window at an empty desk — a dead Salesforce token
just makes that run fail fast, and the app keeps serving the last good data.
It also runs once at login, and launchd catches up a missed run after sleep.

    python3 setup/install_schedule.py            # install / update
    python3 setup/install_schedule.py --remove   # uninstall
    python3 setup/install_schedule.py --run-now  # install, then trigger one run
"""
import os
import re
import subprocess
import sys
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))


def main():
    remove = "--remove" in sys.argv
    import ddconfig as cfg
    user = re.sub(r"[^a-z0-9]+", "", cfg.SF_USERNAME.split("@")[0].lower()) or "user"
    label = f"com.projectdd.{user}.refresh"
    agents = os.path.expanduser("~/Library/LaunchAgents")
    plist = os.path.join(agents, f"{label}.plist")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    if remove:
        if os.path.exists(plist):
            os.remove(plist)
        print(f"removed {label}")
        return

    home = os.path.expanduser("~")
    for protected in ("Desktop", "Documents", "Downloads"):
        if ROOT.startswith(os.path.join(home, protected) + os.sep):
            print(f"! WARNING: this checkout is under ~/{protected}. macOS privacy (TCC) can block a")
            print("  background launchd job from reading it. If scheduled runs fail with")
            print("  'Operation not permitted', move the repo to ~/project-dd (recommended) or give")
            print(f"  {sys.executable} Full Disk Access in System Settings › Privacy & Security.")
    py = sys.executable
    logs = os.path.expanduser("~/Library/Logs/ProjectDD")
    os.makedirs(logs, exist_ok=True)
    os.makedirs(agents, exist_ok=True)
    path_env = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escape(py)}</string>
    <string>{escape(os.path.join(ROOT, "pipeline", "refresh.py"))}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path_env}</string>
    <key>HOME</key><string>{escape(os.path.expanduser("~"))}</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{cfg.SCHEDULE_HOUR}</integer>
    <key>Minute</key><integer>{cfg.SCHEDULE_MINUTE}</integer>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- Logs stay OUTSIDE ~/Desktop and ~/Documents: launchd opens them before exec
       and has no privacy (TCC) grant for those folders — pointing them there makes
       every run die with exit 78 before Python starts. -->
  <key>StandardOutPath</key><string>{escape(logs)}/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{escape(logs)}/launchd.err.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""
    with open(plist, "w") as f:
        f.write(xml)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}")
        sys.exit(1)
    print(f"installed {label}: daily at {cfg.SCHEDULE_HOUR:02d}:{cfg.SCHEDULE_MINUTE:02d} + at login")
    print(f"  plist: {plist}")
    print(f"  logs:  {os.path.join(ROOT, 'pipeline', 'refresh.log')}  (narrative)  ·  {logs}/ (launchd)")
    if "--run-now" in sys.argv:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
        print("  triggered one run now — watch pipeline/refresh.log")


if __name__ == "__main__":
    main()
