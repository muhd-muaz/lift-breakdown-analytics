#!/usr/bin/env python3
"""Month-end management PDF pack.  Usage: python month_end_pdf.py [YYYY-MM]   (default = previous month)

Reads the model tables in data\\demo.sqlite only (no ERP calls), builds an HTML page and prints it to
month-end\\Acme Lifts Breakdown Pack YYYY-MM.pdf with headless Edge. Standalone: does not touch the dashboard files.
Rules: FY = Oct-Sep by ERP Created date; actual/non-actual = confirmed fault-code rule (predicted from remarks
when no code yet); attend = CallDesk Ack -> At-site; units = same unit base as the dashboard."""
import datetime as dt, html, json, os, subprocess, sys, tempfile, time
import sqlite3
import pandas as pd
from common import DB, ROOT, cfg, epoch, log

OUT_DIR = os.path.join(ROOT, "month-end")
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
INK, MUTED, LINE, PRI, SEC, NEU, ACC = "#1F2933", "#6B7680", "#E3E7EB", "#2F5D8C", "#9DB4CC", "#C9D1D9", "#C0392B"


def unit_base(c, fy):
    """Units in the FY: under a contract agreement overlapping the FY, or with a callback / PM job in it.
    (Same rule as build_html.unit_base; copied so this script stays standalone.)"""
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


# ---------- formatting ----------
def n0(v):
    return "–" if v is None or pd.isna(v) else f"{v:,.0f}"


def pct(a, b):
    return None if not b else 100.0 * a / b


def p1(v):
    return "–" if v is None or pd.isna(v) else f"{v:.1f}%"


def m1(v):
    return "–" if v is None or pd.isna(v) else f"{v:,.1f}"


def esc(s):
    return html.escape("" if s is None or (isinstance(s, float) and pd.isna(s)) else str(s))


def table(head, rows, breach=None, cls=""):
    """rows = list of lists of display strings; first column left-aligned, others right. breach(r, j) -> red text."""
    h = "".join(f"<th>{esc(x)}</th>" for x in head)
    body = ""
    for i, r in enumerate(rows):
        tds = "".join(f'<td class="{"b" if breach and breach(i, j) and x not in ("0", "0.0%", "–", "") else ""}">{esc(x)}</td>' for j, x in enumerate(r))
        body += f"<tr>{tds}</tr>"
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'


def nice_max(v):
    if v <= 0:
        return 1
    for m in (1, 2, 2.5, 5, 10):
        step = m * 10 ** (len(str(int(v))) - 1)
        if step * 4 >= v:
            return step * 4 if step * 4 >= v else step * 5
    return v


def bars(labels, series, w=720, h=210, fmt=n0, ymax=None, hi=None, line=None):
    """Stacked bar chart as inline SVG. series = [(name, values, colour)]; hi = index of report month (bold label);
    line = (value, label) dashed accent reference line."""
    pl, pr, pt, pb = 40, 8, 18, 34
    cw, ch = w - pl - pr, h - pt - pb
    tot = [sum((s[1][i] or 0) for s in series) for i in range(len(labels))]
    top = ymax or nice_max(max(tot + [line[0] if line else 0]))
    y = lambda v: pt + ch - ch * v / top
    bw = cw / len(labels)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" font-size="11">']
    for k in range(5):
        v = top * k / 4
        out.append(f'<line x1="{pl}" x2="{w - pr}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{LINE}"/>'
                   f'<text x="{pl - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="{MUTED}">{fmt(v)}</text>')
    for i, lab in enumerate(labels):
        x0, base = pl + i * bw + bw * 0.18, 0
        for _, vals, col in series:
            v = vals[i] or 0
            if v:
                out.append(f'<rect x="{x0:.1f}" y="{y(base + v):.1f}" width="{bw * 0.64:.1f}" '
                           f'height="{y(base) - y(base + v):.1f}" fill="{col}"/>')
            base += v
        if any(s[1][i] is not None for s in series):
            out.append(f'<text x="{x0 + bw * 0.32:.1f}" y="{y(base) - 4:.1f}" text-anchor="middle" fill="{INK}">{fmt(base)}</text>')
        wt = "600" if i == hi else "400"
        out.append(f'<text x="{x0 + bw * 0.32:.1f}" y="{h - pb + 16}" text-anchor="middle" font-weight="{wt}" '
                   f'fill="{INK if i == hi else MUTED}">{esc(lab)}</text>')
    if line:
        out.append(f'<line x1="{pl}" x2="{w - pr}" y1="{y(line[0]):.1f}" y2="{y(line[0]):.1f}" stroke="{ACC}" '
                   f'stroke-dasharray="4 3"/><text x="{w - pr}" y="{y(line[0]) - 4:.1f}" text-anchor="end" '
                   f'fill="{ACC}">{esc(line[1])}</text>')
    lx = pl
    for name, _, col in series:
        out.append(f'<rect x="{lx}" y="{h - 12}" width="10" height="10" fill="{col}"/>'
                   f'<text x="{lx + 14}" y="{h - 3}" fill="{MUTED}">{esc(name)}</text>')
        lx += 22 + 6.5 * len(name)
    out.append("</svg>")
    return "".join(out)


# ---------- data ----------
def build(month=None):
    if not month:
        first = dt.date.today().replace(day=1)
        month = f"{first - dt.timedelta(days=1):%Y-%m}"
    y, m = map(int, month.split("-"))
    fy_no = y + 1 if m >= 10 else y
    fy = f"FY{fy_no}"
    fy_months = [f"{fy_no - 1 if mm >= 10 else fy_no}-{mm:02d}" for mm in list(range(10, 13)) + list(range(1, 10))]
    prev = fy_months[fy_months.index(month) - 1] if fy_months.index(month) else f"{y - 1}-09"
    mlab = lambda k: dt.date(int(k[:4]), int(k[5:]), 1).strftime("%b")
    month_name = dt.date(y, m, 1).strftime("%B %Y")

    c = sqlite3.connect(DB)
    cb = pd.read_sql("SELECT callback_id, created, month_key, fy, status, status_group, lift_key, jobsite_code, "
                     "jobsite_name, n_actual, n_non_actual, n_mantrap, response_minutes, n_matched, "
                     "fault_source, breakdown_type FROM fact_callback", c)
    pm = pd.read_sql("SELECT lift_key, month_key, fy, jobsite_code, n_due, n_done_due FROM fact_pm", c)
    site = pd.read_sql("SELECT jobsite_code, bu_zone FROM dim_jobsite", c).set_index("jobsite_code").bu_zone
    lift = pd.read_sql("SELECT lift_key, jobsite_code, jobsite_name, bu_zone FROM dim_lift", c).set_index("lift_key")
    status_line = ""
    sp = os.path.join(ROOT, "model", "STATUS.txt")
    if os.path.exists(sp):
        status_line = open(sp, encoding="utf-8").read().strip().split("|")[1].strip()
    tm = float(cfg("report", "mantrap_minutes", "40") or 40)
    tb = float(cfg("report", "breakdown_minutes", "40") or 40)

    cb["zone"] = cb.jobsite_code.map(site).fillna("(no zone)")
    pm["zone"] = pm.jobsite_code.map(site).fillna("(no zone)")
    # ops lead 15-Oct-2026: mantrap response over 1 day = likely wrong CallDesk match -> left out of over-target,
    # medians and zone tables; listed separately as "check match" (same rule as the dashboard).
    cb["chk"] = ((cb.n_mantrap == 1) & (cb.response_minutes > 1440)).astype(int)
    cb["resp_raw"] = cb.response_minutes
    cb.loc[cb.chk == 1, "response_minutes"] = float("nan")
    cb["over"] = (cb.response_minutes > cb.n_mantrap.map({1: tm, 0: tb})).astype(int)
    cb["n_resp"] = cb.response_minutes.notna().astype(int)
    fycb = cb[cb.fy == fy]
    fypm = pm[pm.fy == fy]
    sel = {"month": fycb[fycb.month_key == month], "prev": cb[cb.month_key == prev],
           "fytd": fycb[fycb.month_key <= month]}
    selpm = {"month": pm[pm.month_key == month], "prev": pm[pm.month_key == prev],
             "fytd": fypm[fypm.month_key <= month]}
    if not len(sel["month"]):
        raise SystemExit(f"No callbacks for {month} in the model; run the daily pipeline first.")

    # ---- units: actual breakdowns per unit, FY to date ----
    base = sorted(k for k in unit_base(c, fy) if k in lift.index)
    act = sel["fytd"].groupby("lift_key").n_actual.sum()
    u = pd.DataFrame({"lift": base})
    u["act"] = u.lift.map(act).fillna(0).astype(int)
    u["zone"] = u.lift.map(lift.jobsite_code).map(site).fillna("(no zone)")
    n_units, n_over2 = len(u), int((u.act > 2).sum())

    # ---- KPI table ----
    def kpis(k):
        d, p = sel[k], selpm[k]
        man = d[d.n_mantrap == 1]
        return {
            "Callbacks": n0(len(d)),
            "Actual breakdowns": n0(d.n_actual.sum()),
            "Non-actual": n0(d.n_non_actual.sum()),
            "Type unknown": n0(len(d) - d.n_actual.sum() - d.n_non_actual.sum()),
            "Actual share": p1(pct(d.n_actual.sum(), len(d))),
            "Mantraps": n0(len(man)),
            f"Mantraps attended over {tm:.0f} min": n0(man.over.sum()),
            f"All callbacks attended over {tb:.0f} min": n0(d.over.sum()),
            "Mantraps to check match (response over 1 day)": n0(d.chk.sum()),
            "Median attend time (min)": m1(d.response_minutes.median()),
            "Matched to CallDesk": p1(pct(d.n_matched.sum(), len(d))),
            "PM done (of due)": p1(pct(p.n_done_due.sum(), p.n_due.sum())),
        }
    K = {k: kpis(k) for k in sel}
    krows = [[name, K["month"][name], K["prev"][name], K["fytd"][name]] for name in K["month"]]
    krows.append([f"Units with more than 2 actual breakdowns", "", "", f"{n_over2:,} of {n_units:,} ({100 * n_over2 / n_units:.1f}%)"])
    breach_rows = {i for i, r in enumerate(krows) if "over" in r[0] or "more than 2" in r[0]}

    mo = sel["month"]
    cards = [("Callbacks", n0(len(mo)), f"{n0(mo.n_actual.sum())} actual"),
             ("Mantraps", n0(mo.n_mantrap.sum()), f"{n0(mo[mo.n_mantrap == 1].over.sum())} attended over {tm:.0f} min"),
             ("PM done", K["month"]["PM done (of due)"], f"FY to date {K['fytd']['PM done (of due)']}"),
             ("Units over 2 actual", n0(n_over2), f"of {n_units:,} units, FY to date")]

    # ---- monthly trend series (FY months; future months blank) ----
    g = fycb.groupby("month_key")
    lab = [mlab(k) for k in fy_months]
    hi = fy_months.index(month)
    upto = lambda s: [None if k > month else int(s.get(k, 0)) for k in fy_months]
    ch_cb = bars(lab, [("Actual", upto(g.n_actual.sum()), PRI), ("Non-actual", upto(g.n_non_actual.sum()), SEC),
                       ("Unknown", upto(g.size() - g.n_actual.sum() - g.n_non_actual.sum()), NEU)], hi=hi)
    mg = fycb[fycb.n_mantrap == 1].groupby("month_key")
    ch_man = bars(lab, [(f"Attended within {tm:.0f} min or no CallDesk time", upto(mg.size() - mg.over.sum()), SEC),
                        (f"Attended over {tm:.0f} min", upto(mg.over.sum()), ACC)], h=190, hi=hi)
    pg = fypm.groupby("month_key")[["n_due", "n_done_due"]].sum()
    pmv = [None if k > month or not pg.n_due.get(k) else round(100 * pg.n_done_due[k] / pg.n_due[k], 1) for k in fy_months]
    ch_pm = bars(lab, [("PM done % of due", pmv, PRI)], h=170, ymax=100, fmt=lambda v: f"{v:.0f}" if v >= 99.5 or v % 1 == 0 else f"{v:.1f}", hi=hi)

    # ---- by zone tables ----
    zones = sorted(set(u.zone) | set(sel["fytd"].zone))
    zrows = []
    for z in zones:
        uz = u[u.zone == z]
        if not len(uz):
            continue
        o = int((uz.act > 2).sum())
        zrows.append([z, n0(len(uz)), n0(len(uz) - o), n0(o), p1(pct(o, len(uz)))])
    zrows.append(["Total", n0(n_units), n0(n_units - n_over2), n0(n_over2), p1(pct(n_over2, n_units))])
    buckets = [("0", (u.act == 0).sum()), ("1", (u.act == 1).sum()), ("2", (u.act == 2).sum()), ("3 or more", (u.act > 2).sum())]
    brow = table(["Actual breakdowns per unit (FY to date)"] + [b for b, _ in buckets],
                 [["Units"] + [n0(v) for _, v in buckets], ["Share"] + [p1(pct(v, n_units)) for _, v in buckets]],
                 breach=lambda i, j: j == 4)

    # top lifts
    fyd = sel["fytd"]
    t = fyd.groupby("lift_key").agg(act=("n_actual", "sum"), cbs=("callback_id", "size"), man=("n_mantrap", "sum"))
    t = t.sort_values(["act", "cbs"], ascending=False).head(10)
    trows = [[k, lift.jobsite_name.get(k, ""), site.get(lift.jobsite_code.get(k), "") or "",
              n0(r.act), n0(r.cbs), n0(r.man)] for k, r in t.iterrows()]

    def attend_zone(d):
        rows = []
        for z, s in d.groupby("zone"):
            mm = s[s.n_mantrap == 1]
            rows.append([z, n0(len(s)), n0(s.n_actual.sum()), n0(len(mm)), n0(mm.over.sum()), n0(s.over.sum()),
                         m1(s.response_minutes.median())])
        rows.sort(key=lambda r: -int(r[1].replace(",", "")))
        rows.append(["Total", n0(len(d)), n0(d.n_actual.sum()), n0(d.n_mantrap.sum()),
                     n0(d[d.n_mantrap == 1].over.sum()), n0(d.over.sum()), m1(d.response_minutes.median())])
        return rows
    ahead = ["BU / zone", "Callbacks", "Actual", "Mantraps", f"Mantraps >{tm:.0f} min", f"All >{tb:.0f} min", "Median attend (min)"]
    az_m, az_y = attend_zone(sel["month"]), attend_zone(sel["fytd"])
    abreach = lambda i, j: j in (4, 5)
    ck = sel["fytd"][sel["fytd"].chk == 1].sort_values("created")
    ckrows = [[str(r.created)[:10], n0(r.callback_id), r.lift_key or "", r.jobsite_name or "", r.zone, n0(r.resp_raw)]
              for r in ck.itertuples()]

    def pm_zone(p):
        g = p.groupby("zone")[["n_due", "n_done_due"]].sum()
        g = g[g.n_due > 0].sort_values("n_due", ascending=False)
        rows = [[z, n0(r.n_due), n0(r.n_done_due), n0(r.n_due - r.n_done_due), p1(pct(r.n_done_due, r.n_due))] for z, r in g.iterrows()]
        rows.append(["Total", n0(g.n_due.sum()), n0(g.n_done_due.sum()), n0(g.n_due.sum() - g.n_done_due.sum()),
                     p1(pct(g.n_done_due.sum(), g.n_due.sum()))])
        return rows
    pz_m, pz_y = pm_zone(selpm["month"]), {r[0]: r for r in pm_zone(selpm["fytd"])}
    pzrows = [r + pz_y.pop(r[0], ["", "", "", "", ""])[1:] for r in pz_m] if pz_m else []
    pzrows[-1:-1] = [[z, "", "", "", ""] + v[1:] for z, v in pz_y.items() if z != "Total"]

    # ---- backlog (callbacks not yet processed, all years, as at last data pull) ----
    bl = cb[cb.status_group != "Processed"].copy()
    asat = pd.to_datetime(status_line[:16]) if status_line else pd.Timestamp.now()
    bl["age"] = (asat - pd.to_datetime(bl.created, errors="coerce")).dt.days
    ages = [("0-7 days", 0, 7), ("8-30", 8, 30), ("31-90", 31, 90), ("Over 90", 91, 10 ** 6)]
    stats = ["Open", "Approved", "Rejected"]
    brows = []
    for st in stats:
        s = bl[bl.status == st]
        brows.append([st] + [n0(((s.age >= a) & (s.age <= b)).sum()) for _, a, b in ages] + [n0(len(s))])
    brows.append(["Total"] + [n0(((bl.age >= a) & (bl.age <= b)).sum()) for _, a, b in ages] + [n0(len(bl))])
    bz = bl.groupby("zone").agg(n=("callback_id", "size"), o=("status", lambda s: (s == "Open").sum()),
                                a=("status", lambda s: (s == "Approved").sum()), r=("status", lambda s: (s == "Rejected").sum()),
                                old=("age", lambda s: (s > 90).sum())).sort_values("n", ascending=False)
    bzrows = [[z, n0(r.o), n0(r.a), n0(r.r), n0(r.old), n0(r.n)] for z, r in bz.iterrows()]
    by_fy = bl.groupby("fy").size()
    fy_note = ", ".join(f"{k} {v:,}" for k, v in by_fy.items())

    # ---- page ----
    pred_n = int(((sel["fytd"].breakdown_type == "Actual") & (sel["fytd"].fault_source != "ERP")).sum())
    basis = (f"Basis: FY = Oct-Sep, months and FY by ERP Created date. Actual vs non-actual by fault code; non-actual = "
             f"MISUSE, ROA, FIN, E13, E14. Callbacks not yet Processed have no fault code, so their "
             f"type is predicted from the remarks ({pred_n:,} of the FY-to-date actuals are predicted). Attend time = "
             f"CallDesk Ack to At-site, only for callbacks matched to CallDesk; target {tm:.0f} min mantrap, "
             f"{tb:.0f} min non-mantrap (provisional). PM done = PM jobs done of those due.")
    card_html = "".join(f'<div class="card"><div class="cl">{esc(a)}</div><div class="cv">{esc(b)}</div>'
                        f'<div class="cs">{esc(s)}</div></div>' for a, b, s in cards)
    body = f"""
<header><h1>Breakdown and callback report, {month_name}</h1>
<div class="sub">Acme Lifts lift service · {fy} to date covers {mlab(fy_months[0])} {fy_months[0][:4]} to {mlab(month)} {y} · data pulled {esc(status_line)}</div></header>
<div class="cards">{card_html}</div>
<h2>Headline KPIs</h2>
{table(["Measure", mlab(month) + " " + str(y), mlab(prev) + " " + prev[:4], fy + " to date"], krows,
       breach=lambda i, j: i in breach_rows and j > 0, cls="kpi")}
<h2>Callbacks per month, {fy}</h2>
<div class="chart">{ch_cb}</div>
<p class="note">{esc(basis)}</p>

<section class="page">
<h2>Actual breakdowns per unit, {fy} to date</h2>
<p class="lead">{n_over2:,} of {n_units:,} units ({100 * n_over2 / n_units:.1f}%) had more than 2 actual breakdowns so far this FY.</p>
{brow}
<h3>By BU / zone</h3>
{table(["BU / zone", "Units", "At most 2", "More than 2", "More than 2 %"], zrows, breach=lambda i, j: j == 3, cls="small tot")}
<h3>Units with the most actual breakdowns, {fy} to date</h3>
{table(["Unit", "Jobsite", "BU / zone", "Actual", "All callbacks", "Mantraps"], trows, cls="small lifts")}
<p class="note">Units = lifts under a contract agreement in {fy}, or with a callback or PM job in {fy}.</p>
</section>

<section class="page">
<h2>Mantraps and attend time</h2>
<div class="chart">{ch_man}</div>
<h3>{month_name} by BU / zone</h3>
{table(ahead, az_m, breach=abreach, cls="small tot")}
<h3>{fy} to date by BU / zone</h3>
{table(ahead, az_y, breach=abreach, cls="small tot")}
<h3>Check match: mantraps with response over 1 day, {fy} to date ({len(ckrows)})</h3>
{table(["Created", "Callback", "Unit", "Jobsite", "BU / zone", "Response (min)"], ckrows, cls="small lifts")}
<p class="note">Attend counts use only callbacks matched to CallDesk with an At-site time; unmatched callbacks are not counted as over target.
Mantraps with a response over 1 day are likely matched to the wrong CallDesk ticket, so they are left out of the over-target counts,
medians and zone tables above until the match is fixed (same rule as the dashboard).</p>
</section>

<section class="page">
<h2>Preventive maintenance</h2>
<div class="chart">{ch_pm}</div>
<h3>By BU / zone: {month_name} and {fy} to date</h3>
{table(["BU / zone", mlab(month) + " due", "Done", "Missed", "Done %", "FY due", "Done", "Missed", "Done %"], pzrows,
       breach=lambda i, j: j in (3, 7), cls="small tot")}
</section>
<section class="page">
<h2>Backlog: callbacks not yet processed</h2>
<p class="lead">{len(bl):,} callbacks are Open, Approved or Rejected (sent back for rework) as at {asat:%d %b %Y %H:%M}, all years ({fy_note}).</p>
{table(["Status"] + [a for a, _, _ in ages] + ["Total"], brows, breach=lambda i, j: j == 4, cls="tot")}
<h3>By BU / zone</h3>
{table(["BU / zone", "Open", "Approved", "Rejected", "Over 90 days", "Total"], bzrows, breach=lambda i, j: j == 4, cls="small")}
<p class="note">Backlog is a snapshot at the last data pull, not at month end; age = days since ERP Created.</p>
<footer>Generated {dt.datetime.now():%d %b %Y %H:%M} by src\\month_end_pdf.py from data\\demo.sqlite (read-only).</footer>
</section>"""
    page = TEMPLATE.replace("__BODY__", body).replace("__TITLE__", f"Acme Lifts Breakdown Pack {month}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"Acme Lifts Breakdown Pack {month}.pdf")
    tmp = tempfile.mkdtemp(prefix="demo_pdf_")
    src = os.path.join(tmp, "pack.html")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(page)
    pdf = os.path.join(tmp, "pack.pdf")
    edge = next((e for e in EDGE if os.path.exists(e)), None)
    if not edge:
        raise SystemExit("Microsoft Edge not found; HTML left at " + src)
    subprocess.run([edge, "--headless", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={tmp}\\edge",
                    f"--print-to-pdf={pdf}", "file:///" + src.replace("\\", "/")], check=True, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):   # Edge can return before the PDF is on disk
        if os.path.exists(pdf) and os.path.getsize(pdf) > 0:
            break
        time.sleep(0.5)
    time.sleep(1)
    try:
        os.replace(pdf, out)
    except PermissionError:   # ops lead has the old copy open
        out = out[:-4] + f" (updated {dt.datetime.now():%d-%b %H%M}).pdf"
        os.replace(pdf, out)
    log(f"Month-end pack written: {out}")
    return out


TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title><style>
@page { size: A4; margin: 12mm 12mm 14mm; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", system-ui, sans-serif; color: #1F2933; font-size: 11.5px; margin: 0;
       font-variant-numeric: tabular-nums; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 15px; font-weight: 600; margin: 18px 0 8px; }
h3 { font-size: 12.5px; font-weight: 600; margin: 14px 0 6px; }
.sub, .note, footer { color: #6B7680; font-size: 10.5px; }
.note { margin: 8px 0 0; line-height: 1.45; }
.lead { margin: 0 0 10px; font-size: 12.5px; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 16px 0 4px; }
.card { border: 1px solid #E3E7EB; border-radius: 8px; padding: 10px 12px; }
.cl { color: #6B7680; font-size: 11px; } .cv { font-size: 26px; font-weight: 600; margin: 2px 0; } .cs { color: #6B7680; font-size: 10.5px; }
table { width: 100%; border-collapse: collapse; margin: 0 0 4px; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { text-align: right; font-weight: 600; color: #6B7680; font-size: 10.5px; border-bottom: 1px solid #C9D1D9; padding: 4px 6px; }
td { text-align: right; border-bottom: 1px solid #E3E7EB; padding: 4px 6px; }
th:first-child, td:first-child { text-align: left; }
table.small td, table.small th { padding: 3px 6px; font-size: 10.5px; }
table.lifts td:nth-child(2), table.lifts th:nth-child(2), table.lifts td:nth-child(3), table.lifts th:nth-child(3) { text-align: left; }
table.lifts td { white-space: nowrap; }
table.kpi td:first-child { width: 40%; }
table.tot tbody tr:last-child td { font-weight: 600; }
td.b { color: #C0392B; font-weight: 600; }
.chart { border: 1px solid #E3E7EB; border-radius: 8px; padding: 8px; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.page { page-break-before: always; }
footer { margin-top: 16px; }
</style></head><body>__BODY__</body></html>"""

if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else None))
