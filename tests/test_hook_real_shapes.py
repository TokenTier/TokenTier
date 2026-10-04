import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_log_hook import Tmp, run_hook, read_lines, asst  # noqa: E402


def final(content, mid="mf"):
    return {"type": "assistant", "timestamp": "2026-10-03T21:00:09.000Z", "message": {
        "id": mid, "model": "claude-sonnet-5-5", "content": content,
        "usage": {"input_tokens": 1, "output_tokens": 2,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}


def handback(inp):
    return final([{"type": "tool_use", "id": "t1", "name": "SubagentHandback", "input": inp}])


class TestRealShapes(Tmp):
    def stop(self, main, agent_type="mid-worker", msg="", agent_id="a1"):
        return run_hook("SubagentStop", json.dumps({
            "session_id": "sess1", "transcript_path": main, "agent_id": agent_id,
            "agent_type": agent_type, "last_assistant_message": msg}), self.home)

    def test_handback_report(self):
        main = self.make_subagent("a1", [handback({"report": "# Title\n\nPort check fixed. More.\n\nSTATUS: done"})])
        r = self.stop(main)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        ev = read_lines(self.home)[0]
        self.assertEqual(ev["status"], "pass")
        self.assertEqual(ev["result"], "Port check fixed.")

    def test_handback_other_field_name(self):
        main = self.make_subagent("a1", [handback({"message": "Done it.\n\nSTATUS: done"})])
        self.stop(main)
        self.assertEqual(read_lines(self.home)[0]["status"], "pass")

    def test_text_only_ending(self):
        main = self.make_subagent("a1", [final([{"type": "text", "text": "All good.\nSTATUS: done"}])])
        self.stop(main)
        self.assertEqual(read_lines(self.home)[0]["status"], "pass")

    def test_handback_escalate_emits_escalation(self):
        main = self.make_subagent("a1", [handback({"report": "ESCALATE: needs design call\nSTATUS: escalate"})])
        self.stop(main)
        evs = read_lines(self.home)
        self.assertEqual([e["event"] for e in evs], ["task_end", "escalation"])
        self.assertEqual(evs[0]["status"], "escalated")
        self.assertEqual(evs[1]["reason"], "needs design call")

    def test_payload_message_unchanged(self):
        main = self.make_subagent("a1", [handback({"report": "other\nSTATUS: escalate"})])
        self.stop(main, msg="Fine.\nSTATUS: done")
        ev = read_lines(self.home)[0]
        self.assertEqual((ev["status"], ev["result"]), ("pass", "Fine. STATUS: done"))

    def test_unknown_without_report(self):
        main = self.make_subagent("a1", [asst("m1", "2026-10-03T21:00:01.000Z", 1, 1, 0, 0)])
        self.stop(main)
        self.assertEqual(read_lines(self.home)[0]["status"], "unknown")

    def test_noise_stop_ignored(self):
        main = os.path.join(self.tmp, "proj", "sess1.jsonl")
        t0 = time.time()
        r = self.stop(main, agent_type="", msg="commit this", agent_id="zz")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(read_lines(self.home), [])
        with open(os.path.join(self.home, "hook-ignored.log")) as f:
            rec = json.loads(f.readline())
        self.assertEqual((rec["event"], rec["agent_id"]), ("SubagentStop", "zz"))
        self.assertLess(time.time() - t0, 5)

    def test_noise_start_ignored(self):
        main = os.path.join(self.tmp, "proj", "sess1.jsonl")
        r = run_hook("SubagentStart", json.dumps({
            "session_id": "sess1", "transcript_path": main, "agent_id": "zz", "agent_type": ""}), self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(read_lines(self.home), [])
        self.assertTrue(os.path.exists(os.path.join(self.home, "hook-ignored.log")))

    def test_real_agent_missing_transcript_logged(self):
        main = os.path.join(self.tmp, "proj", "sess1.jsonl")
        self.stop(main, agent_type="mid-worker", msg="x\nSTATUS: done")
        ev = read_lines(self.home)[0]
        self.assertEqual(ev["event"], "task_end")
        self.assertIsNone(ev["tokens_total"])

    def _start(self, main):
        return run_hook("SubagentStart", json.dumps({
            "session_id": "sess1", "transcript_path": main, "agent_id": "a1",
            "agent_type": "mid-worker"}), self.home)

    def test_meta_appears_late(self):
        main = self.make_subagent("a1", [], meta=False)
        mp = os.path.join(self.tmp, "proj", "sess1", "subagents", "agent-a1.meta.json")

        def later():
            time.sleep(0.5)
            with open(mp, "w") as f:
                json.dump({"description": "Late label", "toolUseId": "toolu_9"}, f)
        th = threading.Thread(target=later)
        th.start()
        r = self._start(main)
        th.join()
        self.assertEqual(r.returncode, 0)
        ev = read_lines(self.home)[0]
        self.assertIsNone(ev["label"])  # the hook no longer waits; a detached updater fills it in
        deadline = time.time() + 8
        ups = []
        while time.time() < deadline and not ups:
            time.sleep(0.2)
            ups = [e for e in read_lines(self.home) if e["event"] == "task_update"]
        self.assertTrue(ups)
        self.assertEqual((ups[0]["label"], ups[0]["tool_use_id"]), ("Late label", "toolu_9"))

    def test_meta_never_appears(self):
        main = self.make_subagent("a1", [], meta=False)
        t0 = time.time()
        r = self._start(main)
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(r.returncode, 0)
        ev = read_lines(self.home)[0]
        self.assertIsNone(ev["label"])


if __name__ == "__main__":
    unittest.main()
