"""Installer behaviour for the `Stop` hook event (4 -> 5 event upgrade, old manifests)."""
import json
import os
import unittest

from test_installer import SeedMixin, VARIANTS
from tt_helpers import FakeHome, snapshot

EVENTS5 = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "Stop")


class StopHookInstallTest(SeedMixin, FakeHome):
    def spath(self):
        return os.path.join(self.claude, "settings.json")

    def mpath(self):
        return os.path.join(self.tt, "install-manifest.json")

    def downgrade_to_four(self):
        """Turn a fresh install into what a pre-Stop version left behind (settings + manifest)."""
        s = self.settings()
        s["hooks"].pop("Stop")
        raw = self.read(self.spath())
        indent = 2 if raw.startswith(b"{\n  ") else 4
        self.write(self.spath(), json.dumps(s, indent=indent, ensure_ascii=False) + "\n")
        m = self.manifest()
        sres = m["global"]["settings"]
        sres["entries"] = [e for e in sres["entries"] if e["event"] != "Stop"]
        sres["created_events"] = [e for e in sres.get("created_events", []) if e != "Stop"]
        self.write(self.mpath(), json.dumps(m, indent=2))

    def test_fresh_install_has_stop_without_matcher(self):
        self.seed(b'{\n  "model": "opus"\n}\n')
        self.install("--no-service")
        s = self.settings()
        for ev in EVENTS5:
            self.assertEqual(len(s["hooks"][ev]), 1, ev)
        self.assertEqual(set(s["hooks"]["Stop"][0]), {"hooks"})
        self.assertEqual(s["hooks"]["Stop"][0]["hooks"][0]["timeout"], 10)
        h = s["hooks"]["Stop"][0]["hooks"][0]
        # POSIX shell form "... tokentier_log.py Stop", or Windows exec form args [".../tokentier_log.py", "Stop"]
        self.assertTrue(h["command"].endswith("tokentier_log.py Stop") or h.get("args", [None])[-1] == "Stop")
        self.assertEqual(s["hooks"]["SessionStart"][0]["matcher"], "")
        st = self.run_cli("status", check=True).stdout
        self.assertIn("hooks registered (5/5 events)", st)
        d = self.run_cli("doctor")
        self.assertIn("registered exactly once for all 5 events", d.stdout)

    def test_upgrade_from_four_events(self):
        orig, md = VARIANTS["rich_hooks_crlf_md"]
        self.seed(orig, md)
        self.install("--no-service")
        self.downgrade_to_four()
        four = self.settings()
        d = self.run_cli("doctor")
        self.assertIn("not for Stop", d.stdout)
        p = self.install("--no-service")
        s = self.settings()
        for ev in EVENTS5:
            self.assertEqual(sum("tokentier_log.py" in json.dumps(h) for g in s["hooks"][ev] for h in g["hooks"]), 1, ev)
        for ev in EVENTS5[:4]:  # the four existing events are untouched
            self.assertEqual(s["hooks"][ev], four["hooks"][ev], ev)
        self.assertEqual({k: v for k, v in s.items() if k != "hooks"}, {k: v for k, v in four.items() if k != "hooks"})
        self.assertEqual([e["event"] for e in self.manifest()["global"]["settings"]["entries"]], list(EVENTS5))
        # a backup of the 4-event settings was taken before the write
        backups = [k for k in snapshot(self.tt) if "backup" in k.lower() and k.endswith(".json")]
        self.assertTrue(backups, snapshot(self.tt).keys())
        # idempotent
        snap = snapshot(self.home)
        p = self.install("--no-service")
        self.assertIn("Nothing to do", p.stdout)
        self.assertEqual(snapshot(self.home), snap)
        # uninstall restores the original bytes exactly
        self.uninstall()
        self.assertEqual(self.read(self.spath()), orig)
        self.assertEqual(self.read(os.path.join(self.claude, "CLAUDE.md")), md)

    def test_upgrade_round_trip_every_variant(self):
        for name, (sett, md) in VARIANTS.items():
            with self.subTest(name):
                self.setUp()
                self.seed(sett, md)
                self.install("--no-service")
                if sett is not None and self.settings().get("hooks", {}).get("Stop"):
                    self.downgrade_to_four()
                    self.install("--no-service")
                self.uninstall()
                self.assertEqual(self.read(self.spath()), sett)
                self.assertEqual(self.read(os.path.join(self.claude, "CLAUDE.md")), md)

    def test_uninstall_with_old_manifest_lacking_stop(self):
        orig, md = VARIANTS["real_copy"]
        self.seed(orig, md)
        self.install("--no-service")
        m = self.manifest()
        sres = m["global"]["settings"]
        sres["entries"] = [e for e in sres["entries"] if e["event"] != "Stop"]
        sres["created_events"] = [e for e in sres.get("created_events", []) if e != "Stop"]
        self.write(self.mpath(), json.dumps(m, indent=2))
        self.uninstall()
        self.assertEqual(self.read(self.spath()), orig)  # our Stop entry is gone too

    def test_uninstall_keeps_users_own_stop_hook(self):
        orig = b'{\n  "hooks": {\n    "Stop": [\n      {"hooks": [{"type": "command", "command": "echo mine"}]}\n    ]\n  }\n}\n'
        self.seed(orig)
        self.install("--no-service")
        self.assertEqual(len(self.settings()["hooks"]["Stop"]), 2)
        self.uninstall()
        self.assertEqual(self.read(self.spath()), orig)


if __name__ == "__main__":
    unittest.main()
