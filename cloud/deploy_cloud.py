#!/usr/bin/env python3
"""Auto-publish the Project DD cloud mirror (your Databricks App — config databricks_cloud.app_name).

Called BEST-EFFORT and DETACHED by pipeline/refresh.py right after a successful
data promote, and runnable by hand. It self-guards on three fronts and can never
harm a refresh:

  1. Single-flight  — a flock so two publishes never overlap.
  2. Auth gate      — if the cloud Databricks login is not live, it logs and
                      SKIPS (exit 0). The cloud being unreachable is not a failure.
  3. Change gate    — if app/data.js AND app/hygiene_data.js are byte-identical to
                      the last published blobs, it SKIPS (no redundant deploy).
                      --force overrides this.

Every run is appended to cloud/.deploy/AUTO_DEPLOY.log. The FIRST deploy (creating
the app) is a manual step — see the setup guide; this script only keeps an
already-created app fresh.

    python3 cloud/deploy_cloud.py           # publish iff auth live AND data changed
    python3 cloud/deploy_cloud.py --force    # publish regardless of the data hash
"""
import datetime
import fcntl
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
DEPLOY_DIR = os.path.join(HERE, ".deploy")
LOG = os.path.join(DEPLOY_DIR, "AUTO_DEPLOY.log")
LOCK = os.path.join(DEPLOY_DIR, ".publish.lock")
HASH_FILE = os.path.join(DEPLOY_DIR, ".last_data_hash")

sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import ddconfig as cfg  # noqa: E402
PROFILE = cfg.DBX_CLOUD_PROFILE
APP_NAME = cfg.CLOUD_APP_NAME
# Workspace folder (under /Workspace/Users/<you>/) that holds the app's source files.
WS_SOURCE = cfg.CLOUD_WS_SOURCE
# Only the runtime files reach the workspace — never the build/deploy scripts,
# the ledger dir, or OS cruft.
EXCLUDE = [".venv", "__pycache__", ".git", "*.pyc", ".DS_Store", "Icon\r",
           "build_cloud.py", "deploy_cloud.py", ".deploy"]


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        # size-cap the log so an unattended job can't grow it without bound
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512_000:
            with open(LOG, "w"):
                pass
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, timeout, ctx):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        log(f"SKIP: `{cmd[0]}` not on PATH ({ctx})")
        return None
    except subprocess.TimeoutExpired:
        log(f"FAIL: {ctx} timed out after {timeout}s")
        return None


# The change gate covers BOTH generated feeds. data.js is required; hygiene_data.js
# (the Hygiene module's, written by a separate pipeline) is folded in when present,
# so a refresh that only moved the hygiene scores still publishes instead of being
# skipped as "unchanged".
HASHED = ["data.js", "hygiene_data.js", "rob_content.js"]


def data_hash():
    h = hashlib.sha256()
    for name in HASHED:
        path = os.path.join(APP, name)
        if not os.path.exists(path):
            if name == "data.js":
                raise FileNotFoundError(path)
            continue
        h.update(name.encode())
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def auth_user():
    """Return the workspace username if the cloud profile is live, else None (soft skip)."""
    import json
    r = run(["databricks", "current-user", "me", "--profile", PROFILE], 25,
            "auth check")
    if r is None or r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("userName")
    except Exception:
        return None


def publish(force):
    os.makedirs(DEPLOY_DIR, exist_ok=True)

    if not (cfg.CLOUD_ENABLED and PROFILE and APP_NAME):
        log("SKIP: cloud mirror disabled or not configured (config/territory.json -> databricks_cloud).")
        return 0
    # 2) auth gate
    user = auth_user()
    if not user:
        log(f"SKIP: {PROFILE} auth not live — cloud mirror left as-is.")
        return 0

    # 3) change gate
    try:
        cur = data_hash()
    except Exception as e:
        log(f"SKIP: cannot read the generated data feeds ({e})")
        return 0
    prev = ""
    if os.path.exists(HASH_FILE):
        try:
            prev = open(HASH_FILE).read().strip()
        except Exception:
            prev = ""
    if cur == prev and not force:
        log("SKIP: data feeds unchanged since last publish (use --force to override).")
        return 0

    # stage the bundle from the single source of truth (app/)
    r = run([sys.executable, os.path.join(HERE, "build_cloud.py")], 60, "build_cloud")
    if r is None or r.returncode != 0:
        log(f"FAIL: build_cloud.py — {(r.stderr or r.stdout).strip()[:300] if r else 'no result'}")
        return 1
    log(r.stdout.strip() or "build_cloud ok")

    ws_path = f"/Workspace/Users/{user}/{WS_SOURCE}"

    # sync only the runtime files
    sync_cmd = ["databricks", "sync", HERE, ws_path, "--profile", PROFILE]
    for pat in EXCLUDE:
        sync_cmd += ["--exclude", pat]
    r = run(sync_cmd, 180, "databricks sync")
    if r is None or r.returncode != 0:
        log(f"FAIL: databricks sync — {(r.stderr or r.stdout).strip()[:300] if r else 'no result'}")
        return 1
    log("sync ok")

    # deploy
    r = run(["databricks", "apps", "deploy", APP_NAME,
             "--source-code-path", ws_path, "--profile", PROFILE], 300,
            "databricks apps deploy")
    if r is None or r.returncode != 0:
        log(f"FAIL: apps deploy — {(r.stderr or r.stdout).strip()[:300] if r else 'no result'}")
        return 1

    # verify (best-effort)
    import json
    g = run(["databricks", "apps", "get", APP_NAME, "--profile", PROFILE], 60,
            "apps get")
    state = ""
    if g and g.returncode == 0:
        try:
            state = (json.loads(g.stdout).get("active_deployment", {})
                     .get("status", {}).get("state", ""))
        except Exception:
            pass
    log(f"DEPLOYED {APP_NAME} (deployment state: {state or 'unknown'}) — data pushed.")

    # remember what we published so the next unchanged run is a no-op
    try:
        with open(HASH_FILE, "w") as f:
            f.write(cur)
    except Exception:
        pass
    return 0


def main():
    force = "--force" in sys.argv[1:]
    os.makedirs(DEPLOY_DIR, exist_ok=True)
    # single-flight: never let two publishes race
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("SKIP: another cloud publish is already running.")
        os.close(fd)
        return 0
    try:
        return publish(force)
    except Exception as e:  # a detached job must never crash noisily
        log(f"FAIL: unexpected {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
