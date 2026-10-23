#!/usr/bin/env python3
"""Writes 'Breakdown Dashboard.html' (one self-contained file, works offline) from the model tables.

Data is pre-aggregated and dictionary-encoded here; the page filters and charts it with plain JS (no libraries).
Follows ..\\DESIGN.md. Run after transform.py (run_daily does it when [dashboard] html = true)."""
import datetime as dt, json, os, sqlite3
import pandas as pd
from common import DB, ROOT, cfg, epoch, log

OUT = os.path.join(ROOT, "Breakdown Dashboard.html")


class Enc:
    """String columns -> integer codes + one shared dictionary per field (keeps the file small)."""
    def __init__(self):
        self.d = {}

    def col(self, name, s):
        s = s.fillna("").astype(str)
        cats = sorted(s.unique())
        self.d[name] = cats
        idx = {v: i for i, v in enumerate(cats)}
        return [idx[v] for v in s]


def num(s, nd=1):
    return [None if pd.isna(v) else round(float(v), nd) for v in s]


def unit_base(c, fy):
    """Units in the FY: under a contract agreement overlapping the FY, or with a callback / PM job in it."""
    y = int(fy[2:])
    fs, fe = pd.Timestamp(y - 1, 10, 1), pd.Timestamp(y, 9, 30, 23, 59, 59)
    L = lambda t: pd.DataFrame([json.loads(j) for (j,) in c.execute(f"SELECT json FROM {t}")])
    cf, lk, ag = L("raw_sms_config"), L("raw_sms_contract_link"), L("raw_sms_contract_agreement")
    ag["s"], ag["e"] = ag.Startdate.map(epoch), ag.Enddate.map(epoch)
    ok = {i for i, s, e in zip(ag.ID, ag.s, ag.e) if (s is None or s <= fe) and (e is None or e >= fs)}
    code = dict(zip(cf.ID, cf.SerialNumberCode))
    base = {code.get(x) for x in lk[lk.ContractAgreement.isin(ok)].Configuration}
    base |= set(pd.read_sql(f"SELECT DISTINCT lift_key FROM fact_callback WHERE fy='{fy}'", c).lift_key.dropna())
    base |= set(pd.read_sql(f"SELECT DISTINCT lift_key FROM fact_pm WHERE fy='{fy}'", c).lift_key.dropna())
    return base


def build():
    c = sqlite3.connect(DB)
    cb = pd.read_sql("SELECT * FROM fact_callback", c)
    pm = pd.read_sql("SELECT * FROM fact_pm", c)
    lift = pd.read_sql("SELECT lift_key, equipment_type, controller_model, age_years, jobsite_name, bu_zone, state, "
                       "jobsite_code FROM dim_lift", c).set_index("lift_key")
    site = pd.read_sql("SELECT jobsite_code, jobsite_name, bu_zone, state, sector FROM dim_jobsite", c).set_index("jobsite_code")
    staff = pd.read_sql("SELECT staff_key, name, manager FROM dim_staff", c).set_index("staff_key")
    fault = pd.read_sql("SELECT fault_code, category, short_description FROM dim_fault", c).set_index("fault_code")
    E = Enc()

    # ---- callbacks (one row each) ----
    js = cb.jobsite_code
    cbd = {
        "id": cb.callback_id.astype(int).tolist(),
        "date": cb.date_key.fillna("").tolist(),
        "fy": E.col("fy", cb.fy), "mk": E.col("mk", cb.month_key),
        "lift": E.col("lift", cb.lift_key), "site": E.col("site", cb.jobsite_name),
        "zone": E.col("zone", js.map(site.bu_zone)), "state": E.col("state", js.map(site.state)),
        "sector": E.col("sector", js.map(site.sector)),
        "ltype": E.col("ltype", cb.lift_key.map(lift.equipment_type)),
        "ctrl": E.col("ctrl", cb.lift_key.map(lift.controller_model)),
        "eng": E.col("eng", cb.engineer), "sup": E.col("sup", cb.staff_key.map(staff.manager)),
        "fault": E.col("fault", cb.fault_code_final),
        "cat": E.col("cat", cb.fault_code_final.map(fault.category)),
        "bt": E.col("bt", cb.breakdown_type), "src": E.col("src", cb.fault_source),
        "sg": E.col("sg", cb.status_group), "st": E.col("st", cb.status),
        # backlog age: days from ERP Created to build time
        "age": num((pd.Timestamp.now() - pd.to_datetime(cb.created, errors="coerce")).dt.total_seconds() / 86400), "mc": E.col("mc", cb.match_class),
        "ticket": cb.ticket_no.fillna(cb.job_code.map(lambda v: "" if pd.isna(v) else f"T{int(float(v))}")).fillna("").tolist(),
        "man": cb.n_mantrap.astype(int).tolist(),
        "resp": num(cb.response_minutes), "rep": num(cb.repair_minutes),
        "win": [None if pd.isna(v) else int(v) for v in pd.to_numeric(cb.within_target, errors="coerce")],
        "r7": cb.n_repeat_7d.astype(int).tolist(), "r30": cb.n_repeat_30d.astype(int).tolist(),
        "hf": cb.n_has_fault_code.astype(int).tolist(), "ht": cb.n_has_ticket.astype(int).tolist(),
        "hl": cb.n_has_lift.astype(int).tolist(), "m": cb.n_matched.astype(int).tolist(),
        "desc": cb.description.fillna("").str.slice(0, 90).tolist(),
    }
    fdesc = {k: v for k, v in fault.short_description.items()}

    # ---- PM: aggregate lift-months to the filter grain ----
    pm["zone"], pm["state"] = pm.jobsite_code.map(site.bu_zone), pm.jobsite_code.map(site.state)
    pm["sector"], pm["ltype"] = pm.jobsite_code.map(site.sector), pm.lift_key.map(lift.equipment_type)
    pm["eng"] = pm.engineer_staff_key.map(staff.name)
    g = pm.groupby(["fy", "month_key", "zone", "state", "sector", "ltype", "eng"], dropna=False)[["n_due", "n_done_due"]].sum().reset_index()
    pmd = {"fy": E.col("fy_pm", g.fy), "mk": E.col("mk_pm", g.month_key), "zone": E.col("zone_pm", g.zone),
           "state": E.col("state_pm", g.state), "sector": E.col("sector_pm", g.sector), "ltype": E.col("ltype_pm", g.ltype),
           "eng": E.col("eng_pm", g.eng), "due": g.n_due.astype(int).tolist(), "done": g.n_done_due.astype(int).tolist()}

    # ---- units per FY (incl. 0 breakdowns) ----
    units = {}
    for fy in sorted(cb.fy.dropna().unique()):
        base = sorted(k for k in unit_base(c, fy) if k in lift.index)
        sub = cb[cb.fy == fy]
        act = sub.groupby("lift_key").n_actual.sum()
        tot = sub.groupby("lift_key").size()
        pred = sub[(sub.breakdown_type == "Actual") & (sub.fault_source != "ERP")].groupby("lift_key").size()
        li = lift.loc[base]
        js_ = li.jobsite_code
        units[fy] = {"lift": base, "site": li.jobsite_name.fillna("").tolist(),
                     "zone": js_.map(site.bu_zone).fillna("").tolist(), "state": js_.map(site.state).fillna("").tolist(),
                     "sector": js_.map(site.sector).fillna("").tolist(), "ltype": li.equipment_type.fillna("").tolist(),
                     "act": [int(act.get(k, 0)) for k in base], "cb": [int(tot.get(k, 0)) for k in base],
                     "pred": [int(pred.get(k, 0)) for k in base], "age": num(li.age_years)}
    status = open(os.path.join(ROOT, "model", "STATUS.txt"), encoding="utf-8").read().strip() \
        if os.path.exists(os.path.join(ROOT, "model", "STATUS.txt")) else ""
    meta = {"built": f"{dt.datetime.now():%d %b %Y %H:%M}", "status": status,
            "target_mantrap": float(cfg("report", "mantrap_minutes", "40") or 40),
            "target_breakdown": float(cfg("report", "breakdown_minutes", "40") or 40),
            "nonactual": "MISUSE, ROA, FIN, E13, E14"}
    data = {"cb": cbd, "pm": pmd, "units": units, "dict": E.d, "fdesc": fdesc, "meta": meta}
    html = TEMPLATE.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"), default=str))
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, OUT)
    log(f"HTML dashboard written: {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB, {len(cb):,} callbacks)")
    return OUT


_DASH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
TEMPLATE = open(os.path.join(_DASH, "dashboard_template.html"), encoding="utf-8").read().replace(
    "/*__ECHARTS__*/", open(os.path.join(_DASH, "vendor", "echarts.min.js"), encoding="utf-8").read())  # inlined: page stays offline

if __name__ == "__main__":
    build()
