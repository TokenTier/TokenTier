"""Tests for `tokentier stats` (synthetic logs only; temp dirs, never the real ~/.tokentier)."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tt_helpers import BIN, FakeHome, load_cli  # noqa: E402

NOW = dt.datetime(2026, 10, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
MODELS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
AGENTS = {"haiku": "fast-worker", "sonnet": "mid-worker", "opus": "deep-worker"}


def end_event(n, label, tier, status, session="s1", age_days=1, router=True, tokens=None):
    ts = (NOW - dt.timedelta(days=age_days, seconds=-n)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    tk = tokens or {"input": 1000, "output": 1000, "cache_creation": 0, "cache_read": 0}
    return {"v": 1, "ts": ts, "event": "task_end", "session_id": session, "task_id": "t%d" % n,
            "agent_type": AGENTS[tier], "tier": tier, "router_worker": router, "label": label,
            "model": MODELS[tier], "tokens": tk, "tokens_total": sum(tk.values()), "status": status}


def write_logs(home, events):
    d = os.path.join(home, "logs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "2026-10-03.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
            f.write("not json at all\n")  # garbage lines must be skipped


class StatsCore(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()
        self.tmp = tempfile.mkdtemp(prefix="tt-stats-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logs = os.path.join(self.tmp, "logs")

    def stats(self, events, **kw):
        write_logs(self.tmp, events)
        return self.cli.compute_stats(self.logs, NOW, pricing=kw.pop("pricing", None), **kw)

    def batch(self, label, tier, passes, fails, start=0):
        ev = []
        for i in range(passes + fails):
            ev.append(end_event(start + i, label, tier, "pass" if i < passes else "fail",
                                session="s%d" % (start + i)))
        return ev

    def test_task_type_parsing(self):
        tt = self.cli.task_type
        self.assertEqual(tt("[bugfix] Fix login"), "bugfix")
        self.assertEqual(tt("  [Docs] readme"), "docs")
        self.assertEqual(tt("Fix [bugfix] later"), "untagged")
        self.assertEqual(tt(""), "untagged")
        self.assertEqual(tt(None), "untagged")

    def test_empty_and_missing_logs(self):
        self.assertEqual(self.cli.compute_stats(self.logs, NOW), {})
        os.makedirs(self.logs)
        self.assertEqual(self.cli.compute_stats(self.logs, NOW), {})
        self.assertIn("No finished tasks", self.cli.format_stats({}, 30))

    def test_rates_and_recommendation(self):
        ev = self.batch("[bugfix] a", "haiku", 3, 2)            # 5 attempts, 60%
        ev += self.batch("[bugfix] b", "sonnet", 5, 0, 100)     # 5 attempts, 100%
        s = self.stats(ev)["bugfix"]
        self.assertEqual(s["tasks"], 10)
        self.assertEqual(s["tiers"]["haiku"], {"tasks": 5, "pass_rate": 0.6})
        self.assertEqual(s["tiers"]["sonnet"]["pass_rate"], 1.0)
        self.assertIsNone(s["tiers"]["opus"]["pass_rate"])
        self.assertEqual(s["recommended_tier"], "sonnet")
        self.assertEqual(s["recommended_samples"], 5)

    def test_exactly_five_samples_is_enough(self):
        s = self.stats(self.batch("[docs] x", "haiku", 5, 0))["docs"]
        self.assertEqual(s["recommended_tier"], "haiku")

    def test_four_samples_is_insufficient(self):
        s = self.stats(self.batch("[docs] x", "haiku", 4, 0))["docs"]
        self.assertIsNone(s["recommended_tier"])

    def test_eighty_percent_boundary(self):
        s = self.stats(self.batch("[t] x", "haiku", 4, 1))["t"]       # exactly 80%
        self.assertEqual(s["recommended_tier"], "haiku")
        s = self.stats(self.batch("[t] x", "haiku", 7, 3))["t"]       # 70%
        self.assertEqual(s["recommended_tier"], "sonnet")
        s = self.stats(self.batch("[t] x", "haiku", 8, 2))["t"]       # 80% of 10
        self.assertEqual(s["recommended_tier"], "haiku")

    def test_falls_up_one_tier_and_caps_at_opus(self):
        ev = self.batch("[t] x", "haiku", 1, 4) + self.batch("[t] x", "sonnet", 1, 4, 50)
        self.assertEqual(self.stats(ev)["t"]["recommended_tier"], "opus")
        ev = self.batch("[t] x", "opus", 1, 4)
        self.assertEqual(self.stats(ev)["t"]["recommended_tier"], "opus")

    def test_untagged_and_not_in_hints(self):
        ev = self.batch("Fix login", "haiku", 5, 0) + self.batch("[docs] d", "haiku", 5, 0, 100)
        s = self.stats(ev)
        self.assertEqual(s["untagged"]["recommended_tier"], "haiku")
        hints = self.cli.hints_payload(s, NOW)
        self.assertEqual(list(hints["types"]), ["docs"])
        self.assertEqual(hints["types"]["docs"], {"start": "fast-worker", "samples": 5, "pass_rate": 1.0})

    def test_escalation_chain(self):
        ev = [end_event(1, "[feature] f", "haiku", "escalated"),
              end_event(2, "[feature] f", "sonnet", "pass"),
              end_event(3, "[feature] g", "haiku", "pass", session="s2")]
        s = self.stats(ev)["feature"]
        self.assertEqual(s["tasks"], 2)                      # chain counts once
        self.assertEqual(s["attempts"], 3)
        self.assertEqual(s["escalation_rate"], 0.5)
        self.assertEqual(s["tiers"]["haiku"]["pass_rate"], 0.5)
        # a later task with the same label starts a new chain
        ev.append(end_event(4, "[feature] f", "haiku", "pass"))
        self.assertEqual(self.stats(ev)["feature"]["tasks"], 3)

    def test_cost_uses_dashboard_pricing(self):
        pricing = self.cli._load_pricing()
        self.assertIsNotNone(pricing)
        tk = {"input": 1000000, "output": 1000000, "cache_creation": 0, "cache_read": 0}
        ev = [end_event(1, "[t] a", "haiku", "pass", tokens=tk)]
        s = self.stats(ev, pricing=pricing)["t"]
        want = pricing.cost(tk, "haiku")["total"]
        self.assertAlmostEqual(s["avg_cost"], want)
        self.assertAlmostEqual(want, 6.0)  # 1.00 + 5.00 per MTok in dashboard/pricing.json

    def test_days_window_and_unknown_status(self):
        ev = [end_event(1, "[t] old", "haiku", "pass", age_days=40),
              end_event(2, "[t] new", "haiku", "unknown", age_days=1)]
        s = self.stats(ev, days=30)["t"]
        self.assertEqual(s["tasks"], 1)
        self.assertEqual(s["attempts"], 0)  # unknown status is not counted towards rates
        self.assertEqual(self.stats(ev, days=60)["t"]["tasks"], 2)

    def test_hints_payload_shape(self):
        s = self.stats(self.batch("[bugfix] a", "sonnet", 5, 1))
        h = self.cli.hints_payload(s, NOW)
        json.dumps(h)
        self.assertIn("generated", h)
        self.assertEqual(h["types"]["bugfix"]["start"], "mid-worker")
        self.assertEqual(h["types"]["bugfix"]["samples"], 6)
        self.assertEqual(h["types"]["bugfix"]["pass_rate"], 0.83)


class StatsCli(FakeHome):
    def seed(self, events):
        write_logs(self.tt, events)

    def recent(self, n, label, tier, status):
        # use the real clock so the default 30 day window includes them
        e = end_event(n, label, tier, status, session="s%d" % n)
        e["ts"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
        return e

    def test_no_logs_exit_zero_friendly(self):
        p = self.run_cli("stats", "--home", self.tt)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("No finished tasks", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tt, "routing-hints.json")))

    def test_json_output_and_write_hints_atomic(self):
        self.seed([self.recent(i, "[bugfix] x", "sonnet", "pass") for i in range(6)])
        p = self.run_cli("stats", "--home", self.tt, "--json", "--write-hints")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["types"]["bugfix"]["recommended_tier"], "sonnet")
        path = os.path.join(self.tt, "routing-hints.json")
        with open(path, encoding="utf-8") as f:
            hints = json.load(f)
        self.assertEqual(hints["types"]["bugfix"]["start"], "mid-worker")
        self.assertEqual(hints["types"]["bugfix"]["samples"], 6)
        self.assertEqual(hints["types"]["bugfix"]["pass_rate"], 1.0)
        self.assertEqual(data["hints"], hints["types"])
        leftovers = [n for n in os.listdir(self.tt) if n.endswith(".tttmp")]
        self.assertEqual(leftovers, [])
        # rewriting replaces the file in place and stays valid
        p = self.run_cli("stats", "--home", self.tt, "--write-hints")
        self.assertEqual(p.returncode, 0)
        with open(path, encoding="utf-8") as f:
            json.load(f)

    def test_table_output(self):
        self.seed([self.recent(i, "[docs] x", "haiku", "pass") for i in range(5)])
        p = self.run_cli("stats", "--home", self.tt)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("docs", p.stdout)
        self.assertIn("fast-worker", p.stdout)

    def test_bad_days(self):
        p = self.run_cli("stats", "--home", self.tt, "--days", "0")
        self.assertEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()
