#!/usr/bin/env python3
"""Project DD — local manual-refresh helper.

The Project DD app (file://) cannot run Python directly (browser security). This
tiny localhost server bridges it: the in-app "Refresh now" button calls it, the
helper runs refresh.py (the SAME script the scheduled launchd job runs) and reports back
with a typed error code instead of a parse error. On success the app reloads to
pick up the freshly rebuilt data.js.

No Claude session is ever required: when a credential dies, the app gets a
SF_AUTH / DBX_AUTH code and shows a Re-authenticate button that calls /reauth
here, which runs the one fixed login command and opens the browser.

Endpoints (CORS-open, bound to 127.0.0.1 only):
  GET  /status   -> {ok, lastRefresh, busy, progress:{stage,pct}, result:{...}}
  POST /refresh  -> runs refresh.py synchronously (~15-20s healthy, <5s on a
                    dead credential); returns {ok, code, message, hint, reauth,
                    lastRefresh, elapsed}. HTTP 409 if one is already running.
  POST /reauth   -> body {"target":"sf"|"dbx"|"cloud"} (or /reauth/sf etc).
                    'cloud' = the Databricks profile the cloud mirror publishes
                    through; dead only blocks the publish, not the local data.
                    Runs EXACTLY one fixed command, opens the browser, waits for
                    the round-trip. Username and instance URL come from
                    config/territory.json and are never taken from the request.

Run:  python3 refresh_server.py   (or double-click "Open Project DD.command",
       which starts this automatically)
"""
import json, os, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ddcore as dd

HERE = dd.HERE
DATA = dd.DATA
REFRESH = os.path.join(HERE, "refresh.py")
STAMP = os.path.join(DATA, "last_refresh.txt")
PORT = int(os.environ.get("PORT", "8788"))
PYTHON = os.environ.get("DD_PYTHON", sys.executable)

# refresh.py shells out to the sf / databricks CLIs — give it the same minimal PATH
# launchd uses so the CLIs resolve regardless of how this server was started.
ENV = dict(os.environ)
ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + ENV.get("PATH", "")
ENV.setdefault("HOME", os.path.expanduser("~"))

# Two guards, deliberately: this one rejects a second click in THIS process; the
# flock inside refresh.py rejects an overlap with the 09:00 launchd job. Both
# surface to the app as HTTP 409.
_lock = threading.Lock()
_reauth_lock = threading.Lock()


def last_refresh():
    try:
        return open(STAMP).read().strip()
    except Exception:
        return None


def run_refresh():
    """Run refresh.py once. Returns (http_status, body dict)."""
    t0 = time.time()
    try:
        p = subprocess.run([PYTHON, REFRESH], capture_output=True, text=True,
                           env=ENV, timeout=420, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 200, {"ok": False, "code": dd.TIMEOUT,
                     "message": "refresh timed out after 420s",
                     "hint": dd.HINTS[dd.TIMEOUT], "detail": "", "reauth": None,
                     "elapsed": round(time.time() - t0, 1)}
    out = (p.stdout or "")
    res = dd.parse_result(out)
    if res is None:
        # refresh.py died before it could report — surface its real last words.
        lines = [l for l in ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
                 if l.strip() and not l.startswith(dd.RESULT_SENTINEL)]
        res = {"ok": False, "code": dd.INTERNAL,
               "message": f"refresh.py exited {p.returncode} without a result",
               "detail": dd.tail("\n".join(lines[-4:]), 300),
               "hint": dd.HINTS[dd.INTERNAL], "reauth": None,
               "elapsed": round(time.time() - t0, 1)}
    status = 409 if res.get("code") == dd.LOCKED else 200
    return status, res


def run_reauth(target):
    """Run exactly one fixed login command. Nothing from the request is used as
    an argument — `target` only selects between two hard-coded command lists."""
    cmd = dd.REAUTH_CMDS.get(target)
    if not cmd:
        return 400, {"ok": False, "code": "BAD_TARGET",
                     "message": "target must be one of: "
                                + ", ".join(f"'{k}'" for k in dd.REAUTH_CMDS)}
    if not _reauth_lock.acquire(blocking=False):
        return 409, {"ok": False, "code": "REAUTH_BUSY",
                     "message": "a re-authentication is already in progress"}
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=ENV,
                           timeout=300, stdin=subprocess.DEVNULL)
        ok = (p.returncode == 0)
        detail = dd.tail((p.stderr or "") + " " + (p.stdout or ""), 300)
        # Label from the shared map — a hard-coded sf/else pair silently
        # mislabelled the third target ('cloud') as logfood.
        label = dd.CRED_LABELS.get(target, target)
        # `sf org login web` leaves the default-org pointer wherever it was, so
        # the very next query can run against the wrong org. Same follow-up the
        # CLI path runs; skipped entirely when the login itself failed.
        if ok:
            for follow in ([dd.REAUTH_FOLLOWUP[target]]
                           if target in dd.REAUTH_FOLLOWUP else []):
                subprocess.run(follow, capture_output=True, text=True, env=ENV,
                               timeout=60, stdin=subprocess.DEVNULL)
        return 200, {"ok": ok, "target": target, "cmd": " ".join(cmd),
                     "message": (f"{label} re-authenticated"
                                 if ok else f"{label} login failed (exit {p.returncode})"),
                     "detail": detail, "elapsed": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return 200, {"ok": False, "target": target, "cmd": " ".join(cmd),
                     "message": "login window timed out after 300s — "
                                "finish the browser login, then click Refresh again",
                     "detail": "", "elapsed": round(time.time() - t0, 1)}
    except FileNotFoundError:
        return 200, {"ok": False, "target": target, "cmd": " ".join(cmd),
                     "code": dd.CLI_MISSING,
                     "message": f"{cmd[0]} is not on PATH for the helper",
                     "detail": ""}
    finally:
        _reauth_lock.release()


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST,GET,OPTIONS")

    def _json(self, obj, code=200):
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        # A run is "in flight" only while its progress file is both fresh and
        # unfinished — otherwise the last completed run would read as busy forever.
        prog = dd.read_progress(max_age=120)
        running = bool(prog and prog.get("pct", 100) < 100)
        self._json({"ok": True, "lastRefresh": last_refresh(),
                    "busy": _lock.locked() or running,
                    "progress": prog, "result": dd.read_result()})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/reauth"):
            target = path.split("/")[-1] if path not in ("/reauth", "") else None
            if target not in ("sf", "dbx"):
                target = (self._body().get("target") or "").strip().lower()
            code, body = run_reauth(target)
            return self._json(body, code)
        # default: /refresh
        if not _lock.acquire(blocking=False):
            return self._json({"ok": False, "code": dd.LOCKED,
                               "message": "a refresh is already running",
                               "hint": dd.HINTS[dd.LOCKED], "reauth": None,
                               "lastRefresh": last_refresh()}, 409)
        try:
            code, res = run_refresh()
            res["lastRefresh"] = last_refresh()
            # keep the old field name alive for anything still reading it
            res.setdefault("reason", res.get("message", ""))
            self._json(res, code)
        finally:
            _lock.release()

    def log_message(self, *a): pass


if __name__ == "__main__":
    print(f"Project DD refresh helper -> http://localhost:{PORT}")
    print("  POST /refresh  runs the same pull launchd runs at 09:00 daily.")
    print("  POST /reauth   {'target':'sf'|'dbx'|'cloud'} opens the browser login.")
    print("  (Ctrl-C to stop)")
    # THREADING server, not HTTPServer: a refresh occupies its connection for ~20s,
    # and a serial server would (a) queue a second click instead of 409-ing it and
    # (b) stall the /status progress polls behind the very run they are reporting on.
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
