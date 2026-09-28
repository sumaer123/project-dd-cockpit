#!/usr/bin/env python3
"""Assemble raw SFDC + Databricks pulls into app/data.js for Project DD.

Pure data assembly — NO LLM, safe to run from cron. Reads raw_*.json + forecast.json
(manual) and writes ../app/data.js.

Produces:
  - run rates (T7D/T28D, WoW, QoQ, weekly) at territory / AE / account level
  - central FORECAST table (Q1 Base, QTD, T7D, Days Left, GTTB, Q2 Forecast, QoQ, Gap)
  - MECE compute-product matrix from billing SKUs (Jobs/DBSQL/All-Purpose split
    Classic/Photon/Serverless + DLT, GenAI, Lakebase, Other) per account +
    territory top-N — buckets sum exactly to total; attributed views (Unity
    Catalog, AI/BI, Photon overlay) are excluded because they double-count
  - weekly activity per quarter: NEW opps, NEW use cases, PROGRESSED use cases
  - open COMMIT opportunities per quarter
  - open use cases (U1-U5)
"""
import json, os, datetime
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.abspath(__file__))
APP  = os.path.join(os.path.dirname(HERE), "app")

def load(name): return json.load(open(os.path.join(HERE, name)))
def num(v):
    try: return round(float(v), 2)
    except (TypeError, ValueError): return 0.0
def numn(v):   # null-PRESERVING numeric: None stays None (never coerced to 0)
    try: return round(float(v), 2)
    except (TypeError, ValueError): return None
def D(s): return datetime.date.fromisoformat(s)
def dt10(s): return s[:10] if s else None   # SF datetime -> YYYY-MM-DD

# ---- Roster, fiscal calendar and territory label: ALL from config/territory.json ----
# (pipeline/ddconfig.py). setup/configure.py discovers the AEs from Salesforce.
import sys
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "pipeline"))
import ddconfig as cfg
AE_ORDER = list(cfg.AE_ORDER)
QORDER = list(cfg.QORDER)                       # prior FY Q1..Q4 + current FY Q1..Q4
QBOUNDS = {k: tuple(v) for k, v in cfg.QBOUNDS.items()}   # DBX fiscal: starts Feb 1
FYL, PFYL = cfg.FY_LABEL, cfg.PRIOR_FY_LABEL     # "FY27", "FY26"
# MECE compute-product buckets from billing SKUs (sku_consumption_daily) — they sum
# exactly to total. No attributed views (UC, AI/BI, Photon-overlay): those double-count.
PROD8 = [("jobs_classic","Jobs Classic"),("jobs_classic_photon","Jobs Classic Photon"),
         ("jobs_serverless","Jobs Serverless"),
         ("dbsql_classic","DBSQL Classic"),("dbsql_pro","DBSQL Pro"),
         ("dbsql_serverless","DBSQL Serverless"),
         ("ap_classic","All-Purpose Classic"),("ap_photon","All-Purpose Photon"),
         ("ap_serverless","All-Purpose Serverless"),
         ("dlt","DLT"),("genai","GenAI"),("lakebase","Lakebase"),
         ("other","Other (network/storage)")]

# FALLBACK ONLY: quarter targets come live from ConsumptionExt__c via raw_cp_targets.json.
# Optional manual fallback per AE in config: aes[].quota_fallback = [Q1,_,_,Q2,_,_,Q3,Q4]
# (layout kept from the original tool: [Q1 tgt, Q1 act, %, Q2 tgt, Q2 act, %, Q3 tgt, Q4 tgt]).
QUOTA = {a["name"]: a["quota_fallback"] for a in cfg.AES if a.get("quota_fallback")}

def fiscal_q(datestr):
    if not datestr: return None
    y, m = int(datestr[0:4]), int(datestr[5:7])
    if   m in (2,3,4):  fy,q = y+1,1
    elif m in (5,6,7):  fy,q = y+1,2
    elif m in (8,9,10): fy,q = y+1,3
    elif m in (11,12):  fy,q = y+1,4
    else:               fy,q = y,4
    return f"FY'{str(fy)[2:]} Q{q}"

def prev_q(label):
    i = QORDER.index(label) if label in QORDER else -1
    return QORDER[i-1] if i > 0 else None

# ---------------- Accounts ----------------
accounts = {}
for r in load("raw_accounts.json")["result"]["records"]:
    accounts[r["Id"]] = {
        "id":r["Id"], "name":r["Name"],
        "ae":(r.get("Owner") or {}).get("Name","Unknown"),
        "arr":num(r.get("ARR__c")), "t3mArr":num(r.get("T3M_ARR__c")),
        "industry":r.get("Industry") or "", "q":{}, "total27":0.0,
        "prod8":{}, "prod8_t28":{},
    }

# ---- Parent rollup mapping (ultimate_parent_account from gtm_gold) ----
import re
def slug(s): return "p_"+re.sub(r"[^a-z0-9]+","-",(s or "").lower()).strip("-")[:60]
parent_of = {}
try:
    for aid, aname, par in load("raw_parents.json")["result"]["data_array"]:
        parent_of[aid] = par or aname
except Exception:
    pass
# child account_id -> parent entity id (scoped within AE so per-AE totals stay exact)
childToParent = {}; parentMeta = {}
for aid, acc in accounts.items():
    pkey = parent_of.get(aid) or acc["name"]
    pid = slug(acc["ae"] + "|" + pkey)
    childToParent[aid] = pid
    pm = parentMeta.setdefault(pid, {"id":pid,"name":pkey,"ae":acc["ae"],"childIds":[],"children":[]})
    pm["childIds"].append(aid); pm["children"].append(acc["name"])

for aid,fyq,dollars in load("raw_consumption_quarterly.json")["result"]["data_array"]:
    if aid in accounts: accounts[aid]["q"][fyq] = num(dollars)

def parse_prod(fileobj):
    cols = [c["name"] for c in fileobj["manifest"]["schema"]["columns"]]
    out = {}
    for row in fileobj["result"]["data_array"]:
        d = dict(zip(cols, row)); out[d["account_id"]] = d
    return out

pf = parse_prod(load("raw_products.json"))
pf28 = parse_prod(load("raw_products_t28d.json"))
for aid, acc in accounts.items():
    if aid in pf:
        acc["prod8"] = {k: num(pf[aid].get(k)) for k,_ in PROD8}
        acc["prod8"]["total"] = num(pf[aid].get("total"))
        acc["total27"] = acc["prod8"]["total"]
    if aid in pf28:
        acc["prod8_t28"] = {k: num(pf28[aid].get(k)) for k,_ in PROD8}
        acc["prod8_t28"]["total"] = num(pf28[aid].get("total"))

# ---------------- Daily / run rates ----------------
daily_by_acct = defaultdict(dict); all_dates = []
for aid, ds, dollars in load("raw_daily.json")["result"]["data_array"]:
    if aid in accounts:
        daily_by_acct[aid][D(ds)] = num(dollars); all_dates.append(D(ds))
as_of = max(all_dates)
cur_label = fiscal_q(as_of.isoformat())
prev_label = prev_q(cur_label)
cur_start = D(QBOUNDS[cur_label][0]); cur_end = D(QBOUNDS[cur_label][1])
elapsed_cur = (as_of - cur_start).days + 1
prev_days = (cur_start - D(QBOUNDS[prev_label][0])).days
days_left = (cur_end - as_of).days

# latest FULL calendar month (constant; by current data date). e.g. as-of 2026-06-01 -> May 2026
_first_this = as_of.replace(day=1)
_next = _first_this.replace(year=_first_this.year + (1 if _first_this.month==12 else 0),
                            month=1 if _first_this.month==12 else _first_this.month+1)
_last_this = _next - datetime.timedelta(days=1)
if as_of >= _last_this:
    lm_start, lm_end = _first_this, _last_this
else:
    lm_end = _first_this - datetime.timedelta(days=1); lm_start = lm_end.replace(day=1)
lm_label = lm_start.strftime("%b %Y")
def latest_month_sum(daily):
    return round(sum(v for dt,v in daily.items() if lm_start<=dt<=lm_end), 2)

def runrates(daily):
    def wsum(s,e): return sum(v for dt,v in daily.items() if s<=dt<=e)
    td = datetime.timedelta
    t7=wsum(as_of-td(6),as_of)/7.0; t7p=wsum(as_of-td(13),as_of-td(7))/7.0
    t28=wsum(as_of-td(27),as_of)/28.0; t28p=wsum(as_of-td(34),as_of-td(7))/28.0
    weekly=[round(wsum(as_of-td(7*k)-td(6),as_of-td(7*k))/7.0,2) for k in range(51,-1,-1)]  # 52 weekly T7D points
    def pct(n,p): return round((n-p)/p*100,1) if p>0 else None
    return {"t7d":round(t7,2),"wow7":pct(t7,t7p),"t28d":round(t28,2),"wow28":pct(t28,t28p),"weekly":weekly}

def qoq(cur_t, prev_t):
    if prev_t<=0 or elapsed_cur<=0 or prev_days<=0: return None
    cr=cur_t/elapsed_cur; pr=prev_t/prev_days
    return round((cr-pr)/pr*100,1) if pr>0 else None

for aid, acc in accounts.items():
    acc.update(runrates(daily_by_acct.get(aid,{})))
    acc["qoq"] = qoq(acc["q"].get(cur_label,0), acc["q"].get(prev_label,0))
    acc["latestMo"] = latest_month_sum(daily_by_acct.get(aid,{}))

# ---------------- Open UCOs (U1-U5) ----------------
ucos = []
for r in load("raw_ucos.json")["result"]["records"]:
    acct = r.get("Account__r") or {}; gl = r.get("Full_Production_Date__c")
    ucos.append({"id":r["Id"],"name":r["Name"],"account":acct.get("Name",""),
        "accountId":r.get("Account__c"),"ae":(acct.get("Owner") or {}).get("Name","Unknown"),
        "stage":r.get("Stages__c"),"goLive":gl,"onboard":r.get("Implementation_Start_Date__c"),
        # AUTHORITATIVE stage-age straight from SFDC (2026-07-07): daysInStage = exact "days in current
        # stage" (CurrentStageDaysCount__c, the number SFDC's stage bar shows); stageModified = the true
        # current-stage-entry date. The hygiene protocol prefers these over the U{k}Date__c milestone guess.
        "daysInStage":numn(r.get("CurrentStageDaysCount__c")),   # null-preserving → fall back to stageModified when SFDC has no count
        "stageModified":dt10(r.get("Last_Stage_Modified_Date__c")),
        "health":r.get("Implementation_Status__c") or "","monthly":num(r.get("MonthlyTotalDollarDBUs__c")),
        "products":r.get("Use_Case_Type__c") or "","tshirt":r.get("TShirtSize__c") or "","fyq":fiscal_q(gl),
        # --- detail-window fields (not shown in the main tables; surfaced in the use-case popup) ---
        # Next step (SFDC "Next Steps" = Demand_Plan_Next_Steps__c) + when it was last updated.
        # Freshness clock: prefer the dedicated LastNextStepsModifiedDate__c, but its populating flow
        # watches the unused NextSteps__c field and leaves the stamp null on ~27% of records that DO have
        # Demand_Plan_Next_Steps__c text (verified 2026-08-24) — which was being scored "never updated".
        # Fall back to CustomLastModifiedDate__c (agrees same-day with the real stamp 71% of the time,
        # median 0-day gap) ONLY when the stamp is null AND next-step text is present.
        "nextStep":(r.get("Demand_Plan_Next_Steps__c") or "").strip(),
        "nextStepUpdated":dt10(r.get("LastNextStepsModifiedDate__c")) or (dt10(r.get("CustomLastModifiedDate__c")) if (r.get("Demand_Plan_Next_Steps__c") or "").strip() else None),
        "desc":(r.get("Use_Case_Description__c") or "").strip(),
        "proj":num(r.get("ProjectedConsumption__c")),
        "businessImpact":(r.get("BusinessImpact__c") or "").strip(),
        "lastMod":dt10(r.get("LastModifiedDate"))})

# ---------------- Aggregate helper (territory / AE) ----------------
def aggregate(acct_ids):
    daily=defaultdict(float); q=defaultdict(float); prod=defaultdict(float); prod28=defaultdict(float)
    for aid in acct_ids:
        for dt,v in daily_by_acct.get(aid,{}).items(): daily[dt]+=v
        for k,v in accounts[aid]["q"].items(): q[k]+=v
        for k,_ in PROD8: prod[k]+=accounts[aid]["prod8"].get(k,0); prod28[k]+=accounts[aid]["prod8_t28"].get(k,0)
        prod["total"]+=accounts[aid]["prod8"].get("total",0); prod28["total"]+=accounts[aid]["prod8_t28"].get("total",0)
    m=runrates(dict(daily)); m["q"]={k:round(v,2) for k,v in q.items()}
    m["qoq"]=qoq(q.get(cur_label,0),q.get(prev_label,0))
    m["latestMo"]=latest_month_sum(dict(daily))
    m["total27"]=round(sum(v for k,v in q.items() if k in cfg.ACTIVITY_QUARTERS),2)
    m["prod8"]={k:round(v,2) for k,v in prod.items()}; m["prod8_t28"]={k:round(v,2) for k,v in prod28.items()}
    return m

acctsByAE=defaultdict(list)
for aid,acc in accounts.items(): acctsByAE[acc["ae"]].append(aid)
ucosByAE=Counter(u["ae"] for u in ucos)

# ---- Build PARENT-level entities (these become the app's "accounts") ----
parents = {}
for pid, pm in parentMeta.items():
    m = aggregate(pm["childIds"])            # q, run-rates, prod8, prod8_t28, qoq, total27
    arr = sum(accounts[c]["arr"] for c in pm["childIds"])
    t3m = sum(accounts[c]["t3mArr"] for c in pm["childIds"])
    ind = next((accounts[c]["industry"] for c in sorted(pm["childIds"], key=lambda c:-(accounts[c]["total27"])) if accounts[c]["industry"]), "")
    parents[pid] = {**m, "id":pid, "name":pm["name"], "ae":pm["ae"], "arr":round(arr,2),
        "t3mArr":round(t3m,2), "industry":ind,
        "children":sorted(set(pm["children"])), "nChildren":len(set(pm["children"]))}
# re-key open UCOs to parent entity (keep child name in .account)
for u in ucos:
    u["accountId"] = childToParent.get(u["accountId"], u["accountId"])
parentsByAE=defaultdict(list)
for pid,p in parents.items(): parentsByAE[p["ae"]].append(pid)

territory = aggregate(list(accounts.keys()))
perAE={}
for ae in AE_ORDER:
    m=aggregate(acctsByAE.get(ae,[]))
    m["accounts"]=parentsByAE.get(ae,[]); m["nUcos"]=ucosByAE.get(ae,0)
    perAE[ae]=m

# ---------------- Weekly activity per quarter ----------------
# new opps, new use cases, progressed use cases — bucketed by week within quarter
def week_index(d_iso, qstart):
    return (D(d_iso) - qstart).days // 7

def qstart_for(label): return D(QBOUNDS[label][0])
def qend_for(label):   return D(QBOUNDS[label][1])

ACTIVITY_QUARTERS = list(cfg.ACTIVITY_QUARTERS)
# init nested {quarter: {scope: {key: {wid: {newOpps,newUCs,progressed}}}}}
def new_slot(): return {"newOpps":[],"newUCs":[],"progressed":[]}
weekly = {q:{"byAE":defaultdict(lambda:defaultdict(new_slot)),
             "byAcct":defaultdict(lambda:defaultdict(new_slot))} for q in ACTIVITY_QUARTERS}

acct_name_by_id = {aid:a["name"] for aid,a in accounts.items()}

# NEW use cases (by CreatedDate)
uca = load("raw_uco_activity.json")["result"]["records"]
for r in uca:
    acct=r.get("Account__r") or {}; ae=(acct.get("Owner") or {}).get("Name","Unknown")
    aid=r.get("Account__c"); created=dt10(r.get("CreatedDate"))
    item_base={"name":r["Name"],"account":acct.get("Name",""),"accountId":aid,"ae":ae,
        "monthly":num(r.get("MonthlyTotalDollarDBUs__c")),"proj":num(r.get("ProjectedConsumption__c")),
        "stage":r.get("Stages__c") or "U1","desc":(r.get("Use_Case_Description__c") or "")[:200]}
    if created:
        cq=fiscal_q(created)
        if cq in weekly and qstart_for(cq)<=D(created)<=qend_for(cq):
            wid=f"w{week_index(created,qstart_for(cq))}"
            it=dict(item_base,date=created)
            weekly[cq]["byAE"][ae][wid]["newUCs"].append(it)
            if aid: weekly[cq]["byAcct"][aid][wid]["newUCs"].append(it)
    # PROGRESSED (forward-only, highest stage reached per week)
    moves={}  # (quarter,wid) -> highest x
    for x in range(2,7):
        dx=dt10(r.get(f"U{x}Date__c"))
        if not dx: continue
        pq=fiscal_q(dx)
        if pq in weekly and qstart_for(pq)<=D(dx)<=qend_for(pq):
            wid=f"w{week_index(dx,qstart_for(pq))}"
            key=(pq,wid); moves[key]=max(moves.get(key,0),x)
    for (pq,wid),x in moves.items():
        it={"name":r["Name"],"account":acct.get("Name",""),"accountId":aid,"ae":ae,
            "from":f"U{x-1}","to":f"U{x}","monthly":num(r.get("MonthlyTotalDollarDBUs__c")),
            "proj":num(r.get("ProjectedConsumption__c")),"live":(x==6)}
        weekly[pq]["byAE"][ae][wid]["progressed"].append(it)
        if aid: weekly[pq]["byAcct"][aid][wid]["progressed"].append(it)

# NEW opportunities (by CreatedDate)
def opp_item(r):
    acct=r.get("Account") or {}
    return {"id":r.get("Id"),"name":r["Name"],"account":acct.get("Name",""),"accountId":r.get("AccountId"),
            "ae":(acct.get("Owner") or {}).get("Name","Unknown"),"stage":r.get("StageName"),
            # won/closed status from SFDC's authoritative flags — never inferred from the stage name.
            "won":bool(r.get("IsWon")),"closedFlag":bool(r.get("IsClosed")),
            "forecast":r.get("ForecastCategoryName"),"amount":num(r.get("Amount")),
            # IARR = incremental (net-new) ARR from CPQ, distinct from the multi-year TCV in `amount`.
            # termMonths = contract tenure in months (CPQ NumberOfTerms__c preferred; New_Term_in_months__c /
            # ContractTermMonths__c as fallbacks). Front-end shows termMonths/12 as the deal tenure in years.
            "iarr":numn(r.get("CPQ_Incremental_Booking_ARR__c")),
            "termMonths":(numn(r.get("NumberOfTerms__c")) or numn(r.get("New_Term_in_months__c"))
                          or numn(r.get("ContractTermMonths__c"))),
            "close":r.get("CloseDate"),"created":dt10(r.get("CreatedDate")),
            "nextStep":(r.get("Next_Step_Detail__c") or "").strip(),
            "nextStepUpd":dt10(r.get("Next_Steps_Last_Updated__c"))}
for r in load("raw_opps_new.json")["result"]["records"]:
    o=opp_item(r); created=o["created"]
    if not created: continue
    cq=fiscal_q(created)
    if cq in weekly and qstart_for(cq)<=D(created)<=qend_for(cq):
        wid=f"w{week_index(created,qstart_for(cq))}"
        weekly[cq]["byAE"][o["ae"]][wid]["newOpps"].append(o)
        if o["accountId"]: weekly[cq]["byAcct"][o["accountId"]][wid]["newOpps"].append(o)

# ---------------- Commit opportunities (open, by close quarter) ----------------
commit = {q:{"byAE":defaultdict(list),"byAcct":defaultdict(list),"all":[]} for q in ACTIVITY_QUARTERS}
opps_open=[]
for r in load("raw_opps_open.json")["result"]["records"]:
    o=opp_item(r); opps_open.append(o)
    if o["forecast"]!="Commit" or not o["close"]: continue
    cq=fiscal_q(o["close"])
    if cq in commit:
        commit[cq]["byAE"][o["ae"]].append(o)
        if o["accountId"]: commit[cq]["byAcct"][o["accountId"]].append(o)
        commit[cq]["all"].append(o)

# ---------------- Opportunities module — OPEN pipeline (by close quarter) ----------------
# Flat list of every open opp (any forecast category) with its fiscal close-quarter.
# Front-end groups by AE/quarter and filters by forecast category.
# "Open" = SFDC IsWon=false (the raw pull is already IsClosed=false). Using the IsWon flag,
# not a stage-name regex, keeps a genuinely-open "Signed" deal in the pipeline while a deal
# flagged won (even if not yet IsClosed) drops out and instead surfaces as closed-won below.
opps_module = []
for r in load("raw_opps_open.json")["result"]["records"]:
    o = opp_item(r)
    if o.get("won"): continue
    o["fq"] = fiscal_q(o["close"]) if o.get("close") else None
    opps_module.append(o)

# ---------------- Opportunities module — CLOSED-WON (booked this FY, by close quarter) ------
# Deals already won in-territory (IsWon=true, CloseDate >= FY start). Shown alongside open
# pipeline so each quarter's Opportunities view is complete (a renewal won this quarter
# would otherwise never surface in an open-only view).
opps_won = []
for r in load("raw_opps_won.json")["result"]["records"]:
    o = opp_item(r)
    o["won"] = True
    o["fq"] = fiscal_q(o["close"]) if o.get("close") else None
    opps_won.append(o)

# ---------------- Central FORECAST table ----------------
fc = load("forecast.json") if os.path.exists(os.path.join(HERE, "forecast.json")) else {}
QEND = {k:v for k,v in fc.get("quarterEnd",{}).items()}

# LIVE ConsumptionPlan feed (2026-08-28) — raw_cp_forecast.json (Consumption_Forecast__c
# 'AE Forecast' rows: MyForecastCurrency__c per AE per quarter) + raw_cp_targets.json
# (ConsumptionExt__c 'User' rows: Target__c = quota), both pulled by refresh.py from the
# SFDC objects behind the ConsumptionPlan app. Precedence at every consumption site:
# live CP value > forecast.json manual > QUOTA table — a missing or failed pull (or a 0,
# meaning "not yet forecast/no quota set") degrades to the old manual behavior.
CP = {}   # {qlabel: {ae: {"forecast": $, "target": $, "submitted": "YYYY-MM-DD"}}}
try:
    for r in load("raw_cp_forecast.json")["result"]["records"]:
        ql = fiscal_q(r.get("ForecastDate__c")); ae = (r.get("Owner") or {}).get("Name")
        if not ql or not ae: continue
        e = CP.setdefault(ql, {}).setdefault(ae, {})
        e["forecast"] = r.get("MyForecastCurrency__c")
        e["submitted"] = (r.get("LastSubmittedDate__c") or "")[:10] or None
except Exception:
    pass
try:
    for r in load("raw_cp_targets.json")["result"]["records"]:
        ql = fiscal_q(r.get("QuarterStartDate__c")); ae = (r.get("User__r") or {}).get("Name")
        if ql and ae and r.get("Target__c"):
            CP.setdefault(ql, {}).setdefault(ae, {})["target"] = r["Target__c"]
except Exception:
    pass
# The manager's OWN submitted forecast (Consumption_Forecast__c RT 'Manager Forecast', owned
# by the manager) — MyForecastCurrency__c is their committed number per fiscal quarter, DISTINCT
# from the AE rollup (TeamForecastCurrencyRollup__c ≈ sum of the AEs). Surfaced as the
# "My Forecast" row in the central forecast table. A missing/failed pull -> {} (row hidden),
# never a fabricated number.
MY_FC = {}   # {qlabel: {"forecast": $, "rollup": $, "submitted": "YYYY-MM-DD"}}
try:
    for r in load("raw_cp_mgr_forecast.json")["result"]["records"]:
        ql = fiscal_q(r.get("ForecastDate__c")); v = r.get("MyForecastCurrency__c")
        if not ql or not v: continue
        MY_FC[ql] = {"forecast": round(v),
                     "rollup": round(r["TeamForecastCurrencyRollup__c"]) if r.get("TeamForecastCurrencyRollup__c") else None,
                     "submitted": (r.get("LastSubmittedDate__c") or "")[:10] or None}
except Exception:
    pass

def cp_forecast(ae, label):
    v = CP.get(label, {}).get(ae, {}).get("forecast")
    return round(v) if v else None
def cp_target(ae, label):
    v = CP.get(label, {}).get(ae, {}).get("target")
    return round(v) if v else None
def ae_actual_q(ae, label):
    return round(sum(accounts[aid]["q"].get(label,0) for aid in acctsByAE.get(ae,[])),2)

def forecast_row(ae, label):
    """Build forecast row for an AE & quarter. Live QTD/T7D/GTTB; forecast/target live
    from the ConsumptionPlan pull (manual forecast.json / QUOTA as fallbacks); manual q1base."""
    man = fc.get(label,{}).get(ae,{})
    qstart=qstart_for(label); qend=qend_for(label)
    is_current = (label==cur_label)
    is_past = qend < as_of
    qtd = ae_actual_q(ae, label)
    t7d = perAE[ae]["t7d"]
    dleft = max(0,(qend-as_of).days) if not is_past else 0
    gttb = qtd + t7d*dleft if not is_past else qtd
    q1base = man.get("q1base") or ae_actual_q(ae, prev_q(label))
    target = cp_target(ae, label) or man.get("q2target")
    # target fallback from QUOTA by quarter number
    if target is None:
        qn=label[-1]; idx={"1":0,"2":3,"3":6,"4":7}.get(qn,3)
        target=QUOTA.get(ae,[None]*8)[idx]
    forecast=cp_forecast(ae, label) or man.get("forecast") or man.get("q2forecast") or (gttb if is_current else (qtd if is_past else target))
    def safe(n,d): return round(n/d,4) if d else None
    return {"ae":ae,"q1base":q1base,"qtd":round(qtd),"t7d":round(t7d),"daysLeft":dleft,
            "gttb":round(gttb),"forecast":forecast,"target":target,
            "gttbVsFcst":round((forecast-gttb)) if forecast is not None else None,
            "qoqGttb":safe(gttb-q1base,q1base) if q1base else None,
            "qoqFcst":safe(forecast-q1base,q1base) if (forecast and q1base) else None,
            "gapGttb":safe(gttb,target),"gapFcst":safe(forecast,target) if forecast else None}

# Forecast INPUTS (client computes the table so edits + base-chaining are reactive)
TGT_IDX={q:i for q,i in zip(ACTIVITY_QUARTERS,(0,3,6,7))}
forecast_inputs={}
for q in ACTIVITY_QUARTERS:
    man_q=fc.get(q,{})
    tgt={}; fdef={}; base={}
    for ae in AE_ORDER:
        man=man_q.get(ae,{})
        tgt[ae]=cp_target(ae,q) or man.get("q2target") or QUOTA.get(ae,[None]*8)[TGT_IDX[q]]
        base[ae]=man.get("q1base")  # manual base (e.g. Q2's Q1 Base); None -> client chains to prior fcst
        if q==ACTIVITY_QUARTERS[0]: fdef[ae]=round(ae_actual_q(ae,ACTIVITY_QUARTERS[0]))
        else:             fdef[ae]=cp_forecast(ae,q) or man.get("forecast") or man.get("q2forecast")
    forecast_inputs[q]={"targets":tgt,"forecastDefault":fdef,"base":base,
        # provenance (additive; app currently ignores): which AEs came from the live
        # ConsumptionPlan pull and when they last submitted — for a future "as of" badge.
        # ddmodules.verify() ALSO reads these every run: they are how the pipeline proves
        # the CP pull actually landed, because the manual fallback would otherwise hide a
        # dead pull forever (the app would keep serving frozen 2026-06-02 numbers).
        "cpLive":{ae:bool(cp_forecast(ae,q)) for ae in AE_ORDER},
        "cpTargetLive":{ae:bool(cp_target(ae,q)) for ae in AE_ORDER},
        "cpSubmitted":{ae:CP.get(q,{}).get(ae,{}).get("submitted") for ae in AE_ORDER}}

forecast_table={}
for label in ACTIVITY_QUARTERS:
    rows=[forecast_row(ae,label) for ae in AE_ORDER]
    tot={"ae":"Territory","q1base":sum(r["q1base"] or 0 for r in rows),
         "qtd":sum(r["qtd"] for r in rows),"t7d":sum(r["t7d"] for r in rows),
         "daysLeft":days_left if label==cur_label else (rows[0]["daysLeft"] if rows else 0),
         "gttb":sum(r["gttb"] for r in rows),
         "forecast":sum((r["forecast"] or 0) for r in rows),
         "target":sum((r["target"] or 0) for r in rows)}
    tot["gttbVsFcst"]=round(tot["forecast"]-tot["gttb"])
    tot["qoqGttb"]=round((tot["gttb"]-tot["q1base"])/tot["q1base"],4) if tot["q1base"] else None
    tot["qoqFcst"]=round((tot["forecast"]-tot["q1base"])/tot["q1base"],4) if tot["q1base"] else None
    tot["gapGttb"]=round(tot["gttb"]/tot["target"],4) if tot["target"] else None
    tot["gapFcst"]=round(tot["forecast"]/tot["target"],4) if tot["target"] else None
    forecast_table[label]={"rows":rows,"total":tot}

# ---------------- Product matrix (parent-level top accounts) ----------------
prod_matrix=[]
for pid,a in parents.items():
    if a["prod8"].get("total",0)>0:
        row={"account":a["name"],"accountId":pid,"ae":a["ae"],"total":a["prod8"]["total"]}
        for k,_ in PROD8: row[k]=a["prod8"].get(k,0)
        prod_matrix.append(row)
prod_matrix.sort(key=lambda x:-x["total"])
for i,r in enumerate(prod_matrix): r["rank"]=i+1

# ---------------- weekly -> plain dict + week meta ----------------
def quarter_weeks(label):
    qs=qstart_for(label); qe=qend_for(label)
    weeks=[]
    for idx in range(13):
        ws=qs+datetime.timedelta(7*idx)
        if ws>qe: break
        we=min(ws+datetime.timedelta(6),qe)
        state="current" if (label==cur_label and ws<=as_of<=we) else ("past" if we<as_of else "upcoming")
        weeks.append({"id":f"w{idx}","label":ws.strftime("%-d %b"),
                      "range":f"{ws.strftime('%-d %b')} – {we.strftime('%-d %b')}","state":state})
    return weeks

def rekey_byacct(byacct):
    """Merge child-account weekly slots into parent entity ids."""
    out={}
    for aid,wdict in byacct.items():
        pid=childToParent.get(aid,aid)
        tgt=out.setdefault(pid,{})
        for wid,slot in wdict.items():
            ts=tgt.setdefault(wid,{"newOpps":[],"newUCs":[],"progressed":[]})
            for k in ("newOpps","newUCs","progressed"): ts[k].extend(slot.get(k,[]))
    return out

weekly_out={}
for q in ACTIVITY_QUARTERS:
    weekly_out[q]={"weeks":quarter_weeks(q),
        "byAE":{ae:dict(w) for ae,w in weekly[q]["byAE"].items()},
        "byAcct":rekey_byacct({aid:dict(w) for aid,w in weekly[q]["byAcct"].items()})}

commit_out={}
for q in ACTIVITY_QUARTERS:
    cby={}
    for aid,v in commit[q]["byAcct"].items():
        cby.setdefault(childToParent.get(aid,aid),[]).extend(v)
    commit_out[q]={"byAE":{ae:v for ae,v in commit[q]["byAE"].items()},
        "byAcct":cby,"all":commit[q]["all"]}

# ---------------- Insights (optional, from insights.json) ----------------
insights={}
ipath=os.path.join(HERE,"insights.json")
if os.path.exists(ipath):
    try: insights=json.load(open(ipath))
    except Exception: insights={}

# last successful pipeline refresh (written by refresh.py; persists across manual builds)
last_refresh=""
lrpath=os.path.join(HERE,"last_refresh.txt")
if os.path.exists(lrpath):
    try: last_refresh=open(lrpath).read().strip()
    except Exception: last_refresh=""

# ---------------- RoB — weekly leadership review ----------------
# The reviewer (config: reviewer.name — by default the manager's own manager) reviews the current quarter + next quarter every Friday. We snapshot the
# two review quarters each build so the weekly review can diff week-over-week
# ("what's added / slipped / moved"). Snapshots live in data/rob_snapshots/
# <friday>.json: the CURRENT week's file is overwritten each refresh (stays
# fresh); PAST weeks are never rewritten (frozen truth). All snapshots + the
# Q3-goals JSON fold into data.js as D.rob so the app works offline (file://)
# and in the read-only cloud mirror. NO LLM, NO tokens.
def _rob_next_q(label):
    i=QORDER.index(label); return QORDER[i+1] if i+1<len(QORDER) else label
rob_cur=cur_label; rob_next=_rob_next_q(cur_label)
def _rob_top_golives(qlabel,n=10):
    inq=[u for u in ucos if u.get("goLive") and u.get("fyq")==qlabel]
    inq.sort(key=lambda u:(u.get("monthly") or 0),reverse=True)
    return [{"id":u["id"],"name":u["name"],"account":u["account"],"ae":u["ae"],
             "stage":u["stage"],"goLive":u["goLive"],"monthly":u.get("monthly") or 0,
             "nextStep":u.get("nextStep") or ""}
            for u in inq[:n]]
def _rob_commit_slim(qlabel):
    return [{"id":o.get("id"),"name":o.get("name"),"account":o.get("account"),
             "ae":o.get("ae"),"stage":o.get("stage"),"amount":o.get("amount") or 0,
             "close":o.get("close"),"forecast":o.get("forecast")}
            for o in ((commit_out.get(qlabel) or {}).get("all") or [])]
_rob_today=datetime.date.today()
_rob_friday=_rob_today+datetime.timedelta(days=(4-_rob_today.weekday()))  # weekday(): Mon=0..Fri=4
rob_cur_friday=_rob_friday.isoformat()
_rob_q4end=D(QBOUNDS[rob_next][1]); _rob_fridays=[]; _f=_rob_friday
while _f<=_rob_q4end: _rob_fridays.append(_f.isoformat()); _f+=datetime.timedelta(days=7)
_rob_snap={"friday":rob_cur_friday,"asOf":as_of.isoformat(),"curQ":rob_cur,"nextQ":rob_next,
    "goliveCur":_rob_top_golives(rob_cur),"goliveNext":_rob_top_golives(rob_next),
    "commitCur":_rob_commit_slim(rob_cur),"commitNext":_rob_commit_slim(rob_next)}
_rob_dir=os.path.join(HERE,"rob_snapshots"); os.makedirs(_rob_dir,exist_ok=True)
with open(os.path.join(_rob_dir,rob_cur_friday+".json"),"w") as _sf:
    json.dump(_rob_snap,_sf,ensure_ascii=False,separators=(",",":"))
rob_snapshots={}
for _fn in sorted(os.listdir(_rob_dir)):
    if _fn.endswith(".json"):
        try: rob_snapshots[_fn[:-5]]=json.load(open(os.path.join(_rob_dir,_fn)))
        except Exception: pass
rob_q3goals={}
# optional quarter goals typed by the manager: data/quarter_goals.json
# (data/q3_goals.json is the older name and is still read)
_gp=next((p for p in (os.path.join(HERE,"quarter_goals.json"),os.path.join(HERE,"q3_goals.json"))
          if os.path.exists(p)),"")
if _gp:
    try: rob_q3goals=json.load(open(_gp))
    except Exception: rob_q3goals={}
rob={"curFriday":rob_cur_friday,"fridays":_rob_fridays,"curQ":rob_cur,"nextQ":rob_next,
     "snapshots":rob_snapshots,"q3goals":rob_q3goals}

# ---------------- Pipeline Coverage module ----------------
# Per-AE pipeline-sufficiency for the CURRENT + NEXT quarter, scored against a
# target-derived bar:
#   current quarter:  need = target(curQ) − actual(prior quarter)         × 1.5
#   next quarter:     need = target(nextQ) − forecast(current-qtr close)  × 2.0
#   pipeNeeded = max(need,0) × mult ;  coverage% = actual open-UCO pipe ÷ pipeNeeded
#   pipe $ per UCO = MonthlyTotalDollarDBUs__c × 3 (full-quarter run-rate).
# Quarter maths use self-contained (fy,q) tuple arithmetic — NEVER prev_q()/QORDER
# index (unsafe outside FY26–FY27). Targets come from forecast.json q2target or the
# FY'27 QUOTA table; a quarter with no target renders a gray "no-target" card rather
# than crashing (e.g. FY'28 Q1 after the Nov 1 roll). Overdue-but-open UCOs count
# toward the current quarter (flagged pastDue); undated UCOs are surfaced, not counted.
# Opps are computed as a reference line but not scored (cfg.scope="ucos").
# NO LLM, NO tokens. Wrapped so a failure here can NEVER break the refresh.
def build_pipecov():
    cfg = {"curMult":1.5,"nextMult":2.0,"valuation":"inqtr","scope":"ucos",
           "includePastDue":True,"includeUndated":False,"forecastFallback":"target",
           "bands":{"green":100,"amber":75}}
    MULT = {"cur":cfg["curMult"],"next":cfg["nextMult"]}
    def qtuple(label):
        m = re.match(r"FY'(\d{2}) Q(\d)", label or "")
        return (int(m.group(1)), int(m.group(2))) if m else None
    def qmk(fy,q): return f"FY'{fy:02d} Q{q}"
    def qidx(label):
        t = qtuple(label); return t[0]*4+t[1] if t else None
    def q_add(label,n):            # shift a quarter by n (Q4→next-FY Q1 handled)
        t = qtuple(label)
        if not t: return None
        fy,q = t; k = (fy*4+(q-1))+n
        return qmk(k//4, k%4+1)
    curQ  = cur_label
    nextQ = q_add(curQ, 1)
    exitQ = q_add(curQ, -1)        # prior quarter — the current node's "exit" (its actual)
    def tgt_idx(label):            # QUOTA holds current-FY quarter targets only
        if label not in ACTIVITY_QUARTERS: return None
        return {"1":0,"2":3,"3":6,"4":7}.get(label[-1])
    def target_of(ae,label):
        man = fc.get(label,{}).get(ae,{})
        t = cp_target(ae,label) or man.get("q2target")
        if t: return t
        idx = tgt_idx(label)
        if idx is None: return None
        arr = QUOTA.get(ae)
        return arr[idx] if arr else None
    def value_of(monthly):         # legacy flat valuation — kept for reference, NOT used for pipe
        v = cfg["valuation"]
        if v == "m1":  return round(monthly, 2)
        if v == "m12": return round(monthly*12, 2)
        if v == "monthly": return round(monthly, 2)
        return round(monthly*3, 2)               # m3 (full-quarter run-rate)
    def qstart_ym(label):          # (year, month) of a quarter's first day, from QBOUNDS
        s = QBOUNDS.get(label)
        return (int(s[0][0:4]), int(s[0][5:7])) if s else None
    def month_idx_in_q(ym, qlabel):   # 0/1/2 month offset within the quarter, clamped
        qs = qstart_ym(qlabel)
        if not qs or not ym: return 0
        return max(0, min(2, (ym[0]-qs[0])*12 + (ym[1]-qs[1])))
    def inq_value(monthly, u, bucket, past_due):
        # In-quarter revenue: the actual $DBU a go-live bills WITHIN its
        # quarter, weighting each by its go-live month — month 1 → ×3, month 2 → ×2, month 3 → ×1.
        # Mirrors the go-lives module's inQtrRev exactly (app.js golivesFor: monthly × (3 − mIdx)),
        # so Pipeline-Coverage "actual pipe" reconciles with that table. Replaces the old flat ×3.
        if bucket == "cur":
            if past_due:               # overdue → assume it lands THIS month within the current quarter
                ym = (as_of.year, as_of.month)
            else:                      # dated in the current quarter
                gl = u.get("goLive"); ym = (int(gl[0:4]), int(gl[5:7])) if gl else None
            mi = month_idx_in_q(ym, curQ)
        elif bucket == "next":
            gl = u.get("goLive"); ym = (int(gl[0:4]), int(gl[5:7])) if gl else None
            mi = month_idx_in_q(ym, nextQ)
        else:                          # later / undated — informational only, never counted in pipe
            return round(monthly*3, 2)
        return round(monthly * (3 - mi), 2)
    def cur_forecast(ae):          # forecasted current-quarter close → the NEXT node's exit
        man = fc.get(curQ,{}).get(ae,{})
        f = cp_forecast(ae,curQ) or man.get("forecast") or man.get("q2forecast")
        return (f,"forecast") if f is not None else (target_of(ae,curQ),"target-fallback")
    curIdx = qidx(curQ); nextIdx = qidx(nextQ)
    # opp pipe per AE per close-quarter (reference line only)
    oppByAeQ = defaultdict(lambda: defaultdict(float))
    for o in opps_module:
        if o.get("fq") and o.get("ae") in AE_ORDER:
            oppByAeQ[o["ae"]][o["fq"]] += (o.get("amount") or 0)
    perAE = {}
    for ae in AE_ORDER:
        rows = []                  # slim rows: every open UCO of this AE exactly once
        agg = {"cur":{"pipe":0.0,"n":0,"unsized":0,"pastDue":0},
               "next":{"pipe":0.0,"n":0,"unsized":0,"pastDue":0}}
        nUndated=0; undatedMonthly=0.0; nLater=0; laterMonthly=0.0
        for u in ucos:
            if u["ae"] != ae: continue
            monthly = u.get("monthly") or 0        # L188 already coerces null→0.0
            unsized = (monthly == 0)
            fq = u.get("fyq"); fi = qidx(fq) if fq else None
            pastDue=False; b=None
            if not fq:
                b="undated"; nUndated+=1; undatedMonthly+=monthly
            elif curIdx is not None and fi is not None and fi < curIdx:
                b="cur"; pastDue=True                                   # overdue, still open
            elif fi == curIdx:
                b="cur"
            elif nextIdx is not None and fi == nextIdx:
                b="next"
            else:
                b="later"; nLater+=1; laterMonthly+=monthly
            v = inq_value(monthly, u, b, pastDue)   # in-quarter revenue (×3/×2/×1 by go-live month)
            rows.append({"id":u["id"],"b":b,"v":v,"unsized":unsized,"pastDue":pastDue})
            if b in ("cur","next"):
                a=agg[b]; a["pipe"]+=v; a["n"]+=1
                if unsized: a["unsized"]+=1
                if pastDue: a["pastDue"]+=1
        def node(kind, target, exit_v, exitKind):
            mult=MULT[kind]; a=agg[kind]; flags=[]
            pipeUco = round(a["pipe"],2)
            pipeOpps= round(oppByAeQ[ae].get(curQ if kind=="cur" else nextQ, 0), 2)
            pipe    = pipeUco if cfg["scope"]=="ucos" else round(pipeUco+pipeOpps,2)
            if target is None:
                state="no-target"; need=None; pipeNeeded=None; covPct=None
            else:
                need = round(target-(exit_v or 0),2)
                pipeNeeded = round(max(need,0)*mult, 2)
                if need <= 0:
                    state="covered"; covPct=None
                else:
                    state="gap"
                    covPct = round(pipe/pipeNeeded*100,1) if pipeNeeded>0 else None
            if exitKind=="target-fallback": flags.append("forecast-missing")
            if exitKind=="growth": flags.append("above-quota")
            if a["unsized"]: flags.append("unsized")
            if a["pastDue"]: flags.append("past-due")
            return {"target":target,"exit":exit_v,"exitKind":exitKind,"need":need,"mult":mult,
                    "pipeNeeded":pipeNeeded,"pipeUco":pipeUco,"pipeOpps":pipeOpps,"pipe":pipe,
                    "covPct":covPct,"state":state,"nUcos":a["n"],"nUnsized":a["unsized"],
                    "nPastDue":a["pastDue"],"flags":flags}
        # ABOVE-QUOTA rule: an AE forecasting ≥95% of the CURRENT-quarter
        # quota is measured against quota GROWTH, not (target − actual/forecast). Their needed
        # pipe becomes the quarter-over-quarter quota step (this quarter's target − prior
        # quarter's target) × the same multiplier — target-based, ignoring exit. The flag is set
        # once from the current quarter and applied to BOTH quarters (so a strong Q3 performer
        # keeps growth-based coverage in Q4). Everyone below 95% keeps the older exit-based logic.
        tgt_cur = target_of(ae, curQ); tgt_next = target_of(ae, nextQ); tgt_prev = target_of(ae, exitQ)
        fc_cur = (forecast_inputs.get(curQ, {}).get("forecastDefault", {}) or {}).get(ae)
        above_quota = (fc_cur is not None and tgt_cur and (fc_cur / tgt_cur) >= 0.95)
        if above_quota:
            cur_node  = node("cur",  tgt_cur,  tgt_prev, "growth")   # base = prior-quarter target
            next_node = node("next", tgt_next, tgt_cur,  "growth")   # base = current-quarter target
        else:
            fexit, fkind = cur_forecast(ae)
            cur_node  = node("cur",  tgt_cur,  ae_actual_q(ae, exitQ), "actual")
            next_node = node("next", tgt_next, fexit, fkind)
        perAE[ae] = {"cur": cur_node, "next": next_node, "aboveQuota": bool(above_quota),
                     "ucos": rows, "nUndated":nUndated, "undatedMonthly":round(undatedMonthly,2),
                     "nLater":nLater, "laterMonthly":round(laterMonthly,2)}
    def team_node(kind):           # aggregate over GAP-state AEs only (surplus must not mask a shortfall)
        gap = [perAE[ae][kind] for ae in AE_ORDER if perAE[ae][kind]["state"]=="gap"]
        pipe   = round(sum(n["pipe"] for n in gap),2)
        needed = round(sum(n["pipeNeeded"] for n in gap),2)
        return {"pipe":pipe,"pipeNeeded":needed,
                "covPct": round(pipe/needed*100,1) if needed>0 else None,
                "nGap":len(gap),
                "nCovered":  sum(1 for ae in AE_ORDER if perAE[ae][kind]["state"]=="covered"),
                "nNoTarget": sum(1 for ae in AE_ORDER if perAE[ae][kind]["state"]=="no-target")}
    team = {"cur":team_node("cur"), "next":team_node("next")}
    # ISO-week ledger + WoW — isolated try/except so a ledger fault never loses the scores
    history=[]; wow={"prevWeek":None,"team":{"cur":None,"next":None},"perAE":{}}
    try:
        hist_path = os.path.join(HERE,"pipecov_history.json")
        if os.path.exists(hist_path):
            history = json.load(open(hist_path)) or []
        iso = datetime.date.today().isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        prev_entries = [h for h in history if h.get("week")!=week_key]
        prevEntry = sorted(prev_entries, key=lambda h:h.get("week",""))[-1] if prev_entries else None
        entry = {"week":week_key,"asOf":as_of.isoformat(),
                 "team":{"cur":team["cur"]["covPct"],"next":team["next"]["covPct"]},
                 "perAE":{ae:{"cur":perAE[ae]["cur"]["covPct"],"next":perAE[ae]["next"]["covPct"]} for ae in AE_ORDER}}
        history = sorted(prev_entries+[entry], key=lambda h:h.get("week",""))[-26:]
        json.dump(history, open(hist_path,"w"), ensure_ascii=False, separators=(",",":"))
        def _d(cur,pv): return round(cur-pv,1) if (cur is not None and pv is not None) else None
        wow = {"prevWeek": prevEntry["week"] if prevEntry else None,
               "team":{"cur": _d(team["cur"]["covPct"],  (prevEntry["team"]["cur"]  if prevEntry else None)),
                       "next":_d(team["next"]["covPct"], (prevEntry["team"]["next"] if prevEntry else None))},
               "perAE":{ae:{"cur": _d(perAE[ae]["cur"]["covPct"],  ((prevEntry.get("perAE",{}).get(ae) or {}).get("cur")  if prevEntry else None)),
                            "next":_d(perAE[ae]["next"]["covPct"], ((prevEntry.get("perAE",{}).get(ae) or {}).get("next") if prevEntry else None))} for ae in AE_ORDER}}
    except Exception:
        history=[]; wow={"prevWeek":None,"team":{"cur":None,"next":None},"perAE":{}}
    return {"cfg":cfg,"curQ":curQ,"nextQ":nextQ,"exitQ":exitQ,"asOf":as_of.isoformat(),
            "team":team,"perAE":perAE,"wow":wow,"history":history}
try:
    pipecov = build_pipecov()
except Exception as _e:
    pipecov = {"error":str(_e)}

data={
    "meta":{
        "generated":datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "lastRefresh":last_refresh,
        "asOf":as_of.isoformat(),"curFQ":cur_label,"prevFQ":prev_label,
        "daysLeft":days_left,"quarterEnd":QBOUNDS[cur_label][1],"latestMoLabel":lm_label,
        "territory":cfg.TERRITORY_LABEL,
        "manager":cfg.MANAGER_NAME,"reviewer":cfg.REVIEWER_NAME,
        "fyLabel":FYL,"priorFyLabel":PFYL,"aeSlack":cfg.AE_SLACK,
        "cloudAppUrl":cfg.CLOUD_APP_URL,
        "openDef":"Open use cases = Stages U1–U5 (excludes U6/Live & closed).",
        "metricDef":"T7D/T28D = trailing 7/28-day avg daily $DBU. GTTB = QTD + T7D×days-left. QoQ = metric/Q1Base−1. Gap = metric/Q2Target.",
        "sources":"UCs, opps, accounts: Salesforce (live). Consumption & products: gtm_gold.account_consumption_daily + sku_consumption_daily. Forecasts & targets: ConsumptionPlan (live; manual values only as a fallback).",
        "nAccounts":len(parents),"nUcos":len(ucos),
    },
    "qorder":QORDER,"aeOrder":AE_ORDER,"activityQuarters":ACTIVITY_QUARTERS,
    "territory":territory,"perAE":perAE,"accounts":list(parents.values()),"ucos":ucos,
    "forecastInputs":forecast_inputs,"myForecast":MY_FC,
    "qbounds":{k:list(v) for k,v in QBOUNDS.items()},"prodMatrix":prod_matrix,
    "weekly":weekly_out,"commit":commit_out,"opps":opps_module,"oppsWon":opps_won,"prod8labels":PROD8,"insights":insights,
    "rob":rob,"pipecov":pipecov,
}

os.makedirs(APP,exist_ok=True)
with open(os.path.join(APP,"data.js"),"w") as f:
    f.write("// Project DD data — generated "+data["meta"]["generated"]+" — as-of "+as_of.isoformat()+"\n")
    f.write("window.DD_DATA = "); json.dump(data,f,ensure_ascii=False,separators=(",",":")); f.write(";\n")

# stats
ft=forecast_table[cur_label]["total"]
print(f"as-of {as_of} | {cur_label} | days-left {days_left}")
print(f"Accounts {len(accounts)} | open UCs {len(ucos)} | open opps {len(opps_open)} | opps-module {len(opps_module)} | closed-won(FY) {len(opps_won)} (IARR ${sum(o.get('iarr') or 0 for o in opps_won):,.0f}) | new opps(FY) {sum(1 for r in load('raw_opps_new.json')['result']['records'])}")
print(f"Forecast {cur_label} territory: QTD ${ft['qtd']:,.0f} | GTTB ${ft['gttb']:,.0f} | Forecast ${ft['forecast']:,.0f} | Target ${ft['target']:,.0f} | Gap(Fcst) {ft['gapFcst']*100:.1f}%")
_myfc = MY_FC.get(cur_label)
print(f"My Forecast {cur_label}: " + (f"${_myfc['forecast']:,.0f} (submitted {_myfc.get('submitted')}) | AE rollup ${_myfc.get('rollup') or 0:,.0f}" if _myfc else "NOT PULLED — 'My Forecast' row hidden"))
nuc=sum(len(w['newUCs']) for q in [cur_label] for ae in weekly_out[q]['byAE'].values() for w in ae.values())
npo=sum(len(w['newOpps']) for q in [cur_label] for ae in weekly_out[q]['byAE'].values() for w in ae.values())
npr=sum(len(w['progressed']) for q in [cur_label] for ae in weekly_out[q]['byAE'].values() for w in ae.values())
print(f"{cur_label} activity: new UCs {nuc} | new opps {npo} | progressions {npr} | commit opps {len(commit[cur_label]['all'])}")
print("data.js bytes:", os.path.getsize(os.path.join(APP,'data.js')))
if isinstance(pipecov,dict) and not pipecov.get("error"):
    _tc=pipecov["team"]["cur"]
    print(f"Pipecov {pipecov['curQ']}: team coverage {_tc['covPct']}% ({_tc['nGap']} gap / {_tc['nCovered']} covered / {_tc['nNoTarget']} no-target)")
else:
    print("Pipecov: ERROR", (pipecov.get("error") if isinstance(pipecov,dict) else pipecov))
