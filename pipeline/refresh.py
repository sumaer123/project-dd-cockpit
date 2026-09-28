#!/usr/bin/env python3
"""Project DD — autonomous data refresh. NO LLM, NO tokens consumed.

Pulls live data from Salesforce (sf CLI) and Databricks logfood (databricks CLI),
writes raw_*.json into ../data/, then rebuilds ../app/data.js via build_data.py.

Runs unattended from launchd (09:00 daily + on login) and on demand from the
in-app "Refresh now" button via refresh_server.py. Both entry points take the
same flock, so they can never overlap.

Nothing here needs a browser, but everything here needs a live Salesforce
session, and the unattended path deliberately refuses to open a login window —
so the schedule is really a bet on when you are already signed in. Pick an hour
when you are reliably at the machine (config/territory.json -> schedule), then
re-run `python3 setup/install_schedule.py`.

Order of operations matters: ALL THREE credentials are gated up front (~4-5s)
before a single row is pulled, so a dead token fails in seconds instead of after
40s of work. The three probes run concurrently, so the third costs ~0 wall time.
Every CLI call checks its return code and carries the CLI's own stderr into a
typed error — see ddcore.py. Transient network faults get 2 retries; expired
tokens get none, because retrying a dead token is just a slower way to fail.

The three credentials, and what each one costs when it dies:
  * Salesforce (your SFDC login)             — FATAL, no UCOs/opps/accounts
  * Databricks data profile (e.g. logfood)   — FATAL, no consumption
  * Databricks cloud profile (optional)      — NON-fatal, but the cloud mirror
    (your Databricks App) cannot publish. A dead cloud token is reported loudly
    so the mirror never silently falls a day behind the local cockpit.

The consumption forecast is part of THIS protocol — never a manual side-task.
Every run re-pulls each AE's quarterly forecast and quota from the objects behind
the ConsumptionPlan app (Consumption_Forecast__c 'AE Forecast' →
raw_cp_forecast.json, ConsumptionExt__c 'User' → raw_cp_targets.json). Those live
values outrank data/forecast.json and the QUOTA table in build_data.py, which
survive only as frozen fallbacks for a quarter SFDC has no row for. Nobody
hand-types a forecast number into this project.

Every run also VERIFIES that each app module was actually built (ddmodules.py) —
Territory, Quarter Scorecard, Opportunities, New Pipeline, Pipeline Coverage,
RoB, plus the two data-quality contracts that key-existence alone cannot catch:
the live ConsumptionPlan feed (a pull that comes back empty is a FAILURE, because
the manual fallback would otherwise hide it) and next-steps capture on UCOs/opps.

Fail-soft contract (unchanged): a failed refresh never touches data.js — the app
keeps serving the last good snapshot.

Run manually:  python3 refresh.py           (unattended: dead token = fast fail)
               python3 refresh.py --auth    (interactive: opens a browser login
                                             for each dead credential, then runs)
Exit codes:    0 ok · 2 failed · 3 already running
"""
import os, sys, json, subprocess, datetime, time, shutil, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ddcore as dd
from ddcore import RefreshError, log

HERE = dd.HERE
ROOT = dd.ROOT
DATA = dd.DATA
LOG  = dd.LOG

WAREHOUSE = dd.WAREHOUSE
DBX_PROFILE = dd.DBX_PROFILE
import ddconfig as cfg
FY = cfg.FY_START   # current Databricks fiscal year start — window for UC activity & new opps
# Account owners in scope = the AEs listed in config/territory.json (setup/configure.py
# discovers them from Salesforce as the active users reporting to the manager).
OWNER_IDS = cfg.OWNER_IDS
# The manager's OWN Salesforce user id — for their Manager Forecast rollup (see "CP mgr forecast").
MANAGER_ID = cfg.MANAGER_ID
# Catalog that holds gtm_gold (configure.py detects it; on logfood it is `main`).
# Empty = the workspace default catalog, which is what older configs relied on.
CATALOG = cfg.DBX_CATALOG
PRIOR_FY_NUM, CUR_FY_NUM = cfg.PRIOR_FY, cfg.FISCAL_YEAR


# ---------- Salesforce ----------
def sf_query(org, soql, out):
    data = dd.sf_json(["sf", "data", "query", "--target-org", org,
                       "--query", soql, "--json"],
                      timeout=300, ctx="SF query")
    res = data.get("result") or {}
    if not res.get("done", True):
        raise RefreshError(dd.SF_QUERY, "SF query not done (pagination needed)",
                           f"totalSize={res.get('totalSize')}")
    with open(out, "w") as f: json.dump(data, f)
    return res.get("totalSize", len(res.get("records") or []))

# ---------- Databricks ----------
def dbx_sql(stmt):
    payload = {"warehouse_id": WAREHOUSE, "statement": stmt, "wait_timeout": "50s",
               "disposition": "INLINE", "format": "JSON_ARRAY"}
    if CATALOG:
        payload["catalog"] = CATALOG
    res = dd.dbx_json(["databricks", "api", "post", "/api/2.0/sql/statements",
                       "--profile", DBX_PROFILE, "--json", json.dumps(payload)],
                      timeout=300, ctx="DBX statement submit")
    sid = res.get("statement_id")
    state = res.get("status", {}).get("state")
    if not sid:
        code, transient = dd.classify(json.dumps(res)[:400], family="dbx")
        raise RefreshError(code, "DBX returned no statement_id",
                           dd.tail(json.dumps(res), 300), transient)
    # poll until terminal
    waited = 0
    while state in ("PENDING", "RUNNING") and waited < 240:
        time.sleep(5); waited += 5
        res = dd.dbx_json(["databricks", "api", "get",
                           f"/api/2.0/sql/statements/{sid}",
                           "--profile", DBX_PROFILE],
                          timeout=60, ctx="DBX statement poll")
        state = res.get("status", {}).get("state")
    if state in ("PENDING", "RUNNING"):
        raise RefreshError(dd.TIMEOUT, f"DBX statement still {state} after {waited}s",
                           f"statement_id={sid}", transient=True)
    if state != "SUCCEEDED":
        err = (res.get("status", {}).get("error") or {})
        detail = dd.tail(err.get("message") or json.dumps(res.get("status", {})), 300)
        code, transient = dd.classify(detail, err.get("error_code", ""), family="dbx")
        # A failed statement is a query problem unless the text says otherwise.
        if code == dd.DBX_AUTH and "401" not in detail and "unauthor" not in detail.lower():
            code = dd.DBX_QUERY
        raise RefreshError(code, f"DBX state={state}", detail, transient)
    # gather all chunks
    manifest = res.get("manifest", {})
    total_chunks = manifest.get("total_chunk_count", 1)
    rows = res.get("result", {}).get("data_array", []) or []
    for i in range(1, total_chunks):
        rc = dd.dbx_json(["databricks", "api", "get",
                          f"/api/2.0/sql/statements/{sid}/result/chunks/{i}",
                          "--profile", DBX_PROFILE],
                         timeout=120, ctx=f"DBX chunk {i}")
        rows += rc.get("data_array", []) or []
    # normalize to the shape build_data.py expects
    return {"manifest": manifest,
            "result": {"data_array": rows, "row_count": len(rows)},
            "status": {"state": state}}

def dbx_write(stmt, out):
    data = dbx_sql(stmt)
    with open(out, "w") as f: json.dump(data, f)
    return data["result"]["row_count"]

# ---------- staging ----------
def sweep_stale_staging(max_age=3600):
    """Remove abandoned per-run staging dirs (crash/kill), never a live one."""
    try:
        now = time.time()
        for name in os.listdir(DATA):
            if not name.startswith("_staging"):
                continue
            p = os.path.join(DATA, name)
            if os.path.isdir(p) and now - os.path.getmtime(p) > max_age:
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass

def main():
    t0 = time.time()
    # --auth: probe all three credentials and open a browser login for whichever
    # ones are dead, then refresh immediately. Deliberately OPT-IN — launchd runs
    # this same script at 09:00 and must never pop browser windows at a sleeping
    # laptop, so the unattended path keeps failing honestly instead.
    interactive_auth = "--auth" in sys.argv
    prechecked = None
    dd.rotate_log()
    dd.truncate_if_big(os.path.join(HERE, "launchd.out.log"))
    dd.truncate_if_big(os.path.join(HERE, "launchd.err.log"))

    # Auth repair runs BEFORE the lock is taken: a browser SSO can sit for
    # minutes waiting on a human, and holding the single-flight lock through
    # that would block the 09:00 job behind an unattended dialog.
    if interactive_auth:
        log("=== auth check (interactive) ===")
        st = dd.ensure_auth(log_fn=log)
        if not st["sf"] or not st["dbx"]:
            code = dd.SF_AUTH if not st["sf"] else dd.DBX_AUTH
            msg = ("Salesforce login still dead after re-auth" if not st["sf"]
                   else f"Databricks {dd.DBX_PROFILE} login still dead after re-auth")
            log(f"!! REFRESH ABORTED: [{code}] {msg}")
            dd.emit_result(False, code, msg, "", dd.HINTS.get(code, ""),
                           dd.REAUTH_FOR.get(code), time.time() - t0)
            sys.exit(2)
        prechecked = st      # feeds preflight(), which then skips a re-probe

    # Single-flight: the button and the 09:00 launchd job share one flock.
    try:
        lock_fd = dd.acquire_lock()
    except RefreshError as e:
        log(f"!! REFRESH SKIPPED: {e}")
        dd.emit_result(False, e.code, e.message, e.detail,
                       dd.HINTS.get(e.code, ""), None, time.time() - t0)
        sys.exit(3)

    log("=== Project DD refresh start ===")
    sweep_stale_staging()
    # Per-run staging dir: two runs can never delete each other's files.
    tmp = tempfile.mkdtemp(prefix="_staging_", dir=DATA)
    ids_sf = "(" + ",".join("'" + i + "'" for i in OWNER_IDS) + ")"
    try:
        # 0) PREFLIGHT — gate both credentials before any data pull.
        dd.progress("Checking Salesforce + Databricks logins", 6)
        pre = dd.preflight(prechecked=prechecked)
        org = pre["sf"]
        # the cloud profile is probed here but never fatal — see ddcore.preflight_cloud().
        cloud = pre.get("cloud") or {"ok": False, "who": "", "why": "not probed"}
        log(f"  preflight OK in {pre['elapsed']}s"
            + (" (reused --auth probe)" if pre.get("reused") else "")
            + f" — SF {org} · "
            f"DBX {DBX_PROFILE} as {pre['dbx']} · "
            + ("cloud disabled" if not dd.CLOUD_ENABLED
               else f"cloud {dd.DBX_CLOUD_PROFILE} "
               + (f"as {cloud['who']}" if cloud["ok"]
                  else f"DEAD ({dd.tail(cloud['why'], 90)})")))

        # 1) Salesforce — accounts first (Databricks queries need the account-id list)
        dd.progress("Pulling accounts", 16)
        n = dd.with_retry(lambda: sf_query(org,
            "SELECT Id, Name, Owner.Name, ARR__c, T3M_ARR__c, Industry FROM Account "
            f"WHERE OwnerId IN {ids_sf} ORDER BY Owner.Name, Name",
            os.path.join(tmp, "raw_accounts.json")), "accounts")
        log(f"  accounts: {n}")

        # account-id IN-list from the freshly pulled accounts
        accs = json.load(open(os.path.join(tmp, "raw_accounts.json")))["result"]["records"]
        ids_dbx = "(" + ",".join("'" + a["Id"] + "'" for a in accs) + ")"

        # MECE product matrix from BILLING SKUs (gtm_gold.sku_consumption_daily), buckets sum
        # EXACTLY to total. COMPUTE products only: attributed views
        # (uc_dbu_dollars, ai_bi_dbu_dollars, photon_dbu_dollars overlays, sku_type_*) must
        # never be mixed in — they double-count. Serverless SKUs have no Photon variant
        # (Photon is built into serverless pricing), hence no *_serverless Photon bucket.
        # AI/BI has no SKU of its own; its dollars ride on DBSQL Serverless. CASE order matters.
        prod_case = ("CASE"
            " WHEN sku LIKE '%jobs_serverless_compute%' THEN 'jobs_serverless'"
            " WHEN sku LIKE '%jobs_compute_(photon)%' THEN 'jobs_classic_photon'"
            " WHEN sku LIKE '%jobs_compute%' THEN 'jobs_classic'"
            " WHEN sku LIKE '%all_purpose_serverless_compute%' THEN 'ap_serverless'"
            " WHEN sku LIKE '%all_purpose_compute_(photon)%' THEN 'ap_photon'"
            " WHEN sku LIKE '%all_purpose_compute%' THEN 'ap_classic'"
            " WHEN sku LIKE '%serverless_sql_compute%' THEN 'dbsql_serverless'"
            " WHEN sku LIKE '%sql_pro_compute%' THEN 'dbsql_pro'"
            " WHEN sku LIKE '%sql_compute%' THEN 'dbsql_classic'"
            " WHEN sku LIKE '%dlt%' THEN 'dlt'"
            " WHEN sku LIKE '%model_serving%' OR sku LIKE '%real_time_inference%'"
            "  OR sku LIKE '%model_training%' OR sku LIKE '%finetun%' OR sku LIKE '%foundation%' THEN 'genai'"
            " WHEN sku LIKE '%database_serverless_compute%' THEN 'lakebase'"
            " ELSE 'other' END")
        PROD_KEYS = ["jobs_classic","jobs_classic_photon","jobs_serverless",
                     "dbsql_classic","dbsql_pro","dbsql_serverless",
                     "ap_classic","ap_photon","ap_serverless",
                     "dlt","genai","lakebase","other"]
        prod_cols = ", ".join(f"SUM(IF(b='{k}',d,0)) AS {k}" for k in PROD_KEYS) + ", SUM(d) AS total"
        def prod_stmt(where):
            return (f"SELECT account_id, {prod_cols} FROM (SELECT account_id, {prod_case} AS b, "
                    f"dbu_dollars AS d FROM gtm_gold.sku_consumption_daily "
                    f"WHERE account_id IN {ids_dbx} AND {where}) GROUP BY account_id")

        # 2) Remaining Salesforce pulls + all Databricks pulls run CONCURRENTLY.
        # Each round-trip carries a fixed baseline (~3s DBX, ~3s SF); serialized these
        # stack to ~50s. Fanned out over a thread pool the wall time collapses to the
        # single slowest query. Every job writes its own staging file, so there is no
        # shared state; a failure in any job raises out of fut.result() -> fail-safe.
        # Each job is individually retried twice on TRANSIENT faults only.
        def _sf(soql, out): return sf_query(org, soql, os.path.join(tmp, out))
        def _dbx(stmt, out): return dbx_write(stmt, os.path.join(tmp, out))

        jobs = {
            # --- Salesforce ---
            "open UCOs": (_sf, (
                "SELECT Id, Name, Stages__c, Full_Production_Date__c, Implementation_Start_Date__c, "
                "Implementation_Status__c, MonthlyTotalDollarDBUs__c, Use_Case_Type__c, TShirtSize__c, "
                # AUTHORITATIVE stage-age from SFDC itself (2026-07-07): CurrentStageDaysCount__c is
                # the exact "days in CURRENT stage" SFDC shows on the stage bar (e.g. "15 days in U2");
                # Last_Stage_Modified_Date__c is the true current-stage-entry datetime. These REPLACE
                # the old U{k}Date__c-milestone guess (which over-counted after a stage regression).
                "CurrentStageDaysCount__c, Last_Stage_Modified_Date__c, "
                # LastNextStepsModifiedDate__c is the dedicated Next-Steps stamp, but its populating flow
                # watches the unused NextSteps__c field and misses ~27% of real Demand_Plan_Next_Steps__c
                # edits (verified 2026-08-24) — so CustomLastModifiedDate__c is pulled as a freshness fallback.
                "Demand_Plan_Next_Steps__c, LastNextStepsModifiedDate__c, CustomLastModifiedDate__c, Use_Case_Description__c, "
                "ProjectedConsumption__c, BusinessImpact__c, LastModifiedDate, "
                "Account__c, Account__r.Name, Account__r.Owner.Name FROM UseCase__c "
                f"WHERE Account__r.OwnerId IN {ids_sf} AND Stages__c IN ('U1','U2','U3','U4','U5') "
                "ORDER BY Account__r.Owner.Name, Account__r.Name", "raw_ucos.json")),
            # UC activity (created or stage-progressed since FY27 start) — for weekly tables
            "UC activity": (_sf, (
                "SELECT Name, Account__c, Account__r.Name, Account__r.Owner.Name, Stages__c, "
                "Use_Case_Description__c, MonthlyTotalDollarDBUs__c, ProjectedConsumption__c, CreatedDate, "
                "U2Date__c, U3Date__c, U4Date__c, U5Date__c, U6Date__c FROM UseCase__c "
                f"WHERE Account__r.OwnerId IN {ids_sf} AND (CreatedDate >= {FY}T00:00:00Z "
                f"OR U2Date__c >= {FY} OR U3Date__c >= {FY} OR U4Date__c >= {FY} OR U5Date__c >= {FY} OR U6Date__c >= {FY})",
                "raw_uco_activity.json")),
            # Opportunities — open (for commit lists), closed-won (booked this FY), and
            # new-this-FY (for weekly new opps).
            "open opps": (_sf, (
                "SELECT Id, Name, AccountId, Account.Name, Account.Owner.Name, StageName, ForecastCategoryName, "
                "IsWon, IsClosed, Amount, CloseDate, CreatedDate, Type, Next_Step_Detail__c, Next_Steps_Last_Updated__c, "
                # AUTHORITATIVE opp stage-age (2026-07-07): LastStageChangeDate = current-stage entry
                # (days-in-stage = today − this); LeanData__Days_In_Stage__c = the packaged counter as a
                # cross-check. Feed the new opportunity STALE-STAGE + CLOSE-DATE-PAST hygiene lenses.
                "LastStageChangeDate, LeanData__Days_In_Stage__c, "
                # IARR (Incremental Annual Recurring Revenue) = the deal's net-new subscription value
                # (upsell − existing), NOT the multi-year TCV in Amount. CPQ_Incremental_Booking_ARR__c
                # is the CPQ-authored figure (verified against a known renewal). Tenure = NumberOfTerms__c (contract term in MONTHS, /12 = years),
                # with New_Term_in_months__c / ContractTermMonths__c as fallbacks when the CPQ term is blank.
                "CPQ_Incremental_Booking_ARR__c, NumberOfTerms__c, New_Term_in_months__c, ContractTermMonths__c "
                f"FROM Opportunity WHERE Account.OwnerId IN {ids_sf} "
                "AND IsClosed=false ORDER BY Amount DESC", "raw_opps_open.json")),
            # Closed-won opps that BOOKED this FY (CloseDate >= FY start). These are the deals
            # already won in-territory — the Opportunities module must show them alongside open
            # pipeline so a quarter's picture is complete.
            # Won/open status is driven by SFDC's IsWon flag, never by parsing the stage name.
            "won opps": (_sf, (
                "SELECT Id, Name, AccountId, Account.Name, Account.Owner.Name, StageName, ForecastCategoryName, "
                "IsWon, IsClosed, Amount, CloseDate, CreatedDate, Type, Next_Step_Detail__c, Next_Steps_Last_Updated__c, "
                "CPQ_Incremental_Booking_ARR__c, NumberOfTerms__c, New_Term_in_months__c, ContractTermMonths__c "
                f"FROM Opportunity WHERE Account.OwnerId IN {ids_sf} "
                f"AND IsWon=true AND CloseDate >= {FY} ORDER BY CloseDate DESC", "raw_opps_won.json")),
            "new opps": (_sf, (
                "SELECT Id, Name, AccountId, Account.Name, Account.Owner.Name, StageName, ForecastCategoryName, "
                "IsWon, IsClosed, Amount, CloseDate, CreatedDate, "
                "CPQ_Incremental_Booking_ARR__c, NumberOfTerms__c, New_Term_in_months__c, ContractTermMonths__c "
                f"FROM Opportunity WHERE Account.OwnerId IN {ids_sf} "
                f"AND CreatedDate >= {FY}T00:00:00Z ORDER BY CreatedDate DESC", "raw_opps_new.json")),
            # ConsumptionPlan app live pulls (2026-08-28) — the objects behind
            # databricks.lightning.force.com/c/ConsumptionPlan.app, replacing the manually
            # transcribed forecast.json numbers + build_data QUOTA dict (both kept as fallbacks).
            #   Consumption_Forecast__c RT 'AE Forecast': ONE row per AE per fiscal quarter,
            #     ForecastDate__c = quarter's first month; MyForecastCurrency__c = the app's
            #     "AE Forecast" column (verified vs app screenshots to the dollar).
            #   ConsumptionExt__c RT 'User': one row per AE per quarter (QuarterStartDate__c);
            #     Target__c = quota — matched the hand-typed QUOTA table exactly, all 4 quarters.
            "CP forecasts": (_sf, (
                "SELECT Owner.Name, ForecastDate__c, MyForecastCurrency__c, LastSubmittedDate__c "
                "FROM Consumption_Forecast__c WHERE RecordType.Name = 'AE Forecast' "
                f"AND OwnerId IN {ids_sf} AND ForecastDate__c >= {FY}", "raw_cp_forecast.json")),
            "CP targets": (_sf, (
                "SELECT User__r.Name, QuarterStartDate__c, Target__c "
                "FROM ConsumptionExt__c WHERE RecordType.Name = 'User' "
                f"AND User__c IN {ids_sf} AND QuarterStartDate__c >= {FY}", "raw_cp_targets.json")),
            #   Consumption_Forecast__c RT 'Manager Forecast', owned by the manager: ONE row per
            #     fiscal quarter. MyForecastCurrency__c = the manager's OWN committed forecast (the
            #     "My Forecast" line in the cockpit), DISTINCT from the AE rollup in
            #     TeamForecastCurrencyRollup__c (≈ sum of their AEs). Filtering OwnerId=manager
            #     scopes it to their book — NOT their own manager's wider team.
            "CP mgr forecast": (_sf, (
                "SELECT ForecastDate__c, MyForecastCurrency__c, TeamForecastCurrencyRollup__c, "
                "LastSubmittedDate__c FROM Consumption_Forecast__c "
                "WHERE RecordType.Name = 'Manager Forecast' "
                f"AND OwnerId = '{MANAGER_ID}' AND ForecastDate__c >= {FY}", "raw_cp_mgr_forecast.json")),
            # --- Databricks consumption ---
            "quarterly rows": (_dbx, (
                f"SELECT account_id, fiscal_year_quarter, SUM(dbu_dollars) AS dbu_dollars "
                f"FROM gtm_gold.account_consumption_daily WHERE account_id IN {ids_dbx} "
                f"AND fiscal_year IN ({PRIOR_FY_NUM}, {CUR_FY_NUM}) GROUP BY account_id, fiscal_year_quarter",
                "raw_consumption_quarterly.json")),
            f"{cfg.FY_LABEL} product rows": (_dbx, (
                prod_stmt(f"usage_date >= '{FY}'"), "raw_products.json")),
            "T28D product rows": (_dbx, (
                prod_stmt(f"usage_date >= date_sub((SELECT MAX(usage_date) "
                          f"FROM gtm_gold.sku_consumption_daily WHERE account_id IN {ids_dbx}), 27)"),
                "raw_products_t28d.json")),
            "daily rows": (_dbx, (   # 371d = 53 wks, for the 52-week T7D trend
                f"SELECT account_id, usage_date, SUM(dbu_dollars) AS d "
                f"FROM gtm_gold.account_consumption_daily WHERE account_id IN {ids_dbx} "
                f"AND usage_date >= date_sub(current_date(), 371) GROUP BY account_id, usage_date",
                "raw_daily.json")),
            # ultimate parent mapping (for parent-account rollup)
            "parent rows": (_dbx, (
                f"SELECT account_id, account_name, ultimate_parent_account "
                f"FROM gtm_gold.account_consumption_daily WHERE account_id IN {ids_dbx} "
                f"GROUP BY account_id, account_name, ultimate_parent_account", "raw_parents.json")),
        }
        total = len(jobs); done = 0
        dd.progress(f"Pulling 0/{total} datasets", 22)
        with ThreadPoolExecutor(max_workers=total) as ex:
            futs = {ex.submit(dd.with_retry, (lambda f=fn, a=args: f(*a)), label): label
                    for label, (fn, args) in jobs.items()}
            for fut in as_completed(futs):
                label = futs[fut]
                n = fut.result()   # re-raises any job's exception -> fail-safe below
                done += 1
                log(f"  {label}: {n}")
                dd.progress(f"Pulled {done}/{total} datasets", 22 + int(62 * done / total))

        # 3) promote staging -> data, then build
        dd.progress("Promoting data files", 88)
        for fn in ["raw_accounts.json","raw_ucos.json","raw_uco_activity.json",
                   "raw_opps_open.json","raw_opps_won.json","raw_opps_new.json","raw_consumption_quarterly.json",
                   "raw_products.json","raw_products_t28d.json","raw_daily.json","raw_parents.json",
                   "raw_cp_forecast.json","raw_cp_targets.json","raw_cp_mgr_forecast.json"]:
            shutil.move(os.path.join(tmp, fn), os.path.join(DATA, fn))
        log("  raw files promoted")

        # stamp the successful-refresh time (12h, in this Mac's time zone) for "Last refresh"
        stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z")
        with open(os.path.join(DATA, "last_refresh.txt"), "w") as f: f.write(stamp)

        dd.progress("Rebuilding data.js", 92)
        bld = dd.run([sys.executable, os.path.join(DATA, "build_data.py")],
                     timeout=120, ctx="build_data.py")
        if bld.returncode != 0:
            raise RefreshError(dd.BUILD, f"build_data.py exited {bld.returncode}",
                               dd.tail(bld.stderr or bld.stdout, 400))
        log("  build_data.py: " + (bld.stdout.strip().splitlines()[-1] if bld.stdout.strip() else "ok"))

        # 4) publish the cloud mirror (your Databricks App), detached + best-effort.
        # deploy_cloud.py self-guards: it skips silently when the cloud login is not
        # live OR when data.js has not changed, and it NEVER affects this refresh's
        # verdict. Only runs when databricks_cloud.enabled = true in the config.
        dd.progress("Publishing cloud mirror", 99)
        cloud_note = ""
        deploy_cloud = os.path.join(ROOT, "cloud", "deploy_cloud.py")
        if not dd.CLOUD_ENABLED:
            log("  cloud mirror disabled in config — local cockpit only")
        elif not cloud["ok"]:
            # deploy_cloud.py self-guards and would exit quietly here. Quietly is
            # the problem: that silence lets the mirror fall a day behind while
            # this log says OK. Say it out loud, and carry it into the verdict so
            # the in-app Refresh button shows it too.
            cloud_note = f"cloud publish SKIPPED ({dd.DBX_CLOUD_PROFILE} login dead)"
            log(f"  !! {cloud_note} — the LOCAL cockpit is fresh, but "
                f"{dd.CLOUD_APP_NAME} still serves the PREVIOUS snapshot.")
            log(f"     fix: python3 refresh.py --auth   (or: databricks auth login "
                f"--host {dd.DBX_CLOUD_HOST} --profile {dd.DBX_CLOUD_PROFILE})")
        elif os.path.exists(deploy_cloud):
            try:
                os.makedirs(os.path.join(ROOT, "cloud", ".deploy"), exist_ok=True)
                with open(os.path.join(ROOT, "cloud", ".deploy", "AUTO_DEPLOY.log"), "a") as lf:
                    subprocess.Popen([sys.executable, deploy_cloud], stdout=lf, stderr=lf,
                                     start_new_session=True)
                log("  cloud mirror publish kicked (background)")
            except Exception as e:
                log(f"  cloud mirror publish kick failed (non-fatal): {e}")

        # 7) VERIFY every app module actually got built. Until this step existed
        # the run proved only that build_data.py exited 0, which is a far weaker
        # claim than "the cockpit is fresh" — Hygiene, Pipeline Coverage and RoB
        # are each produced by a different (and partly non-fatal) step, so any of
        # them could quietly empty out while the log still said OK. Reported, not
        # raised: data.js is already written and serving, so turning a module
        # regression into a hard failure would throw away a good pull.
        dd.progress("Verifying app modules", 99)
        msum = None
        try:
            import ddmodules
            rows, msum = ddmodules.verify()
            for line in ddmodules.format_rows(rows):
                log(line)
            log(f"  modules: {msum['line']}")
        except Exception as e:                # noqa: BLE001 — never fail the run here
            log(f"  module verification skipped (non-fatal): {type(e).__name__}: {e}")

        elapsed = time.time() - t0
        dd.progress("Done", 100)
        # The verdict carries every caveat, so the in-app Refresh button and the
        # log tell the same story instead of a bare green tick hiding a gap.
        caveats = []
        if msum and msum["failed"]:
            caveats.append(f"{msum['empty'] + msum['missing']} app module(s) "
                           f"failed verification")
        if cloud_note:
            caveats.append(cloud_note)
        msg = f"refresh OK in {elapsed:.0f}s" + (" — " + "; ".join(caveats) if caveats else "")
        log(f"=== {msg} — data.js rebuilt ===")
        dd.emit_result(True, "", msg, "", "", None, elapsed)
    except RefreshError as e:
        log(f"!! REFRESH FAILED: {e}")
        log("   previous data.js left in place (app still serves last good snapshot)")
        dd.progress(f"Failed: {e.code}", 100)
        dd.emit_result(False, e.code, e.message, e.detail,
                       dd.HINTS.get(e.code, ""), dd.REAUTH_FOR.get(e.code),
                       time.time() - t0)
        sys.exit(2)
    except Exception as e:
        log(f"!! REFRESH FAILED: [{dd.INTERNAL}] {type(e).__name__}: {e}")
        log("   previous data.js left in place (app still serves last good snapshot)")
        dd.progress(f"Failed: {dd.INTERNAL}", 100)
        dd.emit_result(False, dd.INTERNAL, f"{type(e).__name__}: {e}", "",
                       dd.HINTS[dd.INTERNAL], None, time.time() - t0)
        sys.exit(2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # only ever this run's own dir
        dd.release_lock(lock_fd)

if __name__ == "__main__":
    main()
