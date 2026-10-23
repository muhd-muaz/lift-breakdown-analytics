#!/usr/bin/env python3
"""One command for the whole demo: fake data -> HTML dashboard -> month-end pack -> monthly report (Windows + Edge)."""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MONTH = sys.argv[1] if len(sys.argv) > 1 else "2026-09"
for cmd, cwd in [(["generate_fake_data.py"], HERE), (["build_html.py"], "src"),
                 (["month_end_pdf.py", MONTH], "src"), (["monthly_report.py", MONTH], "src")]:
    subprocess.run([sys.executable, *cmd], cwd=os.path.join(HERE, cwd), check=True)
