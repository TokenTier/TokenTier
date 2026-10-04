import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from dash_helpers import Clock, PRICING, TZ, at, end, start, write  # noqa: F401 (sets sys.path)
from tokentier_dash.pricing import Pricing
from tokentier_dash.store import Store

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kit", "hooks"))
import test_log_hook as tlh  # noqa: E402

DAY = "2026-10-05"


class StaleActivity(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.clock = Clock(at(DAY, "12:00"))
        self.sub = os.path.join(self.home, "proj", "sess", "subagents")
        os.makedirs(self.sub)

    def tpath(self, aid="abc123"):
        return os.path.join(self.sub, "agent-%s.jsonl" % aid)

    def touch(self, path, minutes_ago):
        with open(path, "a"):
            pass
        t = self.clock().timestamp() - minutes_ago * 60
        os.utime(path, (t, t))

    def view(self, tid="t1"):
        s = Store(self.home, Pricing(PRICING), clock=self.clock, tz=TZ)
        s._stat_ttl = 0
        s.refresh()
        self.s = s
        return next(t for t in s.tasks({"from": "all"})["items"] if t["task_id"] == tid)

    def log(self, start_hm, tp, tid="t1", extra=()):
        ev = start(tid, at(DAY, start_hm))
        if tp is not None:
            ev["subagent_transcript"] = tp
        write(self.home, DAY, [ev] + list(extra))

    def test_fresh_mtime_running(self):
        p = self.tpath()
        self.touch(p, 3)
        self.log("08:00", p)  # started 4 h ago: the 2 h rule must not apply
        v = self.view()
        self.assertEqual(v["status"], "running")
        self.assertIsNone(v["stale_reason"])
        self.assertTrue(v["last_activity"].endswith("+05:00"))
        self.assertEqual(self.s.overview("all", None)["totals"]["running"], 1)

    def test_old_mtime_stale(self):
        p = self.tpath()
        self.touch(p, 16)
        self.log("11:00", p)
        v = self.view()
        self.assertEqual(v["status"], "stale")
        self.assertEqual(v["stale_reason"], "no activity for 16 min")
        self.assertEqual(v["last_activity"], "2026-10-05T11:44:00+05:00")
        self.assertEqual(v["duration_ms"], 44 * 60 * 1000)
        ov = self.s.overview("all", None)["totals"]
        self.assertEqual(ov["running"], 0)
        self.assertEqual(ov["unknown_cost_tasks"], 0)

    def test_mtime_before_start_uses_start(self):
        p = self.tpath()
        self.touch(p, 120)
        self.log("11:50", p)
        self.assertEqual(self.view()["status"], "running")

    def test_missing_file_falls_back_to_2h(self):
        p = self.tpath("nofile")
        self.log("11:00", p)
        self.assertEqual(self.view()["status"], "running")
        self.clock.d = at(DAY, "13:30")
        v = self.view()
        self.assertEqual(v["status"], "stale")
        self.assertIsNone(v["duration_ms"])

    def test_missing_path_old_event(self):
        self.log("09:00", None)
        v = self.view()
        self.assertEqual(v["status"], "stale")
        self.assertIn("no activity for 180 min", v["stale_reason"])

    def test_unsafe_path_ignored_without_stat(self):
        bad = os.path.join(self.home, "secret.jsonl")
        self.touch(bad, 0)
        for i, p in enumerate([bad, self.sub + "/../subagents/agent-x.jsonl",
                               self.sub + "/agent-a_b.jsonl", "subagents/agent-a.jsonl",
                               self.sub + "/agent-a.jsonl.bak", 42]):
            self.log("11:00", p, tid="b%d" % i)
        s = Store(self.home, Pricing(PRICING), clock=self.clock, tz=TZ)
        s.refresh()
        seen = []

        def spy(path, *a, **k):
            seen.append(str(path))
            return real(path, *a, **k)

        real = os.stat
        with mock.patch("tokentier_dash.store.os.stat", side_effect=spy):
            items = s.tasks({"from": "all"})["items"]
        self.assertEqual([x for x in seen if not x.endswith("pricing.json")], [])
        self.assertEqual({t["status"] for t in items}, {"running"})  # 1 h old: fallback rule

    def test_resumed_goes_back_to_running(self):
        p = self.tpath()
        self.touch(p, 30)
        self.log("11:00", p)
        self.assertEqual(self.view()["status"], "stale")
        self.touch(p, 1)
        self.assertEqual(self.view()["status"], "running")

    def test_task_end_wins(self):
        p = self.tpath()
        self.touch(p, 60)
        self.log("10:00", p, extra=[end("t1", at(DAY, "10:30"))])
        v = self.view()
        self.assertEqual(v["status"], "pass")
        self.assertIsNone(v["stale_reason"])

    def test_stat_cache(self):
        p = self.tpath()
        self.touch(p, 1)
        self.log("11:00", p)
        s = Store(self.home, Pricing(PRICING), clock=self.clock, tz=TZ)
        s.refresh()
        with mock.patch("tokentier_dash.store.os.stat", wraps=os.stat) as st:
            s.tasks({"from": "all"})
            s.tasks({"from": "all"})
            n = [c for c in st.call_args_list if str(c[0][0]).endswith("agent-abc123.jsonl")]
            self.assertEqual(len(n), 1)


class HookWritesTranscript(tlh.Tmp):
    def test_start_has_transcript(self):
        main = self.make_subagent("a9", [])
        payload = json.dumps({"session_id": "s", "cwd": "/x/proj", "agent_id": "a9",
                              "agent_type": "fast-worker", "transcript_path": main})
        tlh.run_hook("SubagentStart", payload, self.home)
        (ev,) = [e for e in tlh.read_lines(self.home) if e["event"] == "task_start"]
        exp = os.path.join(self.tmp, "proj", "sess1", "subagents", "agent-a9.jsonl")
        self.assertEqual(ev["subagent_transcript"], os.path.abspath(exp))


if __name__ == "__main__":
    unittest.main()
