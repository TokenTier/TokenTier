"""uninstall cleans TokenTier's internal files in ~/.tokentier; --purge removes the whole folder."""
import json
import os
import subprocess
import sys
import unittest

from tt_helpers import FakeHome, snapshot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "kit", "hooks", "tokentier_log.py")
SID = "sess-cleanup-1"


class UninstallCleanup(FakeHome):
    def setUp(self):
        FakeHome.setUp(self)
        self.write(os.path.join(self.claude, "settings.json"), '{\n  "theme": "dark"\n}\n')
        self.write(os.path.join(self.claude, "CLAUDE.md"), "# mine\n")
        self.orig = snapshot(self.claude)
        self.install("--no-service")

    def fire(self, event):
        tp = os.path.join(self.tmp, "main.jsonl")
        open(tp, "a").close()
        payload = json.dumps({"session_id": SID, "transcript_path": tp, "cwd": self.tmp,
                              "hook_event_name": event, "source": "startup"})
        r = subprocess.run([sys.executable, HOOK, event], input=payload, env=self.env(TOKENTIER_HOME=self.tt),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def make_junk(self):
        self.fire("SessionStart")
        self.fire("Stop")
        self.assertTrue(os.listdir(os.path.join(self.tt, "state")))
        for fn in ("dashboard.log", "hook-errors.log", "hook-ignored.log", "routing-hints.json",
                   "tokentier-dashboard-task.xml"):
            self.write(os.path.join(self.tt, fn), "x")
        self.write(os.path.join(self.tt, "logs", "2026-10-04.jsonl"), '{"a":1}\n')

    def test_plain_uninstall_removes_internal_files_keeps_logs(self):
        self.make_junk()
        p = self.uninstall()
        self.assertFalse(os.path.exists(os.path.join(self.tt, "state")))
        for fn in ("dashboard.log", "hook-errors.log", "hook-ignored.log", "routing-hints.json",
                   "tokentier-dashboard-task.xml", "install-manifest.json", "app"):
            self.assertFalse(os.path.exists(os.path.join(self.tt, fn)), fn)
        self.assertEqual(os.listdir(self.tt), ["logs"])
        self.assertTrue(os.path.isfile(os.path.join(self.tt, "logs", "2026-10-04.jsonl")))
        self.assertEqual(snapshot(self.claude), self.orig)
        p2 = self.uninstall(check=False)  # idempotent: nothing recorded any more, no traceback
        self.assertNotIn("Traceback", p2.stderr)
        self.assertEqual(os.listdir(self.tt), ["logs"])

    def test_plain_uninstall_without_logs_removes_home(self):
        self.fire("SessionStart")
        self.assertTrue(os.path.isdir(os.path.join(self.tt, "state")))
        if os.path.isdir(os.path.join(self.tt, "logs")):
            for f in os.listdir(os.path.join(self.tt, "logs")):
                os.remove(os.path.join(self.tt, "logs", f))
            os.rmdir(os.path.join(self.tt, "logs"))
        self.uninstall()
        self.assertFalse(os.path.exists(self.tt))

    def test_purge_removes_whole_home(self):
        self.make_junk()
        self.write(os.path.join(self.tt, "config.json"), '{"port": 9999}\n')  # user-edited
        p = self.uninstall("--purge")
        self.assertIn("will be deleted", p.stdout)
        self.assertFalse(os.path.exists(self.tt), os.listdir(self.tt) if os.path.isdir(self.tt) else "")
        self.assertEqual(snapshot(self.claude), self.orig)
        p2 = self.uninstall("--purge", check=False)
        self.assertNotIn("Traceback", p2.stderr)

    def test_purge_asks_for_confirmation(self):
        self.make_junk()
        p = self.run_cli("uninstall", "--purge", input="n\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tt, "logs", "2026-10-04.jsonl")))

    def test_purge_refuses_unsafe_home(self):
        keep = os.path.join(self.home, "important.txt")
        self.write(keep, "data")
        installed = snapshot(self.claude)
        for bad in ("/", self.home, os.path.dirname(self.home), self.claude):
            p = self.run_cli("uninstall", "--purge", "--yes", env=self.env(TOKENTIER_HOME=bad))
            self.assertNotEqual(p.returncode, 0, bad)
            self.assertIn("refusing to --purge", p.stderr + p.stdout, bad)
        self.assertTrue(os.path.isfile(keep))
        self.assertEqual(snapshot(self.claude), installed)
        self.assertTrue(os.path.isfile(os.path.join(self.tt, "install-manifest.json")))

    def test_purge_refuses_unmarked_custom_home(self):
        odd = os.path.join(self.tmp, "stuff")
        self.write(os.path.join(odd, "install-manifest.json"), "{}")  # no app/ folder: not clearly ours
        p = self.run_cli("uninstall", "--purge", "--yes", env=self.env(TOKENTIER_HOME=odd))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing to --purge", p.stderr + p.stdout)
        self.assertTrue(os.path.isfile(os.path.join(odd, "install-manifest.json")))

    def test_dry_run_lists_extras_and_deletes_nothing(self):
        self.make_junk()
        before = snapshot(self.tt)
        for extra in ((), ("--purge",)):
            p = self.run_cli("uninstall", "--dry-run", *extra)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(os.path.join(self.tt, "state"), p.stdout)
            self.assertIn("hook-ignored.log", p.stdout)
            self.assertEqual(snapshot(self.tt), before)
        self.assertIn("PURGE", p.stdout)

    def test_project_and_hooks_only_leave_home_files(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        self.install("--project", proj)
        self.make_junk()
        before = {k: v for k, v in snapshot(self.tt).items() if k.split(os.sep)[0] != "install-manifest.json"}
        self.uninstall("--project", proj)
        self.uninstall("--hooks-only")
        after = snapshot(self.tt)
        for rel in before:
            if rel.split(os.sep)[0] in ("state", "logs", "dashboard.log", "hook-errors.log",
                                        "hook-ignored.log", "routing-hints.json"):
                self.assertIn(rel, after, rel)
        self.assertTrue(os.path.isdir(os.path.join(self.tt, "state")))


if __name__ == "__main__":
    unittest.main()
