#!/usr/bin/env python3
"""Project DD — insights generator (SEPARATE, on-demand step).

This is intentionally NOT part of the nightly token-free data pipeline. Run it
when you want fresh insights/actions. It reads the assembled app/data.js, derives
rule-based insights at territory / AE / account level, writes data/insights.json,
then re-runs build_data.py so the app picks them up.

  python3 insights.py            # deterministic, token-free
  ANTHROPIC_API_KEY=... python3 insights.py --ai   # (hook) AI-augmented from email

The deterministic engine flags: forecast gap risk, decelerating run-rate, weak QoQ,
commit coverage gaps, uncontracted consumption, falling accounts, and close-stage
use cases. Output schema is read by build_data.py / app.js:
  {territory:[{title,body}], byAE:{ae:{insights:[...]}}, byAcct:{id:{insights:[...]}}}
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
APP  = os.path.join(ROOT, "app")

def load_data():
    txt = open(os.path.join(APP, "data.js"), encoding="utf-8").read()
    s = txt.index("{"); e = txt.rindex("}") + 1
    return json.loads(txt[s:e])

def money(n):
    n = n or 0; a = abs(n)
    if a >= 1e6: return f"${n/1e6:.2f}".rstrip("0").rstrip(".") + "M"
    if a >= 1e3: return f"${round(n/1e3)}K"
    return f"${round(n)}"

def gen(data):
    cur = data["meta"]["curFQ"]
    ft = data["forecast"][cur]
    fr_by_ae = {r["ae"]: r for r in ft["rows"]}
    perAE = data["perAE"]; accts = {a["id"]: a for a in data["accounts"]}
    commit = data["commit"][cur]
    out = {"territory": [], "byAE": {}, "byAcct": {}, "_generated_by": "deterministic"}

    # ---- Territory ----
    tot = ft["total"]
    behind = [r["ae"] for r in ft["rows"] if (r.get("gapFcst") or 0) < 0.9]
    if behind:
        out["territory"].append({"title": "Forecast risk",
            "body": f"{len(behind)} AE(s) below 90% of Q2 forecast target: {', '.join(behind)}. "
                    f"Territory forecast gap {round((tot.get('gapFcst') or 0)*100)}% of {money(tot['target'])}."})
    decel = [ae for ae in perAE if perAE[ae]["t7d"] < perAE[ae]["t28d"]*0.95]
    if decel:
        out["territory"].append({"title": "Run-rate decelerating",
            "body": f"T7D below T28D for: {', '.join(decel)}. Recent week is softening vs the 28-day trend."})
    commit_tot = sum(o["amount"] for o in commit.get("all", []))
    out["territory"].append({"title": "Commit coverage",
        "body": f"{len(commit.get('all',[]))} open commit opps closing {cur} totalling {money(commit_tot)}; "
                f"territory still {money(max(0, tot['target']-tot['gttb']))} short of target on current GTTB."})

    # ---- per AE ----
    for ae in data["aeOrder"]:
        ins = []
        fr = fr_by_ae.get(ae, {}); m = perAE[ae]
        gap = fr.get("gapFcst")
        if gap is not None and gap < 0.9:
            ins.append({"title": "Behind forecast", "body": f"Forecast at {round(gap*100)}% of target ({money(fr.get('forecast') or 0)} vs {money(fr.get('target') or 0)}). GTTB {money(fr.get('gttb') or 0)} — {money(max(0,(fr.get('target') or 0)-(fr.get('gttb') or 0)))} to close."})
        elif gap is not None and gap >= 1.05:
            ins.append({"title": "Ahead of target", "body": f"Forecast {round(gap*100)}% of target — protect and pull forward upside."})
        if m["t7d"] < m["t28d"]*0.9:
            ins.append({"title": "Run-rate slipping", "body": f"T7D {money(m['t7d'])}/day is below T28D {money(m['t28d'])}/day — consumption cooling this week."})
        if (m.get("qoq") or 0) < 0:
            ins.append({"title": "Negative QoQ", "body": f"Run-rate down {abs(m['qoq'])}% vs last quarter — diagnose top declining accounts."})
        # falling / uncontracted accounts in this AE
        ae_accts = sorted([accts[i] for i in m.get("accounts", [])], key=lambda a: -(a.get("t28d") or 0))
        unc = [a for a in ae_accts if (a.get("arr") or 0) == 0 and (a.get("t28d") or 0) > 300]
        if unc:
            ins.append({"title": "Uncontracted consumption", "body": "Run-rate with $0 ARR — paper it: " + ", ".join(f"{a['name']} ({money(a['t28d'])}/day)" for a in unc[:3]) + "."})
        falling = [a for a in ae_accts if (a.get("wow28") or 0) < -8][:3]
        if falling:
            ins.append({"title": "Accounts falling", "body": "Consumption down WoW: " + ", ".join(f"{a['name']} {a['wow28']}%" for a in falling) + "."})
        out["byAE"][ae] = {"insights": ins}

    # ---- per account ----
    for aid, a in accts.items():
        ins = []
        if (a.get("arr") or 0) == 0 and (a.get("t28d") or 0) > 100:
            ins.append({"title": "Paper the run-rate", "body": f"$0 ARR but consuming {money(a['t28d'])}/day (run-rate ARR {money(a.get('t3mArr') or 0)}). Commercial paperwork is the gap."})
        if (a.get("wow28") or 0) < -10:
            ins.append({"title": "Consumption falling", "body": f"T28D down {a['wow28']}% WoW — investigate workload drop before it compounds."})
        if (a.get("qoq") or 0) > 25:
            ins.append({"title": "Expanding fast", "body": f"Run-rate up {a['qoq']}% QoQ — land an expansion / true-up now."})
        # close-stage open UCs
        ucs = [u for u in data["ucos"] if u["accountId"] == aid and u["stage"] in ("U4", "U5")]
        ucs.sort(key=lambda u: -(u.get("monthly") or 0))
        if ucs:
            u = ucs[0]
            ins.append({"title": "Close-stage use case", "body": f"{u['name']} at {u['stage']} ({money(u.get('monthly') or 0)}/mo) — push to live."})
        cs = commit.get("byAcct", {}).get(aid, [])
        if cs:
            ins.append({"title": "Commit this quarter", "body": f"{len(cs)} commit opp(s) closing {cur}: {money(sum(o['amount'] for o in cs))}."})
        if ins:
            out["byAcct"][aid] = {"insights": ins}
    return out

def main():
    data = load_data()
    ins = gen(data)
    if "--ai" in sys.argv and os.environ.get("ANTHROPIC_API_KEY"):
        # Hook point: augment per-AE 'urgent actions from email' via Gmail + Claude.
        # Left as an explicit opt-in; deterministic insights ship by default.
        ins["_generated_by"] = "deterministic (+AI hook not yet wired)"
    with open(os.path.join(DATA, "insights.json"), "w", encoding="utf-8") as f:
        json.dump(ins, f, ensure_ascii=False, indent=2)
    n = len(ins["territory"]) + sum(len(v["insights"]) for v in ins["byAE"].values()) + sum(len(v["insights"]) for v in ins["byAcct"].values())
    print(f"insights.json written — {n} insights ({len(ins['territory'])} territory, {len(ins['byAE'])} AEs, {len(ins['byAcct'])} accounts)")
    bld = subprocess.run([sys.executable, os.path.join(DATA, "build_data.py")], capture_output=True, text=True)
    print("rebuild:", bld.stdout.strip().splitlines()[-1] if bld.stdout.strip() else bld.stderr[:200])

if __name__ == "__main__":
    main()
