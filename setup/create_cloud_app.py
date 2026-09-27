#!/usr/bin/env python3
"""First-time creation + deploy of the cloud mirror (a Databricks App). Run ONCE.

After this, every successful refresh republishes the app automatically
(pipeline/refresh.py -> cloud/deploy_cloud.py), so you never run this again
unless you rename or recreate the app.

Steps:
  1. checks config/territory.json has databricks_cloud.enabled = true
  2. checks the cloud CLI profile is logged in
  3. `databricks apps create <app_name>` (skipped if it already exists; creating
     the app's compute takes 2–5 minutes the first time)
  4. builds the bundle (cloud/build_cloud.py) and deploys it (deploy_cloud.py --force)
  5. prints the app URL and saves it to the config (databricks_cloud.app_url)

The app is private to your workspace by default: only workspace users you grant
"Can use" (Compute › Apps › <app> › Permissions) can open it.

    python3 setup/create_cloud_app.py
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import ddconfig as cfg  # noqa: E402


def run(cmd, timeout=600):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def app_get(profile, name):
    r = run(["databricks", "apps", "get", name, "--profile", profile, "--output", "json"], 60)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def main():
    if not (cfg.CLOUD_ENABLED and cfg.DBX_CLOUD_PROFILE and cfg.CLOUD_APP_NAME):
        sys.exit("Cloud mirror is not configured. Run `python3 setup/configure.py` and answer "
                 "yes at 'Set up the cloud mirror now?'.")
    prof, name = cfg.DBX_CLOUD_PROFILE, cfg.CLOUD_APP_NAME
    if not os.path.exists(os.path.join(ROOT, "app", "data.js")):
        sys.exit("app/data.js does not exist yet — run `python3 pipeline/refresh.py --auth` first.")
    me = run(["databricks", "current-user", "me", "--profile", prof, "--output", "json"], 60)
    if me.returncode != 0:
        sys.exit(f"Profile {prof} is not logged in. Run: databricks auth login "
                 f"--host {cfg.DBX_CLOUD_HOST} --profile {prof}")
    print(f"✓ {prof} live as {json.loads(me.stdout).get('userName')}")

    app = app_get(prof, name)
    if app:
        print(f"✓ app {name} already exists — redeploying")
    else:
        print(f"creating app {name} (first compute start takes 2–5 min) …")
        r = run(["databricks", "apps", "create", name, "--profile", prof,
                 "--description", "Project DD — read-only territory cockpit"], 900)
        if r.returncode != 0:
            sys.exit(f"apps create failed: {(r.stderr or r.stdout)[:600]}")
        for _ in range(60):
            app = app_get(prof, name) or {}
            st = (app.get("compute_status") or {}).get("state", "")
            if st in ("ACTIVE", "ERROR", "STOPPED"):
                break
            time.sleep(10)
        print(f"✓ app created (compute: {(app.get('compute_status') or {}).get('state', 'unknown')})")

    r = run([sys.executable, os.path.join(ROOT, "cloud", "deploy_cloud.py"), "--force"], 900)
    print((r.stdout or "").strip()[-1500:])
    if r.returncode != 0 or "DEPLOYED" not in (r.stdout or ""):
        sys.exit("deploy did not report DEPLOYED — see cloud/.deploy/AUTO_DEPLOY.log")

    app = app_get(prof, name) or {}
    url = app.get("url", "")
    if url:
        p = cfg.CONFIG_PATH
        c = json.load(open(p))
        c.setdefault("databricks_cloud", {})["app_url"] = url
        with open(p, "w") as f:
            f.write(json.dumps(c, indent=2, ensure_ascii=False) + "\n")
    print(f"\n✓ cloud mirror live: {url or '(see Compute › Apps in the workspace)'}")
    print("  Share it: workspace › Compute › Apps › " + name + " › Permissions › add users (Can use).")


if __name__ == "__main__":
    main()
