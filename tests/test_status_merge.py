import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dash_helpers import ROOT, PRICING, TZ, Clock, at, end, ev, start, toks, write  # noqa: F401  (sets sys.path)
from tokentier_dash.pricing import Pricing
from tokentier_dash.store import Store

sys.path.insert(0, os.path.join(ROOT, "kit", "hooks"))
import tokentier_log as tl  # noqa: E402
from test_log_hook import HOOK, Tmp, asst, read_lines, run_hook  # noqa: E402

DAY = "2026-10-04"


def sub_start(**kw):
    return ev("task_start", at(DAY, "13:51:10"), task_id="t1", status="running", tier="haiku",
              router_worker=True, label=None, **kw)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def task(self, events, tid="t1"):
        write(self.tmp, DAY, events)
        s = Store(self.tmp, Pricing(PRICING), clock=Clock(at(DAY, "15:00")), tz=TZ)
        s.refresh()
        return next(t for t in s.tasks({"from": "all"})["items"] if t["task_id"] == tid)


class TestMerge(StoreCase):
    def test_pass_then_weaker_unknown_keeps_pass(self):
        t = self.task([
            sub_start(),
            end("t1", at(DAY, "13:53:54"), status="pass", status_source="inferred", result="Long summary of work.",
                tokens=toks(10, 20)),
            end("t1", at(DAY, "13:53:59"), status="unknown", status_source=None, result="Short.",
                tokens=toks(10, 25), duration_ms=9000, model="claude-haiku-4-5-20251001")])
        self.assertEqual((t["status"], t["status_source"]), ("pass", "inferred"))
        self.assertEqual(t["tokens"]["output"], 25)          # tokens from the latest event
        self.assertEqual(t["duration_ms"], 9000)
        self.assertEqual(t["result"], "Short.")             # latest non-empty result
        self.assertTrue(t["ended_at"].startswith(DAY + "T13:53:59"))

    def test_unknown_then_explicit_wins(self):
        t = self.task([
            sub_start(),
            end("t1", at(DAY, "13:53:54"), status="unknown", status_source=None),
            end("t1", at(DAY, "13:53:59"), status="escalated", status_source="status_line")])
        self.assertEqual((t["status"], t["status_source"]), ("escalated", "status_line"))

    def test_explicit_not_downgraded_by_inferred_later_overrides_inferred_earlier(self):
        t = self.task([
            sub_start(),
            end("t1", at(DAY, "13:53:54"), status="pass", status_source="inferred"),
            end("t1", at(DAY, "13:53:59"), status="fail", status_source="status_line"),
            end("t1", at(DAY, "13:54:30"), status="pass", status_source="inferred")])
        self.assertEqual((t["status"], t["status_source"]), ("fail", "status_line"))

    def test_equal_timestamp_prefers_longer_result(self):
        d = at(DAY, "13:53:54")
        t = self.task([sub_start(), end("t1", d, result="a much longer result"), end("t1", d, result="short")])
        self.assertEqual(t["result"], "a much longer result")

    def test_label_first_non_null(self):
        t = self.task([
            sub_start(),
            end("t1", at(DAY, "13:53:54"), label=None, tool_use_id="tu1"),
            end("t1", at(DAY, "13:53:59"), label="Second", tool_use_id="tu2")])
        self.assertEqual((t["label"], t["tool_use_id"]), ("Second", "tu1"))

    def test_historic_duplicate_fixture(self):
        # shape of a real log: legacy events without status_source, strong pass then weak unknown
        t = self.task([
            sub_start(),
            end("t1", at(DAY, "13:53:54"), status="pass", result="Did the thing."),
            end("t1", at(DAY, "13:53:59"), status="unknown", result="Done.")])
        self.assertEqual(t["status"], "pass")
        self.assertIsNone(t["status_source"])

    def test_single_unknown_stays_unknown(self):
        t = self.task([sub_start(), end("t1", at(DAY, "13:53:54"), status="unknown")])
        self.assertEqual((t["status"], t["status_source"]), ("unknown", None))


class TestTaskUpdate(StoreCase):
    def upd(self, **kw):
        base = {"v": 1, "ts": at(DAY, "13:51:14").isoformat(timespec="milliseconds"), "event": "task_update",
                "task_id": "t1", "session_id": "s1", "label": None, "tool_use_id": None, "model": None,
                "agent_type": None}
        base.update(kw)
        return base

    def test_fills_missing(self):
        t = self.task([sub_start(), self.upd(label="Late", tool_use_id="tu9", model="claude-haiku-4-5-20251001",
                                             agent_type="fast-worker")])
        self.assertEqual((t["label"], t["tool_use_id"], t["model"], t["agent_type"]),
                         ("Late", "tu9", "claude-haiku-4-5-20251001", "fast-worker"))
        self.assertEqual(t["status"], "running")

    def test_fill_only(self):
        t = self.task([ev("task_start", at(DAY, "13:51:10"), task_id="t1", status="running", label="First",
                          tool_use_id="tuA", tier="haiku", router_worker=True),
                       self.upd(label="Other", tool_use_id="tuB")])
        self.assertEqual((t["label"], t["tool_use_id"]), ("First", "tuA"))

    def test_update_without_start_ignored_then_ok(self):
        write(self.tmp, DAY, [self.upd(label="Orphan")])
        s = Store(self.tmp, Pricing(PRICING), clock=Clock(at(DAY, "15:00")), tz=TZ)
        s.refresh()
        self.assertEqual(s.tasks({"from": "all"})["total"], 0)


class TestInference(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(tl.classify_report("All good, renamed 3 files."), ("pass", "inferred"))
        for marker in tl.NEGATIVE_MARKERS:
            self.assertEqual(tl.classify_report("Something %s happened." % marker.upper()), ("unknown", None), marker)
        self.assertEqual(tl.classify_report(""), ("unknown", None))
        self.assertEqual(tl.classify_report(None), ("unknown", None))
        self.assertEqual(tl.classify_report("   \n "), ("unknown", None))
        self.assertEqual(tl.classify_report("x\nSTATUS: done"), ("pass", "status_line"))
        self.assertEqual(tl.classify_report("x\nSTATUS: escalate"), ("escalated", "status_line"))
        self.assertEqual(tl.classify_report("x\nSTATUS: blocked"), ("fail", "status_line"))
        # explicit STATUS wins over a negative marker in the body
        self.assertEqual(tl.classify_report("Could not at first, fixed later.\nSTATUS: done"), ("pass", "status_line"))


class TestHookEvents(Tmp):
    def stop(self, msg, agent_type="fast-worker", main=None, agent_id="a1", now=None):
        main = main or self.make_subagent(agent_id, [asst("m1", "2026-10-03T21:00:01.000Z", 5, 6, 0, 0)])
        env = {"TOKENTIER_NOW": now} if now else None
        return run_hook("SubagentStop", json.dumps({
            "session_id": "sess1", "transcript_path": main, "agent_id": agent_id,
            "agent_type": agent_type, "last_assistant_message": msg}), self.home, env)

    def test_inferred_and_explicit_fields(self):
        self.stop("Renamed the helpers.")
        (ev1,) = read_lines(self.home)
        self.assertEqual((ev1["status"], ev1["status_source"]), ("pass", "inferred"))

    def test_non_router_inferred_and_unknown(self):
        main = self.make_subagent("a2", [asst("m1", "2026-10-03T21:00:01.000Z", 5, 6, 0, 0)])
        self.stop("It is fine.", agent_type="general-purpose", agent_id="a2", main=main)
        self.stop("Hit an error: boom", agent_type="general-purpose", agent_id="a3", main=main)
        evs = {e["task_id"]: e for e in read_lines(self.home)}
        self.assertEqual((evs["a2"]["status"], evs["a2"]["status_source"]), ("pass", "inferred"))
        self.assertEqual((evs["a3"]["status"], evs["a3"]["status_source"]), ("unknown", None))

    def test_duplicate_weaker_skipped_stronger_appended(self):
        main = self.make_subagent("a1", [asst("m1", "2026-10-03T21:00:01.000Z", 5, 6, 0, 0)])
        self.stop("Renamed the helpers across the repo.", main=main)
        self.stop("Done.", main=main)                      # same status, same tokens, shorter result
        self.stop("", main=main)                           # weaker (unknown), nothing new
        self.assertEqual(len(read_lines(self.home)), 1)
        self.stop("Renamed the helpers.\nSTATUS: done", main=main)   # stronger: explicit status line
        evs = read_lines(self.home)
        self.assertEqual(len(evs), 2)
        self.assertEqual((evs[1]["status"], evs[1]["status_source"]), ("pass", "status_line"))

    def test_duplicate_with_more_tokens_appended(self):
        main = self.make_subagent("a1", [asst("m1", "2026-10-03T21:00:01.000Z", 5, 6, 0, 0)])
        self.stop("Did it.", main=main)
        with open(os.path.join(os.path.dirname(main), "sess1", "subagents", "agent-a1.jsonl"), "a") as f:
            f.write(json.dumps(asst("m2", "2026-10-03T21:00:05.000Z", 50, 60, 0, 0)) + "\n")
        self.stop("Did it.", main=main)
        self.assertEqual(len(read_lines(self.home)), 2)

    def test_escalation_not_duplicated(self):
        main = self.make_subagent("a1", [asst("m1", "2026-10-03T21:00:01.000Z", 5, 6, 0, 0)])
        self.stop("ESCALATE: too hard\nSTATUS: escalate", main=main)
        self.stop("ESCALATE: too hard\nSTATUS: escalate", main=main)
        evs = read_lines(self.home)
        self.assertEqual([e["event"] for e in evs], ["task_end", "escalation"])


class TestLabelUpdater(Tmp):
    def start_payload(self, main):
        return json.dumps({"session_id": "sess1", "transcript_path": main, "agent_id": "a1",
                           "agent_type": "fast-worker", "cwd": "/x/proj"})

    def test_meta_two_seconds_late(self):
        """Meta appears at +2s; label should be sent within ~2.5s of that, then model separately."""
        main = self.make_subagent("a1", [asst("m1", "2026-10-03T21:00:01.000Z", 1, 1, 0, 0)], meta=False)
        mp = os.path.join(self.tmp, "proj", "sess1", "subagents", "agent-a1.meta.json")
        t0 = time.time()
        r = run_hook("SubagentStart", self.start_payload(main), self.home)
        self.assertLess(time.time() - t0, 1.5)   # generous for slow CI; the hook itself polls ~0.4 s
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual([e["event"] for e in read_lines(self.home)], ["task_start"])
        time.sleep(2)
        meta_create_time = time.time()
        with open(mp, "w") as f:
            json.dump({"agentType": "fast-worker", "description": "Late label", "toolUseId": "toolu_7"}, f)

        # Wait for label to appear (should be within ~2.5s of meta creation)
        deadline = meta_create_time + 2.5
        ups = []
        while time.time() < deadline:
            ups = [e for e in read_lines(self.home) if e["event"] == "task_update"]
            if ups:
                break
            time.sleep(0.2)

        self.assertGreaterEqual(len(ups), 1, "Label should arrive promptly")
        u = ups[0]
        self.assertEqual((u["v"], u["task_id"], u["session_id"], u["label"], u["tool_use_id"], u["agent_type"]),
                         (1, "a1", "sess1", "Late label", "toolu_7", "fast-worker"))

        # Wait for model update (may come as a second task_update or may be merged)
        deadline = time.time() + 5
        ups_final = []
        while time.time() < deadline and len(ups_final) < 2:
            ups_final = [e for e in read_lines(self.home) if e["event"] == "task_update"]
            time.sleep(0.1)

        # Check that model appears somewhere (either in first update or second)
        model_found = False
        for u in ups_final:
            if u.get("model") == "claude-haiku-4-5-20251001":
                model_found = True
                break
        self.assertTrue(model_found, "Model should eventually appear in task_update(s)")

    def test_label_appears_promptly_before_model(self):
        """Label should arrive within ~2.5s of meta creation, not waiting 4s for model."""
        main = self.make_subagent("a1", [], meta=False)
        mp = os.path.join(self.tmp, "proj", "sess1", "subagents", "agent-a1.meta.json")
        t0 = time.time()
        r = run_hook("SubagentStart", self.start_payload(main), self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        # Meta file appears at +1s
        time.sleep(1)
        meta_create_time = time.time()
        with open(mp, "w") as f:
            json.dump({"agentType": "fast-worker", "description": "Prompt label", "toolUseId": "tu_quick"}, f)

        # Label should appear within ~2.5s of meta creation
        deadline = meta_create_time + 2.5
        ups = []
        while time.time() < deadline:
            ups = [e for e in read_lines(self.home) if e["event"] == "task_update"]
            if ups:
                break
            time.sleep(0.1)

        self.assertEqual(len(ups), 1)
        u = ups[0]
        self.assertEqual((u["label"], u["tool_use_id"], u["agent_type"]),
                        ("Prompt label", "tu_quick", "fast-worker"))
        # Model should be None since transcript hasn't appeared yet
        self.assertIsNone(u["model"])
        label_arrival_time = time.time() - meta_create_time
        self.assertLess(label_arrival_time, 2.5,
                       f"Label arrived {label_arrival_time:.1f}s after meta, expected < 2.5s")

    def test_model_only_update_after_label(self):
        """Model should arrive in a second task_update if it comes after label."""
        main = self.make_subagent("a1", [], meta=False)
        mp = os.path.join(self.tmp, "proj", "sess1", "subagents", "agent-a1.meta.json")
        tp = os.path.join(self.tmp, "proj", "sess1", "subagents", "agent-a1.jsonl")

        r = run_hook("SubagentStart", self.start_payload(main), self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

        # Meta file appears first (with label)
        time.sleep(0.5)
        with open(mp, "w") as f:
            json.dump({"agentType": "fast-worker", "description": "Test label", "toolUseId": "tu_test"}, f)

        # Wait for label update
        deadline = time.time() + 5
        ups = []
        while time.time() < deadline and len(ups) < 1:
            ups = [e for e in read_lines(self.home) if e["event"] == "task_update"]
            time.sleep(0.1)
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0]["label"], "Test label")
        self.assertIsNone(ups[0]["model"])

        # Transcript appears later with model
        time.sleep(0.5)
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        with open(tp, "w") as f:
            f.write(json.dumps(asst("m1", "2026-10-03T21:00:01.000Z", 5, 6, 0, 0)) + "\n")

        # Wait for model update
        deadline = time.time() + 5
        ups_after = []
        while time.time() < deadline and len(ups_after) < 2:
            ups_after = [e for e in read_lines(self.home) if e["event"] == "task_update"]
            time.sleep(0.1)

        self.assertEqual(len(ups_after), 2)
        # First should have label, no model
        self.assertEqual(ups_after[0]["label"], "Test label")
        self.assertIsNone(ups_after[0]["model"])
        # Second should have model, no label
        self.assertEqual(ups_after[1]["model"], "claude-haiku-4-5-20251001")
        self.assertIsNone(ups_after[1]["label"])

    def test_meta_never_appears_child_exits(self):
        main = self.make_subagent("a1", [], meta=False)
        env = dict(os.environ, TOKENTIER_HOME=self.home, TOKENTIER_LABEL_TIMEOUT="1")
        os.makedirs(self.home)
        t0 = time.time()
        r = subprocess.run([sys.executable, HOOK, "_label", "sess1", "a1", main, "/x/proj", "fast-worker"],
                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(r.stdout, b"")
        self.assertEqual(read_lines(self.home), [])

    def test_label_worker_errors_logged_not_raised(self):
        env = dict(os.environ, TOKENTIER_HOME=self.home)
        os.makedirs(self.home)
        r = subprocess.run([sys.executable, HOOK, "_label"], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
