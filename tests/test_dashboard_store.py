import datetime as dt
import os
import shutil
import tempfile
import unittest

from dash_helpers import (PRICING, TZ, Clock, at, end, ev, iso, start, toks, write)
from tokentier_dash.pricing import Pricing
from tokentier_dash.store import RangeError, Store


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.clock = Clock(at("2026-10-05", "12:00"))

    def store(self):
        s = Store(self.home, Pricing(PRICING), clock=self.clock, tz=TZ)
        s.refresh()
        return s


class PricingTest(unittest.TestCase):
    def test_vector(self):
        p = Pricing(PRICING)
        t = toks(218, 1153, 63069, 1513669)
        key = p.resolve("claude-haiku-4-5-20251001", None)
        self.assertEqual(key, "haiku")
        self.assertAlmostEqual(p.cost(t, key)["total"], 0.23619, places=5)
        self.assertAlmostEqual(p.baseline(t), 0.64201, places=5)

    def test_resolve_fallback_and_unknown(self):
        p = Pricing(PRICING)
        self.assertEqual(p.resolve("weird-model", "sonnet"), "sonnet")
        self.assertIsNone(p.resolve("weird-model", "lead"))
        self.assertIsNone(p.resolve(None, None))

    def test_reload_on_mtime(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "p.json")
        shutil.copy(PRICING, path)
        p = Pricing(path)
        self.assertEqual(p.current()["models"]["haiku"]["input"], 1.0)
        import json
        with open(path) as f:
            data = json.load(f)
        data["models"]["haiku"]["input"] = 9.0
        with open(path, "w") as f:
            json.dump(data, f)
        os.utime(path, (1, os.stat(path).st_mtime + 5))
        self.assertEqual(p.current()["models"]["haiku"]["input"], 9.0)


class LoaderTest(Base):
    def test_merge_start_end(self):
        write(self.home, "2026-10-05", [
            start("a", at("2026-10-05", "10:00"), tool_use_id="tu1"),
            end("a", at("2026-10-05", "10:01"), tokens=toks(218, 1153, 63069, 1513669), result="ok",
                tool_use_id="tu1")])
        s = self.store()
        r = s.tasks({})
        self.assertEqual(r["total"], 1)
        t = r["items"][0]
        self.assertEqual(t["status"], "pass")
        self.assertEqual(t["started_at"], iso(at("2026-10-05", "10:00")))
        self.assertEqual(t["ended_at"], iso(at("2026-10-05", "10:01")))
        self.assertAlmostEqual(t["cost"]["total"], 0.23619, places=5)
        self.assertAlmostEqual(t["baseline_cost"], 0.64201, places=5)
        self.assertAlmostEqual(t["saved"], 0.64201 - 0.23619, places=4)
        self.assertTrue(t["cost_known"])
        self.assertEqual(t["tokens_total"], 218 + 1153 + 63069 + 1513669)

    def test_end_without_start(self):
        write(self.home, "2026-10-05", [end("b", at("2026-10-05", "09:00"),
                                            started_at=iso(at("2026-10-05", "08:58")))])
        t = self.store().tasks({})["items"][0]
        self.assertEqual(t["started_at"], iso(at("2026-10-05", "08:58")))
        self.assertEqual(t["status"], "pass")

    def test_start_without_end_running_then_stale(self):
        write(self.home, "2026-10-05", [start("c", at("2026-10-05", "11:50"))])
        s = self.store()
        t = s.tasks({})["items"][0]
        self.assertEqual(t["status"], "running")
        self.assertEqual(t["duration_ms"], 10 * 60 * 1000)
        self.assertFalse(t["cost_known"])
        self.assertEqual(s.overview("today")["totals"]["unknown_cost_tasks"], 0)
        self.clock.d = at("2026-10-05", "14:00:01")
        t = s.tasks({})["items"][0]
        self.assertEqual(t["status"], "stale")

    def test_duplicate_end_latest_wins(self):
        write(self.home, "2026-10-05", [
            start("d", at("2026-10-05", "10:00")),
            end("d", at("2026-10-05", "10:01"), status="fail"),
            end("d", at("2026-10-05", "10:02"), status="pass", result="second")])
        r = self.store().tasks({})
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["items"][0]["status"], "pass")
        self.assertEqual(r["items"][0]["result"], "second")

    def test_bad_lines_and_partial(self):
        write(self.home, "2026-10-05", [start("e", at("2026-10-05", "10:00"))],
              raw='not json\n[1,2]\n{"event":"task_start"}\n{"event":"task_end","task_id":"e"')
        s = self.store()
        h = s.health()
        self.assertEqual(h["bad_lines"], 3)  # garbage, non-object, task event without id
        self.assertEqual(h["events"], 1)
        # complete the partial line -> now valid, not counted bad
        with open(os.path.join(self.home, "logs", "2026-10-05.jsonl"), "a") as f:
            f.write(',"status":"pass","tokens":{"input":1,"output":1,"cache_creation":0,"cache_read":0},'
                    '"tier":"haiku","ts":"2026-10-05T10:01:00.000+05:00"}\n')
        s.refresh()
        self.assertEqual(s.health()["bad_lines"], 3)
        self.assertEqual(s.tasks({})["items"][0]["status"], "pass")

    def test_incremental_offsets(self):
        write(self.home, "2026-10-05", [end("f1", at("2026-10-05", "10:00"))])
        s = self.store()
        self.assertEqual(s.health()["events"], 1)
        write(self.home, "2026-10-05", [end("f2", at("2026-10-05", "10:05"))])
        self.assertTrue(s.refresh())
        self.assertFalse(s.refresh())
        self.assertEqual(s.health()["events"], 2)

    def test_unknown_cost(self):
        write(self.home, "2026-10-05", [
            end("u", at("2026-10-05", "10:00"), tier="lead", model="mystery-1", router_worker=False),
            end("k", at("2026-10-05", "10:01"))])
        s = self.store()
        o = s.overview("today")
        self.assertEqual(o["totals"]["unknown_cost_tasks"], 1)
        self.assertEqual(o["by_tier"]["unknown"]["tasks"], 1)
        self.assertEqual(o["by_tier"]["haiku"]["tasks"], 1)
        u = [t for t in s.tasks({})["items"] if t["task_id"] == "u"][0]
        self.assertFalse(u["cost_known"])
        self.assertIsNone(u["cost"])
        # only the known task contributes
        self.assertAlmostEqual(o["totals"]["cost"], 100 * 1e-6 + 100 * 5e-6, places=6)

    def test_model_prefix_beats_tier(self):
        write(self.home, "2026-10-05", [end("m", at("2026-10-05", "10:00"), tier="haiku",
                                            model="claude-opus-5-5-20260915")])
        self.assertEqual(self.store().tasks({})["items"][0]["tier"], "opus")


class DateRolloverTest(Base):
    def test_midnight(self):
        self.clock.d = at("2026-10-05", "23:59:30")
        write(self.home, "2026-10-05", [end("late", at("2026-10-05", "23:59"))])
        write(self.home, "2026-10-06", [end("early", at("2026-10-06", "00:01"))])
        s = self.store()
        tasks = {t["task_id"]: t for t in s.tasks({})["items"]}
        self.assertEqual(tasks["late"]["date"], "2026-10-05")
        self.assertEqual(tasks["early"]["date"], "2026-10-06")
        self.assertEqual(s.today(), "2026-10-05")
        self.assertEqual(s.overview("today")["totals"]["tasks"], 1)
        self.clock.d = at("2026-10-06", "00:00:30")
        self.assertEqual(s.today(), "2026-10-06")
        o = s.overview("today")
        self.assertEqual(o["range"], {"from": "2026-10-06", "to": "2026-10-06"})
        self.assertEqual(o["totals"]["tasks"], 1)
        self.assertEqual(s.overview("yesterday")["totals"]["tasks"], 1)
        self.assertEqual(s.overview("yesterday")["range"]["from"], "2026-10-05")

    def test_long_task_crosses_midnight(self):
        self.clock.d = at("2026-10-06", "00:30")
        write(self.home, "2026-10-05", [start("long", at("2026-10-05", "23:50"))])
        s = self.store()
        t = s.tasks({})["items"][0]
        self.assertEqual(t["date"], "2026-10-05")
        self.assertEqual(t["status"], "running")
        self.assertEqual(s.overview("today")["totals"]["tasks"], 0)
        self.assertEqual(s.overview("yesterday")["totals"]["running"], 1)
        # finishes after midnight: stays on start day
        write(self.home, "2026-10-06", [end("long", at("2026-10-06", "00:20"))])
        s.refresh()
        t = s.tasks({})["items"][0]
        self.assertEqual(t["date"], "2026-10-05")
        self.assertEqual(s.overview("today")["totals"]["tasks"], 0)

    def test_timezone_conversion(self):
        # 20:30Z is 01:30 next day at +05:00
        d = dt.datetime(2026, 10, 5, 20, 30, tzinfo=dt.timezone.utc)
        write(self.home, "2026-10-06", [end("tz", d)])
        self.assertEqual(self.store().tasks({})["items"][0]["date"], "2026-10-06")


class QueryTest(Base):
    def seed(self):
        write(self.home, "2026-10-03", [end("d3", at("2026-10-03", "10:00"), project="alpha", session_id="sa")])
        write(self.home, "2026-10-04", [
            start("x1", at("2026-10-04", "09:00"), project="alpha", session_id="sa"),
            end("x1", at("2026-10-04", "09:05"), project="alpha", session_id="sa", status="fail"),
            end("x2", at("2026-10-04", "15:00"), project="beta", session_id="sb", tier="opus",
                model="claude-opus-5-5-x", label="Design cache", result="done well"),
            ev("session_start", at("2026-10-04", "08:59"), session_id="sa", project="alpha"),
            ev("session_end", at("2026-10-04", "16:00"), session_id="sa", project="alpha")])
        write(self.home, "2026-10-05", [
            end("y1", at("2026-10-05", "08:00"), project="alpha", session_id="sc", status="escalated"),
            ev("escalation", at("2026-10-05", "08:00"), task_id="y1", project="alpha", session_id="sc"),
            ev("escalation", at("2026-10-05", "08:30"), task_id="ghost", project="beta", session_id="sc"),
            end("y2", at("2026-10-05", "11:00"), project="beta", session_id="sd", router_worker=False)])
        return self.store()

    def test_newest_first(self):
        s = self.seed()
        ids = [t["task_id"] for t in s.tasks({})["items"]]
        self.assertEqual(ids, ["y2", "y1", "x2", "x1", "d3"])
        self.assertEqual([x["project"] for x in s.projects()], ["beta", "alpha"])
        sess = [x["session_id"] for x in s.sessions()]
        self.assertEqual(sess, ["sd", "sc", "sb", "sa"])
        days = [x["date"] for x in s.overview("all")["daily"]]
        self.assertEqual(days, sorted(days, reverse=True))

    def test_filters(self):
        s = self.seed()
        f = lambda **k: [t["task_id"] for t in s.tasks(k)["items"]]
        self.assertEqual(f(project="beta"), ["y2", "x2"])
        self.assertEqual(f(session="sa"), ["x1", "d3"])
        self.assertEqual(f(tier="opus"), ["x2"])
        self.assertEqual(f(status="fail"), ["x1"])
        self.assertEqual(f(q="cache"), ["x2"])
        self.assertEqual(f(q="done well"), ["x2"])
        self.assertEqual(f(q="BETA"), ["y2", "x2"])
        self.assertEqual(f(router_only="true"), ["y1", "x2", "x1", "d3"])
        self.assertEqual(f(**{"from": "2026-10-04", "to": "2026-10-04"}), ["x2", "x1"])
        self.assertEqual(f(**{"from": "2026-10-04"}), ["y2", "y1", "x2", "x1"])
        self.assertEqual(f(limit="2", offset="1"), ["y1", "x2"])
        self.assertEqual(s.tasks({"limit": 2})["total"], 5)
        self.assertEqual(s.sessions(project="beta")[0]["session_id"], "sd")
        self.assertEqual([x["session_id"] for x in s.sessions(frm="2026-10-04", to="2026-10-04")], ["sb", "sa"])

    def test_escalations_no_double_count(self):
        s = self.seed()
        o = s.overview("all")
        # y1 (status + event, once) + ghost event
        self.assertEqual(o["totals"]["escalations"], 2)
        self.assertEqual(s.overview("all", None, "alpha")["totals"]["escalations"], 1)

    def test_overview_counts_and_keywords(self):
        s = self.seed()
        o = s.overview("all")
        self.assertEqual(o["totals"]["tasks"], 5)
        self.assertEqual(o["totals"]["completed"], 3)
        self.assertEqual(o["totals"]["failed"], 1)
        self.assertEqual(o["range"], {"from": "2026-10-03", "to": "2026-10-05"})
        o7 = s.overview("7d")
        self.assertEqual(o7["range"], {"from": "2026-09-29", "to": "2026-10-05"})
        self.assertEqual(len(s.overview("30d")["daily"]), 30)
        with self.assertRaises(RangeError):
            s.overview("garbage")

    def test_zero_fill(self):
        s = self.seed()
        o = s.overview("2026-10-01", "2026-10-05")
        days = [d["date"] for d in o["daily"]]
        self.assertEqual(days, ["2026-10-05", "2026-10-04", "2026-10-03", "2026-10-02", "2026-10-01"])
        by = {d["date"]: d for d in o["daily"]}
        self.assertEqual(by["2026-10-02"]["tasks"], 0)
        self.assertEqual(by["2026-10-02"]["cost"], 0)
        self.assertEqual(by["2026-10-04"]["tasks"], 2)

    def test_task_detail_chain(self):
        write(self.home, "2026-10-05", [
            end("p1", at("2026-10-05", "10:00"), tool_use_id="TU", status="escalated"),
            end("p2", at("2026-10-05", "10:05"), tool_use_id="TU", tier="sonnet",
                model="claude-sonnet-5-5-x"),
            start("p2", at("2026-10-05", "10:02"), tool_use_id="TU")])
        s = self.store()
        d = s.task("p2")
        self.assertEqual([c["task_id"] for c in d["same_tool_use"]], ["p1"])
        self.assertEqual(len(d["events"]), 2)
        self.assertIsNone(s.task("nope"))


class LegacyTest(Base):
    """Events shaped exactly like `tokentier migrate legacy` writes them."""

    def legacy_pair(self, tid, total, tier="haiku"):
        common = dict(session_id="legacy-2026-10-05", source="legacy", tier=tier, legacy_tier=tier,
                      agent_type="fast-worker", router_worker=True, label="Lint", legacy_id=tid,
                      tool_use_id=None, model=None)
        st = start("legacy-" + tid, at("2026-10-05", "09:00"), **common)
        en = end("legacy-" + tid, at("2026-10-05", "09:01"), tokens=toks(0, 0, 0, 0), tokens_total=total,
                 legacy_tokens_total=total, started_at=st["ts"], **common)
        return [st, en]

    def test_legacy_tokens_not_priced(self):
        write(self.home, "2026-10-05", self.legacy_pair("t1", 1200)
              + [start("n", at("2026-10-05", "10:00")),
                 end("n", at("2026-10-05", "10:01"), tokens=toks(218, 1153, 63069, 1513669))])
        s = self.store()
        t = s.task("legacy-t1")
        self.assertEqual(t["tokens_total"], 1200)
        self.assertTrue(t["legacy"])
        self.assertEqual(t["legacy_tokens_total"], 1200)
        self.assertIsNone(t["cost"])
        self.assertFalse(t["cost_known"])
        self.assertIsNone(t["baseline_cost"])
        self.assertFalse(s.task("n")["legacy"])
        tot = s.overview("today")["totals"]
        self.assertEqual(tot["tasks"], 2)
        self.assertAlmostEqual(tot["cost"], 0.23619, places=5)
        self.assertAlmostEqual(tot["baseline_cost"], 0.64201, places=5)
        self.assertAlmostEqual(tot["saved"], 0.64201 - 0.23619, places=4)
        self.assertEqual(tot["tokens"]["total"], 1200 + 218 + 1153 + 63069 + 1513669)
        self.assertEqual(tot["legacy_tasks"], 1)
        self.assertEqual(tot["legacy_tokens"], 1200)
        self.assertEqual(tot["unknown_cost_tasks"], 0)

    def test_legacy_only_has_no_cost(self):
        write(self.home, "2026-10-05", self.legacy_pair("t1", 50000, "sonnet"))
        tot = self.store().overview("today")["totals"]
        self.assertEqual((tot["cost"], tot["baseline_cost"], tot["saved"]), (0.0, 0.0, 0.0))
        self.assertEqual(tot["tokens"]["total"], 50000)


if __name__ == "__main__":
    unittest.main()
