#!/usr/bin/env python3
"""Builds data/demo.sqlite: a fictional lift-service company ("Acme Lifts") with the same model tables the
reports read (dim_lift, dim_jobsite, dim_staff, dim_fault, fact_callback, fact_pm + three contract tables).

Everything is synthetic: names, codes, sites and numbers come from a seeded random generator, not from any
real system. Shapes are realistic on purpose: a few "problem" units drive most repeat calls, mantraps are a
minority of calls, attend times are log-normal around a 40-minute target, and PM completion drifts upward.
Run: python generate_fake_data.py [--seed 7]"""
import argparse, datetime as dt, json, math, os, random, sqlite3, uuid
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "demo.sqlite")
TODAY = dt.datetime(2026, 10, 22, 8, 0)          # fixed so the demo is reproducible
START = dt.datetime(2021, 10, 1)                  # FY2022 start (FY = Oct-Sep)
PM_START = dt.datetime(2023, 10, 1)
CALL_CENTRE_FROM = dt.datetime(2024, 8, 1)        # call-centre ticket export starts here

ZONES = [("North", "Harbour (ZONE 1)", "Northshire"), ("North", "Uptown (ZONE 2)", "Northshire"),
         ("Central", "Riverside (ZONE 3)", "Midland"), ("Central", "Old Town (ZONE 4)", "Midland"),
         ("Central", "Business Park (ZONE 5)", "Midland"), ("South", "Coastal (ZONE 6)", "Southport"),
         ("South", "Airport (ZONE 7)", "Southport"), ("East", "Hillside (ZONE 8)", "Eastvale")]
SECTORS = [("Private", .55), ("Government", .15), ("Not Applicable", .30)]
SITE_A = ["Maple", "Harbour", "Cedar", "Summit", "Orchid", "Granite", "Willow", "Beacon", "Lakeside", "Crescent",
          "Silverleaf", "Northgate", "Kingsway", "Pinecrest", "Bayview", "Redwood", "Meridian", "Ashford", "Falcon", "Juniper"]
SITE_B = [("Residences", 3), ("Tower", 4), ("Plaza", 3), ("Mall", 8), ("Medical Centre", 5), ("Office Park", 4),
          ("Court", 2), ("Station", 6), ("Hotel", 4), ("Apartments", 2), ("Campus", 5), ("Heights", 3)]
FIRST = ["Avery", "Jordan", "Riley", "Casey", "Morgan", "Quinn", "Rowan", "Sage", "Taylor", "Emerson", "Harper", "Reese",
         "Skyler", "Dakota", "Finley", "Hayden", "Jules", "Kendall", "Linden", "Marlow", "Parker", "Remy", "Sasha", "Tatum"]
LAST = ["Stone", "Pike", "Vale", "Hart", "Brook", "Frost", "Lane", "Moss", "Reed", "Wren", "Cole", "Fenn", "Grove",
        "Hale", "Knox", "Lowe", "Marsh", "North", "Penn", "Quill", "Rowe", "Shaw", "Thorne", "Voss"]
TYPES = [("Passenger Lift", "L-100", .74), ("Escalator", "E-200", .12), ("Fire Lift", "L-110", .05),
         ("Goods Lift", "L-120", .04), ("Home Lift", "L-130", .03), ("Travellator", "E-210", .02)]
CONTROLLERS = ["CX-200", "CX-300", "Nova 5", "Nova 7", "VF-10", "VF-20", "Atlas M", None, None]

# fault catalogue: (code, category, short description, actual breakdown?, weight for lifts, weight for escalators)
FAULTS = [
    ("D01", "DOOR", "Car door operator", 1, 9, 0), ("D02", "DOOR", "Landing door lock", 1, 8, 0),
    ("D03", "DOOR", "Door safety edge / sensor", 1, 7, 0), ("D04", "DOOR", "Door track obstruction", 1, 4, 0),
    ("C01", "CONTROLLER", "Main board fault", 1, 6, 0), ("C02", "CONTROLLER", "Inverter / drive trip", 1, 6, 0),
    ("C03", "CONTROLLER", "Safety circuit open", 1, 5, 0), ("C04", "CONTROLLER", "Levelling sensor", 1, 4, 0),
    ("C05", "CONTROLLER", "Car / landing button", 1, 3, 0), ("C06", "CONTROLLER", "Power supply / breaker", 1, 3, 0),
    ("M01", "MOTOR / CAR", "Brake adjustment", 1, 3, 0), ("M02", "MOTOR / CAR", "Rope / sheave wear", 1, 2, 0),
    ("M03", "MOTOR / CAR", "Car vibration / noise", 1, 2, 0),
    ("U01", "MISUSE", "Vandalism", 0, 4, 1), ("U02", "MISUSE", "Overload / forced door", 0, 8, 0),
    ("U03", "MISUSE", "Water ingress", 0, 3, 1), ("U04", "MISUSE", "Power cut by building", 0, 7, 1),
    ("R01", "ROA", "Reset on arrival, no fault found", 0, 6, 3),
    ("F01", "FIN", "Lighting / fan", 0, 3, 1), ("F02", "FIN", "Panel / finishes", 0, 2, 1),
    ("E01", "ESC", "Step / chain", 1, 0, 6), ("E02", "ESC", "Handrail drive", 1, 0, 6), ("E03", "ESC", "Comb plate safety", 1, 0, 5),
    ("E04", "ESC", "Escalator controller", 1, 0, 4), ("E13", "ESC", "Emergency stop pressed", 0, 0, 5),
    ("E14", "ESC", "Escalator cleaning request", 0, 0, 2),
]
REMARK = {"DOOR": ["Door not closing at L{f}", "Landing door stuck at L{f}", "Door reopening repeatedly", "Door noisy, slow to close"],
          "CONTROLLER": ["Lift not responding to calls", "Lift stopped between floors", "Display error, lift out of service",
                         "Lift tripped, reset needed"],
          "MOTOR / CAR": ["Car shaking during travel", "Unusual noise from machine", "Lift overshooting floor L{f}"],
          "MISUSE": ["Door forced open by user", "Lift stopped, power cut by building", "Water in pit after rain",
                     "Buttons damaged at L{f}"],
          "ROA": ["Lift working on arrival", "Reported stuck, normal on arrival"],
          "FIN": ["Car light not working", "Car fan not working", "Panel loose in car"],
          "ESC": ["Escalator stopped", "Handrail not moving", "Escalator emergency stop activated", "Step noisy"]}


def fy_of(d):
    return f"FY{d.year + 1 if d.month >= 10 else d.year}"


def fy_month(d):
    return (d.month - 10) % 12 + 1


def ms(d):
    return None if d is None else f"/Date({int((d - dt.datetime(1970, 1, 1)).total_seconds() * 1000)})/"


def ts(d):
    return None if d is None else d.strftime("%Y-%m-%d %H:%M:%S")


def wpick(rnd, items, weights):
    return rnd.choices(items, weights=weights, k=1)[0]


def build(seed):
    R = random.Random(seed)
    U = lambda: str(uuid.UUID(int=R.getrandbits(128), version=4))

    # ---- staff: 6 supervisors (one per zone pair) + 36 engineers ----
    sups = [f"{R.choice(FIRST)} {R.choice(LAST)}" for _ in range(6)]
    staff, used = [], set(sups)
    for i in range(36):
        while (n := f"{R.choice(FIRST)} {R.choice(LAST)}") in used:
            pass
        used.add(n)
        staff.append({"staff_key": f"EMP{1001 + i}", "name": n, "cost_center": f"CC{(i % 8) + 1:02d}",
                      "cost_center_desc": f"Service {ZONES[i % 8][1].split(' (')[0]}", "manager": sups[(i % 8) // 2 % 6],
                      "status": "Active" if R.random() > .12 else "Inactive", "resource_type": "Employee",
                      "employment_start": ts(dt.datetime(2012 + R.randrange(12), R.randrange(1, 13), 1)), "employment_end": None})
    eng_by_zone = {z: [s for j, s in enumerate(staff) if j % 8 == k] for k, z in enumerate(ZONES)}

    # ---- sites + units ----
    sites, lifts, used_names = [], [], set()
    for i in range(420):
        z = ZONES[min(int(R.paretovariate(1.6)) - 1, 7) if R.random() < .4 else R.randrange(8)]
        a, (b, size) = R.choice(SITE_A), R.choice(SITE_B)
        name = f"{a} {b}"
        k = 2
        while name in used_names:
            name, k = f"{a} {b} {k}", k + 1
        used_names.add(name)
        code = f"S{1001 + i}"
        n_units = max(1, min(40, int(R.expovariate(1 / size)) + 1))
        sites.append({"jobsite_code": code, "jobsite_name": name, "bu_zone": f"{z[0]} - {z[1]}", "state": z[2],
                      "city": z[1].split(" (")[0], "sector": wpick(R, *zip(*SECTORS)),
                      "subsector": "Commercial" if b in ("Mall", "Plaza", "Office Park", "Hotel") else "Residential",
                      "manager": sups[ZONES.index(z) // 2 % 6], "classification": R.choice(["Gold", "Silver", "Bronze", "Not Applicable"]),
                      "billing_account_code": None, "billing_account_name": None, "status": "Active",
                      "account_id": U(), "callcentre_zone": None, "lifts": n_units, "_z": z})
        for _ in range(n_units):
            t = wpick(R, TYPES, [w for *_, w in TYPES])
            if b in ("Mall", "Station") and R.random() < .3:
                t = TYPES[1]
            age = round(min(30, R.gammavariate(2.2, 4)) + .1, 1)
            lifts.append({"lift_key": f"U{20001 + len(lifts)}", "type": t, "site": sites[-1], "age": age,
                          # proneness: most units are quiet, a long tail are "problem" units
                          "rate": R.lognormvariate(math.log(.075) + age / 40, 1.0)})

    dim_jobsite = pd.DataFrame([{k: v for k, v in s.items() if not k.startswith("_")} for s in sites])
    contract_of = {}
    dim_lift = []
    cfgs, links, agrs = [], [], []
    for l in lifts:
        s, t = l["site"], l["type"]
        start = TODAY - dt.timedelta(days=int(l["age"] * 365))
        ckey = f"CT{s['jobsite_code'][1:]}"
        contract_of[l["lift_key"]] = ckey
        cid, aid = U(), U()
        # contract window: most units under contract all along, some join late or leave early
        cs = max(START, start) if R.random() < .85 else START + dt.timedelta(days=R.randrange(200, 1500))
        ce = None if R.random() < .9 else cs + dt.timedelta(days=R.randrange(365, 1400))
        l["contract"] = (cs, ce)
        cfgs.append({"ID": cid, "SerialNumberCode": l["lift_key"], "ItemCode": t[1], "ItemDescription": t[0], "Active": True})
        links.append({"ID": U(), "Configuration": cid, "ContractAgreement": aid, "Historical": False})
        agrs.append({"ID": aid, "Startdate": ms(cs), "Enddate": ms(ce), "InvoiceFrequency": "M"})
        dim_lift.append({"lift_key": l["lift_key"], "description": f"{t[0]} {l['lift_key']}", "equipment_code": t[1],
                         "equipment_type": t[0], "active": 1, "start_date": ts(start), "config_id": cid, "serial_guid": U(),
                         "header_id": U(), "jobsite_code": s["jobsite_code"], "machine_serial_no": f"MSN{R.randrange(10**6, 10**7)}",
                         "mainboard": None, "controller_model": R.choice(CONTROLLERS) if t[1].startswith("L") else None,
                         "work_permit_no": None, "ebuilder_ar_code": None, "maintenance_code": None, "service_by_contractor": 0,
                         "planned_service_day": None, "age_years": l["age"], "contract_key": ckey,
                         "contract_monthly_rate": round(R.uniform(180, 900), 2), "pm_type": "PM monthly", "maintenance_type": "0",
                         "jobsite_name": s["jobsite_name"], "bu_zone": s["bu_zone"], "state": s["state"]})
    dim_lift = pd.DataFrame(dim_lift)
    dim_fault = pd.DataFrame([{"fault_code": c, "category": cat, "primary_no": None, "sub": c[1:], "short_description": d,
                               "description": d, "rectification": None, "exact_description": f"{c} {d}", "in_exact": 1,
                               "actual_breakdown": a} for c, cat, d, a, *_ in FAULTS])

    # ---- callbacks ----
    cbs, cid = [], 500000
    days = (TODAY - START).days
    for l in lifts:
        t, s = l["type"], l["site"]
        esc = t[0] in ("Escalator", "Travellator")
        fw = [(f[5] if esc else f[4]) ** 1.8 for f in FAULTS]
        n = sum(1 for _ in range(int(days / 30)) if R.random() < l["rate"])
        for _ in range(n):
            d = START + dt.timedelta(days=R.random() * days)
            yr = (d - START).days / 365
            if R.random() > .78 + .06 * yr:              # gentle growth year on year
                continue
            if R.random() < .12 * math.cos((d.month - 1) / 12 * 2 * math.pi) ** 2 and d.month in (11, 12, 1):
                continue                                  # monsoon months a bit wetter, a bit busier elsewhere
            hour = wpick(R, list(range(24)), [1, 1, 1, 1, 1, 2, 4, 7, 10, 10, 9, 9, 8, 8, 9, 9, 8, 7, 6, 5, 4, 3, 2, 1])
            call = d.replace(hour=hour, minute=R.randrange(60), second=R.randrange(60))
            if call > TODAY:
                continue
            created = call + dt.timedelta(minutes=R.lognormvariate(3.5, 1.4))
            f = wpick(R, FAULTS, fw)
            man = int(not esc and t[0] != "Goods Lift" and R.random() < (.24 if f[1] in ("CONTROLLER", "DOOR") else .07))
            age_d = (TODAY - created).days
            if age_d < 120 and R.random() < max(.05, .95 - age_d / 30):
                status = wpick(R, ["Open", "Approved", "Rejected"], [5, 4, 3])
            else:
                status = "Rejected" if R.random() < .025 else "Processed"
            eng = R.choice(eng_by_zone[s["_z"]])
            cid += R.randrange(1, 9)
            row = {"callback_id": cid, "created": ts(created), "status": status, "lift_key": l["lift_key"],
                   "lift_text": l["lift_key"], "jobsite_code": s["jobsite_code"], "jobsite_name": s["jobsite_name"],
                   "staff_key": eng["staff_key"], "engineer": eng["name"], "creator": R.choice(["Helpdesk 1", "Helpdesk 2", "Helpdesk 3"]),
                   "item1": t[0], "mantrap": man, "contract_key": contract_of[l["lift_key"]],
                   "description": ("Passenger trapped. " if man else "") + R.choice(REMARK[f[1]]).format(f=R.randrange(1, 25)),
                   "_f": f, "_call": call}
            # call-centre ticket and attend times
            matched = call >= CALL_CENTRE_FROM and R.random() < .93
            row["match_class"] = ("Matched" if matched else "No Ticket Number") if call >= CALL_CENTRE_FROM \
                else "Outside CallDesk Export Range"
            if matched:
                ack = call + dt.timedelta(minutes=R.uniform(1, 12))
                zone_slow = 1 + .35 * (ZONES.index(s["_z"]) in (5, 7))
                resp = R.lognormvariate(math.log(27 if man else 36), .55) * zone_slow
                if R.random() < .004:
                    resp += 60 * 24 * R.uniform(1.5, 6)   # wrong-ticket match: shows on "check match" list
                site_t = ack + dt.timedelta(minutes=resp)
                rep = R.lognormvariate(math.log(45 if f[3] else 20), .8)
                row.update({"callcentre_ticket_date": ts(call), "callcentre_ack": ts(ack), "callcentre_at_site": ts(site_t),
                            "callcentre_completed": ts(site_t + dt.timedelta(minutes=rep)), "response_minutes": round(resp, 1),
                            "repair_minutes": round(rep, 1), "ack_to_complete_minutes": round(resp + rep, 1)})
            cbs.append(row)

    cb = pd.DataFrame(cbs).sort_values("created").reset_index(drop=True)
    for col in ("callcentre_ticket_date", "callcentre_ack", "callcentre_at_site", "callcentre_completed",
                "response_minutes", "repair_minutes", "ack_to_complete_minutes"):
        if col not in cb:
            cb[col] = None
    cr = pd.to_datetime(cb.created)
    cb["date_key"], cb["month_key"] = cr.dt.strftime("%Y-%m-%d"), cr.dt.strftime("%Y-%m")
    cb["fy"], cb["fy_month_no"] = [fy_of(d) for d in cr], [fy_month(d) for d in cr]
    cb["is_completed"] = None
    fc, fdesc, fsrc, fpred, fprob, ftop, apred, aprob, ffinal = [], [], [], [], [], [], [], [], []
    for f, st in zip(cb._f, cb.status):
        if st == "Processed":
            fc.append(f[0]); fdesc.append(f"{f[0]} {f[2]}"); fsrc.append("ERP")
            fpred.append(None); fprob.append(None); ftop.append(None); apred.append(None); aprob.append(None); ffinal.append(f[0])
        else:
            p = R.betavariate(5, 2)
            fc.append(None); fdesc.append(None)
            fpred.append(f[0]); fprob.append(round(p, 3)); ftop.append(f"{f[0]}:{p:.2f}")
            apred.append(f[3]); aprob.append(round(max(.5, p + .1 if p < .9 else p), 3))
            if p >= .6:
                fsrc.append("Predicted"); ffinal.append(f[0])
            elif R.random() < .8:
                fsrc.append("Predicted (actual flag only)"); ffinal.append(None)
            else:
                fsrc.append("Unknown"); ffinal.append(None)
    cb["fault_code"], cb["fault_desc_exact"], cb["fault_source"] = fc, fdesc, fsrc
    cb["fault_code_pred"], cb["fault_pred_prob"], cb["fault_pred_top3"] = fpred, fprob, ftop
    cb["actual_pred"], cb["actual_pred_prob"], cb["fault_code_final"] = apred, aprob, ffinal
    cat = dict(zip(dim_fault.fault_code, dim_fault.category)); act = dict(zip(dim_fault.fault_code, dim_fault.actual_breakdown))
    cb["fault_category"] = cb.fault_code.map(cat)
    cb["actual_breakdown"] = cb.fault_code.map(act)
    cb["fault_category_final"] = cb.fault_code_final.map(cat)
    cb["actual_breakdown_final"] = [act.get(c) if c else (a if s == "Predicted (actual flag only)" else None)
                                    for c, a, s in zip(cb.fault_code_final, cb.actual_pred, cb.fault_source)]
    cb["breakdown_type"] = cb.actual_breakdown_final.map({1: "Actual", 0: "Non-actual"}).fillna("Unknown")
    cb["status_group"] = cb.status.map({"Processed": "Processed", "Rejected": "Rejected (rework)"}).fillna("Open / Approved")
    old = cr < CALL_CENTRE_FROM
    cb["job_code"] = [R.randrange(100000, 999999) if o or R.random() < .9 else None for o in old]
    cb["ticket_no"] = [None if o or j is not None else f"TK{R.randrange(10**6, 10**7)}" for o, j in zip(old, cb.job_code)]
    cb["match_source"] = ["Old Job Code" if j is not None else ("New Ticket Number" if t else None)
                          for j, t in zip(cb.job_code, cb.ticket_no)]
    cb["start_planned"] = cb.created
    cb["start_realised"] = cb.callcentre_at_site.fillna(cb.created)
    cb["end_planned"] = cb["end_realised"] = None
    cb["target_minutes"] = 40.0
    cb["within_target"] = [None if pd.isna(r) else int(r <= 40) for r in cb.response_minutes]
    cb["call_received"] = [ts(c) for c in cb._call]
    cb["report_date_basis"] = "created"
    cb["n_mantrap"] = cb.mantrap
    cb["n_actual"] = (cb.breakdown_type == "Actual").astype(int)
    cb["n_non_actual"] = (cb.breakdown_type == "Non-actual").astype(int)
    cb["n_has_fault_code"] = cb.fault_code.notna().astype(int)
    cb["n_has_ticket"] = (cb.job_code.notna() | cb.ticket_no.notna()).astype(int)
    cb["n_has_lift"] = 1
    cb["n_matched"] = (cb.match_class == "Matched").astype(int)
    cb["n_processed"] = (cb.status == "Processed").astype(int)
    gap = cr.groupby(cb.lift_key).diff().dt.total_seconds() / 86400
    cb["days_since_prev_on_lift"] = gap.round(2)
    cb["n_repeat_7d"], cb["n_repeat_30d"] = (gap <= 7).astype(int), (gap <= 30).astype(int)
    cb = cb.drop(columns=["_f", "_call"])

    # ---- PM: one planned visit per unit per month from PM_START to 2 months ahead ----
    pms, m = [], PM_START
    while m <= TODAY + dt.timedelta(days=62):
        mk = m.strftime("%Y-%m")
        for l in lifts:
            cs, ce = l["contract"]
            if cs > m or (ce and ce < m):
                continue
            planned = m + dt.timedelta(days=R.randrange(28), hours=R.randrange(8, 17))
            due = planned <= TODAY
            yr = (m - PM_START).days / 365
            done = due and R.random() < (.915 + .02 * yr - (.04 if ZONES.index(l["site"]["_z"]) == 7 else 0))
            eng = R.choice(eng_by_zone[l["site"]["_z"]])
            pms.append({"lift_key": l["lift_key"], "month_key": mk, "jobs": 1, "done_jobs": int(done),
                        "planned_date": ts(planned), "done_date": ts(planned + dt.timedelta(days=R.randrange(3))) if done else None,
                        "engineer_staff_key": eng["staff_key"], "planned": 1, "done": int(done),
                        "pm_status": "Done" if done else ("Not done" if due else "Upcoming"), "fy": fy_of(m),
                        "fy_month_no": fy_month(m), "jobsite_code": l["site"]["jobsite_code"], "date_key": m.strftime("%Y-%m-%d"),
                        "contract_key": contract_of[l["lift_key"]], "n_planned": 1, "n_done": int(done),
                        "n_due": int(due), "n_done_due": int(done)})
        m = (m + dt.timedelta(days=32)).replace(day=1)
    pm = pd.DataFrame(pms)

    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    c = sqlite3.connect(DB)
    for name, df in [("dim_lift", dim_lift), ("dim_jobsite", dim_jobsite), ("dim_staff", pd.DataFrame(staff)),
                     ("dim_fault", dim_fault), ("fact_callback", cb), ("fact_pm", pm)]:
        df.to_sql(name, c, index=False)
    for name, rows in [("raw_sms_config", cfgs), ("raw_sms_contract_link", links), ("raw_sms_contract_agreement", agrs)]:
        c.execute(f"CREATE TABLE {name} (json TEXT)")
        c.executemany(f"INSERT INTO {name} VALUES (?)", [(json.dumps(r),) for r in rows])
    c.commit()
    c.close()
    os.makedirs(os.path.join(ROOT, "model"), exist_ok=True)
    with open(os.path.join(ROOT, "model", "STATUS.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"OK | {TODAY:%d %b %Y %H:%M} (synthetic demo data)")
    print(f"{DB}: {len(lifts):,} units, {len(sites)} sites, {len(cb):,} callbacks, {len(pm):,} PM lift-months")
    print(cb.groupby("fy").agg(callbacks=("callback_id", "size"), mantraps=("n_mantrap", "sum"),
                               actual=("n_actual", "sum")).to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    build(ap.parse_args().seed)
