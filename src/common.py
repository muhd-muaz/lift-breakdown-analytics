"""Shared helpers for the showcase: paths, config, logging, ERP date parsing."""
import configparser, datetime, os, re

SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SRC)
DB = os.path.join(ROOT, "data", "demo.sqlite")
CFG = configparser.ConfigParser(inline_comment_prefixes=(";",))
CFG.read(os.path.join(SRC, "config.ini"), encoding="utf-8")


def cfg(section, key, default=None):
    v = CFG.get(section, key, fallback=default)
    return v.strip() if isinstance(v, str) else v


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def epoch(v):
    """ERP REST dates look like /Date(1700000000000)/ (ms since 1970)."""
    m = re.search(r"/Date\((-?\d+)", str(v or ""))
    if not m or int(m.group(1)) < -2000000000000:
        return None
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=int(m.group(1)))
