"""`tokentier migrate legacy` tests. Writes only into temp homes; the legacy file is read-only."""
import glob
import json
import os
import sys
import time
import unittest

from tt_helpers import FIX, ROOT, FakeHome, snapshot

sys.path.insert(0, os.path.join(ROOT, "dashboard"))
from tokentier_dash.pricing import Pricing  # noqa: E402
from tokentier_dash.store import Store  # noqa: E402

REAL_LEGACY = os.path.join(ROOT, "legacy-data", "router-kit-data.json")
SMALL = os.path.join(FIX, "legacy_small.json")


def events(home):
    out = []
    for p in sorted(glob.glob(os.path.join(home, "logs", "*.jsonl"))):
        with open(p) as f:
            out += [(os.path.basename(p)[:10], json.loads(l)) for l in f if l.strip()]
    return out


@unittest.skipUnless(hasattr(time, "tzset"), "expects TZ=Asia/Karachi to set the local zone (time.tzset); "
                     "not available on Windows")
class MigBase(FakeHome):
    def setUp(self):
        FakeHome.setUp(self)
        self.h = os.path.join(self.tmp, "tthome")

    def mig(self, *args, file=SMALL, check=True, tz="Asia/Karachi"):
        return self.run_cli("migrate", "legacy", "--file", file, "--home", self.h, *args, check=check,
                            env=self.env(TZ=tz))


class MigrateTest(MigBase):
    def test_small_fixture_conversion(self):
        p = self.mig()
        self.assertIn("events   : 11 new, 0 already present", p.stdout)
        evs = events(self.h)
        kinds = [e["event"] for _, e in evs]
        self.assertEqual(kinds.count("session_start"), 2)
        self.assertEqual(kinds.count("task_start"), 5)
        self.assertEqual(kinds.count("task_end"), 4)  # the 'running' task has no task_end
        for day, e in evs:
            self.assertEqual(e["source"], "legacy")
            self.assertEqual(e["v"], 1)
            self.assertEqual(e["project"], "demo-app")
            self.assertEqual(e["project_path"], "/Users/someone/code/demo-app")
            self.assertEqual(e["ts"][:10], day)  # file = local date of ts
        ends = {e["task_id"]: e for _, e in evs if e["event"] == "task_end"}
        self.assertEqual(set(ends), {"legacy-2026-09-30-t1", "legacy-2026-09-30-t2", "legacy-2026-09-30-t3",
                                     "legacy-2026-10-01-t1"})
        t1 = ends["legacy-2026-09-30-t1"]
        self.assertEqual(t1["tokens"], {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0})
        self.assertEqual((t1["tokens_total"], t1["legacy_tokens_total"]), (1200, 1200))
        self.assertEqual((t1["tier"], t1["agent_type"], t1["status"]), ("haiku", "fast-worker", "pass"))
        lead = ends["legacy-2026-09-30-t2"]
        self.assertEqual((lead["tier"], lead["legacy_tier"], lead["router_worker"]), ("sonnet", "lead", False))
        self.assertEqual(ends["legacy-2026-09-30-t3"]["status"], "fixed")
        self.assertEqual(ends["legacy-2026-09-30-t3"]["tier"], "sonnet")
        # timestamps: session at 12:00 local, tasks offset by cumulative duration (+1 s gaps)
        starts = [e for _, e in evs if e["event"] == "task_start" and e["session_id"] == "legacy-2026-09-30"]
        sess = [e for _, e in evs if e["event"] == "session_start" and e["session_id"] == "legacy-2026-09-30"][0]
        self.assertEqual(sess["ts"], "2026-09-30T12:00:00.000+05:00")
        self.assertEqual([s["ts"] for s in starts], ["2026-09-30T12:00:01.000+05:00",
                                                      "2026-09-30T12:01:02.000+05:00",
                                                      "2026-09-30T12:01:03.000+05:00"])
        self.assertEqual(ends["legacy-2026-09-30-t3"]["ts"], "2026-09-30T12:06:03.000+05:00")

    def test_idempotent(self):
        self.mig()
        snap = snapshot(self.h)
        p = self.mig()
        self.assertIn("0 new, 11 already present", p.stdout)
        self.assertEqual(snapshot(self.h), snap)

    def test_dry_run_writes_nothing(self):
        p = self.mig("--dry-run")
        self.assertIn("Dry run", p.stdout)
        self.assertFalse(os.path.exists(self.h))

    def test_redate_and_replace(self):
        p = self.mig("--redate", "2026-09-30:t3=2026-10-02")
        self.assertIn("2026-09-30:t3 -> 2026-10-02", p.stdout)
        evs = events(self.h)
        moved = [(d, e) for d, e in evs if e.get("task_id") == "legacy-2026-09-30-t3"]
        self.assertEqual({d for d, _ in moved}, {"2026-10-02"})
        self.assertEqual({e["session_id"] for _, e in moved}, {"legacy-2026-10-02"})
        self.assertTrue(any(e["event"] == "session_start" and e["session_id"] == "legacy-2026-10-02"
                            for _, e in evs))
        # re-dating something already imported needs --replace and writes nothing otherwise
        snap = snapshot(self.h)
        p = self.mig("--redate", "2026-09-30:t1=2026-10-03", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("--replace", p.stderr)
        self.assertEqual(snapshot(self.h), snap)
        # unrelated non-legacy events survive --replace
        with open(os.path.join(self.h, "logs", "2026-09-30.jsonl"), "a") as f:
            f.write(json.dumps({"v": 1, "event": "session_start", "session_id": "real",
                                "ts": "2026-09-30T09:00:00+05:00", "project": "demo-app"}) + "\n")
        p = self.mig("--redate", "2026-09-30:t1-t2=2026-10-03", "--replace")
        evs = events(self.h)
        self.assertEqual(len([1 for _, e in evs if e.get("source") == "legacy"]), 12)
        self.assertEqual({d for d, e in evs if e.get("task_id") in ("legacy-2026-09-30-t1",
                                                                     "legacy-2026-09-30-t2")}, {"2026-10-03"})
        self.assertTrue(any(e.get("session_id") == "real" for _, e in evs))
        self.assertFalse(os.path.exists(os.path.join(self.h, "logs", "2026-10-02.jsonl")))

    def test_bad_redate(self):
        p = self.mig("--redate", "2026-09-30:t99=2026-10-02", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("unknown task", p.stderr)
        p = self.mig("--redate", "garbage", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertFalse(os.path.exists(self.h))

    def test_store_loads_small(self):
        self.mig()
        st = Store(self.h, Pricing(os.path.join(ROOT, "dashboard", "pricing.json")))
        st.refresh()
        self.assertEqual(st.bad_lines, 0)
        self.assertEqual(st.events, 11)
        o = st.overview("all")
        self.assertEqual(o["totals"]["tasks"], 5)
        self.assertEqual(o["totals"]["failed"], 1)
        t = st.tasks({"from": "all", "limit": 10})
        self.assertEqual(t["total"], 5)
        self.assertEqual({i["status"] for i in t["items"]}, {"pass", "fixed", "fail", "stale"})


@unittest.skipUnless(os.path.isfile(REAL_LEGACY), "legacy-data/router-kit-data.json not present (gitignored)")
class RealLegacyTest(MigBase):
    def test_real_file(self):
        with open(REAL_LEGACY, "rb") as f:
            before = f.read()
        p = self.mig(file=REAL_LEGACY)
        self.assertIn("2026-10-02 (6 tasks), 2026-10-03 (79 tasks)", p.stdout)
        self.assertIn("171 new", p.stdout)
        evs = events(self.h)
        self.assertEqual({d for d, _ in evs}, {"2026-10-02", "2026-10-03"})  # nothing spills into the 4th
        p = self.mig(file=REAL_LEGACY)
        self.assertIn("0 new, 171 already present", p.stdout)
        st = Store(self.h, Pricing(os.path.join(ROOT, "dashboard", "pricing.json")))
        st.refresh()
        self.assertEqual((st.bad_lines, st.events, len(st._tasks)), (0, 171, 85))
        o = st.overview("all")
        self.assertEqual(o["totals"]["tasks"], 85)
        st.projects()
        self.assertEqual(len(st.sessions()), 2)
        for t in st.tasks({"from": "all", "limit": 1000})["items"]:
            st.task(t["task_id"])
        p = self.mig("--redate", "2026-10-03:t78-t79=2026-10-04", "--replace", file=REAL_LEGACY)
        self.assertEqual({d for d, _ in events(self.h)}, {"2026-10-02", "2026-10-03", "2026-10-04"})
        with open(REAL_LEGACY, "rb") as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
