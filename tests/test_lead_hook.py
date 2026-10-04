"""Lead (main session) usage logging: Stop / SessionEnd flushes of the MAIN transcript."""
import json
import os
import subprocess
import sys
import time
import unittest

from test_log_hook import HOOK, Tmp, read_lines, run_hook

SID = "sess-lead-1"
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"


def line(mid, model, i, o, cc, cr, ts="2026-10-04T10:00:00.000Z", side=False, typ="assistant"):
    d = {"type": typ, "isSidechain": side, "timestamp": ts, "message": {
        "id": mid, "model": model, "role": "assistant",
        "usage": {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cc,
                  "cache_read_input_tokens": cr, "server_tool_use": {"web_search_requests": 0}}}}
    return json.dumps(d) + "\n"


class LeadBase(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        self.tpath = os.path.join(self.tmp, "main.jsonl")
        open(self.tpath, "w").close()
        self.proj = os.path.join(self.tmp, "myproj")

    def append(self, *lines, raw=None):
        with open(self.tpath, "ab") as f:
            for l in lines:
                f.write(l.encode())
            if raw:
                f.write(raw)

    def payload(self, event, **kw):
        d = {"session_id": SID, "transcript_path": self.tpath, "cwd": self.proj, "hook_event_name": event}
        d.update(kw)
        return json.dumps(d)

    def hook(self, event, **kw):
        r = run_hook(event, self.payload(event, **kw), self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        return r

    def start(self, **kw):
        self.hook("SessionStart", source="startup", **kw)

    def lead(self):
        return [e for e in read_lines(self.home) if e["event"] == "lead_usage"]

    def state(self):
        with open(os.path.join(self.home, "state", SID + ".json")) as f:
            return json.load(f)

    def errors(self):
        p = os.path.join(self.home, "hook-errors.log")
        return open(p).read() if os.path.exists(p) else ""


class LeadFlushTest(LeadBase):
    def test_single_flush_event_shape(self):
        self.start()
        self.append(line("m1", OPUS, 3, 100, 1000, 5000, ts="2026-10-04T10:00:00.000Z"),
                    line("m2", OPUS, 2, 50, 0, 9000, ts="2026-10-04T10:00:30.000Z"),
                    line("m3", SONNET, 1, 10, 0, 0, ts="2026-10-04T10:01:00.000Z"))
        self.hook("Stop")
        ev = self.lead()
        self.assertEqual([e["model"] for e in ev], [OPUS, SONNET])
        o = ev[0]
        self.assertEqual((o["v"], o["event"], o["session_id"], o["project"], o["project_path"]),
                         (1, "lead_usage", SID, "myproj", self.proj))
        self.assertEqual((o["user_id"], o["workspace_id"], o["tier"]), (None, None, "opus"))
        self.assertEqual(o["tokens"], {"input": 5, "output": 150, "cache_creation": 1000, "cache_read": 14000})
        self.assertEqual((o["tokens_total"], o["turns"]), (15155, 2))
        self.assertLess(o["from_ts"], o["to_ts"])
        self.assertEqual(ev[1]["tier"], "sonnet")
        self.assertEqual(self.errors(), "")
        # nothing new -> nothing logged
        self.hook("Stop")
        self.assertEqual(len(self.lead()), 2)

    def test_two_flushes_message_split_across_them(self):
        self.start()
        # streaming: the same message id appears on several lines with growing usage
        self.append(line("m1", OPUS, 3, 10, 1000, 5000), line("m1", OPUS, 3, 40, 1000, 5000))
        self.hook("Stop")
        self.append(line("m1", OPUS, 3, 120, 1000, 5000), line("m2", OPUS, 2, 7, 0, 100))
        self.hook("Stop")
        ev = self.lead()
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0]["tokens"]["output"], 40)
        self.assertEqual(ev[0]["turns"], 1)
        # second flush: only the growth of m1 (output +80) plus brand new m2; input/cache not repeated
        self.assertEqual(ev[1]["tokens"], {"input": 2, "output": 87, "cache_creation": 0, "cache_read": 100})
        self.assertEqual(ev[1]["turns"], 1)  # m1 was already counted as a turn; m2 is the new one
        tot = {k: sum(e["tokens"][k] for e in ev) for k in ev[0]["tokens"]}
        self.assertEqual(tot, {"input": 5, "output": 127, "cache_creation": 1000, "cache_read": 5100})

    def test_sidechain_and_non_assistant_ignored(self):
        self.start()
        self.append(line("s1", OPUS, 9, 9, 9, 9, side=True), line("u1", OPUS, 9, 9, 9, 9, typ="user"),
                    line("m1", OPUS, 1, 1, 1, 1), line("syn", "<synthetic>", 0, 0, 0, 0),
                    json.dumps({"type": "assistant", "message": {"id": "nousage", "model": OPUS}}) + "\n")
        self.hook("Stop")
        ev = self.lead()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["tokens_total"], 4)

    def test_partial_trailing_line_not_consumed(self):
        self.start()
        full = line("m2", OPUS, 1, 20, 0, 0)
        self.append(line("m1", OPUS, 1, 10, 0, 0), raw=full[:25].encode())
        self.hook("Stop")
        self.assertEqual([e["tokens"]["output"] for e in self.lead()], [10])
        size_after_m1 = len(line("m1", OPUS, 1, 10, 0, 0).encode())
        self.assertEqual(self.state()["offset"], size_after_m1)
        self.append(raw=full[25:].encode())
        self.hook("Stop")
        self.assertEqual([e["tokens"]["output"] for e in self.lead()], [10, 20])
        self.assertEqual(self.errors(), "")

    def test_history_skipped_when_hooks_installed_mid_session(self):
        self.append(line("old1", OPUS, 1, 1000, 0, 0), line("old2", OPUS, 1, 1000, 0, 0))
        self.hook("Stop")  # no SessionStart was seen -> no state file
        self.assertEqual(self.lead(), [])
        st = self.state()
        self.assertTrue(st["history_skipped"])
        self.assertEqual(st["offset"], os.path.getsize(self.tpath))
        self.append(line("new1", OPUS, 1, 5, 0, 0))
        self.hook("Stop")
        ev = self.lead()
        self.assertEqual([e["tokens"]["output"] for e in ev], [5])

    def test_session_start_primes_offset_zero_and_logs_first_turn(self):
        self.append(line("m0", OPUS, 1, 3, 0, 0))  # written before our hook ran, same session
        self.start()
        self.assertEqual(self.state(), {"offset": 0, "emitted": {}, "history_skipped": False})
        self.hook("Stop")
        self.assertEqual(self.lead()[0]["tokens"]["output"], 3)

    def test_resume_keeps_state_and_resume_without_state_skips_history(self):
        self.start()
        self.append(line("m1", OPUS, 1, 10, 0, 0))
        self.hook("Stop")
        before = self.state()
        self.hook("SessionStart", source="resume")
        self.hook("SessionStart", source="compact")
        self.assertEqual(self.state(), before)
        # a resumed session we never saw: earlier transcript content is history, not new spend
        os.unlink(os.path.join(self.home, "state", SID + ".json"))
        self.hook("SessionStart", source="resume")
        self.assertTrue(self.state()["history_skipped"])
        self.hook("Stop")
        self.assertEqual(len(self.lead()), 1)

    def test_session_end_flushes_before_session_end_event(self):
        self.start()
        self.append(line("m1", OPUS, 1, 10, 0, 0))
        self.hook("SessionEnd", reason="other")
        kinds = [e["event"] for e in read_lines(self.home)]
        self.assertEqual(kinds, ["session_start", "lead_usage", "session_end"])

    def test_session_end_still_logged_when_flush_fails(self):
        self.start()
        self.append(line("m1", OPUS, 1, 10, 0, 0))
        r = run_hook("SessionEnd", self.payload("SessionEnd", transcript_path=os.path.join(self.tmp, "gone.jsonl")),
                     self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual([e["event"] for e in read_lines(self.home)], ["session_start", "session_end"])

    def test_corrupt_state_is_treated_as_new_session(self):
        self.start()
        self.append(line("m1", OPUS, 1, 10, 0, 0))
        with open(os.path.join(self.home, "state", SID + ".json"), "w") as f:
            f.write("{not json")
        self.hook("Stop")
        self.assertEqual(self.lead(), [])
        self.assertTrue(self.state()["history_skipped"])

    def test_missing_transcript_or_payload_is_harmless(self):
        for payload in ("", "{}", json.dumps({"session_id": SID}),
                        json.dumps({"session_id": SID, "transcript_path": "/nonexistent/x.jsonl"})):
            r = run_hook("Stop", payload, self.home)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(self.lead(), [])

    def test_emitted_is_capped(self):
        self.start()
        n = 400
        self.append(*[line("m%d" % i, OPUS, 1, 1, 0, 0) for i in range(n)])
        self.hook("Stop")
        st = self.state()
        self.assertEqual(len(st["emitted"]), 300)
        self.assertIn("m%d" % (n - 1), st["emitted"])
        self.assertNotIn("m0", st["emitted"])
        self.assertEqual(self.lead()[0]["tokens"]["input"], n)
        self.assertEqual(self.lead()[0]["turns"], n)

    def test_state_files_pruned_when_old(self):
        sd = os.path.join(self.home, "state")
        os.makedirs(sd)
        old = os.path.join(sd, "old-session.json")
        with open(old, "w") as f:
            f.write("{}")
        t = time.time() - 30 * 86400
        os.utime(old, (t, t))
        self.start()
        self.assertFalse(os.path.exists(old))

    def test_concurrent_stop_hooks_do_not_corrupt_state_or_double_count(self):
        self.start()
        lines = [line("m%d" % i, OPUS, 1, 10 + i, 100, 1000) for i in range(300)]
        self.append(*lines)
        env = dict(os.environ, TOKENTIER_HOME=self.home)
        env.pop("CLAUDE_PROJECT_DIR", None)
        procs = [subprocess.Popen([sys.executable, HOOK, "Stop"], stdin=subprocess.PIPE, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                 for _ in range(2)]
        for p in procs:
            out, _ = p.communicate(self.payload("Stop"))
            self.assertEqual((p.returncode, out), (0, ""))
        ev = self.lead()
        self.assertEqual(sum(e["tokens"]["input"] for e in ev), 300)
        self.assertEqual(sum(e["tokens"]["output"] for e in ev), sum(10 + i for i in range(300)))
        self.assertEqual(self.state()["offset"], os.path.getsize(self.tpath))
        self.assertEqual(self.errors(), "")
        # state file is always whole JSON
        self.assertIsInstance(self.state()["emitted"], dict)


class LeadHookOtherEventsTest(LeadBase):
    def test_stop_never_prints_and_ignores_other_events(self):
        r = run_hook("Notification", self.payload("Notification"), self.home)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(read_lines(self.home), [])


if __name__ == "__main__":
    unittest.main()
