#!/usr/bin/env python3
"""Project DD — territory configurator. Stdlib only, NO LLM, NO tokens.

Run once per manager. It works out WHO you are and WHO works for you straight
from Salesforce, then writes config/territory.json. The rest of the cockpit
(refresh pipeline, data builder, UI, cloud mirror) reads only that file, so the
same code serves any manager on any team.

What it does, in order:
  1. Salesforce login   — checks the `sf` CLI session for your username; opens
                           the browser SSO if it is dead.
  2. Your role          — reads your User record: Id, Name, Title, Role and your
                           own manager (who becomes the "reviewer" in the weekly
                           RoB review module).
  3. Your team          — reads every ACTIVE user whose Manager is you
                           (--depth 2 also walks one level further down, for a
                           second-line manager).
  4. Who is an AE       — an AE in this tool = someone who OWNS Accounts. It
                           counts Accounts per person; account owners are
                           selected by default, everyone else (SAs, SEs,
                           specialists) is listed but left out.
  5. You confirm        — toggle people in/out, add anyone missing by email.
  6. Databricks         — picks the CLI profile that can read the consumption
                           tables (default `logfood`) and the SQL warehouse id,
                           and proves both with a live `SELECT 1`.
  7. Cloud mirror       — optional: the profile/workspace + app name for the
                           read-only Databricks App.
  8. Writes             — config/territory.json (git-ignored; holds ids, never
                           secrets — tokens stay in the sf/databricks CLIs).

Usage:
  python3 setup/configure.py                       # interactive (recommended)
  python3 setup/configure.py --username you@databricks.com
  python3 setup/configure.py --depth 2             # include your managers' reports
  python3 setup/configure.py --ae-emails a@databricks.com,b@databricks.com
                                                   # skip discovery, use exactly these AEs
  python3 setup/configure.py --toggle 3,5          # flip rows 3 and 5 of the team table
  python3 setup/configure.py --add-emails c@databricks.com
                                                   # add people the discovery missed
  python3 setup/configure.py --yes                 # accept every default (non-interactive)
  python3 setup/configure.py --dry-run             # print, don't write
  python3 setup/configure.py --skip-databricks     # keep the saved Databricks + cloud settings

Every question also has a flag, so the whole setup can run without a terminal
(for example driven by Claude Code). A flag always wins over a saved answer:

  python3 setup/configure.py --yes --dry-run --username you@databricks.com \\
      --data-profile logfood --data-host https://<data-workspace-host> \\
      --warehouse <sql-warehouse-id> --no-cloud
  (drop --dry-run to write the file; for the cloud mirror replace --no-cloud with
   --cloud --cloud-profile <name> --cloud-host https://<app-workspace> --app-name <name>)

Without a terminal and without --yes, every prompt quietly takes its default.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "territory.json")
SF_INSTANCE = "https://databricks.my.salesforce.com/"
EMAIL_RE = re.compile(r"^[^@\s'\"]+@[^@\s'\"]+\.[a-z]{2,}$", re.I)
ID_RE = re.compile(r"^005[A-Za-z0-9]{12,15}$")

YES = False  # set from --yes


# ------------------------------------------------------------------ helpers
def say(msg=""):
    print(msg, flush=True)


def ask(prompt, default=""):
    if YES:
        return default
    sfx = f" [{default}]" if default else ""
    try:
        v = input(f"  {prompt}{sfx}: ").strip()
    except EOFError:
        v = ""
    return v or default


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    v = ask(f"{prompt} ({d})", "").lower()
    if not v:
        return default
    return v.startswith("y")


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


def jloads(s):
    s = (s or "").strip()
    i = s.find("{")
    if i < 0:
        return {}
    try:
        return json.loads(s[i:])
    except Exception:
        return {}


def die(msg):
    say(f"\n✗ {msg}")
    sys.exit(1)


def kit_version():
    """The kit release in ./VERSION (the setup guide names the version it was written for)."""
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def soql_str(v):
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


# --------------------------------------------------------------- Salesforce
class SF:
    def __init__(self, username):
        self.username = username

    def query(self, soql):
        r = run(["sf", "data", "query", "--target-org", self.username,
                 "--query", soql, "--json"], timeout=180)
        if r is None:
            die("`sf data query` did not run (CLI missing or timed out).")
        data = jloads(r.stdout)
        if r.returncode != 0 or data.get("status", 0) != 0:
            msg = (data.get("message") or r.stderr or r.stdout or "")[:400]
            die(f"Salesforce query failed: {msg}\n  SOQL: {soql[:300]}{api_hint(msg)}")
        return (data.get("result") or {}).get("records") or []

    def try_query(self, soql):
        """query() that reports instead of exiting: returns (ok, message)."""
        r = run(["sf", "data", "query", "--target-org", self.username,
                 "--query", soql, "--json"], timeout=180)
        if r is None:
            return False, "`sf data query` did not run (CLI missing or timed out)"
        data = jloads(r.stdout)
        if r.returncode != 0 or data.get("status", 0) != 0:
            return False, (data.get("message") or r.stderr or r.stdout or "")[:300]
        return True, ""


def api_hint(msg):
    """Turn Salesforce's 'API disabled' family of errors into a next step."""
    m = (msg or "").upper()
    if any(k in m for k in ("API_DISABLED", "API_CURRENTLY_DISABLED", "API IS NOT ENABLED",
                            "API IS DISABLED", "REST API IS NOT ENABLED")):
        return ("\n  → Your Salesforce user cannot use the API. Browser access is not enough: the sf "
                "CLI needs API access. Ask your Salesforce admins for it (the setup guide's access "
                "checklist says where), then re-run.")
    return ""


def sf_default_username():
    r = run(["sf", "config", "get", "target-org", "--json"], timeout=30)
    if r is None:
        return ""
    try:
        return (jloads(r.stdout).get("result") or [{}])[0].get("value") or ""
    except Exception:
        return ""


def sf_connected(username):
    r = run(["sf", "org", "display", "--target-org", username, "--json"], timeout=60)
    if r is None or r.returncode != 0:
        return False
    res = jloads(r.stdout).get("result") or {}
    return (res.get("connectedStatus") or "").lower() == "connected"


def ensure_sf(username):
    if shutil.which("sf") is None:
        die("Salesforce CLI `sf` not found. Install it: brew install sf  (then re-run).")
    if sf_connected(username):
        say(f"  ✓ Salesforce session live for {username}")
        return
    say(f"  Salesforce session for {username} is not live — opening browser SSO "
        f"(finish the login in the browser; 5 min timeout)…")
    r = run(["sf", "org", "login", "web", f"--instance-url={SF_INSTANCE}", "--set-default"],
            timeout=320)
    if r is None or r.returncode != 0:
        die("Salesforce login did not complete. Re-run this script and finish the browser SSO.")
    run(["sf", "config", "set", f"target-org={username}", "--global"], timeout=30)
    if not sf_connected(username):
        die(f"Logged in, but not as {username}. Check the username you typed matches your SSO login.")
    say(f"  ✓ Salesforce session live for {username}")


USER_FIELDS = ("Id, Name, Username, Email, Title, IsActive, UserRole.Name, "
               "ManagerId, Manager.Name, Manager.Email")


def fetch_me(sf):
    rows = sf.query(f"SELECT {USER_FIELDS} FROM User WHERE Username = {soql_str(sf.username)} LIMIT 1")
    if not rows:
        rows = sf.query(f"SELECT {USER_FIELDS} FROM User WHERE Email = {soql_str(sf.username)} "
                        "AND IsActive = true LIMIT 1")
    if not rows:
        die(f"No Salesforce User found for {sf.username}.")
    return rows[0]


def fetch_reports(sf, manager_ids):
    if not manager_ids:
        return []
    inlist = "(" + ",".join(soql_str(i) for i in manager_ids) + ")"
    return sf.query(f"SELECT {USER_FIELDS} FROM User WHERE ManagerId IN {inlist} "
                    "AND IsActive = true ORDER BY Name")


def fetch_users_by_email(sf, emails):
    inlist = "(" + ",".join(soql_str(e) for e in emails) + ")"
    return sf.query(f"SELECT {USER_FIELDS} FROM User WHERE (Email IN {inlist} OR Username IN {inlist}) "
                    "AND IsActive = true ORDER BY Name")


def account_counts(sf, user_ids):
    if not user_ids:
        return {}
    inlist = "(" + ",".join(soql_str(i) for i in user_ids) + ")"
    rows = sf.query(f"SELECT OwnerId, COUNT(Id) n FROM Account WHERE OwnerId IN {inlist} GROUP BY OwnerId")
    return {r["OwnerId"]: int(r.get("n") or r.get("expr0") or 0) for r in rows}


def role_of(u):
    return ((u.get("UserRole") or {}).get("Name")) or ""


# Every Salesforce object the refresh reads. Probing each one here turns a missing
# permission into a named warning at setup time, instead of an SF_QUERY failure on
# the first refresh.
SF_OBJECTS = [
    ("Account", "accounts"),
    ("UseCase__c", "use cases"),
    ("Opportunity", "opportunities"),
    ("Consumption_Forecast__c", "ConsumptionPlan forecasts"),
    ("ConsumptionExt__c", "ConsumptionPlan quotas"),
]


def check_sf_objects(sf):
    bad = []
    for obj, what in SF_OBJECTS:
        ok, msg = sf.try_query(f"SELECT Id FROM {obj} LIMIT 1")
        if not ok:
            bad.append((obj, what, msg))
    if not bad:
        say("  ✓ API can read " + ", ".join(o for o, _ in SF_OBJECTS))
        return True
    hint = api_hint(" ".join(m for _, _, m in bad))
    if hint and len(bad) == len(SF_OBJECTS):
        die("Salesforce API access is off for your user." + hint)
    for obj, what, msg in bad:
        say(f"  ! cannot read {obj} ({what}): {msg[:160]}")
    say("    The first refresh will stop with SF_QUERY until you can read these. The setup")
    say("    guide's access checklist says what to ask for and where.")
    return False


# --------------------------------------------------------------- team picker
def pick_team(people, counts, toggles=(), select_all=False):
    """people: list of User rows. Returns the selected subset (AEs).

    select_all  start with everyone ticked (an explicit --ae-emails list)
    toggles     1-based row numbers to flip before the table is shown (--toggle):
                the non-interactive twin of typing row numbers at the prompt
    """
    sel = {p["Id"]: (select_all or counts.get(p["Id"], 0) > 0) for p in people}
    for i in toggles:
        if 1 <= i <= len(people):
            sel[people[i - 1]["Id"]] = not sel[people[i - 1]["Id"]]
        else:
            say(f"  ! --toggle {i}: there is no row {i} (1–{len(people)}), ignored")

    def show():
        say("")
        say("   #  in  name                          title / role                                accounts")
        say("  ──  ──  ────────────────────────────  ──────────────────────────────────────────  ────────")
        for i, p in enumerate(people, 1):
            tr = " / ".join(x for x in (p.get("Title") or "", role_of(p)) if x)[:42]
            say(f"  {i:>2}  {'✓ ' if sel[p['Id']] else '  '}  {p['Name'][:28]:<28}  {tr:<42}  {counts.get(p['Id'], 0):>8}")
        say("")

    while True:
        show()
        if YES:
            break
        v = ask("Toggle people by number (e.g. 2,5), or press Enter to accept", "")
        if not v:
            break
        for tok in re.split(r"[,\s]+", v):
            if tok.isdigit() and 1 <= int(tok) <= len(people):
                pid = people[int(tok) - 1]["Id"]
                sel[pid] = not sel[pid]
    return [p for p in people if sel[p["Id"]]]


def add_people(sf, emails_csv, aes, counts):
    """Add users by email (the 'Add anyone missing?' answer, or --add-emails)."""
    emails = [e.strip() for e in (emails_csv or "").split(",") if e.strip()]
    bad = [e for e in emails if not EMAIL_RE.match(e)]
    if bad:
        say(f"  ! not email addresses, ignored: {', '.join(bad)}")
    good = [e for e in emails if EMAIL_RE.match(e)]
    if not good:
        return aes
    have = {p["Id"] for p in aes}
    add = [p for p in fetch_users_by_email(sf, good) if p["Id"] not in have]
    counts.update(account_counts(sf, [p["Id"] for p in add]))
    say(f"  added: {', '.join(p['Name'] for p in add) or 'nobody (not found as active users, or already in)'}")
    return aes + add


# --------------------------------------------------------------- Databricks
def dbx_profiles():
    r = run(["databricks", "auth", "profiles", "--output", "json"], timeout=60)
    if r is None or r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout).get("profiles") or []
    except Exception:
        return []


def dbx_live(profile):
    r = run(["databricks", "current-user", "me", "--profile", profile, "--output", "json"], timeout=45)
    if r is None or r.returncode != 0:
        return ""
    try:
        return json.loads(r.stdout).get("userName") or ""
    except Exception:
        return ""


def dbx_login(profile, host):
    say(f"  opening browser login for Databricks profile '{profile}' ({host}) …")
    r = run(["databricks", "auth", "login", "--host", host, "--profile", profile], timeout=320)
    return r is not None and r.returncode == 0


BUSY = "BUSY"   # prefix of the error text when the warehouse, not access, is the problem


def dbx_sql(profile, warehouse, stmt, catalog="", max_wait=180):
    """Run one statement on the warehouse. Returns (True, rows) or (False, error text).

    The shared logfood warehouse can queue a statement past the API's 50 s wait, so
    keep polling until it finishes (up to max_wait). A statement still queued at the
    end is reported as BUSY, never as a missing permission."""
    payload = {"warehouse_id": warehouse, "statement": stmt, "wait_timeout": "50s",
               "disposition": "INLINE", "format": "JSON_ARRAY"}
    if catalog:
        payload["catalog"] = catalog
    r = run(["databricks", "api", "post", "/api/2.0/sql/statements", "--profile", profile,
             "--json", json.dumps(payload)], timeout=120)
    if r is None or r.returncode != 0:
        return False, ((r.stderr or r.stdout)[:300] if r else "CLI did not run")
    res = jloads(r.stdout)
    sid = res.get("statement_id")
    state = (res.get("status") or {}).get("state")
    waited = 0
    while state in ("PENDING", "RUNNING") and sid and waited < max_wait:
        time.sleep(5)
        waited += 5
        g = run(["databricks", "api", "get", f"/api/2.0/sql/statements/{sid}",
                 "--profile", profile], timeout=60)
        if g is None or g.returncode != 0:
            continue
        res = jloads(g.stdout) or res
        state = (res.get("status") or {}).get("state")
    if state == "SUCCEEDED":
        return True, (res.get("result") or {}).get("data_array") or []
    if state in ("PENDING", "RUNNING"):
        if sid:
            run(["databricks", "api", "post", f"/api/2.0/sql/statements/{sid}/cancel",
                 "--profile", profile], timeout=30)
        return False, (f"{BUSY}: warehouse still {state} after {50 + waited}s. It is busy; "
                       "this is not an access problem")
    return False, json.dumps(res.get("status") or res)[:300]


# The two consumption tables the refresh reads, both in schema gtm_gold. The catalog
# is detected and saved (logfood: main), so the refresh never depends on the
# workspace's default catalog.
GTM_TABLES = ("account_consumption_daily", "sku_consumption_daily")

# Both tables carry an account-level row filter.
# Outside this Opal group a query quietly returns only the accounts in your own
# book: no error, just missing rows.
BYPASS_GROUP = "datasets.main.gtm_data.read"


def find_catalog(profile, wh, wanted="", saved=""):
    """Where does gtm_gold live? An explicit --catalog is trusted as given. Otherwise
    the saved catalog, then the workspace default (current_catalog()), then Unity
    Catalog's own table index — so a saved catalog that stopped working is replaced
    by a fresh detection. Returns (catalog, ok, detail)."""
    # LIMIT 0: the engine resolves the table and checks SELECT while planning, then
    # reads nothing, so the probe stays quick even when reading the table would not.
    probe = f"SELECT 1 FROM gtm_gold.{GTM_TABLES[0]} LIMIT 0"
    if wanted:
        ok, detail = dbx_sql(profile, wh, probe, wanted)
        return wanted, ok, ("" if ok else detail)
    if saved:
        ok, detail = dbx_sql(profile, wh, probe, saved)
        if ok or str(detail).startswith(BUSY):
            return saved, ok, ("" if ok else detail)
        say(f"  ! saved catalog '{saved}' no longer finds gtm_gold; detecting again")
    # A busy warehouse ends the search at once: queuing more statements behind it
    # would only multiply the wait, and "busy" must never read as "no access".
    ok, rows = dbx_sql(profile, wh, "SELECT current_catalog()")
    if not ok and str(rows).startswith(BUSY):
        return "", False, rows
    default = str(rows[0][0]) if ok and rows and rows[0] else ""
    ok, detail = dbx_sql(profile, wh, probe, default)
    if ok or str(detail).startswith(BUSY):
        return default, ok, ("" if ok else detail)
    ok2, rows = dbx_sql(profile, wh,
                        "SELECT DISTINCT table_catalog FROM system.information_schema.tables "
                        f"WHERE table_schema = 'gtm_gold' AND table_name = '{GTM_TABLES[0]}'")
    if not ok2 and str(rows).startswith(BUSY):
        return default, False, rows
    cands = sorted({str(r[0]) for r in rows if r and r[0]}) if ok2 else []
    if not cands:
        return default, False, detail
    pick = cands[0] if len(cands) == 1 else ask(
        f"gtm_gold exists in several catalogs ({', '.join(cands)}). Which one",
        "main" if "main" in cands else cands[0])
    ok, detail = dbx_sql(profile, wh, probe, pick)
    return pick, ok, ("" if ok else detail)


def check_row_filter(profile, wh, catalog):
    """Warn, up front, when the row filter will hide part of the territory."""
    ok, rows = dbx_sql(profile, wh, f"SELECT is_account_group_member('{BYPASS_GROUP}')", catalog)
    if not ok:
        say("  ? could not check membership of the gtm_gold row-filter group (skipped)")
        return None
    if rows and str(rows[0][0]).lower() == "true":
        say(f"  ✓ member of {BYPASS_GROUP}: the account row filter hides nothing")
        return True
    say(f"  ! you are not in {BYPASS_GROUP}. Without it the gtm_gold row filter shows only")
    say("    the accounts in your own book, so your AEs' consumption comes back partial or")
    say("    empty WITHOUT any error. Request it in Opal (setup guide, access checklist)")
    say("    before the first refresh.")
    say("    The refresh's 'Consumption visibility' check flags it if you skip this.")
    return False


def configure_databricks(prev, opt):
    if shutil.which("databricks") is None:
        die("Databricks CLI not found. Install it: brew tap databricks/tap && brew install databricks "
            "(no admin rights? see the setup guide's no-admin install).")
    profs = dbx_profiles()
    names = [p.get("name") for p in profs]
    say(f"  Databricks CLI profiles on this Mac: {', '.join(names) or '(none)'}")
    d = prev.get("databricks_data") or {}
    profile = opt.data_profile or ask(
        "Profile that can read the consumption tables (gtm_gold.*)",
        d.get("profile") or ("logfood" if "logfood" in names else (names[0] if names else "logfood")))
    known = next((p.get("host") for p in profs if p.get("name") == profile), "") or ""
    host = opt.data_host or ask("Workspace URL for that profile", known or d.get("host", ""))
    if not host:
        die("A workspace URL is needed for the data profile (--data-host; the setup guide lists it).")
    if known and host.rstrip("/") != known.rstrip("/"):
        say(f"  ! profile '{profile}' already points at {known}; keeping that host. To change it, run: "
            f"databricks auth login --host {host} --profile {profile}")
        host = known
    who = dbx_live(profile)
    if not who:
        if not dbx_login(profile, host) or not (who := dbx_live(profile)):
            die(f"Databricks profile '{profile}' is not logged in. Run: "
                f"databricks auth login --host {host} --profile {profile}")
    say(f"  ✓ Databricks '{profile}' live as {who}")
    wh = opt.warehouse or ask("SQL warehouse id on that workspace (the hex id in the warehouse URL)",
                              d.get("warehouse_id", ""))
    if not wh:
        die("A SQL warehouse id is needed (--warehouse; the setup guide lists one).")
    ok, detail = dbx_sql(profile, wh, "SELECT 1 AS ok")
    if not ok:
        die(f"Warehouse {wh} did not run SELECT 1: {detail}\n"
            "  → Pick a warehouse you have 'Can use' on (SQL Warehouses in the workspace UI) "
            "and pass its id with --warehouse.")
    say(f"  ✓ warehouse {wh} answers SQL")
    catalog, ok, detail = find_catalog(profile, wh, (opt.catalog or "").strip(),
                                       saved=(d.get("catalog") or "").strip())
    if ok:
        say(f"  ✓ gtm_gold.{GTM_TABLES[0]} readable (catalog '{catalog or 'workspace default'}')")
        for t in GTM_TABLES[1:]:
            ok_t, det_t = dbx_sql(profile, wh, f"SELECT 1 FROM gtm_gold.{t} LIMIT 0", catalog)
            say(f"  ✓ gtm_gold.{t} readable" if ok_t else
                f"  ! could not read gtm_gold.{t} ({str(det_t)[:160]}): the refresh will fail until you can.")
        check_row_filter(profile, wh, catalog)
    elif str(detail).startswith(BUSY):
        say(f"  ? could not confirm gtm_gold access: {detail}. Carry on: the first refresh")
        say("    waits longer and retries by itself (or re-run configure.py later).")
    else:
        say(f"  ! could not read gtm_gold.{GTM_TABLES[0]} ({str(detail)[:160]}): the refresh will fail "
            f"until this profile can. Request {BYPASS_GROUP} in Opal (setup guide, access checklist).")
    return {"profile": profile, "host": host.rstrip("/"), "warehouse_id": wh, "catalog": catalog}


def configure_cloud(prev, first_name, opt):
    c = prev.get("databricks_cloud") or {}
    say("")
    say("  The cloud mirror is a read-only copy of the cockpit hosted as a Databricks App,")
    say("  so you (and anyone you share it with) can open it from any browser.")
    if opt.cloud is False:
        say("  skipped (--no-cloud): local cockpit only. Re-run with --cloud to add it later.")
        return {"enabled": False}
    if opt.cloud is None and not ask_yes("Set up the cloud mirror now?", c.get("enabled", True)):
        return {"enabled": False}
    profs = dbx_profiles()
    names = [p.get("name") for p in profs]
    profile = opt.cloud_profile or ask("Databricks CLI profile for the workspace that will HOST the app",
                                       c.get("profile") or "")
    known = next((p.get("host") for p in profs if p.get("name") == profile), "") or ""
    host = opt.cloud_host or ask("Workspace URL for that profile", known or c.get("host", ""))
    if not (profile and host):
        say("  ! cloud mirror left disabled (no profile/host). Re-run configure.py later.")
        return {"enabled": False}
    if known and host.rstrip("/") != known.rstrip("/"):
        say(f"  ! profile '{profile}' already points at {known}; keeping that host")
        host = known
    if profile not in names or not dbx_live(profile):
        if not dbx_login(profile, host) or not dbx_live(profile):
            say("  ! login failed — cloud mirror left disabled for now.")
            return {"enabled": False, "profile": profile, "host": host}
    slug = re.sub(r"[^a-z0-9-]+", "-", (first_name or "my").lower()).strip("-")
    app = opt.app_name or ask("Databricks App name (lowercase, digits, dashes)",
                              c.get("app_name") or f"dd-{slug}-cockpit")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,28}[a-z0-9]", app):
        die(f"Invalid app name {app!r}: use 3–30 lowercase letters, digits and dashes.")
    return {"enabled": True, "profile": profile, "host": host.rstrip("/"), "app_name": app,
            "workspace_source_dir": c.get("workspace_source_dir") or "project-dd",
            "app_url": c.get("app_url", "")}


# --------------------------------------------------------------------- main
def main():
    global YES
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", help="your Salesforce username (usually your work email)")
    ap.add_argument("--depth", type=int, default=1, choices=(1, 2),
                    help="1 = your direct reports (default); 2 = also their reports")
    ap.add_argument("--ae-emails", default="", help="comma-separated AE emails — skips discovery")
    ap.add_argument("--toggle", default="", help="team-table row numbers to flip in/out, e.g. 2,5")
    ap.add_argument("--add-emails", default="", help="comma-separated emails to add to the team")
    ap.add_argument("--label", help="territory label shown in the app header")
    ap.add_argument("--reviewer", help="who reviews your weekly RoB (the module title)")
    ap.add_argument("--data-profile", help="Databricks CLI profile for the consumption data (default logfood)")
    ap.add_argument("--data-host", help="workspace URL of the data profile")
    ap.add_argument("--warehouse", help="SQL warehouse id on the data workspace")
    ap.add_argument("--catalog", help="catalog that holds gtm_gold (default: detected)")
    ap.add_argument("--cloud", dest="cloud", action="store_true", help="set up the cloud mirror")
    ap.add_argument("--no-cloud", dest="cloud", action="store_false", help="local cockpit only")
    ap.add_argument("--cloud-profile", help="Databricks CLI profile of the workspace that hosts the app")
    ap.add_argument("--cloud-host", help="workspace URL that hosts the app")
    ap.add_argument("--app-name", help="Databricks App name (lowercase letters, digits, dashes)")
    ap.add_argument("--yes", action="store_true", help="accept every default (no prompts)")
    ap.add_argument("--dry-run", action="store_true", help="print the config, do not write it")
    ap.add_argument("--skip-databricks", action="store_true",
                    help="keep the saved Databricks and cloud settings")
    ap.set_defaults(cloud=None)
    a = ap.parse_args()
    YES = a.yes
    try:
        toggles = [int(x) for x in re.split(r"[,\s]+", a.toggle.strip()) if x]
    except ValueError:
        die(f"--toggle takes row numbers such as 2,5 (got {a.toggle!r})")

    prev = {}
    if os.path.exists(CONFIG_PATH):
        try:
            prev = json.load(open(CONFIG_PATH))
        except Exception:
            prev = {}

    say(f"\n━━ Project DD — territory setup (kit {kit_version()}) ━━\n")
    say("Step 1/5 · Salesforce login")
    username = a.username or (prev.get("manager") or {}).get("sfdc_username") or sf_default_username()
    username = ask("Your Salesforce username (work email)", username)
    if not EMAIL_RE.match(username or ""):
        die(f"{username!r} does not look like a Salesforce username (an email address).")
    ensure_sf(username)
    sf = SF(username)
    check_sf_objects(sf)

    say("\nStep 2/5 · Your role")
    me = fetch_me(sf)
    mgr = me.get("Manager") or {}
    say(f"  You:        {me['Name']}  ·  {me.get('Title') or '—'}  ·  role: {role_of(me) or '—'}")
    say(f"  Reports to: {mgr.get('Name') or '—'}")

    say("\nStep 3/5 · Your team")
    if a.ae_emails:
        emails = [e.strip() for e in a.ae_emails.split(",") if e.strip()]
        bad = [e for e in emails if not EMAIL_RE.match(e)]
        if bad:
            die(f"Not emails: {bad}")
        people = fetch_users_by_email(sf, emails)
        missing = sorted(set(e.lower() for e in emails) -
                         {(p.get("Email") or "").lower() for p in people} -
                         {(p.get("Username") or "").lower() for p in people})
        if missing:
            say(f"  ! not found as active Salesforce users: {', '.join(missing)}")
    else:
        people = fetch_reports(sf, [me["Id"]])
        if a.depth == 2 and people:
            people += fetch_reports(sf, [p["Id"] for p in people])
        say(f"  Found {len(people)} active people reporting to you"
            + (" (2 levels)" if a.depth == 2 else "") + ".")
        if not people:
            say("  Nobody reports to you in Salesforce's Manager field. Re-run with")
            say("  --ae-emails a@databricks.com,b@databricks.com to list your AEs directly.")
            sys.exit(1)
    counts = account_counts(sf, [p["Id"] for p in people])
    say("  ✓ = owns Salesforce Accounts (treated as an AE). Toggle anyone who should be in/out.")
    aes = pick_team(people, counts, toggles, select_all=bool(a.ae_emails))
    if a.add_emails:
        aes = add_people(sf, a.add_emails, aes, counts)
    while not YES:
        extra = ask("Add anyone missing? Enter comma-separated emails, or press Enter to continue", "")
        if not extra:
            break
        aes = add_people(sf, extra, aes, counts)
    if not aes:
        die("No AEs selected — nothing to build a territory from.")
    say(f"  Territory AEs ({len(aes)}): " + ", ".join(p["Name"] for p in aes))
    zero = [p["Name"] for p in aes if counts.get(p["Id"], 0) == 0]
    if zero:
        say(f"  ! owns no Accounts today (their rows will be empty): {', '.join(zero)}")

    label = a.label or ask("Territory label shown in the app header", prev.get("territory_label") or role_of(me) or f"{me['Name']} — Territory")
    reviewer = a.reviewer or ask("Who reviews your weekly RoB (the module title)", (prev.get("reviewer") or {}).get("name") or mgr.get("Name") or "Leadership")

    say("\nStep 4/5 · Databricks (consumption data)")
    if a.skip_databricks and prev.get("databricks_data"):
        dbx = prev["databricks_data"]
        say(f"  kept: profile {dbx.get('profile')} · warehouse {dbx.get('warehouse_id')}")
    else:
        dbx = configure_databricks(prev, a)

    say("\nStep 5/5 · Cloud mirror (optional)")
    if a.skip_databricks and prev.get("databricks_cloud"):
        cloud = prev["databricks_cloud"]
        say(f"  kept: {cloud}")
    else:
        cloud = configure_cloud(prev, (me["Name"] or "").split(" ")[0], a)

    prev_aes = {x.get("sfdc_user_id"): x for x in prev.get("aes", [])}
    cfg = {
        "_generated": f"setup/configure.py (kit {kit_version()}) on {datetime.date.today().isoformat()} — re-run it to change anything",
        "manager": {"name": me["Name"], "sfdc_username": username, "sfdc_user_id": me["Id"],
                    "title": me.get("Title") or "", "role": role_of(me)},
        "reviewer": {"name": reviewer, "sfdc_user_id": me.get("ManagerId") or ""},
        "territory_label": label,
        "aes": [{"name": p["Name"], "sfdc_user_id": p["Id"], "email": p.get("Email") or "",
                 "title": p.get("Title") or "", "role": role_of(p),
                 "accounts_owned": counts.get(p["Id"], 0),
                 "slack_user_id": (prev_aes.get(p["Id"]) or {}).get("slack_user_id", ""),
                 "include": True} for p in aes],
        "salesforce": {"instance_url": SF_INSTANCE},
        "databricks_data": dbx,
        "databricks_cloud": cloud,
        "schedule": prev.get("schedule") or {"hour": 9, "minute": 0},
        "fiscal_year": prev.get("fiscal_year"),
    }
    bad = [x["name"] for x in cfg["aes"] if not ID_RE.match(x["sfdc_user_id"])]
    if bad:
        die(f"Unexpected Salesforce ids for: {bad}")

    out = json.dumps(cfg, indent=2, ensure_ascii=False)
    if a.dry_run:
        say("\n--- config (dry run, not written) ---\n" + out)
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
    with open(CONFIG_PATH, "w") as f:
        f.write(out + "\n")
    say(f"\n✓ wrote {os.path.relpath(CONFIG_PATH, ROOT)}  ({len(cfg['aes'])} AEs · reviewer {reviewer})")
    say("\nNext:  python3 pipeline/refresh.py --auth      # first data pull (~30–60 s)")
    say("       python3 setup/install_schedule.py         # daily auto-refresh")
    if cloud.get("enabled"):
        say("       python3 setup/create_cloud_app.py         # one-time cloud app create + deploy")
    say("       open 'Open Project DD.command'            # the cockpit")


if __name__ == "__main__":
    main()
