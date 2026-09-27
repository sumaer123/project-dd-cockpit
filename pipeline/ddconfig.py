#!/usr/bin/env python3
"""Project DD — territory configuration loader. Stdlib only, NO LLM.

Every person-, territory- and workspace-specific value lives in ONE file:

    config/territory.json      (git-ignored; written by setup/configure.py)

Nothing in the pipeline, the data builder, the cloud publisher or the UI
hard-codes a name, a Salesforce id, a quota or a workspace. They all read
this module, so the same repo serves any manager's team: run
`python3 setup/configure.py`, which discovers your role and the AEs under
you from Salesforce and writes the file, and the whole cockpit re-targets.

See config/territory.example.json for the schema.
"""
import datetime
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("DD_CONFIG") or os.path.join(ROOT, "config", "territory.json")


class ConfigError(RuntimeError):
    pass


def _load():
    if not os.path.exists(CONFIG_PATH):
        raise ConfigError(
            f"{CONFIG_PATH} not found. Run `python3 setup/configure.py` first — it "
            "reads your role and your team from Salesforce and writes this file.")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    aes = [a for a in cfg.get("aes", []) if a.get("include", True)]
    if not aes:
        raise ConfigError(f"{CONFIG_PATH} lists no AEs (aes[] empty or all include=false).")
    for a in aes:
        if not re.fullmatch(r"005[A-Za-z0-9]{12,15}", a.get("sfdc_user_id", "")):
            raise ConfigError(f"AE {a.get('name')!r} has an invalid sfdc_user_id "
                              f"{a.get('sfdc_user_id')!r} (expected a 15/18-char User id starting 005).")
    cfg["aes"] = aes
    return cfg


CFG = _load()

# ---- people ---------------------------------------------------------------
MANAGER = CFG["manager"]
MANAGER_NAME = MANAGER["name"]
MANAGER_ID = MANAGER["sfdc_user_id"]
SF_USERNAME = MANAGER["sfdc_username"]
REVIEWER_NAME = (CFG.get("reviewer") or {}).get("name") or "Leadership"
TERRITORY_LABEL = CFG.get("territory_label") or f"{MANAGER_NAME} — Territory"

AES = CFG["aes"]
AE_ORDER = [a["name"] for a in AES]
OWNER_IDS = [a["sfdc_user_id"] for a in AES]
AE_SLACK = {a["name"]: a.get("slack_user_id", "") for a in AES if a.get("slack_user_id")}

# ---- Salesforce -----------------------------------------------------------
SF_INSTANCE_URL = (CFG.get("salesforce") or {}).get("instance_url") or "https://databricks.my.salesforce.com/"

# ---- Databricks: the DATA workspace (consumption tables) -------------------
_dd = CFG.get("databricks_data") or {}
DBX_PROFILE = _dd.get("profile") or "logfood"
DBX_DATA_HOST = _dd.get("host") or ""
WAREHOUSE = _dd.get("warehouse_id") or ""

# ---- Databricks: the CLOUD workspace (hosts the read-only Databricks App) --
_dc = CFG.get("databricks_cloud") or {}
CLOUD_ENABLED = bool(_dc.get("enabled", False))
DBX_CLOUD_PROFILE = _dc.get("profile") or ""
DBX_CLOUD_HOST = _dc.get("host") or ""
CLOUD_APP_NAME = _dc.get("app_name") or ""
CLOUD_WS_SOURCE = _dc.get("workspace_source_dir") or "project-dd"
CLOUD_APP_URL = _dc.get("app_url") or ""

# ---- schedule ------------------------------------------------------------
_sch = CFG.get("schedule") or {}
SCHEDULE_HOUR = int(_sch.get("hour", 9))
SCHEDULE_MINUTE = int(_sch.get("minute", 0))


# ---- fiscal calendar (Databricks FY starts 1 Feb; FY'27 = Feb 2026–Jan 2027) --
def _auto_fy(today=None):
    t = today or datetime.date.today()
    return t.year + 1 if t.month >= 2 else t.year


FISCAL_YEAR = int(CFG.get("fiscal_year") or _auto_fy())   # e.g. 2027
PRIOR_FY = FISCAL_YEAR - 1
FY_START = f"{FISCAL_YEAR - 1}-02-01"                      # e.g. 2026-02-01


def qlabel(fy, q):
    return f"FY'{fy % 100:02d} Q{q}"


def _qbounds(fy, q):
    start_month = {1: 2, 2: 5, 3: 8, 4: 11}[q]
    y = fy - 1
    s = datetime.date(y, start_month, 1)
    em = start_month + 3
    ey = y + (1 if em > 12 else 0)
    em = em - 12 if em > 12 else em
    e = datetime.date(ey, em, 1) - datetime.timedelta(days=1)
    return s.isoformat(), e.isoformat()


# prior FY + current FY, in order — what build_data.py used to hard-code as FY'26..FY'27
QORDER = [qlabel(fy, q) for fy in (PRIOR_FY, FISCAL_YEAR) for q in (1, 2, 3, 4)]
QBOUNDS = {qlabel(fy, q): _qbounds(fy, q) for fy in (PRIOR_FY, FISCAL_YEAR) for q in (1, 2, 3, 4)}
ACTIVITY_QUARTERS = QORDER[4:]
FY_LABEL = f"FY{FISCAL_YEAR % 100:02d}"          # "FY27"
PRIOR_FY_LABEL = f"FY{PRIOR_FY % 100:02d}"       # "FY26"


def sf_in(ids):
    """SOQL IN-list from a list of ids."""
    return "(" + ",".join("'" + i + "'" for i in ids) + ")"


if __name__ == "__main__":
    print(f"config: {CONFIG_PATH}")
    print(f"manager: {MANAGER_NAME} <{SF_USERNAME}> ({MANAGER_ID}) · reviewer: {REVIEWER_NAME}")
    print(f"territory: {TERRITORY_LABEL}")
    print(f"AEs ({len(AES)}): " + ", ".join(AE_ORDER))
    print(f"data: profile={DBX_PROFILE} warehouse={WAREHOUSE or '<missing>'}")
    print(f"cloud: enabled={CLOUD_ENABLED} profile={DBX_CLOUD_PROFILE or '-'} app={CLOUD_APP_NAME or '-'}")
    print(f"fiscal: {FY_LABEL} (start {FY_START}) · quarters {ACTIVITY_QUARTERS}")
