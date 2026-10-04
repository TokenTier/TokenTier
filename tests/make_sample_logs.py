#!/usr/bin/env python3
"""Generate realistic demo TokenTier logs.

    python3 tests/make_sample_logs.py <dir> [--days N] [--seed S]

Writes <dir>/logs/YYYY-MM-DD.jsonl (use <dir> as TOKENTIER_HOME). Dates are relative to the
real clock: today (only up to "now"), yesterday, ... N-1 days ago. Python 3.9+, stdlib only.
"""
import argparse
import datetime as dt
import json
import os
import random
import sys

PROJECTS = [("webshop-api", "/Users/demo/code/webshop-api"),
            ("webshop-app", "/Users/demo/code/webshop-app"),
            ("billing-api", "/Users/demo/work/billing-api"),
            ("docs-site", "/Users/demo/code/docs-site")]

TASKS = [  # label, preferred tier, result
    ("Fix login bug: session cookie dropped after the OAuth redirect", "sonnet", "Fixed null session check in auth middleware. STATUS: done"),
    ("Run test suite", "haiku", "All 148 tests pass. STATUS: done"),
    ("Rename helper functions", "haiku", "Renamed 12 helpers across 5 files. STATUS: done"),
    ("Add cursor pagination to the orders list endpoint", "sonnet", "Added limit/offset with tests. STATUS: done"),
    ("Summarise README", "haiku", "Summary written. STATUS: done"),
    ("Refactor payment module into charge, refund and payout services", "opus", "Split module into 3 services, tests updated. STATUS: done"),
    ("Design caching strategy", "opus", "Proposed write-through cache with TTL. STATUS: done"),
    ("Update dependencies", "haiku", "Bumped 9 packages, lockfile regenerated. STATUS: done"),
    ("Investigate flaky CI job: integration tests time out on cold cache", "opus", "Race in fixture teardown identified and fixed. STATUS: done"),
    ("Format changelog", "haiku", "Changelog formatted. STATUS: done"),
    ("Write migration for invoices table (add currency, backfill EUR)", "sonnet", "Migration added and applied locally. STATUS: done"),
    ("Fix typo in onboarding copy", "haiku", "Typo fixed. STATUS: done"),
    ("Review auth PR", "sonnet", "Two issues found, comments drafted. STATUS: done"),
    ("Generate API client types", "haiku", "Types generated from OpenAPI. STATUS: done"),
    ("Debug memory leak in the export worker after large CSV jobs", "opus", "Leak traced to unclosed streams. STATUS: done"),
]
AGENTS = {"haiku": "fast-worker", "sonnet": "mid-worker", "opus": "deep-worker"}
MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5-5-20260915",
          "opus": "claude-opus-5-5-20260915"}
NEXT = {"haiku": "sonnet", "sonnet": "opus"}


def iso(d):
    s = d.astimezone().strftime("%z")
    return d.astimezone().strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (d.microsecond // 1000) + s[:3] + ":" + s[3:]


def hexid(rng, n=16):
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def tokens_for(rng, tier):
    scale = {"haiku": 1.0, "sonnet": 1.6, "opus": 2.4}[tier]
    return {"input": rng.randint(40, 600), "output": int(rng.randint(500, 4000) * scale),
            "cache_creation": int(rng.randint(15000, 90000) * scale),
            "cache_read": int(rng.randint(200000, 2200000) * scale)}


def lead_events(lrng, base, sid, proj, t0, t1):
    """Realistic lead_usage deltas (one per flush) for one session between t0 and t1."""
    if (t1 - t0).total_seconds() < 5:
        return []
    model = lrng.choice(["opus", "opus", "sonnet"])  # the user's own choice of main model
    n = lrng.randint(4, 9)
    fr = sorted(lrng.random() for _ in range(n))
    out = []
    for i, f in enumerate(fr):
        when = t0 + (t1 - t0) * f
        turns = lrng.randint(1, 6)
        tk = {"input": lrng.randint(5, 40) * turns, "output": lrng.randint(600, 3500) * turns,
              "cache_creation": lrng.randint(4000, 45000) * turns,
              "cache_read": lrng.randint(60000, 380000) * turns}
        ev = base("lead_usage", when, sid, proj)
        ev.update(model=MODELS[model], tier=model, tokens=tk, tokens_total=sum(tk.values()), turns=turns,
                  from_ts=iso(when - dt.timedelta(seconds=lrng.randint(20, 240))), to_ts=iso(when))
        out.append((when, ev))
    return out


def generate(out_dir, days=7, now=None, seed=7):
    rng = random.Random(seed)
    lrng = random.Random(seed + 1000)  # separate stream: adding lead usage leaves the task data unchanged
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone()
    events = []  # (datetime, dict)

    def base(kind, when, sid, proj):
        return {"v": 1, "ts": iso(when), "event": kind, "session_id": sid, "project": proj[0],
                "project_path": proj[1], "user_id": None, "workspace_id": None}

    midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(days):
        day0 = midnight_today - dt.timedelta(days=back)
        if back == 0:
            lo, hi = day0, now - dt.timedelta(seconds=30)
            if (hi - lo).total_seconds() < 600:
                lo = day0
        else:
            lo, hi = day0.replace(hour=9), day0.replace(hour=19)
        span = (hi - lo).total_seconds()
        if span < 60:
            continue
        nsess = rng.randint(2, 3)
        for si in range(nsess):
            proj = PROJECTS[(back + si + rng.randint(0, 1)) % len(PROJECTS)]
            sid = "sess-%s" % hexid(rng, 12)
            s_start = lo + dt.timedelta(seconds=span * si / nsess)
            s_end_lim = lo + dt.timedelta(seconds=span * (si + 1) / nsess)
            events.append((s_start, base("session_start", s_start, sid, proj)))
            cursor = s_start + dt.timedelta(seconds=rng.randint(5, 40))
            ntasks = rng.randint(3, 6)
            for _ in range(ntasks):
                label, tier, result = rng.choice(TASKS)
                chain = [tier]
                outcome = rng.random()
                if tier != "opus" and outcome < 0.15:
                    chain.append(NEXT[tier])  # escalated once
                tool_use = "toolu_%s" % hexid(rng, 12)
                for i, tr in enumerate(chain):
                    dur = rng.randint(8, 240) * 1000
                    t0 = cursor
                    t1 = t0 + dt.timedelta(milliseconds=dur)
                    if t1 > min(hi, s_end_lim):
                        break
                    tid = "agent-%s" % hexid(rng, 16)
                    last = i == len(chain) - 1
                    status, text = "pass", result
                    if not last:
                        status = "escalated"
                        text = "ESCALATE: needs deeper reasoning than %s. STATUS: escalate" % tr
                    elif rng.random() < 0.08:
                        status, text = "fail", "Could not complete: tests still failing. STATUS: blocked"
                    common = {"task_id": tid, "agent_type": AGENTS[tr], "tier": tr, "router_worker": True,
                              "label": label, "tool_use_id": tool_use}
                    st = base("task_start", t0, sid, proj)
                    st.update(common, status="running")
                    events.append((t0, st))
                    en = base("task_end", t1, sid, proj)
                    tk = tokens_for(rng, tr)
                    en.update(common, model=MODELS[tr], started_at=iso(t0), duration_ms=dur, tokens=tk,
                              tokens_total=sum(tk.values()), status=status, result=text)
                    events.append((t1, en))
                    if status == "escalated":
                        es = base("escalation", t1, sid, proj)
                        es.update(task_id=tid, from_tier=tr, reason="needs deeper reasoning than %s" % tr)
                        events.append((t1, es))
                    cursor = t1 + dt.timedelta(seconds=rng.randint(3, 60))
            if back == 0 and si == nsess - 1:
                # two tasks still running right now
                for lab, tr in (("Run test suite", "haiku"), ("Refactor payment module into charge, refund and payout services", "opus")):
                    t0 = now - dt.timedelta(seconds=rng.randint(30, 400))
                    if t0 < lo:
                        t0 = lo
                    tid = "agent-%s" % hexid(rng, 16)
                    st = base("task_start", t0, sid, proj)
                    st.update(task_id=tid, agent_type=AGENTS[tr], tier=tr, router_worker=True, label=lab,
                              tool_use_id="toolu_%s" % hexid(rng, 12), status="running")
                    events.append((t0, st))
            else:
                ee = min(cursor + dt.timedelta(seconds=30), hi)
                events.append((ee, base("session_end", ee, sid, proj)))
            lead_end = min(cursor + dt.timedelta(seconds=20), hi, now - dt.timedelta(seconds=5))
            events.extend(lead_events(lrng, base, sid, proj, s_start, lead_end))
    events.sort(key=lambda x: x[0])
    logdir = os.path.join(out_dir, "logs")
    os.makedirs(logdir, exist_ok=True)
    byday = {}
    for when, ev in events:
        byday.setdefault(when.astimezone().strftime("%Y-%m-%d"), []).append(ev)
    for day, evs in byday.items():
        with open(os.path.join(logdir, day + ".jsonl"), "a", encoding="utf-8") as f:
            for ev in evs:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    return len(events)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    n = generate(a.dir, a.days, seed=a.seed)
    print("wrote %d events to %s/logs (use TOKENTIER_HOME=%s)" % (n, a.dir, a.dir))


if __name__ == "__main__":
    main()
