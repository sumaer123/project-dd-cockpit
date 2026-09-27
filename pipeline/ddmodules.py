#!/usr/bin/env python3
"""Project DD — app-module manifest and post-build verification. NO LLM, stdlib only.

WHY THIS EXISTS
---------------
The refresh used to prove exactly one thing: that build_data.py exited 0. That is
a much weaker claim than "the app is fresh". Every module in the cockpit draws
from a different corner of the bundle, and several are built by steps that were
deliberately non-fatal — the hygiene rebuild is best-effort, Pipeline Coverage is
computed deep inside build_data.py, RoB reads its own snapshot dir. So any of
them could silently go stale or empty while the run still printed `refresh OK`.
That is not a hypothetical: a dead cloud token once let the local refresh report
success while the cloud mirror sat a full day behind.

This module turns each nav entry in the sidebar into an explicit, checkable
contract: the keys it needs, how many rows counts as "populated", and how stale
its own timestamp is allowed to get. verify() runs after the build and returns a
per-module verdict, so "all parts of the app were made" becomes something the
pipeline asserts rather than something a human hopes.

Verdicts are OK / EMPTY / MISSING / STALE. Only MISSING and EMPTY are treated as
failures by the caller — STALE is reported loudly but does not fail a run,
because a module can legitimately lag (RoB snapshots are weekly).
"""
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")

OK, EMPTY, MISSING, STALE = "OK", "EMPTY", "MISSING", "STALE"

# Sentinel for verify()'s bundle args. Using None as "read it off disk" would
# leave no way to say "this bundle is ABSENT" — the exact case the Hygiene
# module needs to be testable in, since its build step is allowed to fail.
_LOAD = object()


def _read_js_object(path, var):
    """Parse `window.<var> = { ... };` out of a generated JS bundle.

    These files are machine-written by our own builders, so the object is always
    the first `{` after the assignment and the last `}` in the file. Regex-free
    on purpose: the payload contains braces inside strings, so slicing on the
    assignment and the final brace is both faster and safer than trying to match
    balanced delimiters.
    """
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        s = f.read()
    marker = f"window.{var}"
    i = s.find(marker)
    if i < 0:
        return None
    start = s.find("{", i)
    end = s.rstrip().rstrip(";").rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except Exception:
        return None


def _dig(obj, path):
    """Fetch a dotted path, returning None rather than raising on any miss."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _size(v):
    if v is None:
        return 0
    if isinstance(v, (list, dict, str)):
        return len(v)
    return 1


# Every entry mirrors one block in renderSidebar() (app/app.js). `src` picks the
# bundle; `keys` are the paths that must exist and be non-empty; `minrows` is the
# smallest count that still means "populated" for the headline key.
MODULES = [
    {"label": "Territory",          "src": "data",
     "keys": ["territory", "perAE", "accounts", "prodMatrix"],
     "count": "accounts", "minrows": 1},
    {"label": "Quarter Scorecard",  "src": "data",
     "keys": ["perAE", "qbounds", "qorder", "aeOrder"],
     "count": "aeOrder", "minrows": 1},
    {"label": "Opportunities",      "src": "data",
     "keys": ["opps", "oppsWon", "commit"],
     "count": "opps", "minrows": 1},
    {"label": "New Pipeline",       "src": "data",
     "keys": ["opps", "weekly"],
     "count": "opps", "minrows": 1},
    {"label": "Pipeline Coverage",  "src": "data",
     "keys": ["pipecov.perAE", "pipecov.team", "pipecov.curQ"],
     "count": "pipecov.perAE", "minrows": 1},
    {"label": "RoB — Weekly Review", "src": "data",
     "keys": ["rob.curFriday", "rob.fridays"],
     "count": "rob.fridays", "minrows": 1},
    {"label": "Partner Exposure",     "src": "data",
     "keys": ["ucos", "accounts"],
     "count": "ucos", "minrows": 1},
]

# Next-steps capture is a data-quality contract, not a module: the fields are
# pulled every run (Demand_Plan_Next_Steps__c on UCOs, Next_Step_Detail__c on
# opps) and land as `nextStep`. Coverage swinging to zero means the SOQL or the
# mapper broke, which no key-existence check would ever catch.
NEXTSTEP_CHECKS = [
    {"label": "UCO next steps", "coll": "ucos", "field": "nextStep",
     "stamp": "nextStepUpdated"},
    {"label": "Opp next steps", "coll": "opps", "field": "nextStep",
     "stamp": "nextStepUpd"},
]


# The ConsumptionPlan feed is a standing step of the protocol, not a one-off:
# every run re-pulls each AE's forecast (Consumption_Forecast__c 'AE Forecast') and
# quota (ConsumptionExt__c 'User') — the two objects behind
# databricks.lightning.force.com/c/ConsumptionPlan.app — into raw_cp_forecast.json
# and raw_cp_targets.json, and build_data.py stamps which AEs the live values
# actually reached (forecastInputs[q].cpLive / .cpTargetLive).
CP_RAW = ("raw_cp_forecast.json", "raw_cp_targets.json")
CP_MAXAGE_H = 36


def _cp_row(data):
    """Verify the live ConsumptionPlan feed for the CURRENT quarter.

    Why this is a checked contract and not merely a build step: build_data.py
    deliberately falls back to the frozen manual forecast.json / QUOTA table
    whenever a live value is missing or zero. That fallback is what keeps the app
    up when SFDC is unreachable — but it also means a broken SOQL, a renamed
    RecordType, or a promote that never happened would leave every screen looking
    perfectly healthy while the cockpit quietly served forecasts typed off a
    2026-06-02 screenshot. So: zero live AEs in the current quarter is a hard
    failure, and a raw CP file that did not get rewritten is reported STALE.
    """
    if data is None:
        return {"label": "Consumption forecast", "verdict": MISSING, "n": 0,
                "note": "data bundle unreadable"}
    q = _dig(data, "pipecov.curQ")
    fi = (data.get("forecastInputs") or {}).get(q) if q else None
    if not isinstance(fi, dict) or "cpLive" not in fi:
        return {"label": "Consumption forecast", "verdict": MISSING, "n": 0,
                "note": f"no forecastInputs.cpLive for {q or 'current quarter'}"}

    live = fi.get("cpLive") or {}
    tlive = fi.get("cpTargetLive") or {}
    sub = fi.get("cpSubmitted") or {}
    tot = len(live)
    nf = sum(1 for v in live.values() if v)
    nt = sum(1 for v in tlive.values() if v)
    note = f"{q} {nf}/{tot} forecasts · {nt}/{tot} targets live"
    dates = sorted(d for d in sub.values() if d)
    if dates:
        note += f", oldest submit {dates[0]}"

    if nf == 0 or nt == 0:
        return {"label": "Consumption forecast", "verdict": EMPTY, "n": nf,
                "note": note + " — CP pull empty, app is on the MANUAL fallback"}

    # a live-looking bundle built from yesterday's raw files is the quiet failure
    data_dir = os.path.join(ROOT, "data")
    gone = [f for f in CP_RAW if not os.path.exists(os.path.join(data_dir, f))]
    if gone:
        return {"label": "Consumption forecast", "verdict": MISSING, "n": nf,
                "note": note + " — missing " + ", ".join(gone)}
    oldest = min(os.path.getmtime(os.path.join(data_dir, f)) for f in CP_RAW)
    age_h = (time.time() - oldest) / 3600.0
    if age_h > CP_MAXAGE_H:
        return {"label": "Consumption forecast", "verdict": STALE, "n": nf,
                "note": note + f" — raw CP files {age_h:.0f}h old"}

    # Partial coverage is legitimate for a future quarter but not for THIS one:
    # an AE with no live row is an AE whose number came off the frozen manual
    # file. Warn (STALE never fails a run) and name them, so the gap is a fact
    # about that AE's submission rather than a mystery about the pipeline.
    behind = sorted(ae for ae in live if not (live.get(ae) and tlive.get(ae)))
    if behind:
        return {"label": "Consumption forecast", "verdict": STALE, "n": nf,
                "note": note + " — on manual fallback: " + ", ".join(behind)}
    return {"label": "Consumption forecast", "verdict": OK, "n": nf, "note": note}


def _mgr_fc_row(data):
    """Verify the manager's OWN submitted forecast (data.myForecast) for the current quarter.

    Same silent-fallback logic as _cp_row: the "My Forecast" row is simply hidden when
    the Manager Forecast pull returns nothing, so without this check a broken SOQL or a
    renamed RecordType would make the manager's number quietly vanish while every other screen
    looked healthy. Missing key = MISSING; no value for the current quarter = EMPTY; raw
    file not rewritten = STALE. STALE/EMPTY warn but do not throw away a good pull.
    """
    if data is None:
        return {"label": "My forecast", "verdict": MISSING, "n": 0,
                "note": "data bundle unreadable"}
    q = _dig(data, "pipecov.curQ")
    mfc = data.get("myForecast")
    if not isinstance(mfc, dict):
        return {"label": "My forecast", "verdict": MISSING, "n": 0,
                "note": "no myForecast in data.js"}
    cur = mfc.get(q) if q else None
    if not cur or cur.get("forecast") is None:
        return {"label": "My forecast", "verdict": EMPTY, "n": 0,
                "note": f"no Manager Forecast for {q or 'current quarter'} — 'My Forecast' row hidden"}
    note = f"{q} ${cur['forecast']:,.0f}"
    if cur.get("submitted"):
        note += f", submitted {cur['submitted']}"
    if cur.get("rollup") is not None:
        note += f" (AE rollup ${cur['rollup']:,.0f})"
    raw = os.path.join(ROOT, "data", "raw_cp_mgr_forecast.json")
    if not os.path.exists(raw):
        return {"label": "My forecast", "verdict": MISSING, "n": 1,
                "note": note + " — missing raw_cp_mgr_forecast.json"}
    age_h = (time.time() - os.path.getmtime(raw)) / 3600.0
    if age_h > CP_MAXAGE_H:
        return {"label": "My forecast", "verdict": STALE, "n": 1,
                "note": note + f" — raw file {age_h:.0f}h old"}
    return {"label": "My forecast", "verdict": OK, "n": 1, "note": note}


def _parse_iso(ts):
    """Best-effort ISO-8601 -> epoch. Returns None on anything unparseable."""
    if not ts or not isinstance(ts, str):
        return None
    t = ts.strip().replace("Z", "+0000")
    t = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", t)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            import datetime as _dt
            return _dt.datetime.strptime(t, fmt).timestamp()
        except Exception:
            continue
    return None


def verify(data=_LOAD, hyg=_LOAD):
    """Check every app module. Returns (rows, summary) — never raises.

    Pass nothing to read both bundles off disk (the normal pipeline call). Pass
    an explicit None to assert the ABSENT case, or a dict to check one in memory.

    rows: [{label, verdict, n, note}] in sidebar order.
    summary: {ok, empty, missing, stale, failed(bool), line(str)}
    """
    if data is _LOAD:
        data = _read_js_object(os.path.join(APP, "data.js"), "DD_DATA")
    if hyg is _LOAD:
        hyg = _read_js_object(os.path.join(APP, "hygiene_data.js"), "HYGIENE_DATA")

    rows = []
    for m in MODULES:
        obj = data if m["src"] == "data" else hyg
        if obj is None:
            rows.append({"label": m["label"], "verdict": MISSING, "n": 0,
                         "note": f"{m['src']} bundle unreadable"})
            continue
        missing = [k for k in m["keys"] if _dig(obj, k) is None]
        if missing:
            rows.append({"label": m["label"], "verdict": MISSING, "n": 0,
                         "note": "no " + ", ".join(missing)})
            continue
        n = _size(_dig(obj, m["count"]))
        if n < m.get("minrows", 1):
            rows.append({"label": m["label"], "verdict": EMPTY, "n": n,
                         "note": f"{m['count']} has {n} rows"})
            continue
        note = ""
        verdict = OK
        if m.get("tskey"):
            ts = _parse_iso(_dig(obj, m["tskey"]))
            if ts is not None:
                age_h = (time.time() - ts) / 3600.0
                if age_h > m.get("maxage_h", 36):
                    verdict, note = STALE, f"built {age_h:.0f}h ago"
        rows.append({"label": m["label"], "verdict": verdict, "n": n,
                     "note": note})

    # live ConsumptionPlan feed (forecasts + quota targets) for the current quarter
    rows.append(_cp_row(data))
    # the manager's OWN submitted Manager Forecast for the current quarter
    rows.append(_mgr_fc_row(data))

    # next-steps capture coverage
    if data is not None:
        for c in NEXTSTEP_CHECKS:
            coll = data.get(c["coll"]) or []
            tot = len(coll)
            got = sum(1 for r in coll
                      if isinstance(r, dict) and str(r.get(c["field"]) or "").strip())
            stamped = sum(1 for r in coll
                          if isinstance(r, dict) and r.get(c["stamp"]))
            pct = (100.0 * got / tot) if tot else 0.0
            # Zero captured across a non-empty collection means the pull or the
            # mapper broke — that is a hard failure, not a hygiene observation.
            verdict = OK if (tot == 0 or got > 0) else EMPTY
            rows.append({"label": c["label"], "verdict": verdict, "n": got,
                         "note": f"{got}/{tot} populated ({pct:.0f}%), "
                                 f"{stamped} stamped"})

    ok = sum(1 for r in rows if r["verdict"] == OK)
    empty = sum(1 for r in rows if r["verdict"] == EMPTY)
    miss = sum(1 for r in rows if r["verdict"] == MISSING)
    stale = sum(1 for r in rows if r["verdict"] == STALE)
    parts = [f"{ok} ok"]
    if stale:
        parts.append(f"{stale} stale")
    if empty:
        parts.append(f"{empty} EMPTY")
    if miss:
        parts.append(f"{miss} MISSING")
    summary = {"ok": ok, "empty": empty, "missing": miss, "stale": stale,
               "failed": bool(empty or miss),
               "line": f"{len(rows)} checks — " + ", ".join(parts)}
    return rows, summary


def format_rows(rows):
    """One compact line per check, for refresh.log."""
    mark = {OK: "OK ", STALE: "~", EMPTY: "!!", MISSING: "XX"}
    out = []
    for r in rows:
        tag = mark.get(r["verdict"], "??")
        note = f" — {r['note']}" if r["note"] else ""
        out.append(f"    [{tag}] {r['label']:<20} n={r['n']}{note}")
    return out


if __name__ == "__main__":
    rows, summary = verify()
    print("\n".join(format_rows(rows)))
    print(summary["line"])
    raise SystemExit(1 if summary["failed"] else 0)
