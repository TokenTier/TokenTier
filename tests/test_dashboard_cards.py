"""Card data for the dashboard: /api/projects and /api/sessions extensions, task sort/counts, and the
regression that a task_update without project fields must never create a project or session."""
import unittest

from dash_helpers import at, end, ev, start, toks, write
from test_dashboard_api import ApiBase
from test_dashboard_store import Base
from test_lead_hook import OPUS
from test_lead_store import lead

M = 1000000


def update(tid, d, **kw):
    """task_update as the hook writes it when the payload lacks cwd: no project / project_path at all."""
    e = ev("task_update", d, task_id=tid, **kw)
    if "project" not in kw:
        e.pop("project", None)
        e.pop("project_path", None)
    return e


class UpdateEventRegressionTest(Base):
    def test_update_without_project_creates_no_project_or_session(self):
        write(self.home, "2026-10-05", [
            start("a", at("2026-10-05", "10:00")),
            update("a", at("2026-10-05", "10:00:30"), label="Better label", session_id="s1"),
            # update for a task never seen, in a session never seen: ignored entirely
            update("ghost", at("2026-10-05", "10:02"), label="Ghost", session_id="s-ghost"),
            end("a", at("2026-10-05", "10:03"), label="Better label")])
        s = self.store()
        names = [p["project"] for p in s.projects()]
        self.assertEqual(names, ["proj"])
        self.assertNotIn("unknown", names)
        self.assertEqual(s.projects()[0]["sessions"], 1)
        self.assertEqual([r["session_id"] for r in s.sessions()], ["s1"])
        self.assertEqual(s.tasks({})["total"], 1)

    def test_update_is_attributed_to_its_task_project(self):
        write(self.home, "2026-10-05", [
            start("a", at("2026-10-05", "10:00"), project="alpha"),
            update("a", at("2026-10-05", "11:30"), session_id="s1")])
        p = self.store().projects()
        self.assertEqual([x["project"] for x in p], ["alpha"])
        # the update counts as activity of the task's project
        self.assertTrue(p[0]["last_active"].startswith("2026-10-05T11:30"))

    def test_update_before_start_does_not_create_unknown(self):
        write(self.home, "2026-10-05", [
            update("a", at("2026-10-05", "09:59"), label="early"),
            start("a", at("2026-10-05", "10:00"))])
        self.assertEqual([x["project"] for x in self.store().projects()], ["proj"])

    def test_end_without_project_inherits_from_start(self):
        e = end("a", at("2026-10-05", "10:03"))
        e.pop("project")
        e.pop("project_path")
        write(self.home, "2026-10-05", [start("a", at("2026-10-05", "10:00"), project="beta"), e])
        s = self.store()
        self.assertEqual(s.tasks({})["items"][0]["project"], "beta")
        self.assertEqual([x["project"] for x in s.projects()], ["beta"])


class ProjectCardsTest(Base):
    def setUp(self):
        super().setUp()
        write(self.home, "2026-10-05", [
            start("a", at("2026-10-05", "09:00")), end("a", at("2026-10-05", "09:01"), tokens=toks(M, 0, 0, 0)),
            start("b", at("2026-10-05", "09:05"), tier="sonnet"),
            end("b", at("2026-10-05", "09:06"), tier="sonnet", model="claude-sonnet-5-5", status="fail",
                tokens=toks(M, 0, 0, 0)),
            start("c", at("2026-10-05", "11:50"), tier="opus"),                       # running
            lead("2026-10-05", "10:00", tokens=toks(M, 0, 0, 0)),                     # opus lead: 4.00
            start("z", at("2026-10-05", "08:00"), project="other", session_id="s9"),
            end("z", at("2026-10-05", "08:01"), project="other", session_id="s9")])
        write(self.home, "2026-10-03", [
            start("o", at("2026-10-03", "10:00"), tier="opus"),
            end("o", at("2026-10-03", "10:02"), tier="opus", model=OPUS, status="escalated",
                tokens=toks(0, M, 0, 0))])
        write(self.home, "2026-09-01", [  # older than the 14-day window
            start("old", at("2026-09-01", "10:00")), end("old", at("2026-09-01", "10:01"), tokens=toks(M, 0, 0, 0))])

    def test_fields(self):
        rows = {p["project"]: p for p in self.store().projects()}
        p = rows["proj"]
        for k in ("project", "project_path", "tasks", "sessions", "cost", "last_active"):  # unchanged fields
            self.assertIn(k, p)
        self.assertEqual(p["tasks"], 5)
        self.assertEqual(p["running"], 1)
        self.assertEqual(p["tier_mix"], {"haiku": 2, "sonnet": 1, "opus": 2, "unknown": 0})
        self.assertEqual(p["status_counts"], {"pass": 2, "fail": 1, "escalated": 1, "running": 1, "stale": 0,
                                              "unknown": 0})
        # haiku 1.00 + 1.00 (old), sonnet 2.00, opus output 20.00
        self.assertAlmostEqual(p["cost"], 24.0, places=6)
        self.assertAlmostEqual(p["baseline_cost"], 4.0 + 4.0 + 4.0 + 20.0, places=6)
        self.assertAlmostEqual(p["saved"], 32.0 - 24.0, places=6)
        self.assertAlmostEqual(p["lead_cost"], 4.0, places=6)
        self.assertEqual(p["lead_tokens"], M)
        self.assertAlmostEqual(p["spend"], 28.0, places=6)
        self.assertTrue(p["first_active"].startswith("2026-09-01T10:00"))
        self.assertTrue(p["last_active"].startswith("2026-10-05T11:50"))
        self.assertEqual(rows["other"]["tasks"], 1)
        self.assertEqual(rows["other"]["sessions"], 1)

    def test_daily_is_14_zero_filled_local_days(self):
        p = {x["project"]: x for x in self.store().projects()}["proj"]
        d = p["daily"]
        self.assertEqual(len(d), 14)
        self.assertEqual(d[-1]["date"], "2026-10-05")
        self.assertEqual(d[0]["date"], "2026-09-22")
        by = {x["date"]: x for x in d}
        self.assertAlmostEqual(by["2026-10-05"]["cost"], 3.0, places=6)
        self.assertAlmostEqual(by["2026-10-05"]["lead_cost"], 4.0, places=6)
        self.assertEqual(by["2026-10-05"]["tasks"], 3)
        self.assertAlmostEqual(by["2026-10-03"]["cost"], 20.0, places=6)
        self.assertEqual(by["2026-10-04"], {"date": "2026-10-04", "cost": 0.0, "lead_cost": 0.0, "tasks": 0})

    def test_api(self):
        rows = self.store().projects()
        self.assertEqual(rows[0]["project"], "proj")  # most recently active first


class SessionCardsTest(Base):
    def test_fields_and_state(self):
        write(self.home, "2026-10-05", [
            ev("session_start", at("2026-10-05", "08:00"), session_id="done"),
            start("a", at("2026-10-05", "08:01"), session_id="done", label="Cheap"),
            end("a", at("2026-10-05", "08:02"), session_id="done", label="Cheap", tokens=toks(10, 0, 0, 0)),
            start("b", at("2026-10-05", "08:03"), session_id="done", tier="opus", label="Pricey"),
            end("b", at("2026-10-05", "08:04"), session_id="done", tier="opus", model=OPUS, label="Pricey",
                tokens=toks(M, 0, 0, 0)),
            start("c", at("2026-10-05", "08:05"), session_id="done", label="Latest"),
            end("c", at("2026-10-05", "08:06"), session_id="done", label="Latest", status="fail"),
            ev("session_end", at("2026-10-05", "08:10"), session_id="done"),
            start("r", at("2026-10-05", "11:00"), session_id="live", label="Still going"),
            start("q", at("2026-10-05", "09:00"), session_id="quiet"),
            end("q", at("2026-10-05", "09:30"), session_id="quiet"),
            start("x", at("2026-10-05", "11:40"), session_id="recent"),
            end("x", at("2026-10-05", "11:45"), session_id="recent")])
        write(self.home, "2026-10-04", [
            start("y", at("2026-10-04", "10:00"), session_id="yday"),
            end("y", at("2026-10-04", "10:05"), session_id="yday")])
        rows = {r["session_id"]: r for r in self.store().sessions()}
        d = rows["done"]
        self.assertEqual(d["state"], "finished")
        self.assertTrue(d["ended_at"].startswith("2026-10-05T08:10"))
        self.assertEqual(d["preview_label"], "Latest")
        self.assertEqual(d["top_label"], "Pricey")
        self.assertAlmostEqual(d["top_cost"], 4.0, places=6)
        self.assertEqual(d["tier_mix"], {"haiku": 2, "sonnet": 0, "opus": 1, "unknown": 0})
        self.assertEqual(d["status_counts"]["fail"], 1)
        self.assertEqual(d["status_counts"]["pass"], 2)
        self.assertTrue(d["last_active"].startswith("2026-10-05T08:10"))
        self.assertTrue(d["first_active"].startswith("2026-10-05T08:00"))
        self.assertEqual(rows["live"]["state"], "active")          # running task
        self.assertEqual(rows["live"]["running"], 1)
        self.assertEqual(rows["recent"]["state"], "active")        # activity 15 min ago, no session_end
        self.assertEqual(rows["quiet"]["state"], "idle")           # 2.5 h quiet
        self.assertEqual(rows["yday"]["state"], "finished")        # > 6 h quiet
        self.assertIsNone(rows["yday"]["ended_at"])

    def test_lead_only_session(self):
        write(self.home, "2026-10-05", [lead("2026-10-05", "11:55", sid="s2")])
        r = self.store().sessions()[0]
        self.assertEqual((r["session_id"], r["tasks"], r["preview_label"], r["state"]), ("s2", 0, None, "active"))
        self.assertGreater(r["spend"], 0)


class TaskSortAndCountsTest(Base):
    def setUp(self):
        super().setUp()
        write(self.home, "2026-10-05", [
            start("a", at("2026-10-05", "09:00")), end("a", at("2026-10-05", "09:01"), duration_ms=5000,
                                                       tokens=toks(10, 10, 0, 0)),
            start("b", at("2026-10-05", "09:05"), tier="opus"),
            end("b", at("2026-10-05", "09:06"), tier="opus", model=OPUS, duration_ms=1000, tokens=toks(M, 0, 0, 0)),
            start("c", at("2026-10-05", "09:10")),
            end("c", at("2026-10-05", "09:11"), duration_ms=90000, status="fail", tokens=toks(0, 0, 0, 5 * M)),
            start("r", at("2026-10-05", "11:59"))])

    def ids(self, **p):
        return [t["task_id"] for t in self.store().tasks(p)["items"]]

    def test_sorts(self):
        self.assertEqual(self.ids(), ["r", "c", "b", "a"])
        self.assertEqual(self.ids(sort="cost"), ["b", "c", "a", "r"])
        self.assertEqual(self.ids(sort="tokens"), ["c", "b", "a", "r"])
        self.assertEqual(self.ids(sort="duration")[0], "c")
        self.assertEqual(self.ids(sort="bogus"), ["r", "c", "b", "a"])

    def test_multi_status_and_counts(self):
        r = self.store().tasks({"status": "fail,running"})
        self.assertEqual(sorted(t["task_id"] for t in r["items"]), ["c", "r"])
        # status counts ignore the status facet; tier counts honour it
        self.assertEqual(r["counts"]["status"], {"pass": 2, "fail": 1, "escalated": 0, "running": 1,
                                                 "stale": 0, "unknown": 0})
        self.assertEqual(r["counts"]["tier"], {"haiku": 2})
        r = self.store().tasks({"tier": "opus"})
        self.assertEqual(r["counts"]["tier"], {"haiku": 3, "opus": 1})
        self.assertEqual(r["counts"]["status"]["pass"], 1)


class CardsApiTest(ApiBase):
    def test_endpoints_carry_card_fields(self):
        p = self.get("/api/projects")[1][0]
        for k in ("daily", "tier_mix", "status_counts", "running", "saved", "baseline_cost", "lead_cost",
                  "first_active", "last_active", "spend"):
            self.assertIn(k, p)
        s = self.get("/api/sessions")[1][0]
        for k in ("preview_label", "ended_at", "state", "tier_mix", "status_counts", "last_active"):
            self.assertIn(k, s)
        t = self.get("/api/tasks?sort=cost&status=pass,fail")[1]
        self.assertEqual(t["total"], 1)
        self.assertIn("counts", t)


if __name__ == "__main__":
    unittest.main()
