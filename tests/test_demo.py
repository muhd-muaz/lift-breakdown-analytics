"""Sanity checks on the synthetic company and the dashboard built from it. Run: pytest (after build_demo.py or the CI steps)."""
import json, os, sqlite3

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "demo.sqlite")
HTML = os.path.join(ROOT, "Breakdown Dashboard.html")


@pytest.fixture(scope="module")
def cb():
    with sqlite3.connect(DB) as c:
        return pd.read_sql("SELECT * FROM fact_callback", c)


def test_five_full_financial_years(cb):
    assert {"FY2022", "FY2023", "FY2024", "FY2025", "FY2026"} <= set(cb.fy)


def test_shapes_are_realistic(cb):
    fy = cb[cb.fy == "FY2026"]
    assert 2000 < len(fy) < 6000
    assert 0.05 < fy.n_mantrap.mean() < 0.25            # mantraps are a minority of calls
    assert 0.5 < fy.n_actual.mean() < 0.85              # most calls are actual breakdowns
    assert fy.callback_id.is_unique


def test_flags_agree_with_types(cb):
    assert (cb.n_actual == (cb.breakdown_type == "Actual")).all()
    assert (cb.within_target.dropna() == (cb.response_minutes.dropna() <= 40)).all()
    assert cb.loc[cb.status != "Processed", "fault_code"].isna().all()   # codes only once processed


def test_pm_never_done_before_due():
    with sqlite3.connect(DB) as c:
        pm = pd.read_sql("SELECT n_due, n_done_due FROM fact_pm", c)
    assert (pm.n_done_due <= pm.n_due).all()
    assert 0.85 < pm.n_done_due.sum() / pm.n_due.sum() < 1


def test_dashboard_embeds_all_data(cb):
    html = open(HTML, encoding="utf-8").read()
    data, _ = json.JSONDecoder().raw_decode(html, html.index('{"cb":'))
    assert len(data["cb"]["id"]) == len(cb)
    assert set(data["units"]) >= {"FY2026"}
    assert "echarts" in html.lower()
