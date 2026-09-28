#!/usr/bin/env python3
"""Install (or remove) the daily auto-refresh as a macOS launchd job.

Generates ~/Library/LaunchAgents/com.projectdd.<you>.refresh.plist from
config/territory.json (schedule.hour / schedule.minute) with the absolute paths
of THIS checkout, then loads it. The job runs pipeline/refresh.py WITHOUT --auth,
so it can never pop a browser window at an empty desk — a dead Salesforce token
just makes that run fail fast, and the app keeps serving the last good data.
It also runs once at login, and launchd catches up a missed run after sleep.

launchd starts jobs with a bare environment, so the plist carries its own PATH:
the folders where `sf`, `databricks` and python3 live ON THIS MAC (Homebrew, or a
no-admin install under ~/.local) are looked up now and written into it. Re-run
this script whenever you move or reinstall either CLI.

    python3 setup/install_schedule.py            # install / update
    python3 setup/install_schedule.py --dry-run  # print the plist, change nothing
    python3 setup/install_schedule.py --remove   # uninstall
    python3 setup/install_schedule.py --run-now  # install, then trigger one run
"""
import os
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

BASE_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
NO_ADMIN_DIRS = ["~/.local/bin", "~/.local/sf/bin"]   # the setup guide's no-admin install


def stable_python():
    """The interpreter the job runs: the unversioned `python3` on PATH (for Homebrew,
    /opt/homebrew/bin/python3, which every `brew upgrade python` re-points) rather
    than sys.executable's versioned keg path, which an upgrade plus `brew cleanup`
    deletes, silently stranding the job."""
    cand = shutil.which("python3")
    if cand:
        r = subprocess.run([cand, "-c", "import sys; print(sys.version_info >= (3, 9))"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() == "True":
            return cand
    return sys.executable


def job_path(py):
    """PATH for the job: where the two CLIs and python live now, then the usual dirs."""
    dirs = []
    for tool in ("sf", "databricks"):
        found = shutil.which(tool)
        if found:
            dirs.append(os.path.dirname(found))
        else:
            print(f"! `{tool}` is not on PATH in this shell: the scheduled refresh will fail with "
                  f"CLI_MISSING until it is installed (then re-run this script).")
    dirs.append(os.path.dirname(py))
    dirs += [os.path.expanduser(d) for d in NO_ADMIN_DIRS] + BASE_PATH
    out = []
    for d in dirs:
        if d and d not in out:
            out.append(d)
    return ":".join(out)


def main():
    remove = "--remove" in sys.argv
    dry = "--dry-run" in sys.argv
    import ddconfig as cfg
    user = re.sub(r"[^a-z0-9]+", "", cfg.SF_USERNAME.split("@")[0].lower()) or "user"
    label = f"com.projectdd.{user}.refresh"
    agents = os.path.expanduser("~/Library/LaunchAgents")
    plist = os.path.join(agents, f"{label}.plist")
    uid = os.getuid()
    if remove:
        if dry:
            print(f"(dry run) would unload {label} and delete {plist}")
            return
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
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
    py = stable_python()
    logs = os.path.expanduser("~/Library/Logs/ProjectDD")
    path_env = job_path(py)
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
    <key>PATH</key><string>{escape(path_env)}</string>
    <key>HOME</key><string>{escape(home)}</string>
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
    if dry:
        print(xml)
        print(f"(dry run) would write {plist} and load it — nothing changed")
        return
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    os.makedirs(logs, exist_ok=True)
    os.makedirs(agents, exist_ok=True)
    with open(plist, "w") as f:
        f.write(xml)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}")
        sys.exit(1)
    print(f"installed {label}: daily at {cfg.SCHEDULE_HOUR:02d}:{cfg.SCHEDULE_MINUTE:02d} + at login")
    print(f"  plist:  {plist}")
    print(f"  python: {py}")
    print(f"  PATH:   {path_env}")
    print(f"  logs:   {os.path.join(ROOT, 'pipeline', 'refresh.log')}  (narrative)  ·  {logs}/ (launchd)")
    if "--run-now" in sys.argv:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
        print("  triggered one run now — watch pipeline/refresh.log")


if __name__ == "__main__":
    main()
