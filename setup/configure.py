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
  python3 setup/configure.py --yes                 # accept every default (non-interactive)
  python3 setup/configure.py --dry-run             # print, don't write
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

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
            die(f"Salesforce query failed: {(data.get('message') or r.stderr or r.stdout)[:400]}\n"
                f"  SOQL: {soql[:300]}")
        return (data.get("result") or {}).get("records") or []


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


# --------------------------------------------------------------- team picker
def pick_team(people, counts):
    """people: list of User rows. Returns the selected subset (AEs)."""
    sel = {p["Id"]: counts.get(p["Id"], 0) > 0 for p in people}

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


def dbx_sql_ok(profile, warehouse, stmt="SELECT 1 AS ok"):
    payload = {"warehouse_id": warehouse, "statement": stmt, "wait_timeout": "50s",
               "disposition": "INLINE", "format": "JSON_ARRAY"}
    r = run(["databricks", "api", "post", "/api/2.0/sql/statements", "--profile", profile,
             "--json", json.dumps(payload)], timeout=120)
    if r is None or r.returncode != 0:
        return False, (r.stderr or r.stdout)[:300] if r else "CLI did not run"
    res = jloads(r.stdout)
    st = (res.get("status") or {}).get("state")
    if st == "SUCCEEDED":
        return True, res
    return False, json.dumps(res.get("status") or res)[:300]


def configure_databricks(prev):
    if shutil.which("databricks") is None:
        die("Databricks CLI not found. Install it: brew tap databricks/tap && brew install databricks")
    profs = dbx_profiles()
    names = [p.get("name") for p in profs]
    say(f"  Databricks CLI profiles on this Mac: {', '.join(names) or '(none)'}")
    d = prev.get("databricks_data") or {}
    profile = ask("Profile that can read the consumption tables (gtm_gold.*)",
                  d.get("profile") or ("logfood" if "logfood" in names else (names[0] if names else "logfood")))
    host = next((p.get("host") for p in profs if p.get("name") == profile), "") or d.get("host", "")
    host = ask("Workspace URL for that profile", host)
    if not host:
        die("A workspace URL is needed for the data profile (see the setup guide, step 3).")
    who = dbx_live(profile)
    if not who:
        if not dbx_login(profile, host) or not (who := dbx_live(profile)):
            die(f"Databricks profile '{profile}' is not logged in. Run: "
                f"databricks auth login --host {host} --profile {profile}")
    say(f"  ✓ Databricks '{profile}' live as {who}")
    wh = ask("SQL warehouse id on that workspace (the hex id in the warehouse URL)", d.get("warehouse_id", ""))
    if not wh:
        die("A SQL warehouse id is needed (see the setup guide, step 3).")
    ok, detail = dbx_sql_ok(profile, wh)
    if not ok:
        die(f"Warehouse {wh} did not run SELECT 1: {detail}")
    say(f"  ✓ warehouse {wh} answers SQL")
    ok, detail = dbx_sql_ok(profile, wh, "SELECT account_id FROM gtm_gold.account_consumption_daily LIMIT 1")
    if ok:
        say("  ✓ gtm_gold.account_consumption_daily is readable")
    else:
        say(f"  ! could not read gtm_gold.account_consumption_daily ({detail[:160]}) — "
            "the refresh will fail until this profile has access.")
    return {"profile": profile, "host": host.rstrip("/"), "warehouse_id": wh}


def configure_cloud(prev, first_name):
    c = prev.get("databricks_cloud") or {}
    say("")
    say("  The cloud mirror is a read-only copy of the cockpit hosted as a Databricks App,")
    say("  so you (and anyone you share it with) can open it from any browser.")
    if not ask_yes("Set up the cloud mirror now?", c.get("enabled", True)):
        return {"enabled": False}
    profs = dbx_profiles()
    names = [p.get("name") for p in profs]
    profile = ask("Databricks CLI profile for the workspace that will HOST the app",
                  c.get("profile") or "")
    host = next((p.get("host") for p in profs if p.get("name") == profile), "") or c.get("host", "")
    host = ask("Workspace URL for that profile", host)
    if not (profile and host):
        say("  ! cloud mirror left disabled (no profile/host). Re-run configure.py later.")
        return {"enabled": False}
    if profile not in names or not dbx_live(profile):
        if not dbx_login(profile, host):
            say("  ! login failed — cloud mirror left disabled for now.")
            return {"enabled": False, "profile": profile, "host": host}
    slug = re.sub(r"[^a-z0-9-]+", "-", (first_name or "my").lower()).strip("-")
    app = ask("Databricks App name (lowercase, digits, dashes)", c.get("app_name") or f"dd-{slug}-cockpit")
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
    ap.add_argument("--yes", action="store_true", help="accept every default")
    ap.add_argument("--dry-run", action="store_true", help="print the config, do not write it")
    ap.add_argument("--skip-databricks", action="store_true", help="keep the existing Databricks settings")
    a = ap.parse_args()
    YES = a.yes

    prev = {}
    if os.path.exists(CONFIG_PATH):
        try:
            prev = json.load(open(CONFIG_PATH))
        except Exception:
            prev = {}

    say("\n━━ Project DD — territory setup ━━\n")
    say("Step 1/5 · Salesforce login")
    username = a.username or (prev.get("manager") or {}).get("sfdc_username") or sf_default_username()
    username = ask("Your Salesforce username (work email)", username)
    if not EMAIL_RE.match(username or ""):
        die(f"{username!r} does not look like a Salesforce username (an email address).")
    ensure_sf(username)
    sf = SF(username)

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
    aes = pick_team(people, counts)
    while not YES:
        extra = ask("Add anyone missing? Enter comma-separated emails, or press Enter to continue", "")
        if not extra:
            break
        add = fetch_users_by_email(sf, [e.strip() for e in extra.split(",") if EMAIL_RE.match(e.strip())])
        have = {p["Id"] for p in aes}
        add = [p for p in add if p["Id"] not in have]
        counts.update(account_counts(sf, [p["Id"] for p in add]))
        aes += add
        say(f"  added: {', '.join(p['Name'] for p in add) or 'nobody (not found)'}")
    if not aes:
        die("No AEs selected — nothing to build a territory from.")
    say(f"  Territory AEs ({len(aes)}): " + ", ".join(p["Name"] for p in aes))
    zero = [p["Name"] for p in aes if counts.get(p["Id"], 0) == 0]
    if zero:
        say(f"  ! owns no Accounts today (their rows will be empty): {', '.join(zero)}")

    label = ask("Territory label shown in the app header", prev.get("territory_label") or role_of(me) or f"{me['Name']} — Territory")
    reviewer = ask("Who reviews your weekly RoB (the module title)", (prev.get("reviewer") or {}).get("name") or mgr.get("Name") or "Leadership")

    say("\nStep 4/5 · Databricks (consumption data)")
    if a.skip_databricks and prev.get("databricks_data"):
        dbx = prev["databricks_data"]
        say(f"  kept: profile {dbx.get('profile')} · warehouse {dbx.get('warehouse_id')}")
    else:
        dbx = configure_databricks(prev)

    say("\nStep 5/5 · Cloud mirror (optional)")
    if a.skip_databricks and prev.get("databricks_cloud"):
        cloud = prev["databricks_cloud"]
        say(f"  kept: {cloud}")
    else:
        cloud = configure_cloud(prev, (me["Name"] or "").split(" ")[0])

    prev_aes = {x.get("sfdc_user_id"): x for x in prev.get("aes", [])}
    cfg = {
        "_generated": f"setup/configure.py on {datetime.date.today().isoformat()} — re-run it to change anything",
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
    say("       open app/index.html                       # the cockpit")
    say("       python3 setup/install_schedule.py         # daily auto-refresh")


if __name__ == "__main__":
    main()
