import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

TZ = dt.timezone(dt.timedelta(hours=5))
PRICING = os.path.join(ROOT, "dashboard", "pricing.json")


def at(day, hms, tz=TZ):
    """aware datetime from 'YYYY-MM-DD' and 'HH:MM[:SS]'"""
    p = (hms + ":00").split(":")[:3]
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=int(p[0]), minute=int(p[1]), second=int(p[2]), tzinfo=tz)
    return d


def iso(d):
    return d.isoformat(timespec="milliseconds")


class Clock(object):
    def __init__(self, d):
        self.d = d

    def __call__(self):
        return self.d


def ev(kind, d, **kw):
    e = {"v": 1, "ts": iso(d), "event": kind, "session_id": kw.pop("session_id", "s1"),
         "project": kw.pop("project", "proj"), "project_path": "/x/proj"}
    e.update(kw)
    return e


def toks(i=100, o=100, cc=0, cr=0):
    return {"input": i, "output": o, "cache_creation": cc, "cache_read": cr}


def start(tid, d, **kw):
    kw.setdefault("tier", "haiku")
    kw.setdefault("router_worker", True)
    kw.setdefault("label", "Task " + tid)
    return ev("task_start", d, task_id=tid, status="running", **kw)


def end(tid, d, **kw):
    kw.setdefault("tier", "haiku")
    kw.setdefault("router_worker", True)
    kw.setdefault("label", "Task " + tid)
    kw.setdefault("model", "claude-haiku-4-5-20251001")
    kw.setdefault("tokens", toks())
    kw.setdefault("status", "pass")
    kw.setdefault("duration_ms", 1000)
    return ev("task_end", d, task_id=tid, **kw)


def write(home, day, events, raw=None, mode="a"):
    os.makedirs(os.path.join(home, "logs"), exist_ok=True)
    with open(os.path.join(home, "logs", day + ".jsonl"), mode) as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
        if raw:
            f.write(raw)
