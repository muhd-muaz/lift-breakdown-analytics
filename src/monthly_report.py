#!/usr/bin/env python3
"""Monthly breakdown report: every number with What / Why / What now.  Usage: python monthly_report.py [YYYY-MM]

Reads the model tables in data\\demo.sqlite only (no ERP calls) and prints
month-end\\Acme Lifts Monthly Breakdown Report YYYY-MM.pdf with headless Edge. Reuses the formatting helpers and page
style of month_end_pdf.py (that script is untouched). Same rules: FY = Oct-Sep by ERP Created date; actual vs
non-actual by the confirmed fault-code rule (predicted from remarks when not yet coded); attend = CallDesk Ack ->
At-site, target 40 min (mantrap firm, non-mantrap provisional); mantrap response over 1 day = check match, left out.
Why lines state only what the data shows; anything inferred is marked "(inferred)"."""
import datetime as dt, os, subprocess, sys, tempfile, time
import sqlite3
import pandas as pd
from common import DB, ROOT, cfg, log
from month_end_pdf import EDGE, TEMPLATE, esc, n0, p1, m1, pct, table, unit_base

OUT_DIR = os.path.join(ROOT, "month-end")
TOP = 3


# ---------- wording ----------
def chg(cur, old, label, unit="", pts=False):
    """'up 12 (4.3%) on August' / 'down 1.2 pts on Sep 2025' / 'same as August'."""
    if old is None or pd.isna(old) or cur is None or pd.isna(cur):
        return f"no {label} figure"
    d = cur - old
    if abs(d) < (0.05 if pts else 0.5):
        return f"same as {label}"
    word = "up" if d > 0 else "down"
    if pts:
        return f"{word} {abs(d):.1f} pts on {label}"
    rel = f" ({abs(d) / old:.0%})" if old else ""
    return f"{word} {abs(d):,.0f}{unit}{rel} on {label}"


def short(s, n=34):
    s = "" if s is None or (isinstance(s, float) and pd.isna(s)) else str(s).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def zname(z):
    return str(z).replace(" - ", " ").replace("(ZONE ", "Z").replace(")", "") if z else "(no zone)"


def movers(cur, old, key, n=TOP, up=True):
    """Biggest increases (or decreases) in row count by `key` between two frames -> [(key, cur, old)]."""
    a, b = cur.groupby(key).size(), old.groupby(key).size()
    d = (a.reindex(a.index.union(b.index), fill_value=0) - b.reindex(a.index.union(b.index), fill_value=0))
    d = d[d > 0] if up else d[d < 0]
    d = d.sort_values(ascending=not up).head(n)
    return [(k, int(a.get(k, 0)), int(b.get(k, 0))) for k in d.index]


def mv_text(ms, fmt=lambda k: k):
    return ", ".join(f"{fmt(k)} {o}→{c}" for k, c, o in ms)


def top_counts(d, key, n=TOP):
    return d.groupby(key).size().sort_values(ascending=False).head(n)


# ---------- data ----------
def load(month):
    c = sqlite3.connect(DB)
    cb = pd.read_sql("SELECT callback_id, created, month_key, fy, status, status_group, lift_key, jobsite_code, "
                     "jobsite_name, engineer, n_actual, n_non_actual, n_mantrap, response_minutes, n_matched, "
                     "n_has_ticket, fault_code_final f, fault_category_final cat, fault_source, breakdown_type, "
                     "callcentre_ticket_date FROM fact_callback", c)
    pm = pd.read_sql("SELECT lift_key, month_key, fy, jobsite_code, planned_date, pm_status, engineer_staff_key, "
                     "n_due, n_done_due FROM fact_pm", c)
    site = pd.read_sql("SELECT jobsite_code, bu_zone FROM dim_jobsite", c).set_index("jobsite_code").bu_zone
    lift = pd.read_sql("SELECT lift_key, jobsite_code, jobsite_name, bu_zone FROM dim_lift", c).set_index("lift_key")
    fault = pd.read_sql("SELECT fault_code, short_description FROM dim_fault", c).set_index("fault_code").short_description
    staff = dict(c.execute("SELECT staff_key, name FROM dim_staff"))
    sp = os.path.join(ROOT, "model", "STATUS.txt")
    status_line = open(sp, encoding="utf-8").read().strip().split("|")[1].strip() if os.path.exists(sp) else ""

    tm = float(cfg("report", "mantrap_minutes", "40") or 40)
    tb = float(cfg("report", "breakdown_minutes", "40") or 40)
    cb["created"] = pd.to_datetime(cb.created, format="mixed")
    cb["zone"] = cb.jobsite_code.map(site).fillna("(no zone)")
    cb["site"] = cb.jobsite_name.fillna(cb.jobsite_code).fillna("(no site)")
    cb["chk"] = ((cb.n_mantrap == 1) & (cb.response_minutes > 1440)).astype(int)
    cb.loc[cb.chk == 1, "response_minutes"] = float("nan")
    cb["tgt"] = cb.n_mantrap.map({1: tm, 0: tb})
    cb["over"] = (cb.response_minutes > cb.tgt).astype(int)
    # call time = CallDesk ticket time when matched (ERP Created can be days later)
    call = pd.to_datetime(cb.callcentre_ticket_date, format="mixed", errors="coerce")
    cb["office"] = ((call.dt.weekday < 5) & (call.dt.hour >= 8) & (call.dt.hour < 18)).astype(object)
    cb.loc[call.isna(), "office"] = None
    # actual repeat: another actual breakdown on the same lift within 7 days of the previous one
    a = cb[(cb.n_actual == 1) & cb.lift_key.notna()].sort_values(["lift_key", "created"])
    gap = a.groupby("lift_key").created.diff().dt.total_seconds() / 86400
    cb["rep7"] = 0
    cb.loc[a.index, "rep7"] = (gap <= 7).astype(int)
    cb.loc[a.index, "prev_eng"] = a.groupby("lift_key").engineer.shift()
    cb.loc[a.index, "prev_f"] = a.groupby("lift_key").f.shift()
    pm["zone"] = pm.jobsite_code.map(site).fillna("(no zone)")
    pm["site"] = pm.lift_key.map(lift.jobsite_name).fillna(pm.jobsite_code)
    pm["eng"] = pm.engineer_staff_key.map(staff).fillna("(no engineer)")
    pm["planned_date"] = pd.to_datetime(pm.planned_date, format="mixed", errors="coerce")
    return c, cb, pm, site, lift, fault, status_line, tm, tb


def build(month=None):
    if not month:
        first = dt.date.today().replace(day=1)
        month = f"{first - dt.timedelta(days=1):%Y-%m}"
    y, m = map(int, month.split("-"))
    fy_no = y + 1 if m >= 10 else y
    fy, fy_ly = f"FY{fy_no}", f"FY{fy_no - 1}"
    prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    ly = f"{y - 1}-{m:02d}"
    lab = lambda k: dt.date(int(k[:4]), int(k[5:]), 1).strftime("%B")
    labY = lambda k: dt.date(int(k[:4]), int(k[5:]), 1).strftime("%b %Y")
    P, L = lab(prev), labY(ly)
    month_name = dt.date(y, m, 1).strftime("%B %Y")

    c, cb, pm, site, lift, fault, status_line, tm, tb = load(month)
    M, Pv, LY = (cb[cb.month_key == k] for k in (month, prev, ly))
    if not len(M):
        raise SystemExit(f"No callbacks for {month} in the model; run the daily pipeline first.")
    pmM, pmP, pmL = (pm[pm.month_key == k] for k in (month, prev, ly))
    fname = lambda f: f"{f} {short(fault.get(f, ''), 30)}" if f and not pd.isna(f) else "not yet coded"
    blocks, actions = [], []   # actions: (priority, text)

    def block(key, title, value, sub, what, why, now, extra="", vals=None):
        blocks.append(dict(key=key, title=title, value=value, sub=sub, what=what, why=why, now=now, extra=extra, vals=vals))

    # ===== 1. Callbacks =====
    act = lambda d: int(d.n_actual.sum())
    non = lambda d: int(d.n_non_actual.sum())
    unk = lambda d: len(d) - act(d) - non(d)
    zup = movers(M, Pv, "zone")
    sites = M.groupby("site").agg(n=("callback_id", "size"), a=("n_actual", "sum"), u=("lift_key", "nunique"),
                                  z=("zone", "first")).sort_values("n", ascending=False)
    top5 = sites.head(5)
    mis = M[M.f.fillna("").str.match(r"^5")]
    mis_site = top_counts(mis, "site")
    why = [f"Split: {act(M):,} actual ({chg(act(M), act(Pv), P)}), {non(M):,} non-actual ({chg(non(M), non(Pv), P)}), "
           f"{unk(M):,} not yet typed."]
    if zup:
        why.append(f"Zones with the biggest rise on {P}: {mv_text(zup, zname)}.")
    zdn = movers(M, Pv, "zone", up=False)
    if zdn:
        why.append(f"Biggest falls: {mv_text(zdn, zname)}.")
    why.append(f"Top 5 sites gave {int(top5.n.sum())} callbacks ({top5.n.sum() / len(M):.0%} of the month): " +
               "; ".join(f"{short(s)} {int(r.n)} ({int(r.u)} units)" for s, r in top5.iterrows()) + ".")
    if len(mis):
        why.append(f"Misuse was {len(mis)} calls, led by " +
                   ", ".join(f"{short(s)} {n}" for s, n in mis_site.items()) + ".")
    now = [f"Joint inspection with the client at {short(s)} ({int(r.n)} callbacks on {int(r.u)} units, {int(r.a)} actual)."
           for s, r in top5.head(2).iterrows()]
    if len(mis_site) and mis_site.iloc[0] >= 3:
        now.append(f"Raise misuse with building management at {short(mis_site.index[0])} ({mis_site.iloc[0]} misuse calls).")
    actions.append((2, now[0]))
    block("cb", "Callbacks", n0(len(M)), f"{act(M):,} actual",
          f"{len(M):,} callbacks in {month_name}, {chg(len(M), len(Pv), P)} and {chg(len(M), len(LY), L)}.",
          why, now, vals=(len(M), len(Pv), len(LY)))

    # ===== 2. Actual breakdowns =====
    aM, aP, aL = (d[d.n_actual == 1] for d in (M, Pv, LY))
    cat_up = movers(aM, aP, "cat")
    codes = top_counts(aM.dropna(subset=["f"]), "f", 4)
    per_unit = aM.groupby("lift_key").size()
    u2 = per_unit[per_unit >= 2]
    asites = aM.groupby("site").agg(n=("callback_id", "size"), u=("lift_key", "nunique")).sort_values("n", ascending=False).head(TOP)
    pred = int((aM.fault_source != "ERP").sum())
    why = []
    if cat_up:
        why.append(f"Fault groups that rose most on {P}: {mv_text(cat_up, lambda k: k if isinstance(k, str) else 'not yet coded')}.")
    cat_dn = movers(aM, aP, "cat", up=False)
    if cat_dn:
        why.append(f"Groups that fell: {mv_text(cat_dn, lambda k: k if isinstance(k, str) else 'not yet coded')}.")
    why.append("Most common faults: " + "; ".join(f"{fname(f)} ×{n}" for f, n in codes.items()) + ".")
    why.append(f"{len(u2)} units broke down 2 or more times in the month and gave {int(u2.sum())} of the {len(aM)} "
               f"actual breakdowns ({u2.sum() / max(len(aM), 1):.0%}).")
    why.append("Sites with most actual breakdowns: " + "; ".join(f"{short(s)} {int(r.n)} on {int(r.u)} units" for s, r in asites.iterrows()) + ".")
    if pred:
        why.append(f"{pred} of the {len(aM)} are not yet processed, so their type comes from the remarks (predicted).")
    worst_u = per_unit.sort_values(ascending=False).head(5)
    now = [f"Technical review of the {min(5, len(u2))} units with most breakdowns this month: " +
           ", ".join(f"{k} ({short(lift.jobsite_name.get(k, ''), 30)}, ×{n})" for k, n in worst_u.items() if n >= 2) + "."]
    if len(codes):
        f0 = codes.index[0]
        multi = aM[aM.f == f0].groupby("lift_key").size()
        now.append(f"Check the fix method for {fname(f0)}: {codes.iloc[0]} cases on {len(multi)} units"
                   + (f", {int((multi >= 2).sum())} units had it more than once." if (multi >= 2).any() else "."))
    actions.append((1, now[0]))
    block("act", "Actual breakdowns", n0(len(aM)), f"{len(aM) / len(M):.0%} of callbacks",
          f"{len(aM):,} actual breakdowns, {chg(len(aM), len(aP), P)} and {chg(len(aM), len(aL), L)}. "
          f"Actual share {len(aM) / len(M):.0%} ({len(aP) / max(len(Pv), 1):.0%} in {P}).",
          why, now, vals=(len(aM), len(aP), len(aL)))

    # ===== 3. Mantraps =====
    mM, mP, mL = (d[d.n_mantrap == 1] for d in (M, Pv, LY))
    mz = movers(mM, mP, "zone")
    mcat = top_counts(mM.assign(cat=mM.cat.fillna("not yet coded")), "cat", 4)
    mu = mM.groupby("lift_key").size()
    mu2 = mu[mu >= 2]
    # mantraps in the last 90 days on this month's mantrap units
    since = pd.Timestamp(y, m, 1) - pd.Timedelta(days=90)
    m90 = cb[(cb.n_mantrap == 1) & (cb.created >= since) & (cb.month_key <= month)].groupby("lift_key").size()
    hot = m90[m90.index.isin(mM.lift_key) & (m90 >= 2)].sort_values(ascending=False)
    msites = top_counts(mM, "site")
    why = [f"Fault groups behind the mantraps: " + ", ".join(f"{k} {v}" for k, v in mcat.items()) + "."]
    if mz:
        why.append(f"Zones up most on {P}: {mv_text(mz, zname)}.")
    why.append("Sites with most mantraps: " + ", ".join(f"{short(s)} {n}" for s, n in msites.items()) + ".")
    why.append(f"{len(mu2)} units had 2 or more mantraps this month; {len(hot)} of this month's mantrap units had 2 or "
               f"more in the last 90 days.")
    now = []
    if len(hot):
        now.append("Priority fix and sign-off by a senior technician on repeat-mantrap units: " +
                   ", ".join(f"{k} ({short(lift.jobsite_name.get(k, ''), 30)}, {n} in 90 days)" for k, n in hot.head(5).items()) + ".")
        actions.append((1, now[-1]))
    mmis = mM[mM.cat == "MISUSE"]
    if len(mmis):
        now.append(f"{len(mmis)} mantraps were misuse: send the list to building management at " +
                   ", ".join(short(s, 26) for s in top_counts(mmis, "site", 2).index) + ".")
    mact = top_counts(mM[mM.n_actual == 1].dropna(subset=["cat"]), "cat", 1)
    if len(mact):
        k = mM[(mM.cat == mact.index[0])].lift_key.nunique()
        now.append(f"{mact.index[0]} was the main real fault behind mantraps ({mact.iloc[0]}): check it on those {k} units at next PM.")
    block("man", "Mantraps", n0(len(mM)), f"{len(mu)} units",
          f"{len(mM)} mantraps, {chg(len(mM), len(mP), P)} and {chg(len(mM), len(mL), L)}.",
          why, now, vals=(len(mM), len(mP), len(mL)))

    # ===== 4. Mantrap attend time =====
    def att(d):
        r = d[d.response_minutes.notna()]
        return len(r), int(r.over.sum()), r.response_minutes.median()
    nM, oM, medM = att(mM)
    nP, oP, medP = att(mP)
    nL, oL, medL = att(mL)
    late = mM[mM.over == 1]
    why = [f"{nM} of {len(mM)} mantraps have a CallDesk attend time; {len(mM) - nM} are unmatched or missing At-site "
           f"and are not counted" + (f" ({int(mM.chk.sum())} over 1 day left out as a likely wrong match)." if mM.chk.sum() else ".")]
    if len(late):
        oh = late.office.dropna()
        if len(oh):
            why.append(f"{int((~oh.astype(bool)).sum())} of the {len(late)} late ones were called in after hours or at weekends "
                       f"(outside Mon-Fri 08:00-18:00).")
        why.append("Late by zone: " + ", ".join(f"{zname(z)} {n}" for z, n in top_counts(late, "zone").items()) + ".")
        le = top_counts(late, "engineer")
        if le.iloc[0] >= 2:
            why.append("Engineers with 2 or more late mantraps: " + ", ".join(f"{e.title()} {n}" for e, n in le[le >= 2].items()) + ".")
        why.append("Slowest: " + "; ".join(f"{short(r.site, 26)} {r.response_minutes:.0f} min" for r in
                                         late.sort_values("response_minutes", ascending=False).head(3).itertuples()) + ".")
    now = []
    if len(late):
        lz = top_counts(late, "zone", 1)
        now.append(f"Go through each late mantrap with the {zname(lz.index[0])} supervisor ({lz.iloc[0]} late); "
                   "check where the nearest engineer was when the call came in.")
        if len(late.office.dropna()) and (~late.office.dropna().astype(bool)).mean() >= 0.5:
            now.append("Most late mantraps were after hours: check the standby roster covers those zones (inferred).")
        actions.append((1, f"Mantraps over {tm:.0f} min: {oM} this month. " + now[0]))
    if len(mM) - nM:
        now.append(f"Get CallDesk ticket numbers onto the {len(mM) - nM} unmatched mantraps so their attend time counts.")
    block("matt", f"Mantraps attended over {tm:.0f} min", n0(oM), f"of {nM} measured · median {m1(medM)} min",
          f"{oM} of {nM} measured mantraps ({p1(pct(oM, nM))}) were attended over {tm:.0f} min, "
          f"{chg(oM, oP, P)} and {chg(oM, oL, L)}. Median attend {m1(medM)} min ({m1(medP)} in {P}).",
          why, now, vals=(oM, oP, oL))

    # ===== 5. Non-mantrap attend time (provisional) =====
    nm = lambda d: d[d.n_mantrap == 0]
    qM, qoM, qmedM = att(nm(M))
    qP, qoP, qmedP = att(nm(Pv))
    qL, qoL, _ = att(nm(LY))
    lateq = nm(M)[nm(M).over == 1]
    why = ["Late by zone (share of that zone's measured calls): " + ", ".join(
        f"{zname(z)} {n} ({n / max(int(nm(M)[nm(M).zone == z].response_minutes.notna().sum()), 1):.0%})"
        for z, n in top_counts(lateq, "zone").items()) + "."]
    oh = lateq.office.dropna()
    if len(oh):
        why.append(f"{int((~oh.astype(bool)).sum())} of {len(lateq)} late calls came in after hours or at weekends.")
    now = [f"Target is provisional: confirm whether {tb:.0f} min is right for non-mantrap calls before chasing these."]
    zl = nm(M).dropna(subset=["response_minutes"]).groupby("zone").over.agg(["size", "mean"])
    zl = zl[zl["size"] >= 20].sort_values("mean", ascending=False)
    if len(zl) and zl["mean"].iloc[0] >= 0.5:
        now.append(f"{zname(zl.index[0])} was late on {zl['mean'].iloc[0]:.0%} of calls: check engineer coverage and "
                   "travel distance there before setting its target (inferred).")
    block("att", f"Non-mantrap calls attended over {tb:.0f} min", n0(qoM), f"of {qM} measured · provisional target",
          f"{qoM} of {qM} measured non-mantrap callbacks ({p1(pct(qoM, qM))}) took over {tb:.0f} min, "
          f"{chg(pct(qoM, qM), pct(qoP, qP), P, pts=True)} and {chg(pct(qoM, qM), pct(qoL, qL), L, pts=True)}. "
          f"Median {m1(qmedM)} min ({m1(qmedP)} in {P}).",
          why, now, vals=(qoM, qoP, qoL))

    # ===== 6. Repeats within 7 days =====
    rM, rP, rL = (d[d.rep7 == 1] for d in (aM, aP, aL))
    same = rM[rM.f.notna() & (rM.f == rM.prev_f)]
    eng_att = aM.groupby("engineer").size()
    eng_rep = rM.groupby("prev_eng").size()
    er = pd.DataFrame({"att": eng_att, "rep": eng_rep}).fillna(0)
    er = er[(er.att >= 5) & (er.rep >= 2)]
    er["rate"] = er.rep / er.att
    er = er.sort_values(["rate", "rep"], ascending=False).head(TOP)
    why = [f"{len(same)} of the {len(rM)} repeats had the same fault code as the visit before, so the first fix did not hold."
           if len(rM) else "No repeats this month."]
    if len(rM):
        why.append(f"They were on {rM.lift_key.nunique()} units; most at " +
                   ", ".join(f"{short(s)} {n}" for s, n in top_counts(rM, "site").items()) + ".")
        sc = top_counts(same.dropna(subset=["f"]), "f")
        if len(sc):
            why.append("Faults that came back most: " + ", ".join(f"{fname(f)} ×{n}" for f, n in sc.items()) + ".")
    if len(er):
        why.append("Engineers whose fixes most often came back within 7 days (5+ jobs this month): " +
                   ", ".join(f"{e.title()} {int(r.rep)} of {int(r.att)}" for e, r in er.iterrows()) + ".")
    now = ["Supervisor to check the repeat jobs with the same fault code: was the part replaced or only reset?"]
    if len(er):
        now.append(f"Ride-along or coaching for {er.index[0].title()} ({int(er.iloc[0].rep)} of {int(er.iloc[0].att)} fixes came back).")
        actions.append((2, f"{len(rM)} breakdowns were repeats within 7 days. " + now[0]))
    block("rep", "Repeat breakdowns within 7 days", n0(len(rM)), f"{len(rM) / max(len(aM), 1):.0%} of actual",
          f"{len(rM)} actual breakdowns came within 7 days of the last one on the same unit, "
          f"{chg(len(rM), len(rP), P)} and {chg(len(rM), len(rL), L)}.",
          why, now, vals=(len(rM), len(rP), len(rL)))

    # ===== 7. PM =====
    pr = lambda p: pct(p.n_done_due.sum(), p.n_due.sum())
    miss = pmM[(pmM.n_due > 0) & (pmM.n_done_due == 0)]
    mzn = miss.groupby("zone").size().sort_values(ascending=False)
    zrate = pmM.groupby("zone")[["n_due", "n_done_due"]].sum()
    noeng = int((miss.eng == "(no engineer)").sum())
    me = top_counts(miss[miss.eng != "(no engineer)"], "eng")
    # breakdowns this month on units whose PM due this month was missed
    a_miss = aM[aM.lift_key.isin(miss.lift_key)]
    why = [f"{len(miss)} PM visits due were not done. Most misses: " +
           ", ".join(f"{zname(z)} {n} ({100 - pct(zrate.n_done_due[z], zrate.n_due[z]):.0f}% of due)" for z, n in mzn.head(TOP).items()) + "."]
    why.append("Engineers with most missed PM: " + ", ".join(f"{short(e.title(), 28)} {n}" for e, n in me.items()) +
               (f"; {noeng} missed visits had no engineer assigned." if noeng else "."))
    why.append(f"{len(a_miss)} of this month's {len(aM)} actual breakdowns were on units whose PM was missed this month. "
               f"(FY analysis in analysis\\worst_lifts.py found missed PM is not the main cause of repeat breakdowns.)")
    now = [f"Reschedule the {len(miss)} missed visits in the first week of next month, starting with {zname(mzn.index[0])}."
           if len(mzn) else "No missed PM visits."]
    if noeng:
        now.append(f"Assign an engineer to every PM visit: {noeng} missed ones had none.")
    if len(me) and me.iloc[0] >= 10:
        now.append(f"Check workload for {me.index[0].title()} ({me.iloc[0]} missed): too many units or not logging the job?")
    actions.append((3, now[0]))
    block("pm", "PM done", p1(pr(pmM)), f"{len(miss)} missed of {int(pmM.n_due.sum()):,} due",
          f"PM done {p1(pr(pmM))} of due, {chg(pr(pmM), pr(pmP), P, pts=True)} and {chg(pr(pmM), pr(pmL), L, pts=True)}.",
          why, now, vals=(pr(pmM), pr(pmP), pr(pmL)))

    # ===== 8. Units with more than 2 actual breakdowns, FY to date =====
    def over2(fyk, upto):
        a = cb[(cb.fy == fyk) & (cb.month_key <= upto) & (cb.n_actual == 1) & cb.lift_key.isin(lift.index)].groupby("lift_key").size()
        return a[a > 2]
    o_now, o_ly = over2(fy, month), over2(fy_ly, ly)
    o_prev = over2(fy, prev) if m != 10 else o_now.iloc[:0]
    base = [k for k in unit_base(c, fy) if k in lift.index]
    new = o_now.index.difference(o_prev.index)
    wu = pd.DataFrame({"n": o_now})
    wu["site"] = wu.index.map(lift.jobsite_name)
    clus = wu.groupby("site").size().sort_values(ascending=False)
    clus = clus[clus >= 3]
    fy_a = cb[(cb.fy == fy) & (cb.month_key <= month) & (cb.n_actual == 1) & cb.lift_key.isin(o_now.index)]
    rep_share = fy_a.rep7.mean()
    why = [f"{len(new)} units crossed the line this month."]
    if len(clus):
        why.append(f"They cluster: {len(clus)} sites have 3 or more such units ({int(clus.sum())} units in all), led by " +
                   ", ".join(f"{short(s)} {n}" for s, n in clus.head(TOP).items()) + ".")
    why.append(f"{rep_share:.0%} of these units' breakdowns this FY came within 7 days of the previous one (fixes not holding); "
               "missed PM is not the main driver (analysis\\worst_lifts.py).")
    now = []
    if len(clus):
        now.append(f"Site-level plan (survey, modernisation quote or extra visits) for {short(clus.index[0])} "
                   f"({clus.iloc[0]} units over 2).")
        actions.append((2, now[-1]))
    if len(new):
        nn = o_now[new].sort_values(ascending=False).head(5)
        now.append("Add this month's new entries to the worst-lift list: " +
                   ", ".join(f"{k} ({short(lift.jobsite_name.get(k, ''), 30)})" for k in nn.index) + ".")
    block("u2", f"Units with more than 2 actual breakdowns, {fy} to date", n0(len(o_now)),
          f"of {len(base):,} units ({len(o_now) / len(base):.1%})",
          f"{len(o_now)} units, {chg(len(o_now), len(o_prev), 'end of ' + P)} and "
          f"{chg(len(o_now), len(o_ly), 'the same point of ' + fy_ly)}.",
          why, now, vals=(len(o_now), len(o_prev), len(o_ly)))

    # ===== 9. Backlog =====
    bl = cb[cb.status_group != "Processed"].copy()
    asat = pd.to_datetime(status_line[:16]) if status_line else pd.Timestamp.now()
    bl["age"] = (asat - bl.created).dt.days
    blM = bl[bl.month_key == month]
    old = bl[bl.age > 90]
    rej = bl[bl.status == "Rejected"]
    why = [f"{len(blM)} of them were created in {month_name} ({len(blM) / len(M):.0%} of the month not yet processed)."]
    why.append(f"{len(old):,} are over 90 days old; most in " + ", ".join(f"{zname(z)} {n}" for z, n in top_counts(old, "zone").items()) + ".")
    why.append(f"{len(rej):,} were rejected and sent back for rework; most in " +
               ", ".join(f"{zname(z)} {n}" for z, n in top_counts(rej, "zone").items()) + ".")
    now = [f"Each zone clears its callbacks over 90 days, starting with {zname(top_counts(old, 'zone', 1).index[0])}."
           if len(old) else "Nothing over 90 days.",
           f"Fix and resubmit the {len(rej):,} rejected callbacks: until processed they have no fault code, so actual vs "
           "non-actual for them is a prediction."]
    actions.append((3, now[0]))
    block("bl", "Backlog not yet processed", n0(len(bl)), f"as at {asat:%d %b %H:%M}",
          f"{len(bl):,} callbacks (all years) are Open, Approved or Rejected at the last data pull. "
          "Snapshot only, so no month-on-month comparison yet.",
          why, now)

    # ===== 10. CallDesk match =====
    mr = lambda d: pct(d.n_matched.sum(), len(d))
    um = M[M.n_matched == 0]
    zm = M.groupby("zone").agg(n=("callback_id", "size"), u=("n_matched", lambda s: (s == 0).sum()))
    zm = zm[zm.u > 0].sort_values("u", ascending=False).head(TOP)
    why = [f"{len(um)} callbacks were not matched; {int((um.n_has_ticket == 0).sum())} of them have no CallDesk ticket "
           f"number in ERP, {int(um.n_mantrap.sum())} are mantraps."]
    why.append("Most unmatched: " + ", ".join(f"{zname(z)} {int(r.u)} of {int(r.n)}" for z, r in zm.iterrows()) + ".")
    now = ["Key the CallDesk ticket number on every callback in ERP, so it can be matched."]
    if len(zm) and zm.u.iloc[0] / zm.n.iloc[0] >= 0.8:
        now.insert(0, f"Check whether {zname(zm.index[0])} logs calls in CallDesk at all ({int(zm.u.iloc[0])} of "
                      f"{int(zm.n.iloc[0])} unmatched; inferred).")
    block("ec", "Matched to CallDesk", p1(mr(M)), f"{len(um)} unmatched",
          f"{p1(mr(M))} of callbacks matched to a CallDesk ticket, {chg(mr(M), mr(Pv), P, pts=True)} and "
          f"{chg(mr(M), mr(LY), L, pts=True)}.",
          why, now, vals=(mr(M), mr(Pv), mr(LY)))

    # ---------- page ----------
    def fmtv(b, v):
        return "–" if v is None or pd.isna(v) else (p1(v) if b["value"].endswith("%") else n0(v))
    glance = [[b["title"], b["value"], fmtv(b, b["vals"][1]), fmtv(b, b["vals"][2])] if b["vals"] else
              [b["title"], b["value"], "–", "–"] for b in blocks]
    acts = "".join(f"<li>{esc(t)}</li>" for _, t in sorted(actions, key=lambda x: x[0])[:6])
    sec = ""
    for i, b in enumerate(blocks):
        li = lambda xs: "".join(f"<li>{esc(x)}</li>" for x in xs)
        sec += f"""<div class="blk"><div class="bh"><div><div class="bt">{i + 1}. {esc(b['title'])}</div>
<div class="bs">{esc(b['sub'])}</div></div><div class="bv">{esc(b['value'])}</div></div>
<div class="row"><div class="lab">What</div><div>{esc(b['what'])}</div></div>
<div class="row"><div class="lab">Why</div><ul>{li(b['why'])}</ul></div>
<div class="row"><div class="lab">What now</div><ul class="now">{li(b['now'])}</ul></div>{b['extra']}</div>"""
    basis = (f"Basis: FY = Oct-Sep; months by ERP Created date. Actual vs non-actual by fault code (non-actual = MISUSE, ROA, FIN, E13, E14); callbacks not yet processed are typed from their remarks (predicted). "
             f"Attend = CallDesk Ack to At-site, matched callbacks only; target {tm:.0f} min mantrap, {tb:.0f} min "
             f"non-mantrap (provisional); mantrap response over 1 day left out as a likely wrong match. Repeat = actual "
             f"breakdown within 7 days of the previous actual on the same unit; the engineer named is the one on the "
             f"earlier job. After hours = call time outside Mon-Fri 08:00-18:00 (CallDesk ticket time). Why lines show "
             f"what the data shows; they do not prove cause.")
    body = f"""
<header><h1>Monthly breakdown report, {month_name}</h1>
<div class="sub">Acme Lifts lift service · compared with {P} {prev[:4]} and {L} · data pulled {esc(status_line)}</div></header>
<h2>At a glance</h2>
{table(["Measure", labY(month), labY(prev), L], glance, cls="kpi")}
<h2>Do these first</h2>
<ol class="acts">{acts}</ol>
<p class="note">{esc(basis)}</p>
<section class="page">{sec}
<footer>Generated {dt.datetime.now():%d %b %Y %H:%M} by src\\monthly_report.py from data\\demo.sqlite (read-only).</footer>
</section>"""
    style = """
.acts { margin: 0 0 6px 18px; padding: 0; } .acts li { margin: 0 0 6px; line-height: 1.45; font-size: 12px; }
.blk { border: 1px solid #E3E7EB; border-radius: 8px; padding: 10px 12px 8px; margin: 0 0 10px; page-break-inside: avoid; }
.bh { display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 1px solid #E3E7EB;
      padding-bottom: 6px; margin-bottom: 6px; }
.bt { font-size: 13.5px; font-weight: 600; } .bs { color: #6B7680; font-size: 10.5px; margin-top: 1px; }
.bv { font-size: 24px; font-weight: 600; line-height: 1; }
.row { display: grid; grid-template-columns: 62px 1fr; gap: 8px; padding: 3px 0; line-height: 1.45; }
.lab { color: #6B7680; font-weight: 600; font-size: 10.5px; text-transform: uppercase; letter-spacing: .03em; padding-top: 1px; }
.row ul { margin: 0; padding-left: 14px; } .row li { margin: 0 0 2px; }
ul.now li { color: #1F2933; } ul.now li::marker { color: #2F5D8C; }
table.kpi td:first-child { width: 52%; }
</style>"""
    page = TEMPLATE.replace("</style>", style, 1).replace("__BODY__", body).replace("__TITLE__", f"Acme Lifts Monthly Breakdown Report {month}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"Acme Lifts Monthly Breakdown Report {month}.pdf")
    tmp = tempfile.mkdtemp(prefix="demo_mr_")
    src = os.path.join(tmp, "report.html")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(page)
    pdf = os.path.join(tmp, "report.pdf")
    edge = next((e for e in EDGE if os.path.exists(e)), None)
    if not edge:
        raise SystemExit("Microsoft Edge not found; HTML left at " + src)
    subprocess.run([edge, "--headless", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={tmp}\\edge",
                    f"--print-to-pdf={pdf}", "file:///" + src.replace("\\", "/")], check=True, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):   # Edge can return before the PDF is on disk
        if os.path.exists(pdf):
            break
        time.sleep(0.5)
    time.sleep(1)
    try:
        os.replace(pdf, out)
    except PermissionError:   # ops lead has the old copy open
        out = out[:-4] + f" (updated {dt.datetime.now():%d-%b %H%M}).pdf"
        os.replace(pdf, out)
    log(f"Monthly report written: {out}  (HTML: {src})")
    return out


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else None))
