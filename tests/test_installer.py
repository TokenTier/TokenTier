"""Installer / uninstaller / doctor tests. All runs use a throwaway HOME (see tt_helpers)."""
import glob
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tt_helpers import BIN, IS_WINDOWS, POSIX_ONLY, ROOT, FakeHome, fixture, free_port, load_cli, snapshot

cli = load_cli()
LINE = "For non-trivial tasks, follow the tokentier-router skill."
EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "Stop")


def kit(name):
    sub = "skills/tokentier-router/SKILL.md" if name == "SKILL.md" else "agents/" + name
    with open(os.path.join(ROOT, "kit", sub), "rb") as f:
        return f.read()


class SeedMixin(object):
    def seed(self, settings=None, claude_md=None, settings_mode=None):
        if settings is not None:
            self.write(os.path.join(self.claude, "settings.json"), settings, settings_mode)
        if claude_md is not None:
            self.write(os.path.join(self.claude, "CLAUDE.md"), claude_md)

    def leftovers(self):
        """Everything under ~/.tokentier except logs/."""
        return sorted(k for k in snapshot(self.tt) if not (k == "logs" or k.startswith("logs" + os.sep)))


# --------------------------------------------------------------------- round trip
VARIANTS = {
    "real_copy": (fixture("real_settings.json"), fixture("real_CLAUDE.md")),
    "missing_both": (None, None),
    "empty_files": (b"", b""),
    "rich_hooks_crlf_md": (fixture("rich_settings.json"),
                           b"# My rules\r\n\r\nBe concise.\r\nNo trailing newline here"),
    "compact_json_md_no_nl": (b'{"model":"sonnet","hooks":{"SessionEnd":[{"hooks":[{"type":"command",'
                              b'"command":"true"}]}]}}', b"Line one\nline two"),
    "tab_indent_empty_hooks": (b'{\n\t"hooks": {},\n\t"theme": "dark"\n}\n', b"\n"),
    "md_ends_blank_line": (b'{\n    "a": [1, 2, {"b": null}]\n}\n', b"# Title\n\ntext\n\n"),
    "bom_settings": (b'\xef\xbb\xbf{\n  "x": 1\n}\n', b"x\n"),
}


class RoundTripTest(SeedMixin, FakeHome):
    def check_round_trip(self, settings, claude_md, extra_install=()):
        self.seed(settings, claude_md, settings_mode=0o600 if settings is not None else None)
        before = snapshot(self.home)
        self.install("--no-service", *extra_install)
        s = self.settings()
        for ev in EVENTS:
            ours = [h for g in s["hooks"][ev] for h in g.get("hooks", []) if cli.is_our_handler(h)]
            self.assertEqual(len(ours), 1, ev)
        md = self.read(os.path.join(self.claude, "CLAUDE.md")).decode()
        self.assertEqual(sum(1 for l in md.splitlines() if l.strip() == LINE), 1)
        self.uninstall()
        self.assertEqual(self.read(os.path.join(self.claude, "settings.json")), settings)
        self.assertEqual(self.read(os.path.join(self.claude, "CLAUDE.md")), claude_md)
        after = snapshot(self.home)
        after = {k: v for k, v in after.items() if not k.startswith(".tokentier")}
        self.assertEqual(after, before)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(os.path.isdir(os.path.join(self.tt, "logs")))
        if settings is not None and not IS_WINDOWS:  # NTFS has no POSIX mode bits
            self.assertEqual(os.stat(os.path.join(self.claude, "settings.json")).st_mode & 0o777, 0o600)

    def test_variants(self):
        for name, (settings, md) in VARIANTS.items():
            with self.subTest(name):
                self.setUp()
                self.check_round_trip(settings, md)

    def test_existing_agents_differ_backup_and_restore(self):
        old = b"---\nname: fast-worker\nmodel: haiku\n---\nmy own version\n"
        self.write(os.path.join(self.claude, "agents", "fast-worker.md"), old)
        self.write(os.path.join(self.claude, "agents", "other.md"), b"user agent\n")
        before = snapshot(self.home)
        out = self.install("--no-service").stdout
        self.assertIn("backup", out)
        self.assertEqual(self.read(os.path.join(self.claude, "agents", "fast-worker.md")), kit("fast-worker.md"))
        backups = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(self.tt, "backups"))
                   for f in fs if f == "fast-worker.md"]
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.read(backups[0]), old)
        self.uninstall()
        self.assertEqual(self.read(os.path.join(self.claude, "agents", "fast-worker.md")), old)
        self.assertEqual({k: v for k, v in snapshot(self.home).items() if not k.startswith(".tokentier")}, before)

    def test_identical_preexisting_agents_left_alone(self):
        for a in ("fast-worker.md", "mid-worker.md", "deep-worker.md", "deep-worker-low.md"):
            self.write(os.path.join(self.claude, "agents", a), kit(a))
        self.write(os.path.join(self.claude, "skills", "tokentier-router", "SKILL.md"), kit("SKILL.md"))
        self.install("--no-service")
        m = self.manifest()
        self.assertTrue(all(r.get("preexisting") for r in m["global"]["files"].values()))
        self.assertEqual(m["backups"], [])
        self.uninstall()
        for a in ("fast-worker.md", "mid-worker.md", "deep-worker.md", "deep-worker-low.md"):
            self.assertEqual(self.read(os.path.join(self.claude, "agents", a)), kit(a))

    def test_upgrade_of_preexisting_identical_agent_restores_original(self):
        self.write(os.path.join(self.claude, "agents", "mid-worker.md"), kit("mid-worker.md"))
        self.install("--no-service")
        repo2 = os.path.join(self.tmp, "repo2")
        for d in ("bin", "kit", "dashboard"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(repo2, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(ROOT, "VERSION"), repo2)
        self.write(os.path.join(repo2, "kit", "agents", "mid-worker.md"), kit("mid-worker.md") + b"\nv2 rule\n")
        self.run_cli("install", "--yes", "--no-service", bin_path=os.path.join(repo2, "bin", "tokentier"),
                     check=True)
        self.assertTrue(self.read(os.path.join(self.claude, "agents", "mid-worker.md")).endswith(b"v2 rule\n"))
        self.uninstall()
        self.assertEqual(self.read(os.path.join(self.claude, "agents", "mid-worker.md")), kit("mid-worker.md"))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "agents", "fast-worker.md")))

    def test_purge_removes_logs(self):
        self.install("--no-service")
        self.write(os.path.join(self.tt, "logs", "2026-10-04.jsonl"), b'{"event":"x"}\n')
        self.uninstall()
        self.assertTrue(os.path.isfile(os.path.join(self.tt, "logs", "2026-10-04.jsonl")))
        p = self.run_cli("uninstall", "--purge", "--yes")
        self.assertEqual(p.returncode, 1)  # nothing installed any more
        self.install("--no-service")
        self.uninstall("--purge")
        self.assertFalse(os.path.exists(self.tt))

    @unittest.skipIf(IS_WINDOWS, POSIX_ONLY)  # symlinks need admin/developer mode on Windows
    def test_symlinked_settings_kept_as_symlink(self):
        target = os.path.join(self.home, "dotfiles", "claude-settings.json")
        self.write(target, fixture("rich_settings.json"))
        os.makedirs(self.claude)
        os.symlink(target, os.path.join(self.claude, "settings.json"))
        self.install("--no-service")
        self.assertTrue(os.path.islink(os.path.join(self.claude, "settings.json")))
        self.assertIn("tokentier_log.py", self.read(target).decode())
        self.uninstall()
        self.assertTrue(os.path.islink(os.path.join(self.claude, "settings.json")))
        self.assertEqual(self.read(target), fixture("rich_settings.json"))


# ------------------------------------------------------------------- install behaviour
class InstallTest(SeedMixin, FakeHome):
    def test_dry_run_writes_nothing(self):
        self.seed(fixture("rich_settings.json"), b"hello\n")
        before = snapshot(self.home)
        p = self.run_cli("install", "--dry-run", check=True)
        self.assertEqual(snapshot(self.home), before)
        self.assertIn("Dry run: nothing was written", p.stdout)
        self.assertIn("+++ %s (after)" % os.path.join(self.claude, "settings.json"), p.stdout)
        self.assertIn("+++ %s (after)" % os.path.join(self.claude, "CLAUDE.md"), p.stdout)
        self.assertIn("+" + cli.MARK_START, p.stdout)
        self.assertIn("app/hooks/tokentier_log.py", p.stdout)
        # uninstall dry-run after a real install also writes nothing
        self.install("--no-service")
        before = snapshot(self.home)
        p = self.run_cli("uninstall", "--dry-run", check=True)
        self.assertEqual(snapshot(self.home), before)
        self.assertIn("-" + cli.MARK_START, p.stdout)

    def test_dry_run_project_and_service_write_nothing(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        before_h, before_p = snapshot(self.home), snapshot(proj)
        self.run_cli("install", "--dry-run", "--project", proj, "--port", str(free_port()),
                     env=self.env(TOKENTIER_PLATFORM="darwin"), check=True)
        self.assertEqual((snapshot(self.home), snapshot(proj)), (before_h, before_p))

    def test_idempotent_double_install(self):
        self.seed(fixture("rich_settings.json"), b"# rules\n")
        self.install("--no-service")
        snap = snapshot(self.home)
        p = self.install("--no-service")
        self.assertIn("Nothing to do", p.stdout)
        self.assertEqual(snapshot(self.home), snap)
        # with a service too (files only; no launchctl), twice
        port = str(free_port())
        self.install("--port", port, env=self.env(TOKENTIER_PLATFORM="darwin"))
        snap = snapshot(self.home)
        p = self.install("--port", port, env=self.env(TOKENTIER_PLATFORM="darwin"))
        self.assertIn("Nothing to do", p.stdout)
        self.assertEqual(snapshot(self.home), snap)

    def test_merge_preserves_user_settings_and_order(self):
        self.seed(fixture("rich_settings.json"))
        orig = json.loads(fixture("rich_settings.json").decode())
        self.install("--no-service")
        s = self.settings()
        self.assertEqual(list(s.keys()), list(orig.keys()))  # no reordering, nothing dropped
        for k in orig:
            if k != "hooks":
                self.assertEqual(s[k], orig[k])
        self.assertEqual(s["hooks"]["PreToolUse"], orig["hooks"]["PreToolUse"])
        # user's own SessionStart / SubagentStop groups stay first and untouched
        self.assertEqual(s["hooks"]["SessionStart"][0], orig["hooks"]["SessionStart"][0])
        self.assertEqual(s["hooks"]["SubagentStop"][0], orig["hooks"]["SubagentStop"][0])
        self.assertEqual(len(s["hooks"]["SessionStart"]), 2)
        g = s["hooks"]["SubagentStop"][1]
        if IS_WINDOWS:  # exec form; exact shape is covered in test_windows.py
            self.assertTrue(cli.is_our_handler(g["hooks"][0]) and "args" in g["hooks"][0])
        else:
            self.assertEqual(g, {"matcher": "", "hooks": [{
                "type": "command", "timeout": 10,
                "command": "python3 %s SubagentStop" % os.path.join(self.tt, "app", "hooks", "tokentier_log.py")}]})
        raw = self.read(os.path.join(self.claude, "settings.json")).decode()
        self.assertIn('\n  "model": "opus"', raw)  # 2-space indent kept
        self.assertIn("héllo wörld ✓", raw)  # no \u escapes
        self.assertTrue(raw.endswith("}\n"))

    @unittest.skipIf(IS_WINDOWS, POSIX_ONLY)
    def test_hook_path_change_updated_in_place(self):
        self.seed(fixture("real_settings.json"))
        self.install("--no-service")
        sp = os.path.join(self.claude, "settings.json")
        s = self.settings()
        s["hooks"]["SessionEnd"][0]["hooks"][0]["command"] = "python3 /old/place/tokentier_log.py SessionEnd"
        self.write(sp, json.dumps(s, indent=2))
        self.install("--no-service")
        s = self.settings()
        self.assertEqual(len(s["hooks"]["SessionEnd"]), 1)
        self.assertIn(os.path.join(self.tt, "app", "hooks"), s["hooks"]["SessionEnd"][0]["hooks"][0]["command"])

    def test_invalid_json_aborts_and_changes_nothing(self):
        cases = [b'{"a": 1,,}', b"[1, 2]", b'{"hooks": []}', b'{"hooks": {"SessionStart": {}}}',
                 b'{"a": 1, "a": 2}']
        for bad in cases:
            with self.subTest(bad=bad):
                self.setUp()
                self.seed(bad, b"keep me\n")
                before = snapshot(self.home)
                p = self.run_cli("install", "--yes", "--no-service")
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("ERROR", p.stderr)
                self.assertIn("changed nothing", p.stderr)
                self.assertEqual(snapshot(self.home), before)
                self.assertEqual(self.read(os.path.join(self.claude, "settings.json")), bad)

    def test_non_interactive_without_yes_refuses(self):
        before = snapshot(self.home)
        p = self.run_cli("install", "--no-service", input="")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--yes", p.stderr)
        self.assertEqual(snapshot(self.home), before)

    def test_preexisting_snippet_line(self):
        md = b"# Notes\n\nFor non-trivial tasks, follow the tokentier-router skill.\n\nMore.\n"
        self.seed(None, md)
        self.install("--no-service")
        self.assertEqual(self.read(os.path.join(self.claude, "CLAUDE.md")), md)
        self.assertEqual(self.manifest()["global"]["claude_md"]["state"], "preexisting")
        self.uninstall()
        self.assertEqual(self.read(os.path.join(self.claude, "CLAUDE.md")), md)

    def test_claude_md_markers_and_created_file(self):
        self.install("--no-service")
        md = self.read(os.path.join(self.claude, "CLAUDE.md")).decode()
        self.assertEqual(md, "%s\n%s\n%s\n" % (cli.MARK_START, LINE, cli.MARK_END))
        cm = self.manifest()["global"]["claude_md"]
        self.assertEqual((cm["state"], cm["created_file"]), ("added", True))
        self.install("--no-service")  # still exactly once
        self.assertEqual(self.read(os.path.join(self.claude, "CLAUDE.md")).decode().count(LINE), 1)
        self.uninstall()
        self.assertIsNone(self.read(os.path.join(self.claude, "CLAUDE.md")))
        self.assertFalse(os.path.exists(self.claude))  # we created ~/.claude, it is empty again

    def test_claude_md_user_edits_inside_block_kept(self):
        self.seed(None, b"top\n")
        self.install("--no-service")
        p = os.path.join(self.claude, "CLAUDE.md")
        text = self.read(p).decode().replace(LINE + "\n", LINE + "\nmy extra rule\n")
        self.write(p, text)
        self.uninstall()
        self.assertEqual(self.read(p), b"top\n\nmy extra rule\n")

    def test_backups_created(self):
        self.seed(fixture("rich_settings.json"), b"x\n")
        self.install("--no-service")
        m = self.manifest()
        bk = m["global"]["settings"]["backup_original"]
        self.assertTrue(bk.startswith(os.path.join(self.tt, "backups") + os.sep))
        self.assertEqual(self.read(bk), fixture("rich_settings.json"))
        self.assertIn(bk, [b["backup"] for b in m["backups"]])

    def test_user_modified_files_not_deleted(self):
        self.seed(fixture("rich_settings.json"))
        self.install("--no-service")
        agent = os.path.join(self.claude, "agents", "fast-worker.md")
        self.write(agent, b"edited by me\n")
        sp = os.path.join(self.claude, "settings.json")
        s = self.settings()
        s["hooks"]["SessionEnd"][-1]["hooks"][0]["timeout"] = 99  # user tweaked our entry
        s["newKey"] = True
        self.write(sp, json.dumps(s, indent=2, ensure_ascii=False) + "\n")
        p = self.uninstall()
        self.assertIn("changed after install; left in place", p.stdout)
        self.assertIn("SessionEnd hook was modified by you; left it in place", p.stdout)
        self.assertEqual(self.read(agent), b"edited by me\n")
        s = self.settings()
        self.assertTrue(s["newKey"])
        self.assertEqual(sum(cli.is_our_handler(h) for ev in EVENTS for g in s["hooks"].get(ev, [])
                             for h in g["hooks"]), 1)
        self.assertEqual(s["hooks"]["PreToolUse"], json.loads(fixture("rich_settings.json"))["hooks"]["PreToolUse"])
        self.assertTrue(os.path.isdir(os.path.join(self.tt, "backups")))  # kept: not everything restored

    def test_project_install_while_global_hooks_exist(self):
        self.seed(fixture("real_settings.json"))
        self.install("--no-service")
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        p = self.install("--project", proj, "--no-service")
        self.assertIn("logged twice", p.stdout)
        self.assertIsNone(self.read(os.path.join(proj, ".claude", "settings.json")))
        self.assertEqual(self.read(os.path.join(proj, ".claude", "agents", "deep-worker.md")), kit("deep-worker.md"))
        self.assertIn(LINE, self.read(os.path.join(proj, "CLAUDE.md")).decode())
        m = self.manifest()
        self.assertEqual(len(m["projects"]), 1)
        self.assertEqual(m["projects"][0]["settings"]["hooks_skipped"], "global TokenTier hooks are already installed")
        self.assertEqual(m["projects"][0]["settings"]["entries"], [])
        # a second project is recorded too; project-only uninstall keeps global
        proj2 = os.path.join(self.tmp, "proj2")
        os.makedirs(proj2)
        self.install("--project", proj2, "--no-service")
        self.assertEqual(len(self.manifest()["projects"]), 2)
        self.uninstall("--project", proj)
        self.assertEqual(snapshot(proj), {})
        self.assertEqual([p["project_dir"] for p in self.manifest()["projects"]], [proj2])
        self.assertTrue(os.path.isfile(os.path.join(self.tt, "app", "hooks", "tokentier_log.py")))
        self.uninstall()
        self.assertEqual(snapshot(proj2), {})
        self.assertEqual(self.read(os.path.join(self.claude, "settings.json")), fixture("real_settings.json"))

    def test_project_first_then_global_doctor_warns_duplicates(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        self.install("--project", proj, "--no-service")
        ps = os.path.join(proj, ".claude", "settings.json")
        self.assertIn("tokentier_log.py", self.read(ps).decode())
        p = self.install("--no-service")
        self.assertIn("uninstall --project %s --hooks-only" % proj, p.stdout)
        d = self.run_cli("doctor")
        self.assertIn("WARN hooks", d.stdout)
        self.assertIn("more than once", d.stdout)
        self.assertIn("tokentier uninstall --project %s --hooks-only" % proj, d.stdout)
        u = self.uninstall("--project", proj, "--hooks-only")
        self.assertIn("Removed the TokenTier hooks from project %s." % proj, u.stdout)
        self.assertIn("agents, skill and CLAUDE.md line were kept", u.stdout)
        self.assertNotIn("Uninstalled:", u.stdout)
        self.assertIsNone(self.read(ps))  # we created it; empty again -> deleted
        self.assertTrue(os.path.isfile(os.path.join(proj, ".claude", "agents", "mid-worker.md")))
        d = self.run_cli("doctor")
        self.assertIn("PASS hooks                  registered exactly once", d.stdout)
        self.uninstall()
        self.assertEqual(snapshot(proj), {})

    def test_no_hooks_flag(self):
        self.seed(fixture("real_settings.json"))
        self.install("--no-service", "--no-hooks")
        self.assertEqual(self.read(os.path.join(self.claude, "settings.json")), fixture("real_settings.json"))
        self.assertEqual(self.manifest()["global"]["settings"]["hooks_skipped"], "--no-hooks")

    def test_manifest_correctness(self):
        self.seed(fixture("rich_settings.json"), b"# x\n")
        self.install("--no-service")
        m = self.manifest()
        with open(os.path.join(ROOT, "VERSION")) as f:
            self.assertEqual(m["version"], f.read().strip())
        self.assertTrue(m["installed_at"])
        for rel, h in m["app"]["files"].items():
            self.assertEqual(cli.sha256_file(os.path.join(self.tt, "app", rel)), h, rel)
        for rel in ("hooks/tokentier_log.py", "bin/tokentier", "dashboard/server.py", "dashboard/pricing.json",
                    "dashboard/public/index.html", "VERSION", "dashboard/tokentier_dash/store.py"):
            self.assertIn(rel, m["app"]["files"])
        if not IS_WINDOWS:
            self.assertTrue(os.access(os.path.join(self.tt, "app", "bin", "tokentier"), os.X_OK))
        g = m["global"]
        self.assertEqual(len(g["files"]), 5)
        for path, r in g["files"].items():
            self.assertEqual(cli.sha256_file(path), r["sha256"])
            self.assertTrue(r["created"])
        s = self.settings()
        self.assertEqual([e["event"] for e in g["settings"]["entries"]], list(EVENTS))
        for e in g["settings"]["entries"]:
            self.assertIn(e["group"], s["hooks"][e["event"]])
        self.assertEqual(g["claude_md"]["state"], "added")
        self.assertEqual(m["projects"], [])
        self.assertIsNone(m["service"])

    def test_pricing_user_edit_preserved_on_upgrade(self):
        self.install("--no-service")
        # build an "upgraded" copy of the repo with new default pricing
        repo2 = os.path.join(self.tmp, "repo2")
        for d in ("bin", "kit", "dashboard"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(repo2, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(ROOT, "VERSION"), repo2)
        newp = os.path.join(repo2, "dashboard", "pricing.json")
        data = json.loads(self.read(newp))
        data["updated"] = "2099-01-01"
        self.write(newp, json.dumps(data, indent=2) + "\n")
        bin2 = os.path.join(repo2, "bin", "tokentier")
        app_pricing = os.path.join(self.tt, "app", "dashboard", "pricing.json")
        # case 1: not edited -> replaced
        self.run_cli("install", "--yes", "--no-service", bin_path=bin2, check=True)
        self.assertEqual(self.read(app_pricing), self.read(newp))
        self.assertIsNone(self.read(app_pricing + ".new"))
        # case 2: user edits, then another upgrade -> theirs kept, new one next to it
        mine = b'{"models": {}, "note": "mine"}\n'
        self.write(app_pricing, mine)
        data["updated"] = "2099-02-02"
        self.write(newp, json.dumps(data, indent=2) + "\n")
        p = self.run_cli("install", "--yes", "--no-service", bin_path=bin2, check=True)
        self.assertIn("pricing.json.new", p.stdout)
        self.assertEqual(self.read(app_pricing), mine)
        self.assertEqual(self.read(app_pricing + ".new"), self.read(newp))
        self.assertIn("using your edited pricing.json", self.run_cli("doctor").stdout)
        p = self.uninstall()
        self.assertEqual(self.read(os.path.join(self.tt, "pricing.json.user-edited")), mine)

    def test_runs_from_installed_copy_after_repo_is_gone(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        self.install("--no-service", "--no-hooks")
        app_bin = os.path.join(self.tt, "app", "bin", "tokentier")
        self.run_cli("install", "--yes", "--no-service", "--project", proj, bin_path=app_bin, check=True)
        self.assertEqual(self.read(os.path.join(proj, ".claude", "agents", "fast-worker.md")), kit("fast-worker.md"))
        self.run_cli("uninstall", "--yes", bin_path=app_bin, check=True)
        self.assertEqual(snapshot(proj), {})

    @unittest.skipIf(IS_WINDOWS, POSIX_ONLY + " (bash wrappers; install.ps1 is checked in test_windows.py)")
    def test_wrapper_scripts(self):
        env = self.env()
        p = subprocess.run(["bash", os.path.join(ROOT, "install.sh"), "--dry-run"], env=env, cwd=self.tmp,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Dry run", p.stdout)
        self.assertEqual(snapshot(self.home), {})
        p = subprocess.run(["bash", os.path.join(ROOT, "uninstall.sh"), "--dry-run"], env=env, cwd=self.tmp,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not installed", p.stderr)

    @unittest.skipIf(IS_WINDOWS, POSIX_ONLY)
    def test_custom_home_puts_env_in_command(self):
        custom = os.path.join(self.tmp, "tthome")
        self.install("--no-service", "--home", custom)
        cmd = self.settings()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.startswith("TOKENTIER_HOME=%s python3 " % custom))
        r = subprocess.run(["sh", "-c", cmd], input=json.dumps({"session_id": "s1", "cwd": self.tmp}),
                           env=self.env(), universal_newlines=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(glob.glob(os.path.join(custom, "logs", "*.jsonl"))), 1)
        self.assertFalse(os.path.exists(self.tt))
        self.uninstall("--home", custom)


# ------------------------------------------------------------------ hooks end-to-end
@unittest.skipIf(IS_WINDOWS, POSIX_ONLY + " (runs hook commands with sh -c)")
class HookEndToEndTest(SeedMixin, FakeHome):
    def test_commands_from_settings_run_and_log(self):
        self.seed(fixture("rich_settings.json"))
        self.install("--no-service")
        s = self.settings()

        def cmd_for(ev):
            return [h["command"] for g in s["hooks"][ev] for h in g["hooks"] if "tokentier_log.py" in h["command"]][0]

        proj = os.path.join(self.tmp, "myproj")
        sess = os.path.join(self.tmp, "t", "sess1.jsonl")
        sub = os.path.join(self.tmp, "t", "sess1", "subagents")
        os.makedirs(sub)
        os.makedirs(proj)
        with open(os.path.join(sub, "agent-abc.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-04T10:00:00Z", "message": {
                "id": "m1", "model": "claude-haiku-4-5", "usage": {
                    "input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40}}}) + "\n")
        with open(os.path.join(sub, "agent-abc.meta.json"), "w") as f:
            json.dump({"agentType": "fast-worker", "description": "Do a thing", "toolUseId": "toolu_1"}, f)
        env = self.env()
        for ev, payload in (
                ("SessionStart", {"session_id": "s1", "cwd": proj, "hook_event_name": "SessionStart"}),
                ("SubagentStop", {"session_id": "s1", "cwd": proj, "transcript_path": sess, "agent_id": "abc",
                                  "agent_type": "fast-worker", "hook_event_name": "SubagentStop",
                                  "last_assistant_message": "Done.\nSTATUS: done"})):
            r = subprocess.run(["sh", "-c", cmd_for(ev)], input=json.dumps(payload), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
            self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        lines = []
        for p in glob.glob(os.path.join(self.tt, "logs", "*.jsonl")):
            with open(p) as f:
                lines += [json.loads(l) for l in f if l.strip()]
        kinds = sorted(l["event"] for l in lines)
        self.assertEqual(kinds, ["session_start", "task_end"])
        te = [l for l in lines if l["event"] == "task_end"][0]
        self.assertEqual((te["tier"], te["status"], te["tokens_total"], te["project"]), ("haiku", "pass", 100, "myproj"))


# ------------------------------------------------------------------ service files
class ServiceTest(SeedMixin, FakeHome):
    def test_launchd_plist(self):
        port = free_port()
        p = self.install("--port", str(port), env=self.env(TOKENTIER_PLATFORM="darwin"))
        self.assertIn("skipped, TOKENTIER_NO_SERVICE_EXEC", p.stdout)
        path = os.path.join(self.home, "Library", "LaunchAgents", "dev.tokentier.dashboard.plist")
        pl = plistlib.loads(self.read(path))
        self.assertEqual(pl["Label"], "dev.tokentier.dashboard")
        self.assertTrue(pl["RunAtLoad"])
        self.assertTrue(pl["KeepAlive"])
        args = pl["ProgramArguments"]
        self.assertTrue(os.path.isabs(args[0]))
        self.assertEqual(args[1:], [os.path.join(self.tt, "app", "dashboard", "server.py"),
                                    "--host", "127.0.0.1", "--port", str(port)])
        self.assertEqual(pl["EnvironmentVariables"], {"TOKENTIER_HOME": self.tt})
        self.assertEqual(pl["StandardOutPath"], os.path.join(self.tt, "dashboard.log"))
        self.assertEqual(pl["StandardErrorPath"], os.path.join(self.tt, "dashboard.log"))
        m = self.manifest()
        self.assertEqual((m["service"]["kind"], m["service"]["path"], m["service"]["port"]), ("launchd", path, port))
        p = self.uninstall(env=self.env(TOKENTIER_PLATFORM="darwin"))
        self.assertIn("launchctl bootout gui/", p.stdout)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library")))
        self.assertEqual(self.leftovers(), [])

    @unittest.skipIf(IS_WINDOWS, POSIX_ONLY)
    def test_systemd_unit(self):
        port = free_port()
        self.install("--port", str(port), env=self.env(TOKENTIER_PLATFORM="linux"))
        path = os.path.join(self.home, ".config", "systemd", "user", "tokentier-dashboard.service")
        text = self.read(path).decode()
        server = os.path.join(self.tt, "app", "dashboard", "server.py")
        self.assertIn("[Service]\n", text)
        self.assertRegex(text, r"\nExecStart=/\S*python3?\S* %s --host 127\.0\.0\.1 --port %d\n"
                         % (server.replace(".", r"\."), port))
        self.assertIn("\nEnvironment=TOKENTIER_HOME=%s\n" % self.tt, text)
        self.assertIn("\nRestart=on-failure\n", text)
        self.assertIn("\nStandardOutput=append:%s\n" % os.path.join(self.tt, "dashboard.log"), text)
        self.assertIn("[Install]\nWantedBy=default.target\n", text)
        p = self.uninstall(env=self.env(TOKENTIER_PLATFORM="linux"))
        self.assertIn("systemctl --user disable --now tokentier-dashboard.service", p.stdout)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config")))

    def test_systemd_unit_quotes_spaces(self):
        text = cli.systemd_unit("/home/a b/.tokentier", 8899, "/usr/bin/python3")
        self.assertIn('ExecStart=/usr/bin/python3 "/home/a b/.tokentier/app/dashboard/server.py"', text)
        self.assertIn('Environment="TOKENTIER_HOME=/home/a b/.tokentier"', text)

    def test_busy_port_warns_but_installs(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        port = s.getsockname()[1]
        p = self.install("--port", str(port), env=self.env(TOKENTIER_PLATFORM="darwin"))
        self.assertIn("already used by another program", p.stdout)
        self.assertIn("never kills", p.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.home, "Library", "LaunchAgents",
                                                    "dev.tokentier.dashboard.plist")))
        d = self.run_cli("doctor", env=self.env(TOKENTIER_PLATFORM="darwin"))
        self.assertIn("is used by another program", d.stdout)
        self.assertEqual(d.returncode, 1)  # service installed but port taken -> FAIL

    def _install_on(self, port):
        return self.install("--port", str(port), env=self.env(TOKENTIER_PLATFORM="darwin"))

    def test_free_port_no_warning(self):
        p = self._install_on(free_port())
        self.assertNotIn("already used by another program", p.stdout + p.stderr)
        self.assertNotIn("already running", p.stdout)

    def test_foreign_listener_warns(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        self.addCleanup(s.close)
        p = self._install_on(s.getsockname()[1])
        self.assertIn("already used by another program", p.stdout + p.stderr)

    def test_tokentier_listener_no_warning(self):
        class H(BaseHTTPRequestHandler):
            server_version = "TokenTier"

            def do_GET(self):
                body = b'{"ok": true, "version": "test"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        p = self._install_on(srv.server_address[1])
        self.assertNotIn("already used by another program", p.stdout + p.stderr)
        self.assertNotIn("never kills", p.stdout + p.stderr)

    @unittest.skipIf(IS_WINDOWS, POSIX_ONLY + " (SO_REUSEADDR/TIME_WAIT semantics)")
    def test_time_wait_socket_reported_as_free(self):
        # Regression test: after a socket is closed with TIME_WAIT on macOS, bind() with SO_REUSEADDR
        # should succeed and port_state() should return 'free'.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        self.addCleanup(client.close)

        accepted, _ = listener.accept()
        self.addCleanup(accepted.close)

        # Close in reverse order: accepted first, then client, then listener (creates TIME_WAIT)
        accepted.close()
        client.close()
        listener.close()
        # Both sockets are now closed; listener port is in TIME_WAIT

        # port_state should report 'free' despite TIME_WAIT, thanks to SO_REUSEADDR
        self.assertEqual(cli.port_state(port), "free")

        # Install --dry-run should not warn about the port being used
        p = self.run_cli("install", "--dry-run", "--port", str(port),
                        env=self.env(TOKENTIER_PLATFORM="darwin"))
        self.assertNotIn("already used by another program", p.stdout + p.stderr)

    def test_unsupported_platform_prints_manual_command(self):
        p = self.install(env=self.env(TOKENTIER_PLATFORM="sunos5"))
        self.assertIn("No always-on service", p.stdout)
        self.assertIn("dashboard --port", p.stdout)
        self.assertIsNone(self.manifest()["service"])


# ------------------------------------------------------------------ doctor / status
class DoctorStatusTest(SeedMixin, FakeHome):
    def test_doctor_and_status_after_install(self):
        self.seed(fixture("real_settings.json"), fixture("real_CLAUDE.md"))
        self.install("--no-service")
        d = self.run_cli("doctor")
        self.assertEqual(d.returncode, 0, d.stdout)
        for name in ("python", "installed", "app files", "global agents", "global CLAUDE.md", "hooks",
                     "log dir", "hook self-test"):
            self.assertRegex(d.stdout, r"(?m)^PASS %s\b" % name)
        self.assertEqual(glob.glob(os.path.join(self.tt, "logs", "*")), [])  # self-test used a temp dir
        st = self.run_cli("status", check=True).stdout
        self.assertIn("hooks registered (5/5 events)", st)
        self.assertIn("0 files, 0 events", st)

    def test_doctor_fails_when_not_installed_or_hooks_missing(self):
        d = self.run_cli("doctor")
        self.assertEqual(d.returncode, 1)
        self.install("--no-service")
        sp = os.path.join(self.claude, "settings.json")
        s = self.settings()
        del s["hooks"]["SubagentStop"]
        self.write(sp, json.dumps(s, indent=2))
        d = self.run_cli("doctor")
        self.assertEqual(d.returncode, 1)
        self.assertIn("missing for SubagentStop", d.stdout)


# ------------------------------------------------------------------ pure helpers
class PureHelpersTest(unittest.TestCase):
    def test_claude_md_insert_remove_round_trip(self):
        for text in ("", "a", "a\n", "a\n\n", "a\r\nb", "a\r\nb\r\n", "\n", "x\n\n\n"):
            new, ins = cli.claude_md_insert(text, LINE)
            self.assertEqual(new.count(LINE), 1)
            self.assertTrue(cli.find_block(new))
            back, note = cli.claude_md_remove(new, {"inserted": ins}, LINE)
            self.assertEqual((back, note), (text, None), repr(text))
        new, _ = cli.claude_md_insert("a\r\nb", LINE)
        self.assertEqual(new, "a\r\nb\r\n\r\n%s\r\n%s\r\n%s\r\n" % (cli.MARK_START, LINE, cli.MARK_END))

    def test_line_outside_block(self):
        self.assertTrue(cli.line_outside_block("x\n  %s  \r\n" % LINE, LINE))
        new, _ = cli.claude_md_insert("x\n", LINE)
        self.assertFalse(cli.line_outside_block(new, LINE))

    def test_json_style_round_trip(self):
        for raw in (b'{\n  "a": 1\n}', b'{\n    "a": [\n        1\n    ]\n}\n', b'{\r\n\t"a": 1\r\n}\r\n'):
            data = json.loads(raw.decode())
            self.assertEqual(cli.dump_settings(data, cli.json_style(raw)), raw)

    def test_hook_group_schema(self):
        g = cli.hook_group("SubagentStop", "/x/.tokentier", "linux")
        self.assertEqual(set(g), {"matcher", "hooks"})
        self.assertEqual(set(g["hooks"][0]), {"type", "command", "timeout"})
        self.assertEqual(g["hooks"][0]["type"], "command")
        g = cli.hook_group("Stop", "/x/.tokentier", "linux")  # Stop takes no matcher
        self.assertEqual(set(g), {"hooks"})
        self.assertEqual(set(g["hooks"][0]), {"type", "command", "timeout"})


if __name__ == "__main__":
    unittest.main()
