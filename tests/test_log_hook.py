import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "kit", "hooks", "tokentier_log.py")
sys.path.insert(0, os.path.join(ROOT, "kit", "hooks"))
import tokentier_log as tl  # noqa: E402


def run_hook(event, stdin, home, extra_env=None):
    env = dict(os.environ, TOKENTIER_HOME=home, TOKENTIER_LABEL_TIMEOUT="4")
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, HOOK, event], input=stdin, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)


def read_lines(home):
    out = []
    for p in sorted(glob.glob(os.path.join(home, "logs", "*.jsonl"))):
        with open(p) as f:
            out += [json.loads(l) for l in f if l.strip()]
    return out


def asst(mid, ts, inp, out, cc, cr, model="claude-haiku-4-5-20251001"):
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": model,
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}}}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_subagent(self, agent_id, lines, meta=True):
        main = os.path.join(self.tmp, "proj", "sess1.jsonl")
        d = os.path.join(self.tmp, "proj", "sess1", "subagents")
        os.makedirs(d)
        with open(os.path.join(d, "agent-%s.jsonl" % agent_id), "w") as f:
            for l in lines:
                f.write(json.dumps(l) + "\n")
        if meta:
            with open(os.path.join(d, "agent-%s.meta.json" % agent_id), "w") as f:
                json.dump({"agentType": "fast-worker", "description": "Phase 0 scaffold",
                           "toolUseId": "toolu_1", "spawnDepth": 1}, f)
        return main


class TestUsage(Tmp):
    def test_dedupe_last_per_message_id(self):
        main = self.make_subagent("a1", [
            {"type": "user", "timestamp": "2026-10-03T21:00:00.000Z", "message": {}},
            asst("m1", "2026-10-03T21:00:01.000Z", 1, 5, 100, 1000),
            asst("m1", "2026-10-03T21:00:02.000Z", 2, 50, 100, 1000),
            asst("m2", "2026-10-03T21:00:03.500Z", 3, 7, 10, 20),
            asst("m2", "2026-10-03T21:00:10.000Z", 4, 70, 10, 20),
        ])
        u = tl.parse_transcript_usage(tl.subagent_paths(main, "a1")[0])
        self.assertEqual((u["input"], u["output"], u["cache_creation"], u["cache_read"]),
                         (6, 120, 110, 1020))
        self.assertEqual(u["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(u["duration_ms"], 10000)

    def test_synthetic_transcript_structure(self):
        """Test parsing a synthetic subagent transcript with deduplication."""
        main = self.make_subagent("a_synth", [
            {"type": "user", "timestamp": "2026-10-03T21:00:00.000Z", "message": {}},
            asst("msg_1", "2026-10-03T21:00:01.000Z", 100, 500, 5000, 50000),
            asst("msg_1", "2026-10-03T21:00:02.000Z", 150, 750, 5000, 50000),  # Updated msg_1
            asst("msg_2", "2026-10-03T21:00:03.000Z", 68, 653, 63000, 1513600),
            asst("msg_2", "2026-10-03T21:00:10.000Z", 218, 1153, 63069, 1513669),  # Updated msg_2 (final)
        ])
        u = tl.parse_transcript_usage(tl.subagent_paths(main, "a_synth")[0])
        # Last values per message_id: msg_1=(150,750,5000,50000), msg_2=(218,1153,63069,1513669)
        self.assertEqual((u["input"], u["output"], u["cache_creation"], u["cache_read"]),
                         (368, 1903, 68069, 1563669))
        self.assertEqual(u["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(u["duration_ms"], 10000)


class TestPure(unittest.TestCase):
    def test_status(self):
        self.assertEqual(tl.parse_status("ok\nSTATUS: done", True), "pass")
        self.assertEqual(tl.parse_status("x\nstatus: Escalate\n", True), "escalated")
        self.assertEqual(tl.parse_status("x\nSTATUS: blocked", False), "fail")
        self.assertEqual(tl.parse_status("no status", False), "pass")
        self.assertEqual(tl.parse_status("no status", True), "pass")  # inferred, see test_status_inference
        self.assertEqual(tl.parse_status(None, True), "unknown")

    def test_tier(self):
        self.assertEqual(tl.tier_for("fast-worker", "claude-opus-4"), "haiku")
        self.assertEqual(tl.tier_for("mid-worker"), "sonnet")
        self.assertEqual(tl.tier_for("deep-worker"), "opus")
        self.assertEqual(tl.tier_for("Explore", "claude-sonnet-5-5"), "sonnet")
        self.assertEqual(tl.tier_for("general-purpose", "claude-opus-4-7"), "opus")
        self.assertEqual(tl.tier_for("general-purpose", None), "unknown")

    @unittest.skipUnless(hasattr(tl.time, "tzset"), "needs time.tzset (TZ env); not available on Windows")
    def test_local_iso(self):
        os.environ["TZ"] = "Asia/Karachi"
        try:
            s = tl.local_iso(tl.parse_ts("2026-10-03T21:03:49.243Z"))
        finally:
            del os.environ["TZ"]
            tl._tzset()
        self.assertEqual(s, "2026-10-04T02:03:49.243+05:00")


class TestHook(Tmp):
    @unittest.skipUnless(hasattr(tl.time, "tzset"), "needs time.tzset (TZ env); not available on Windows")
    def test_midnight_file_selection(self):
        env = {"TZ": "Asia/Karachi"}
        p = json.dumps({"session_id": "s", "cwd": "/x/proj"})
        # 23:59:59 local = 18:59:59Z ; 00:00:01 local next day = 19:00:01Z
        run_hook("SessionStart", p, self.home, dict(env, TOKENTIER_NOW="2026-10-03T18:59:59Z"))
        run_hook("SessionStart", p, self.home, dict(env, TOKENTIER_NOW="2026-10-03T19:00:01Z"))
        files = sorted(os.listdir(os.path.join(self.home, "logs")))
        self.assertEqual(files, ["2026-10-03.jsonl", "2026-10-04.jsonl"])

    def test_concurrent_appends(self):
        procs = []
        for i in range(12):
            env = dict(os.environ, TOKENTIER_HOME=self.home)
            payload = json.dumps({"session_id": "s%d" % i, "cwd": "/x/p", "agent_id": "a%d" % i,
                                  "agent_type": "fast-worker", "last_assistant_message": "y" * 5000})
            p = subprocess.Popen([sys.executable, HOOK, "SubagentStop"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, env=env, universal_newlines=True)
            p.stdin.write(payload)
            p.stdin.close()
            procs.append(p)
        for p in procs:
            self.assertEqual(p.wait(), 0)
            p.stdout.close()
        lines = read_lines(self.home)
        self.assertEqual(len(lines), 12)
        self.assertEqual(len({l["task_id"] for l in lines}), 12)

    def test_garbage_stdin(self):
        r = run_hook("SubagentStop", "{not json!!", self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertTrue(os.path.exists(os.path.join(self.home, "hook-errors.log")))

    def test_unknown_event_ignored(self):
        r = run_hook("Whatever", "{}", self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(read_lines(self.home), [])

    def test_missing_transcript(self):
        payload = json.dumps({"session_id": "s", "cwd": "/x/proj", "agent_id": "zz",
                              "agent_type": "mid-worker", "transcript_path": "/nonexistent/s.jsonl",
                              "last_assistant_message": "done\nSTATUS: done"})
        r = run_hook("SubagentStop", payload, self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        (ev,) = read_lines(self.home)
        self.assertEqual(ev["event"], "task_end")
        self.assertIsNone(ev["model"])
        self.assertIsNone(ev["tokens_total"])
        self.assertEqual(ev["tier"], "sonnet")
        self.assertEqual(ev["status"], "pass")

    def test_full_stop_and_escalation(self):
        main = self.make_subagent("a9", [
            asst("m1", "2026-10-03T21:00:00.000Z", 1, 2, 3, 4),
            asst("m1", "2026-10-03T21:00:05.000Z", 10, 20, 30, 40),
        ])
        payload = json.dumps({"session_id": "s", "cwd": "/x/proj", "agent_id": "a9",
                              "agent_type": "fast-worker", "transcript_path": main,
                              "last_assistant_message": "too hard\nESCALATE: needs design\nSTATUS: escalate"})
        r = run_hook("SubagentStop", payload, self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        evs = read_lines(self.home)
        self.assertEqual([e["event"] for e in evs], ["task_end", "escalation"])
        te = evs[0]
        self.assertEqual(te["tokens"], {"input": 10, "output": 20, "cache_creation": 30, "cache_read": 40})
        self.assertEqual(te["tokens_total"], 100)
        self.assertEqual(te["duration_ms"], 5000)
        self.assertEqual(te["label"], "Phase 0 scaffold")
        self.assertEqual(te["status"], "escalated")
        self.assertEqual(evs[1]["from_tier"], "haiku")
        self.assertIn("needs design", evs[1]["reason"])

    def test_subagent_start(self):
        main = self.make_subagent("a3", [])
        payload = json.dumps({"session_id": "s", "cwd": "/x/proj", "agent_id": "a3",
                              "agent_type": "deep-worker", "transcript_path": main})
        run_hook("SubagentStart", payload, self.home, {"CLAUDE_PROJECT_DIR": "/p/myproj"})
        (ev,) = read_lines(self.home)
        self.assertEqual((ev["event"], ev["tier"], ev["status"], ev["project"], ev["label"]),
                         ("task_start", "opus", "running", "myproj", "Phase 0 scaffold"))


if __name__ == "__main__":
    unittest.main()
