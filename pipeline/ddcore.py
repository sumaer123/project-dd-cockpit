#!/usr/bin/env python3
"""Project DD — shared refresh plumbing. NO LLM, NO tokens, stdlib only.

Everything in here exists to answer one question honestly: *why* did the refresh
fail, and what should the human do about it? The old pipeline called
json.loads(r.stdout) on every CLI result without checking the return code, so a
dead Salesforce token, a dead Databricks token, a DNS blip and a genuine query
error all collapsed into the same useless string:

    Expecting value: line 1 column 1 (char 0)

This module replaces that with:
  * run_checked / sf_json / dbx_json  — return code + stderr always inspected
  * RefreshError(code, ...)           — typed, machine-readable failure codes
  * preflight()                       — auth gate that runs BEFORE any data pull
  * acquire_lock()                    — one flock shared by the helper and launchd
  * progress() / emit_result()        — what the UI polls instead of guessing

Imported by refresh.py (the pipeline) and refresh_server.py (the localhost helper).
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
LOG = os.path.join(HERE, "refresh.log")
LOCK_PATH = os.path.join(HERE, ".refresh.lock")
PROGRESS_PATH = os.path.join(HERE, ".refresh_progress.json")
RESULT_PATH = os.path.join(HERE, ".refresh_result.json")

# ---- identity. Read from config/territory.json (see ddconfig.py) — NEVER
# prompted for, never derived from an HTTP request.
import ddconfig as _cfg
SF_INSTANCE_URL = _cfg.SF_INSTANCE_URL
SF_USERNAME = _cfg.SF_USERNAME
DBX_PROFILE = _cfg.DBX_PROFILE
WAREHOUSE = _cfg.WAREHOUSE

# The cloud mirror publishes to a DIFFERENT Databricks workspace under a SEPARATE
# OAuth token from the data profile. It expires on its own clock, so it gets its
# own probe. Hosts are pinned because `databricks auth login` with no --host drops
# into an interactive prompt and hangs a non-tty caller forever.
CLOUD_ENABLED = _cfg.CLOUD_ENABLED
DBX_CLOUD_PROFILE = _cfg.DBX_CLOUD_PROFILE or "cloud-disabled"
DBX_LOGFOOD_HOST = _cfg.DBX_DATA_HOST
DBX_CLOUD_HOST = _cfg.DBX_CLOUD_HOST
CLOUD_APP_NAME = _cfg.CLOUD_APP_NAME or "the cloud app"

# The two re-auth commands, verbatim. The helper's /reauth endpoint runs exactly
# these — no arguments are ever taken from the HTTP request.
REAUTH_CMDS = {
    "sf": ["sf", "org", "login", "web",
           f"--instance-url={SF_INSTANCE_URL}", "--set-default"],
    "dbx": ["databricks", "auth", "login", "--host", DBX_LOGFOOD_HOST,
            "--profile", DBX_PROFILE],
    "cloud": ["databricks", "auth", "login", "--host", DBX_CLOUD_HOST,
              "--profile", DBX_CLOUD_PROFILE],
}

# `sf org login web` needs the default-org pointer reset afterwards or the very
# next query runs against whatever org was default before.
REAUTH_FOLLOWUP = {
    "sf": ["sf", "config", "set", f"target-org={SF_USERNAME}", "--global"],
}

# ---------------------------------------------------------------- error codes
SF_AUTH = "SF_AUTH"          # Salesforce session/refresh token is dead
DBX_AUTH = "DBX_AUTH"        # Databricks data profile is dead
CLOUD_AUTH = "CLOUD_AUTH"    # cloud profile dead — local data fine, cloud publish blocked
NETWORK = "NETWORK"          # DNS / connection / 5xx / transport
TIMEOUT = "TIMEOUT"          # a CLI call blew its wall clock
SF_QUERY = "SF_QUERY"        # SOQL / object / field error — not an auth problem
DBX_QUERY = "DBX_QUERY"      # SQL / warehouse error — not an auth problem
BUILD = "BUILD"              # build_data.py failed
LOCKED = "LOCKED"            # another refresh already holds the lock
CLI_MISSING = "CLI_MISSING"  # sf / databricks not on PATH
INTERNAL = "INTERNAL"        # anything we could not classify

HINTS = {
    SF_AUTH: "Salesforce login expired — click Re-authenticate (opens the "
             "Databricks SFDC login in your browser).",
    DBX_AUTH: f"Databricks {DBX_PROFILE} login expired — click Re-authenticate "
              f"(opens the {DBX_PROFILE} OAuth page in your browser).",
    CLOUD_AUTH: f"Databricks {DBX_CLOUD_PROFILE} login expired — the LOCAL cockpit is "
                f"still fresh, but the cloud mirror ({CLOUD_APP_NAME}) could not be "
                "published. Re-authenticate to bring the two back in sync.",
    NETWORK: "Network problem reaching Salesforce or Databricks. Check VPN / "
             "connectivity, then retry.",
    TIMEOUT: "A CLI call hung past its timeout. Retry; if it repeats, check VPN.",
    SF_QUERY: "A Salesforce query was rejected (SOQL/field/permission). The "
              "pipeline needs a code fix, not a re-login.",
    DBX_QUERY: "A Databricks SQL statement failed. Check the warehouse "
               f"({WAREHOUSE}) and the gtm_gold tables.",
    BUILD: "Data pulled fine but build_data.py failed. data.js is unchanged.",
    LOCKED: "A refresh is already running (button or 09:00 launchd job). Wait "
            "for it to finish.",
    CLI_MISSING: "The sf or databricks CLI is not on PATH for this process. If you "
                 "installed or moved either one, re-run `python3 setup/install_schedule.py` "
                 "so the daily job picks up the new location.",
    INTERNAL: "Unclassified failure — see pipeline/refresh.log for the raw error.",
}

REAUTH_FOR = {SF_AUTH: "sf", DBX_AUTH: "dbx", CLOUD_AUTH: "cloud"}


class RefreshError(RuntimeError):
    """A failure with a machine-readable code and the CLI's own words attached."""

    def __init__(self, code, message, detail="", transient=False):
        self.code = code
        self.message = message
        self.detail = detail or ""
        self.transient = transient
        super().__init__(self.__str__())

    def __str__(self):
        s = f"[{self.code}] {self.message}"
        if self.detail:
            s += f" | {self.detail}"
        return s

    def payload(self):
        return {"code": self.code, "message": self.message, "detail": self.detail,
                "hint": HINTS.get(self.code, ""), "reauth": REAUTH_FOR.get(self.code)}


# ------------------------------------------------------------------- logging
LOG_MAX_BYTES = 1_000_000
LOG_KEEP = 3


def rotate_log(path=LOG, max_bytes=LOG_MAX_BYTES, keep=LOG_KEEP):
    """Size-rotate a log file: refresh.log -> .1 -> .2 -> .3, oldest dropped."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(keep - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
    except Exception:
        pass  # logging must never be the thing that breaks a refresh


def truncate_if_big(path, max_bytes=512_000):
    """launchd owns its stdout/stderr fds, so those we cap rather than rotate.
    They only ever duplicate refresh.log, so nothing unique is lost."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            with open(path, "w"):
                pass
    except Exception:
        pass


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------- error-text classification
# Checked in this order. NETWORK wins over auth on purpose: "Unable to refresh
# session due to: getaddrinfo ENOTFOUND" is a transport failure wearing an auth
# error's clothes, and retrying it is correct where retrying a dead token is not.
NETWORK_PATTERNS = (
    "getaddrinfo", "enotfound", "eai_again", "econnrefused", "econnreset",
    "etimedout", "epipe", "socket hang up", "network is unreachable",
    "temporary failure in name resolution", "no such host", "dial tcp",
    "i/o timeout", "connection reset", "connection refused", "connection aborted",
    "tls handshake", "handshake failure", "502 bad gateway", "bad gateway",
    "503 service unavailable", "service unavailable", "504 gateway",
    "gateway timeout", "temporarily unavailable", "try again later",
    "proxy connect", "certificate has expired", "unable to get local issuer",
)
SF_AUTH_PATTERNS = (
    "refresh token", "token validity expired", "expired access", "invalid_grant",
    "invalid session id", "invalid_session_id", "unable to refresh session",
    "no authorization information", "authinfocreationerror", "namedorgnotfound",
    "noorgfound", "no default environment", "no default org", "not authorized to",
    "must be authenticated", "authentication failure", "requires a username",
    "no authinfo", "org not found", "sfdx_auth", "expired or invalid",
)
DBX_AUTH_PATTERNS = (
    "cannot configure default credentials", "default auth", "invalid_grant",
    "token expired", "expired token", "unauthorized", "401", "403",
    "not authenticated", "databricks auth login", "oauth", "refresh token",
    "cannot load", "no such profile", "profile not found", "credentials",
    "permission denied", "invalid access token",
)


def _hit(blob, patterns):
    return any(p in blob for p in patterns)


def classify(*texts, family="sf"):
    """(code, transient) from whatever the CLI actually said."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob.strip():
        return (INTERNAL, False)
    if _hit(blob, NETWORK_PATTERNS):
        return (NETWORK, True)
    if family == "sf" and _hit(blob, SF_AUTH_PATTERNS):
        return (SF_AUTH, False)
    if family == "dbx" and _hit(blob, DBX_AUTH_PATTERNS):
        return (DBX_AUTH, False)
    return (SF_QUERY if family == "sf" else DBX_QUERY, False)


def tail(s, n=300):
    """First ~n chars of a CLI's own error text, whitespace-collapsed."""
    return re.sub(r"\s+", " ", (s or "").strip())[:n]


def loads_tolerant(s):
    """Parse JSON that may be preceded by CLI chatter. Returns None, never raises.

    The databricks CLI writes notices to stderr, but this keeps us honest if any
    CLI ever leaks a banner onto stdout.
    """
    if not s:
        return None
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for opener in "{[":
        i = s.find(opener)
        if i > 0:
            try:
                return json.loads(s[i:])
            except Exception:
                continue
    return None


# ------------------------------------------------------------- subprocess layer
def run(cmd, timeout=300, ctx="", env=None):
    """subprocess.run that turns process-level failures into typed errors."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RefreshError(CLI_MISSING, f"{cmd[0]} is not on PATH",
                           f"while running: {ctx or ' '.join(cmd[:3])}")
    except subprocess.TimeoutExpired:
        raise RefreshError(TIMEOUT, f"{ctx or cmd[0]} timed out after {timeout}s",
                           "", transient=True)


def run_checked(cmd, timeout=300, ctx="", family="sf", env=None):
    """Run a CLI and REFUSE to continue on a non-zero exit. The ban on bare
    json.loads(r.stdout) lives here."""
    r = run(cmd, timeout=timeout, ctx=ctx, env=env)
    if r.returncode != 0:
        code, transient = classify(r.stderr, r.stdout, family=family)
        raise RefreshError(code, f"{ctx or cmd[0]} exited {r.returncode}",
                           tail(r.stderr or r.stdout), transient)
    return r


def sf_json(cmd, timeout=300, ctx="sf"):
    """`sf ... --json` always emits JSON — including on failure, where the JSON
    body carries a far better message than the exit code does. So parse first,
    then judge; fall back to stderr when there is no JSON at all."""
    r = run(cmd, timeout=timeout, ctx=ctx)
    data = loads_tolerant(r.stdout)
    if data is None:
        code, transient = classify(r.stderr, r.stdout, family="sf")
        raise RefreshError(code,
                           f"{ctx}: sf produced no JSON (exit {r.returncode})",
                           tail(r.stderr or r.stdout) or "empty stdout and stderr",
                           transient)
    if data.get("status") not in (0, None):
        msg = data.get("message") or data.get("name") or "unknown sf error"
        code, transient = classify(msg, str(data.get("name", "")), r.stderr,
                                   family="sf")
        raise RefreshError(code, f"{ctx}: {tail(msg, 200)}",
                           tail(json.dumps(data.get("data") or data.get("name") or ""), 300),
                           transient)
    return data


def dbx_json(cmd, timeout=300, ctx="databricks"):
    """databricks CLI: non-zero exit means the JSON body is unreliable, so the
    return code is checked BEFORE parsing."""
    r = run(cmd, timeout=timeout, ctx=ctx)
    if r.returncode != 0:
        code, transient = classify(r.stderr, r.stdout, family="dbx")
        raise RefreshError(code, f"{ctx} exited {r.returncode}",
                           tail(r.stderr or r.stdout) or "empty stdout and stderr",
                           transient)
    data = loads_tolerant(r.stdout)
    if data is None:
        code, transient = classify(r.stderr, r.stdout, family="dbx")
        raise RefreshError(code, f"{ctx}: databricks produced no JSON",
                           tail(r.stderr or r.stdout) or "empty stdout", transient)
    return data


def with_retry(fn, label, attempts=3, base_delay=2.0, on_retry=None):
    """2 retries with backoff — for TRANSIENT failures only.

    An expired token is not transient: retrying it wastes 6s and tells the human
    nothing new, so auth errors raise on the first attempt.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except RefreshError as e:
            last = e
            if not e.transient or i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            msg = (f"  {label}: transient {e.code} ({tail(e.detail or e.message, 120)}) "
                   f"— retry {i + 1}/{attempts - 1} in {delay:.0f}s")
            (on_retry or log)(msg)
            time.sleep(delay)
    raise last


# ------------------------------------------------------------------- preflight
def preflight_sf():
    """`sf org display --json` and assert Connected Status == Connected."""
    try:
        data = sf_json(["sf", "org", "display", "--target-org", SF_USERNAME, "--json"], timeout=20,
                       ctx="Salesforce preflight")
    except RefreshError as e:
        # A hung preflight is a transport problem, not a credential problem.
        if e.code == TIMEOUT:
            raise RefreshError(NETWORK, "Salesforce preflight timed out",
                               e.detail, transient=True)
        raise
    res = (data.get("result") or {})
    status = (res.get("connectedStatus") or "").strip()
    user = res.get("username") or ""
    if status.lower() != "connected":
        code, transient = classify(status, family="sf")
        if code in (SF_QUERY, INTERNAL):
            code, transient = SF_AUTH, False
        raise RefreshError(code, f"Salesforce not connected ({tail(status, 160)})",
                           f"org: {user or 'unknown'}", transient)
    return user or SF_USERNAME


def preflight_dbx():
    """Cheapest live probe on the data profile — no warehouse, no SQL."""
    try:
        data = dbx_json(["databricks", "current-user", "me", "--profile", DBX_PROFILE],
                        timeout=20, ctx="Databricks preflight")
    except RefreshError as e:
        if e.code == TIMEOUT:
            raise RefreshError(NETWORK, "Databricks preflight timed out",
                               e.detail, transient=True)
        raise
    who = data.get("userName") or data.get("displayName")
    if not who:
        raise RefreshError(DBX_AUTH, "Databricks profile returned no identity",
                           tail(json.dumps(data), 200))
    return who


def preflight_cloud():
    """Probe the cloud-mirror profile that the cloud mirror publishes through.

    Deliberately NON-FATAL and never raises: a dead cloud token must not cost
    the manager a local refresh, because local data is the thing they actually read.
    But it must not be SILENT either. Before this probe existed, refresh.py
    gated only SF + data, so an expired cloud-mirror let the run print
    `refresh OK` while deploy_cloud.py failed alone in a background log — which
    is exactly how the cockpit and its cloud mirror drifted a day apart on
    2026-08-26. The caller turns a False here into a loud line and a
    CLOUD_AUTH verdict instead of a shrug.
    """
    if not CLOUD_ENABLED:
        return {"ok": False, "who": "", "why": "cloud mirror disabled in config"}
    try:
        data = dbx_json(["databricks", "current-user", "me",
                         "--profile", DBX_CLOUD_PROFILE],
                        timeout=20, ctx="Databricks cloud preflight")
    except RefreshError as e:
        return {"ok": False, "who": "", "why": e.message}
    except Exception as e:                # noqa: BLE001 — never raise from here
        return {"ok": False, "who": "", "why": f"{type(e).__name__}: {e}"}
    who = data.get("userName") or data.get("displayName")
    if not who:
        return {"ok": False, "who": "", "why": "profile returned no identity"}
    return {"ok": True, "who": who, "why": ""}


def preflight(prechecked=None):
    """Gate ALL THREE credentials before a single row is pulled — ~3-4s healthy.

    Both Databricks probes ride DAEMON threads while the (slower, and far more
    often dead) Salesforce probe runs on the main thread. Daemon matters: when
    Salesforce fails we raise immediately and the process exits without waiting
    on the others, which is what keeps a dead-token verdict fast. Because all
    three run concurrently, adding the cloud probe costs ~0 wall time — it
    finishes well inside the Salesforce probe it runs beside.

    SF and the data profile are FATAL (no data without them). cloud-mirror is reported, not
    raised — see preflight_cloud().
    """
    t0 = time.time()
    # A --auth run has just probed all three, ~1s ago. Re-probing them here cost
    # a wasted ~4s on every interactive refresh — the single largest avoidable
    # chunk of the run. Reuse that verdict when it was clean. The window for a
    # token to die in between is ~1s, and if it does the first query raises a
    # typed auth error anyway, so nothing is silently lost.
    if prechecked and prechecked.get("sf") and prechecked.get("dbx"):
        w = prechecked.get("who") or {}
        return {"sf": w.get("sf") or SF_USERNAME, "dbx": w.get("dbx") or "",
                "cloud": {"ok": bool(prechecked.get("cloud")),
                          "who": w.get("cloud", ""),
                          "why": "" if prechecked.get("cloud")
                                 else (prechecked.get("detail", {}) or {}).get("cloud", "dead")},
                "elapsed": 0.0, "reused": True}
    box = {}

    def _probe_dbx():
        try:
            box["dbx"] = preflight_dbx()
        except RefreshError as e:
            box["err"] = e
        except Exception as e:            # noqa: BLE001 — never lose the reason
            box["err"] = RefreshError(INTERNAL, f"Databricks preflight: {e}")

    def _probe_cloud():
        box["cloud"] = preflight_cloud()  # never raises, by contract

    th = threading.Thread(target=_probe_dbx, daemon=True)
    thc = threading.Thread(target=_probe_cloud, daemon=True)
    th.start()
    thc.start()
    sf_user = preflight_sf()               # raises first if SF is the dead one
    th.join(timeout=25)
    if "err" in box:
        raise box["err"]
    if "dbx" not in box:
        raise RefreshError(NETWORK, "Databricks preflight did not return in 25s",
                           f"profile {DBX_PROFILE}", transient=True)
    # Never let a slow cloud probe hold the pipeline: whatever it has by now is
    # the answer, and "unknown" is treated as not-ok by the caller.
    thc.join(timeout=max(0.5, 25 - (time.time() - t0)))
    cloud = box.get("cloud") or {"ok": False, "who": "",
                                 "why": "cloud preflight did not return in time"}
    return {"sf": sf_user, "dbx": box["dbx"], "cloud": cloud,
            "elapsed": round(time.time() - t0, 2)}


# ------------------------------------------------------ interactive auth repair
CRED_LABELS = {"sf": "Salesforce", "dbx": f"Databricks {DBX_PROFILE}",
               "cloud": f"Databricks {DBX_CLOUD_PROFILE}"}


def auth_status():
    """Probe all three credentials CONCURRENTLY. Never raises. ~2-4s.

    Returns {"sf": bool, "dbx": bool, "cloud": bool, "detail": {...}}. Used by
    ensure_auth() and by the `--auth` fast path; deliberately separate from
    preflight() because this one reports on all three rather than raising on the
    first dead one — you cannot fix three tokens if you only ever hear about the
    first.
    """
    out, detail = {}, {}

    who = {}

    def _sf():
        try:
            who["sf"] = preflight_sf(); out["sf"] = True; detail["sf"] = "connected"
        except Exception as e:              # noqa: BLE001
            out["sf"] = False; detail["sf"] = tail(str(e), 160)

    def _dbx():
        try:
            who["dbx"] = preflight_dbx(); out["dbx"] = True; detail["dbx"] = "connected"
        except Exception as e:              # noqa: BLE001
            out["dbx"] = False; detail["dbx"] = tail(str(e), 160)

    def _cloud():
        r = preflight_cloud()
        out["cloud"] = r["ok"]; who["cloud"] = r["who"]
        detail["cloud"] = r["why"] or "connected"

    ths = [threading.Thread(target=f, daemon=True) for f in (_sf, _dbx, _cloud)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=30)
    return {"sf": out.get("sf", False), "dbx": out.get("dbx", False),
            "cloud": out.get("cloud", False), "who": who, "detail": detail}


def ensure_auth(log_fn=None, order=("sf", "dbx", "cloud")):
    """Probe all three, then open a browser login for EACH dead one, in order.

    INTERACTIVE ONLY. launchd must never call this: a 09:00 cron that silently
    pops three browser windows at a sleeping laptop is worse than a refresh that
    fails honestly. refresh.py gates it behind an explicit --auth for exactly
    that reason.

    Returns the post-repair auth_status(). Repairs are attempted for every dead
    credential even if one of them fails, because all three expire together (one
    SSO event kills the lot) and half-fixed auth just fails later and slower.
    """
    say = log_fn or (lambda m: None)
    st = auth_status()
    dead = [k for k in order if not st[k] and (k != "cloud" or CLOUD_ENABLED)]
    if not dead:
        say(f"  auth: all live (SF · {DBX_PROFILE}" + (f" · {DBX_CLOUD_PROFILE})" if CLOUD_ENABLED else ")"))
        return st
    say(f"  auth: {len(dead)} dead ({', '.join(CRED_LABELS[k] for k in dead)}) "
        f"— opening browser login")
    for k in dead:
        say(f"  auth: re-authenticating {CRED_LABELS[k]} …")
        try:
            r = run(REAUTH_CMDS[k], timeout=300, ctx=f"reauth {k}")
            if r.returncode != 0:
                say(f"  auth: {CRED_LABELS[k]} FAILED — "
                    f"{tail(r.stderr or r.stdout, 200)}")
                continue
            for follow in ([REAUTH_FOLLOWUP[k]] if k in REAUTH_FOLLOWUP else []):
                run(follow, timeout=60, ctx=f"reauth-followup {k}")
            say(f"  auth: {CRED_LABELS[k]} ok")
        except Exception as e:              # noqa: BLE001 — one failure != stop
            say(f"  auth: {CRED_LABELS[k]} FAILED — {type(e).__name__}: {e}")
    st = auth_status()
    say("  auth: " + " · ".join(
        f"{CRED_LABELS[k]} {'OK' if st[k] else 'DEAD'}" for k in order))
    return st


# ------------------------------------------------------- single-flight locking
def acquire_lock():
    """One exclusive flock for BOTH entry points (the button and launchd).

    refresh_server.py deliberately does NOT hold this lock while it spawns
    refresh.py — the child takes it, so there is exactly one owner and no
    parent/child deadlock. An overlapping run raises LOCKED, which the helper
    turns into HTTP 409.
    """
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            holder = os.read(fd, 64).decode("utf-8", "replace").strip()
        except Exception:
            pass
        os.close(fd)
        raise RefreshError(LOCKED, "another refresh is already running"
                           + (f" (pid {holder})" if holder else ""))
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    try:
        os.fsync(fd)
    except Exception:
        pass
    return fd


def release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def lock_holder_pid():
    """Best-effort read of who holds the lock, without taking it."""
    try:
        with open(LOCK_PATH) as f:
            return f.read().strip() or None
    except Exception:
        return None


# ------------------------------------------------------- progress + result I/O
def _atomic_write(path, obj):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        pass


def progress(stage, pct):
    _atomic_write(PROGRESS_PATH, {"stage": stage, "pct": int(pct),
                                  "pid": os.getpid(), "ts": time.time()})


def read_progress(max_age=900):
    try:
        with open(PROGRESS_PATH) as f:
            p = json.load(f)
        if time.time() - float(p.get("ts", 0)) > max_age:
            return None
        return p
    except Exception:
        return None


RESULT_SENTINEL = "__DD_RESULT__ "


def emit_result(ok, code="", message="", detail="", hint="", reauth=None, elapsed=0.0):
    """Last line of stdout is a machine-readable verdict the helper parses, and
    the same object is persisted for /status."""
    obj = {"ok": bool(ok), "code": code, "message": message, "detail": detail,
           "hint": hint, "reauth": reauth, "elapsed": round(elapsed, 1),
           "ts": time.time()}
    _atomic_write(RESULT_PATH, obj)
    print(RESULT_SENTINEL + json.dumps(obj), flush=True)
    return obj


def parse_result(stdout):
    """Pull the verdict back out of a refresh.py run. None if it never got there."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(RESULT_SENTINEL):
            try:
                return json.loads(line[len(RESULT_SENTINEL):])
            except Exception:
                return None
    return None


def read_result():
    try:
        with open(RESULT_PATH) as f:
            return json.load(f)
    except Exception:
        return None
