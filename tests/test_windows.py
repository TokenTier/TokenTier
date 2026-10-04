"""Windows support, verified by simulation on any OS (TOKENTIER_PLATFORM=win32, fake msvcrt /
winreg / subprocess / socket). Nothing here needs Windows, and nothing touches the real HOME,
registry, Task Scheduler or launchd: CLI runs use a throwaway HOME (see tt_helpers) with
TOKENTIER_NO_SERVICE_EXEC=1, and in-process service tests replace subprocess.run and winreg.

What this cannot prove is how real Windows behaves (Claude Code spawning the exec-form hook,
schtasks accepting the XML, msvcrt semantics, console windows): see tests/windows_smoke.ps1.
"""
import glob
import io
import json
import ntpath
import os
import posixpath
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from tt_helpers import IS_WINDOWS, POSIX_ONLY, ROOT, FakeHome, fixture, free_port, load_cli, snapshot

cli = load_cli()
sys.path.insert(0, os.path.join(ROOT, "kit", "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
import tokentier_log as tl  # noqa: E402
import server as srv  # noqa: E402

EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "Stop")
WIN = {"TOKENTIER_PLATFORM": "win32"}
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

# settings.json / CLAUDE.md as Windows editors write them: UTF-8 BOM and CRLF
BOM_CRLF_SETTINGS = (b'\xef\xbb\xbf{\r\n  "model": "opus",\r\n  "hooks": {\r\n    "SessionStart": [\r\n'
                     b'      {\r\n        "matcher": "",\r\n        "hooks": [\r\n          {\r\n'
                     b'            "type": "command",\r\n            "command": "echo hi"\r\n          }\r\n'
                     b'        ]\r\n      }\r\n    ]\r\n  },\r\n  "theme": "dark \xc3\xa9"\r\n}\r\n')
CRLF_SETTINGS = b'{\r\n    "permissions": {\r\n        "allow": []\r\n    }\r\n}'
BOM_CRLF_MD = b"\xef\xbb\xbf# Meine Regeln\r\n\r\nSei knapp.\r\n"
CRLF_MD_NO_NL = b"# rules\r\n\r\nno trailing newline"


def ours(settings, ev):
    return [h for g in settings["hooks"][ev] for h in g.get("hooks", []) if cli.is_our_handler(h)]


class WinCli(FakeHome):
    def env(self, **extra):
        e = dict(WIN)
        e.update(extra)
        return FakeHome.env(self, **e)

    def seed(self, settings=None, md=None):
        if settings is not None:
            self.write(os.path.join(self.claude, "settings.json"), settings)
        if md is not None:
            self.write(os.path.join(self.claude, "CLAUDE.md"), md)


# ----------------------------------------------------------------- hook entry form
class WindowsHookFormTest(WinCli):
    def test_exec_form_entries(self):
        self.install("--no-service")
        s = self.settings()
        script = cli.fwd(os.path.join(self.tt, "app", "hooks", "tokentier_log.py"))
        for ev in EVENTS:
            h = ours(s, ev)
            self.assertEqual(len(h), 1, ev)
            h = h[0]
            self.assertEqual(list(h), ["type", "command", "args", "timeout"])
            self.assertEqual(h["type"], "command")
            self.assertEqual(h["command"], cli.fwd(cli.hook_python()))
            self.assertTrue(os.path.isabs(h["command"]))
            self.assertNotIn("\\", h["command"] + "".join(h["args"]))
            self.assertEqual(h["args"], [script, ev])  # default home: no --home
            self.assertEqual(h["timeout"], 10)
            self.assertNotIn("shell", h)
        self.assertEqual(set(s["hooks"]["Stop"][0]), {"hooks"})  # Stop takes no matcher
        self.assertEqual(s["hooks"]["SubagentStop"][0]["matcher"], "")

    def test_exec_form_runs_without_a_shell(self):
        """Spawn the registered command + args directly (what Claude Code does in exec form)."""
        self.install("--no-service")
        s = self.settings()
        proj = os.path.join(self.tmp, "my proj")
        os.makedirs(proj)
        for ev in ("SessionStart", "SessionEnd"):
            h = ours(s, ev)[0]
            r = subprocess.run([h["command"]] + h["args"], input=json.dumps({"session_id": "w1", "cwd": proj}),
                               env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=30)
            self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        lines = []
        for p in glob.glob(os.path.join(self.tt, "logs", "*.jsonl")):
            raw = self.read(p)
            self.assertNotIn(b"\r", raw)  # JSONL is LF on every OS
            lines += [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
        self.assertEqual(sorted(l["event"] for l in lines), ["session_end", "session_start"])
        self.assertEqual(lines[0]["project"], "my proj")

    def test_custom_home_passed_as_argument(self):
        custom = os.path.join(self.tmp, "tt home")
        self.install("--no-service", "--home", custom)
        h = ours(self.settings(), "SessionStart")[0]
        self.assertEqual(h["args"][-3:], ["SessionStart", "--home", cli.fwd(custom)])
        r = subprocess.run([h["command"]] + h["args"], input=json.dumps({"session_id": "s1", "cwd": self.tmp}),
                           env=self.env(), universal_newlines=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(glob.glob(os.path.join(custom, "logs", "*.jsonl"))), 1)
        self.assertFalse(os.path.exists(self.tt))
        self.uninstall("--home", custom)

    def test_round_trip_crlf_and_bom_byte_exact(self):
        cases = {"bom_crlf": (BOM_CRLF_SETTINGS, BOM_CRLF_MD), "crlf_no_trailing": (CRLF_SETTINGS, CRLF_MD_NO_NL),
                 "rich": (fixture("rich_settings.json"), b"x\r\n"), "missing": (None, None),
                 "empty": (b"", b"")}
        for name, (st, md) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.seed(st, md)
                before = snapshot(self.home)
                self.install("--no-service")
                raw = self.read(os.path.join(self.claude, "settings.json"))
                if st and st.startswith(b"\xef\xbb\xbf"):
                    self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
                if st and b"\r\n" in st:
                    self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))  # CRLF kept everywhere
                    self.assertIn(b'"args"', raw)
                if md and b"\r\n" in md:
                    m = self.read(os.path.join(self.claude, "CLAUDE.md"))
                    self.assertNotIn(b"\n", m.replace(b"\r\n", b""))
                    if md.startswith(b"\xef\xbb\xbf"):
                        self.assertTrue(m.startswith(b"\xef\xbb\xbf"))
                for ev in EVENTS:
                    self.assertEqual(len(ours(self.settings(), ev)), 1)
                self.uninstall()
                self.assertEqual(self.read(os.path.join(self.claude, "settings.json")), st)
                self.assertEqual(self.read(os.path.join(self.claude, "CLAUDE.md")), md)
                after = {k: v for k, v in snapshot(self.home).items() if not k.startswith(".tokentier")}
                self.assertEqual(after, before)

    def test_upgrade_posix_entries_to_exec_form_and_back(self):
        self.seed(BOM_CRLF_SETTINGS, None)
        self.install("--no-service", env=FakeHome.env(self, TOKENTIER_PLATFORM="darwin"))
        h = ours(self.settings(), "SubagentStop")[0]
        self.assertNotIn("args", h)
        self.assertTrue(h["command"].startswith("python3 "))
        # same machine, now treated as Windows: converted in place, still exactly one entry per event
        self.install("--no-service")
        for ev in EVENTS:
            hs = ours(self.settings(), ev)
            self.assertEqual(len(hs), 1, ev)
            self.assertIn("args", hs[0])
        # and back to Linux: args removed again
        self.install("--no-service", env=FakeHome.env(self, TOKENTIER_PLATFORM="linux"))
        h = ours(self.settings(), "SubagentStop")[0]
        self.assertNotIn("args", h)
        self.uninstall()
        self.assertEqual(self.read(os.path.join(self.claude, "settings.json")), BOM_CRLF_SETTINGS)

    def test_modified_windows_entry_left_in_place(self):
        self.install("--no-service")
        sp = os.path.join(self.claude, "settings.json")
        s = self.settings()
        ours(s, "SessionEnd")[0]["timeout"] = 30
        self.write(sp, json.dumps(s, indent=2) + "\n")
        p = self.uninstall()
        self.assertIn("SessionEnd hook was modified by you; left it in place", p.stdout)
        self.assertEqual(sum(len(ours(self.settings(), ev)) for ev in self.settings()["hooks"]), 1)

    def test_dry_run_writes_nothing_and_shows_exec_form(self):
        self.seed(CRLF_SETTINGS, BOM_CRLF_MD)
        before = snapshot(self.home)
        p = self.run_cli("install", "--dry-run", check=True)
        self.assertEqual(snapshot(self.home), before)
        self.assertIn('"args"', p.stdout)
        self.assertIn("tokentier_log.py", p.stdout)
        self.assertIn("schtasks", p.stdout)
        self.assertIn("Dry run: nothing was written", p.stdout)

    def test_doctor_checks_hook_interpreter(self):
        self.install("--no-service")
        d = self.run_cli("doctor")
        self.assertRegex(d.stdout, r"(?m)^PASS hook python\b")
        self.assertRegex(d.stdout, r"(?m)^PASS hook self-test\b")
        self.assertNotIn("python3 on PATH", d.stdout)
        sp = os.path.join(self.claude, "settings.json")
        s = self.settings()
        for ev in EVENTS:
            ours(s, ev)[0]["command"] = "C:/nowhere/python.exe"
        self.write(sp, json.dumps(s, indent=2))
        d = self.run_cli("doctor")
        self.assertEqual(d.returncode, 1)
        self.assertRegex(d.stdout, r"(?m)^FAIL hook python .*C:/nowhere/python\.exe")

    def test_doctor_flags_posix_form_on_windows(self):
        self.install("--no-service", env=FakeHome.env(self, TOKENTIER_PLATFORM="linux"))
        d = self.run_cli("doctor")
        self.assertRegex(d.stdout, r"(?m)^WARN hook form\b")

    def test_cmd_shim(self):
        self.install("--no-service")
        shim = os.path.join(self.tt, "app", "bin", "tokentier.cmd")
        data = self.read(shim)
        self.assertEqual(data, cli.cmd_shim(cli.hook_python()))
        self.assertTrue(data.startswith(b'@"') and data.endswith(b'" "%~dp0tokentier" %*\r\n'))
        m = self.manifest()
        self.assertEqual(m["app"]["files"]["bin/tokentier.cmd"], cli.sha256_bytes(data))
        p = self.install("--no-service")
        self.assertIn("Nothing to do", p.stdout)  # idempotent, shim not rewritten
        self.uninstall()
        self.assertFalse(os.path.exists(shim))

    def test_no_shim_on_posix(self):
        self.install("--no-service", env=FakeHome.env(self, TOKENTIER_PLATFORM="linux"))
        self.assertFalse(os.path.exists(os.path.join(self.tt, "app", "bin", "tokentier.cmd")))
        self.assertNotIn("bin/tokentier.cmd", self.manifest()["app"]["files"])

    def test_install_output_mentions_path_tip_and_cmd(self):
        p = self.install("--no-service")
        self.assertIn("tokentier.cmd", p.stdout)
        self.assertIn("PATH tip", p.stdout)
        self.assertIn("does not change PATH", p.stdout)
        self.assertNotIn("alias tokentier=", p.stdout)


class HookDetectionTest(unittest.TestCase):
    def test_both_forms_detected(self):
        posix = {"type": "command", "command": "python3 /h/.tokentier/app/hooks/tokentier_log.py Stop"}
        win = {"type": "command", "command": "C:/Python312/python.exe",
               "args": ["C:/Users/A B/.tokentier/app/hooks/tokentier_log.py", "Stop"]}
        other = {"type": "command", "command": "C:/x/node.exe", "args": ["lint.js"]}
        for h in (posix, win):
            self.assertTrue(cli.is_our_handler(h))
        for h in (other, {"type": "command", "command": "echo"}, "junk", None, {"args": "tokentier_log.py"}):
            self.assertFalse(cli.is_our_handler(h))
        groups = [{"hooks": [other, win]}, {"hooks": [posix]}, "bad", {"hooks": "bad"}]
        self.assertEqual(cli.our_hook_refs(groups), [(0, 1), (1, 0)])

    def test_count_in_mixed_settings_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        p = os.path.join(d, "settings.json")
        data = {"hooks": {"Stop": [{"hooks": [cli.hook_handler("Stop", "/h/.tokentier", "win32")]}],
                          "SessionEnd": [cli.hook_group("SessionEnd", "/h/.tokentier", "linux")]}}
        with open(p, "w") as f:
            json.dump(data, f)
        counts, err = cli.count_our_hooks(p)
        self.assertIsNone(err)
        self.assertEqual((counts["Stop"], counts["SessionEnd"], counts["SessionStart"]), (1, 1, 0))
        self.assertEqual(len(cli.our_handlers(p)), 2)

    def test_handler_paths_with_spaces_and_backslashes(self):
        with mock.patch.object(cli, "hook_python", return_value="C:\\Program Files\\Python312\\python.exe"), \
                mock.patch.object(cli, "default_tokentier_home", return_value="C:\\Users\\Ann Lee\\.tokentier"):
            h = cli.hook_handler("SubagentStop", "C:\\Users\\Ann Lee\\.tokentier", "win32")
            self.assertEqual(h["command"], "C:/Program Files/Python312/python.exe")
            self.assertEqual(len(h["args"]), 2)  # one argument per element, no quoting
            self.assertTrue(h["args"][0].startswith("C:/Users/Ann Lee/.tokentier"))
            self.assertTrue(h["args"][0].endswith("/app/hooks/tokentier_log.py"))
            self.assertEqual(h["args"][1], "SubagentStop")
            custom = cli.hook_handler("Stop", "D:\\tt data", "win32")
            self.assertEqual(custom["args"][-2:], ["--home", "D:/tt data"])

    def test_posix_command_unchanged(self):
        # byte-for-byte the v0.1 form; existing installs are matched by it
        with mock.patch.object(cli, "default_tokentier_home", return_value="/home/u/.tokentier"):
            self.assertEqual(cli.hook_handler("Stop", "/home/u/.tokentier", "linux"),
                             {"type": "command", "command": "python3 /home/u/.tokentier/app/hooks/tokentier_log.py Stop",
                              "timeout": 10})
            self.assertEqual(cli.hook_handler("Stop", "/a b", "darwin")["command"],
                             "TOKENTIER_HOME='/a b' python3 '/a b/app/hooks/tokentier_log.py' Stop")

    def test_update_handler(self):
        h = {"type": "command", "command": "python3 /x/tokentier_log.py Stop", "timeout": 99, "statusMessage": "x"}
        want = {"type": "command", "command": "C:/py/python.exe", "args": ["C:/t/tokentier_log.py", "Stop"],
                "timeout": 10}
        cli.update_handler(h, want)
        self.assertEqual(h, {"type": "command", "command": "C:/py/python.exe", "timeout": 99, "statusMessage": "x",
                             "args": ["C:/t/tokentier_log.py", "Stop"]})
        cli.update_handler(h, {"type": "command", "command": "python3 /x/tokentier_log.py Stop", "timeout": 10})
        self.assertNotIn("args", h)

    def test_platform_override(self):
        for v, want in (("win32", "win32"), ("Windows", "win32"), ("nt", "win32"), ("darwin", "darwin"),
                        ("linux2", "linux")):
            with mock.patch.dict(os.environ, {"TOKENTIER_PLATFORM": v}):
                self.assertEqual(cli.platform_name(), want)
                ctx = cli.Ctx("/tmp/x")
                if want == "win32":
                    ctx.service_exec = False
                    self.assertEqual(cli.service_manager(ctx), ("schtasks", None))


# --------------------------------------------------------------------- paths
class PathTest(unittest.TestCase):
    def test_backup_rel_windows(self):
        self.assertEqual(cli.backup_rel("C:\\Users\\Ann\\.claude\\settings.json", ntpath),
                         "C\\Users\\Ann\\.claude\\settings.json")
        self.assertEqual(cli.backup_rel("c:/Users/Ann/x.json", ntpath), "c\\Users\\Ann\\x.json")
        self.assertEqual(cli.backup_rel("\\\\srv\\share\\h\\settings.json", ntpath),
                         "UNC\\srv\\share\\h\\settings.json")
        # the point of it: joined under the backup dir, never escaping it
        bdir = "C:\\Users\\Ann\\.tokentier\\backups\\20261004-120000"
        dest = ntpath.join(bdir, cli.backup_rel("C:\\Users\\Ann\\.claude\\settings.json", ntpath))
        self.assertTrue(dest.startswith(bdir + "\\"), dest)
        self.assertFalse(ntpath.isabs(cli.backup_rel("D:\\x", ntpath)))

    def test_backup_rel_posix_unchanged(self):
        self.assertEqual(cli.backup_rel("/Users/a/.claude/settings.json", posixpath), "Users/a/.claude/settings.json")
        p = "/Users/a/.claude/settings.json"
        self.assertEqual(cli.backup_rel(p), os.path.abspath(p).lstrip(os.sep))

    def test_same_path_case_insensitive_with_windows_normcase(self):
        with mock.patch.object(cli.os.path, "normcase", ntpath.normcase):
            self.assertTrue(cli.same_path("/tmp/Proj/A", "/tmp/proj/a"))
            m = {"projects": [{"project_dir": "/tmp/Work/MyProj"}]}
            self.assertIs(cli.find_project(m, "/tmp/work/myproj"), m["projects"][0])
        if os.path.normcase("A") == "A":  # POSIX: still case-sensitive
            self.assertFalse(cli.same_path("/tmp/Proj", "/tmp/proj"))

    def test_fwd_and_quote(self):
        self.assertEqual(cli.fwd("C:\\a b\\c"), "C:/a b/c")
        self.assertEqual(cli.shell_quote("C:\\a b", "win32"), '"C:\\a b"')
        self.assertEqual(cli.shell_quote("/a b", "linux"), "'/a b'")

    def test_windowless_python(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        py = os.path.join(d, "python.exe")
        self.assertEqual(cli.windowless_python(py), py)  # no pythonw next to it
        open(os.path.join(d, "pythonw.exe"), "w").close()
        self.assertEqual(cli.windowless_python(py), os.path.join(d, "pythonw.exe"))
        self.assertEqual(cli.windowless_python("/usr/bin/python3"), "/usr/bin/python3")
        self.assertEqual(tl.child_python("C:/P/python3.12.exe", isfile=lambda p: True),
                         os.path.join("C:/P", "pythonw3.12.exe"))
        self.assertEqual(tl.child_python("C:/P/python.exe", isfile=lambda p: False), "C:/P/python.exe")

    def test_safe_console_on_cp1252(self):
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding="cp1252")
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", out):
            cli._safe_console()
            cli.say("ok \u2713 \u00e9")
            out.flush()
        self.assertEqual(buf.getvalue(), b"ok ? \xe9\n")


# ---------------------------------------------------------------- atomic writes
class AtomicWriteRetryTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, True))
        self.p = os.path.join(self.d, "settings.json")
        with open(self.p, "wb") as f:
            f.write(b"old")

    def flaky(self, errors):
        real = os.replace
        calls = []

        def fake(src, dst):
            calls.append((src, dst))
            if errors:
                raise errors.pop(0)
            return real(src, dst)
        return fake, calls

    def test_cli_retries_permission_error(self):
        e32 = OSError(13, "sharing violation")
        e32.winerror = 32
        fake, calls = self.flaky([PermissionError(13, "denied"), e32])
        with mock.patch.object(cli.os, "replace", fake), mock.patch.object(cli.time, "sleep") as sl:
            cli.atomic_write(self.p, b"new")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sl.call_count, 2)
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), b"new")
        self.assertEqual(os.listdir(self.d), ["settings.json"])  # temp file gone

    def test_cli_gives_up_and_cleans_up(self):
        fake, calls = self.flaky([PermissionError(13, "denied")] * 20)
        with mock.patch.object(cli.os, "replace", fake), mock.patch.object(cli.time, "sleep"):
            with self.assertRaises(PermissionError):
                cli.atomic_write(self.p, b"new")
        self.assertEqual(len(calls), cli.REPLACE_RETRIES)
        self.assertEqual(os.listdir(self.d), ["settings.json"])
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), b"old")

    def test_cli_does_not_retry_other_errors(self):
        fake, calls = self.flaky([FileNotFoundError(2, "gone")])
        with mock.patch.object(cli.os, "replace", fake), mock.patch.object(cli.time, "sleep"):
            with self.assertRaises(FileNotFoundError):
                cli.atomic_write(self.p, b"new")
        self.assertEqual(len(calls), 1)

    def test_hook_state_write_retries(self):
        fake, calls = self.flaky([PermissionError(13, "denied")] * 3)
        sp = os.path.join(self.d, "s.json")
        with mock.patch.object(tl.os, "replace", fake), mock.patch.object(tl.time, "sleep"):
            tl._write_state(sp, {"offset": 1, "emitted": {}, "history_skipped": False})
        self.assertEqual(len(calls), 4)
        self.assertEqual(tl._read_state(sp)["offset"], 1)
        self.assertEqual(sorted(os.listdir(self.d)), ["s.json", "settings.json"])

    def test_hook_replace_retry_limits(self):
        sleeps = []
        e = OSError(5, "access denied")
        e.winerror = 5
        with mock.patch.object(tl.os, "replace", side_effect=[e, e, None]):
            tl.replace_retry("a", "b", sleep=sleeps.append)
        self.assertEqual(sleeps, [tl.REPLACE_DELAY] * 2)
        with mock.patch.object(tl.os, "replace", side_effect=IsADirectoryError(21, "dir")):
            with self.assertRaises(IsADirectoryError):
                tl.replace_retry("a", "b", sleep=sleeps.append)


# ------------------------------------------------------------------ locking
class FakeMsvcrt(object):
    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self, fail_times=0, exc=None):
        self.fail_times, self.exc, self.calls = fail_times, exc, []

    def locking(self, fd, mode, nbytes):
        self.calls.append((fd, mode, nbytes, os.lseek(fd, 0, 1)))
        if mode == self.LK_NBLCK:
            if self.exc is not None:
                raise self.exc
            if self.fail_times:
                self.fail_times -= 1
                raise OSError(36, "Resource deadlock avoided")


class LockTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, True))

    def use(self, fake):
        p1 = mock.patch.object(tl, "fcntl", None)
        p2 = mock.patch.object(tl, "msvcrt", fake)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def lines(self):
        out = []
        for p in glob.glob(os.path.join(self.home, "logs", "*.jsonl")):
            with open(p, "rb") as f:
                raw = f.read()
            self.assertNotIn(b"\r", raw)
            out += [json.loads(l) for l in raw.decode().splitlines() if l.strip()]
        return out

    def test_msvcrt_branch_locks_separate_file(self):
        fake = FakeMsvcrt()
        self.use(fake)
        tl.append_event(self.home, {"event": "x", "n": 1})
        tl.append_event(self.home, {"event": "x", "n": 2})
        self.assertEqual([l["n"] for l in self.lines()], [1, 2])
        modes = [c[1] for c in fake.calls]
        self.assertEqual(modes, [fake.LK_NBLCK, fake.LK_UNLCK] * 2)
        self.assertTrue(all(c[2] == 1 and c[3] == 0 for c in fake.calls))  # 1 byte at offset 0
        self.assertTrue(os.path.isfile(os.path.join(self.home, "logs", tl.APPEND_LOCK_NAME)))

    def test_msvcrt_retries_then_locks(self):
        fake = FakeMsvcrt(fail_times=3)
        self.use(fake)
        tl.append_event(self.home, {"event": "x"})
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual([c[1] for c in fake.calls].count(fake.LK_NBLCK), 4)
        self.assertEqual([c[1] for c in fake.calls][-1], fake.LK_UNLCK)

    def test_lock_timeout_still_appends_quickly(self):
        fake = FakeMsvcrt(fail_times=10 ** 6)
        self.use(fake)
        with mock.patch.object(tl, "APPEND_LOCK_WAIT", 0.3):
            t0 = time.time()
            tl.append_event(self.home, {"event": "x"})
            el = time.time() - t0
        self.assertEqual(len(self.lines()), 1)  # never lost
        self.assertLess(el, 1.0)
        self.assertNotIn(fake.LK_UNLCK, [c[1] for c in fake.calls])  # not locked -> no unlock

    def test_default_lock_wait_is_about_one_second(self):
        self.assertLessEqual(tl.APPEND_LOCK_WAIT, 1.0)
        fake = FakeMsvcrt(fail_times=10 ** 6)
        self.use(fake)
        t0 = time.time()
        tl.append_event(self.home, {"event": "x"})
        self.assertLess(time.time() - t0, 1.6)
        self.assertEqual(len(self.lines()), 1)

    def test_unexpected_lock_error_never_crashes(self):
        self.use(FakeMsvcrt(exc=RuntimeError("weird")))
        tl.append_event(self.home, {"event": "x"})
        self.assertEqual(len(self.lines()), 1)

    def test_no_locking_primitive(self):
        self.use(None)
        tl.append_event(self.home, {"event": "x"})
        self.assertEqual(len(self.lines()), 1)
        with tl._Lock(os.path.join(self.home, "a.lock"), 0.1) as lk:
            self.assertTrue(lk.acquired)

    def test_state_lock_msvcrt(self):
        fake = FakeMsvcrt()
        self.use(fake)
        with tl._Lock(os.path.join(self.home, "s.lock"), 0.5) as lk:
            self.assertTrue(lk.acquired)
        self.assertEqual([c[1] for c in fake.calls], [fake.LK_NBLCK, fake.LK_UNLCK])
        busy = FakeMsvcrt(fail_times=10 ** 6)
        self.use(busy)
        t0 = time.time()
        with tl._Lock(os.path.join(self.home, "s.lock"), 0.2) as lk:
            self.assertFalse(lk.acquired)
        self.assertLess(time.time() - t0, 1.0)

    def test_lead_flush_gives_up_when_state_locked(self):
        self.use(FakeMsvcrt(fail_times=10 ** 6))
        tp = os.path.join(self.home, "t.jsonl")
        with open(tp, "w") as f:
            f.write("{}\n")
        out = tl.lead_flush(self.home, {"session_id": "s", "transcript_path": tp}, tl._dt.datetime.now(
            tl._dt.timezone.utc), wait=0.1)
        self.assertEqual(out, [])

    def test_hook_handle_with_msvcrt(self):
        self.use(FakeMsvcrt())
        evs = tl.handle("SessionStart", {"session_id": "s1", "cwd": "/x/proj", "source": "startup"}, self.home)
        self.assertEqual([e["event"] for e in evs], ["session_start"])
        self.assertEqual([l["event"] for l in self.lines()], ["session_start"])

    def test_cli_append_lines_msvcrt_branch(self):
        fake = FakeMsvcrt()
        p = os.path.join(self.home, "logs", "2026-10-04.jsonl")
        with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": fake}):
            cli._append_lines(p, [{"event": "a"}, {"event": "b"}])
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b'{"event":"a"}\n{"event":"b"}\n')
        self.assertEqual([c[1] for c in fake.calls], [fake.LK_NBLCK, fake.LK_UNLCK])

    @unittest.skipIf(IS_WINDOWS, "fcntl is POSIX-only")
    def test_posix_flock_path_unchanged(self):
        self.assertIsNotNone(tl.fcntl)
        tl.append_event(self.home, {"event": "x"})
        self.assertEqual(len(self.lines()), 1)
        self.assertFalse(os.path.exists(os.path.join(self.home, "logs", tl.APPEND_LOCK_NAME)))


# ------------------------------------------------------------ hook process bits
class HookProcessTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, True))

    def test_spawn_label_child_windows_flags(self):
        calls = []

        def popen(args, **kw):
            calls.append((args, kw))
            if len(calls) == 1:
                raise OSError(5, "breakaway not allowed")
            return mock.Mock()
        tl.spawn_label_child(self.home, {"session_id": "s", "agent_id": "a", "transcript_path": "t", "cwd": "c"},
                             "fast-worker", popen=popen, windows=True)
        self.assertEqual(len(calls), 2)
        (a1, k1), (a2, k2) = calls
        self.assertEqual(k1["creationflags"], tl.WIN_DETACHED_PROCESS | tl.WIN_CREATE_NEW_PROCESS_GROUP
                         | tl.WIN_CREATE_BREAKAWAY_FROM_JOB)
        self.assertEqual(k2["creationflags"], tl.WIN_DETACHED_PROCESS | tl.WIN_CREATE_NEW_PROCESS_GROUP)
        for k in (k1, k2):
            self.assertTrue(k["close_fds"])
            self.assertEqual((k["stdin"], k["stdout"], k["stderr"]), (subprocess.DEVNULL,) * 3)
            self.assertEqual(k["env"]["TOKENTIER_HOME"], self.home)
            self.assertNotIn("start_new_session", k)
            self.assertNotIn("shell", k)
        self.assertEqual(a1[2:], ["_label", "s", "a", "t", "c", "fast-worker"])

    def test_spawn_label_child_posix(self):
        calls = []
        tl.spawn_label_child(self.home, {"agent_id": "a"}, "x", popen=lambda a, **k: calls.append((a, k)),
                             windows=False)
        a, k = calls[0]
        self.assertEqual(a[0], sys.executable)
        self.assertTrue(k["start_new_session"])
        self.assertNotIn("creationflags", k)
        self.assertEqual(k["env"]["TOKENTIER_HOME"], self.home)

    def test_split_home_arg(self):
        self.assertEqual(tl.split_home_arg(["h", "Stop", "--home", "C:/t"]), (["h", "Stop"], "C:/t"))
        self.assertEqual(tl.split_home_arg(["h", "Stop"]), (["h", "Stop"], None))
        self.assertEqual(tl.split_home_arg(["h", "_label", "--home", "x", "y"]),
                         (["h", "_label", "--home", "x", "y"], None))

    def test_home_argument_and_utf8_stdin_on_ansi_console(self):
        """Exec-form call with --home, and a UTF-8 payload read correctly although the child's text
        stdin would decode as cp1252 (what a Windows console gives python.exe)."""
        target = os.path.join(self.home, "custom")
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        env.pop("TOKENTIER_HOME", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        payload = json.dumps({"session_id": "s1", "cwd": "/x/Projekt-\u00fc\u2713"}, ensure_ascii=False).encode()
        r = subprocess.run([sys.executable, tl.__file__, "SessionStart", "--home", target], input=payload, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)
        files = glob.glob(os.path.join(target, "logs", "*.jsonl"))
        self.assertEqual(len(files), 1)
        with open(files[0], "rb") as f:
            ev = json.loads(f.read().decode("utf-8"))
        self.assertEqual(ev["project"], "Projekt-\u00fc\u2713")


# ----------------------------------------------------------------- port probe
class FakeSock(object):
    instances = []
    fail_bind = False

    def __init__(self, *a):
        self.opts = []
        FakeSock.instances.append(self)

    def setsockopt(self, level, opt, val):
        self.opts.append((level, opt, val))

    def bind(self, addr):
        if FakeSock.fail_bind:
            raise OSError(10048, "in use")

    def close(self):
        pass


class PortProbeTest(unittest.TestCase):
    def probe(self, plat, fail_bind=False):
        FakeSock.instances, FakeSock.fail_bind = [], fail_bind
        with mock.patch.dict(os.environ, {"TOKENTIER_PLATFORM": plat}), \
                mock.patch.object(cli.socket, "socket", FakeSock), \
                mock.patch.object(cli.socket, "SO_EXCLUSIVEADDRUSE", -5, create=True), \
                mock.patch.object(cli.socket, "create_connection", side_effect=OSError("refused")), \
                mock.patch.object(cli, "http_health", return_value=None), \
                mock.patch.object(cli.time, "sleep"):
            st = cli.port_state(8899)
        return st, FakeSock.instances[0].opts

    def test_windows_uses_exclusive_probe(self):
        st, opts = self.probe("win32")
        self.assertEqual(st, "free")
        self.assertEqual(opts, [(socket.SOL_SOCKET, -5, 1)])
        st, opts = self.probe("win32", fail_bind=True)
        self.assertEqual(st, "other")

    def test_posix_keeps_reuseaddr(self):
        st, opts = self.probe("darwin")
        self.assertEqual((st, opts), ("free", [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]))

    def test_windows_real_listener_is_busy(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        with mock.patch.dict(os.environ, {"TOKENTIER_PLATFORM": "win32"}), mock.patch.object(cli.time, "sleep"):
            self.assertEqual(cli.port_state(s.getsockname()[1], retries=0), "other")


class HealthProbeProxyTest(unittest.TestCase):
    def test_health_probe_ignores_proxies(self):
        port = free_port()
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                                          "NO_PROXY": "", "no_proxy": ""}):
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class H(BaseHTTPRequestHandler):
                server_version = "TokenTier"

                def do_GET(self):
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass
            s = HTTPServer(("127.0.0.1", port), H)
            t = threading.Thread(target=s.serve_forever, daemon=True)
            t.start()
            try:
                self.assertEqual(cli.http_health(port), {"ok": True})
            finally:
                s.shutdown()
                s.server_close()


# --------------------------------------------------------------- Task Scheduler
class TaskXmlTest(unittest.TestCase):
    def test_xml_fields(self):
        home = "C:\\Users\\Ann & Bo\\.tokentier"
        py = "C:\\Program Files\\Python312\\pythonw.exe"
        data = cli.task_xml(home, 8899, py, user="PC\\ann")
        self.assertTrue(data.startswith(b"\xff\xfe"))  # UTF-16 LE BOM, as schtasks expects
        self.assertIn('encoding="UTF-16"', data.decode("utf-16"))
        root = ET.fromstring(data)
        f = lambda path: root.find(path, TASK_NS)  # noqa: E731
        self.assertEqual(root.tag, "{%s}Task" % TASK_NS["t"])
        self.assertEqual(f("t:Triggers/t:LogonTrigger/t:UserId").text, "PC\\ann")
        self.assertEqual(f("t:Triggers/t:LogonTrigger/t:Enabled").text, "true")
        self.assertEqual(f("t:Principals/t:Principal/t:LogonType").text, "InteractiveToken")
        self.assertEqual(f("t:Principals/t:Principal/t:RunLevel").text, "LeastPrivilege")
        expect = {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                  "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true", "ExecutionTimeLimit": "PT0S",
                  "RestartOnFailure/t:Interval": "PT1M", "RestartOnFailure/t:Count": "999", "Enabled": "true"}
        for k, v in expect.items():
            self.assertEqual(f("t:Settings/t:" + k).text, v, k)
        self.assertEqual(f("t:Actions/t:Exec/t:Command").text, py)
        args = f("t:Actions/t:Exec/t:Arguments").text
        self.assertEqual(args, subprocess.list2cmdline([os.path.join(home, "app", "dashboard", "server.py"),
                                                        "--home", home, "--host", "127.0.0.1", "--port", "8899"]))
        self.assertIn('"%s"' % home, args)  # spaces -> quoted; & survived XML escaping
        self.assertEqual(f("t:Actions/t:Exec/t:WorkingDirectory").text, home)  # deletable app dir

    def test_xml_without_user(self):
        root = ET.fromstring(cli.task_xml("/h", 1234, "/py", user=""))
        self.assertIsNone(root.find("t:Triggers/t:LogonTrigger/t:UserId", TASK_NS))

    def test_runkey_command(self):
        self.assertEqual(cli.runkey_command("C:\\U\\A B\\.tokentier", 8899, "C:\\P\\pythonw.exe"),
                         'C:\\P\\pythonw.exe "%s" --home "C:\\U\\A B\\.tokentier" --host 127.0.0.1 --port 8899'
                         % os.path.join("C:\\U\\A B\\.tokentier", "app", "dashboard", "server.py"))


class FakeWinreg(object):
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self):
        self.values = {}
        self.ops = []

    def OpenKey(self, root, path, res, access):
        return (root, path)

    def CreateKeyEx(self, root, path, res, access):
        return (root, path)

    def CloseKey(self, k):
        pass

    def QueryValueEx(self, k, name):
        if (k, name) not in self.values:
            raise FileNotFoundError(2, "no value")
        return self.values[(k, name)], self.REG_SZ

    def SetValueEx(self, k, name, res, typ, value):
        self.ops.append(("set", k, name, value))
        self.values[(k, name)] = value

    def DeleteValue(self, k, name):
        self.ops.append(("del", k, name))
        if (k, name) not in self.values:
            raise FileNotFoundError(2, "no value")
        del self.values[(k, name)]


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class SchtasksTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, True))
        self.ctx = cli.Ctx(self.home)
        self.ctx.platform = "win32"
        self.ctx.service_exec = True  # every external call below is mocked
        self.reg = FakeWinreg()
        p = mock.patch.object(cli, "WINREG", self.reg)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        self.path = os.path.join(self.home, cli.TASK_XML_NAME)
        self.svc = {"kind": "schtasks", "path": self.path, "port": 8899, "python": "C:/P/pythonw.exe"}

    def run_with(self, responses, fn, *a):
        def fake_run(cmd, **kw):
            self.calls.append((cmd, kw))
            return responses.pop(0) if responses else cp()
        with mock.patch.object(cli.subprocess, "run", fake_run), \
                mock.patch.object(cli, "http_health", return_value=None), \
                mock.patch.object(cli, "start_detached") as sd, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            fn(*a)
        return sd, out.getvalue()

    def argv(self):
        for _, kw in self.calls:
            self.assertNotIn("shell", kw)  # never shell=True
        return [c for c, _ in self.calls]

    def test_create_and_run(self):
        info = {"kind": "schtasks", "path": self.path, "reload": True, "svc": self.svc}
        self.run_with([cp(1, "", "ERROR: The system cannot find the file specified."), cp(0), cp(0, '"x","N/A","Ready"'),
                       cp(0)], cli.ensure_service, self.ctx, info)
        q = ["schtasks", "/Query", "/TN", "TokenTier Dashboard", "/FO", "CSV", "/NH"]
        self.assertEqual(self.argv(), [q, ["schtasks", "/Create", "/TN", "TokenTier Dashboard", "/XML", self.path, "/F"],
                                       q, ["schtasks", "/Run", "/TN", "TokenTier Dashboard"]])
        self.assertNotIn("runkey", self.svc)

    def test_already_running_not_restarted_without_reload(self):
        info = {"kind": "schtasks", "path": self.path, "reload": False, "svc": self.svc}
        self.run_with([cp(0, '"\\TokenTier Dashboard","N/A","Running"'), cp(0, '"x","N/A","Running"')],
                      cli.ensure_service, self.ctx, info)
        self.assertEqual([c[1] for c in self.argv()], ["/Query", "/Query"])

    def test_access_denied_falls_back_to_run_key(self):
        info = {"kind": "schtasks", "path": self.path, "reload": True, "svc": self.svc}
        sd, out = self.run_with([cp(1), cp(1, "", "ERROR: Access is denied.")], cli.ensure_service, self.ctx, info)
        want = cli.runkey_command(self.home, 8899, "C:/P/pythonw.exe")
        self.assertEqual(self.reg.values[(("HKCU", cli.RUNKEY_PATH), "TokenTier Dashboard")], want)
        self.assertEqual(self.svc["runkey"], {"key": "HKCU\\" + cli.RUNKEY_PATH, "name": "TokenTier Dashboard",
                                              "value": want})
        self.assertIn("Access is denied", out)
        self.assertIn("Fallback", out)
        sd.assert_called_once()
        self.assertEqual(sd.call_args[0][0], cli.server_argv(self.home, 8899, "C:/P/pythonw.exe"))
        self.assertNotIn("/Run", [c[1] for c in self.argv()])

    def test_missing_schtasks_falls_back(self):
        info = {"kind": "schtasks", "path": self.path, "reload": True, "svc": self.svc}

        def boom(cmd, **kw):
            raise FileNotFoundError(2, "schtasks")
        with mock.patch.object(cli.subprocess, "run", boom), mock.patch.object(cli, "http_health", return_value=None), \
                mock.patch.object(cli, "start_detached"), mock.patch("sys.stdout", new_callable=io.StringIO):
            cli.ensure_service(self.ctx, info)
        self.assertIn("runkey", self.svc)

    def test_successful_create_removes_old_fallback(self):
        self.reg.values[(("HKCU", cli.RUNKEY_PATH), "TokenTier Dashboard")] = "old"
        self.svc["runkey"] = {"name": "TokenTier Dashboard"}
        info = {"kind": "schtasks", "path": self.path, "reload": True, "svc": self.svc}
        self.run_with([cp(1), cp(0), cp(0, "Running")], cli.ensure_service, self.ctx, info)
        self.assertNotIn("runkey", self.svc)
        self.assertEqual(self.reg.values, {})

    def test_stop_service_ends_deletes_and_removes_run_key(self):
        self.reg.values[(("HKCU", cli.RUNKEY_PATH), "TokenTier Dashboard")] = "x"
        self.svc["runkey"] = {"name": "TokenTier Dashboard"}
        self.run_with([], cli.stop_service, self.ctx, self.svc)
        self.assertEqual(self.argv(), [["schtasks", "/End", "/TN", "TokenTier Dashboard"],
                                       ["schtasks", "/Delete", "/TN", "TokenTier Dashboard", "/F"]])
        self.assertEqual(self.reg.values, {})

    def test_service_running(self):
        self.calls = []
        with mock.patch.object(cli.subprocess, "run", return_value=cp(0, '"\\TokenTier Dashboard","N/A","Running"')), \
                mock.patch.object(cli, "http_health", return_value=None):
            self.assertTrue(cli.service_running(self.ctx, self.svc))
        with mock.patch.object(cli.subprocess, "run", return_value=cp(0, '"x","N/A","Ready"')), \
                mock.patch.object(cli, "http_health", return_value=None):
            self.assertFalse(cli.service_running(self.ctx, self.svc))
        with mock.patch.object(cli.subprocess, "run", return_value=cp(0, '"x","N/A","Bereit"')), \
                mock.patch.object(cli, "http_health", return_value={"ok": True}):
            self.assertTrue(cli.service_running(self.ctx, self.svc))  # localised output: health decides
        self.assertEqual(cli.service_label(self.svc), "scheduled task")
        self.assertIn("Run entry", cli.service_label(dict(self.svc, runkey={"name": "x"})))

    def test_no_service_exec_runs_nothing(self):
        self.ctx.service_exec = False
        info = {"kind": "schtasks", "path": self.path, "reload": True, "svc": self.svc}
        with mock.patch.object(cli.subprocess, "run") as run, mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.ensure_service(self.ctx, info)
            cli.stop_service(self.ctx, dict(self.svc, runkey={"name": "x"}))
            self.assertIsNone(cli.service_running(self.ctx, self.svc))
        run.assert_not_called()
        self.assertEqual(self.reg.ops, [])
        self.assertIn("skipped, TOKENTIER_NO_SERVICE_EXEC", out.getvalue())

    def test_unlink_retry_warns_instead_of_crashing(self):
        e = OSError(13, "in use")
        e.winerror = 32
        with mock.patch.object(cli.os, "unlink", side_effect=e), mock.patch.object(cli.time, "sleep") as sl, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertFalse(cli.unlink_retry("C:/x/dashboard.log"))
        self.assertEqual(sl.call_count, cli.REPLACE_RETRIES - 1)
        self.assertIn("in use", out.getvalue())
        with mock.patch.object(cli.os, "unlink", side_effect=[e, None]), mock.patch.object(cli.time, "sleep"):
            self.assertTrue(cli.unlink_retry("C:/x/dashboard.log"))
        with mock.patch.object(cli.os, "unlink", side_effect=FileNotFoundError(2, "gone")):
            self.assertTrue(cli.unlink_retry("C:/x/missing"))

    def test_runkey_helpers(self):
        self.assertIsNone(cli.runkey_get())
        self.assertFalse(cli.runkey_delete())
        cli.runkey_set("cmd line")
        self.assertEqual(cli.runkey_get(), "cmd line")
        self.assertTrue(cli.runkey_delete())
        self.assertIsNone(cli.runkey_get())


class WindowsServiceCliTest(WinCli):
    def test_install_writes_task_xml_and_uninstall_removes_it(self):
        port = free_port()
        p = self.install("--port", str(port))
        self.assertIn('skipped, TOKENTIER_NO_SERVICE_EXEC is set) schtasks /Create /TN "TokenTier Dashboard"', p.stdout)
        self.assertIn("scheduled task", p.stdout)
        path = os.path.join(self.tt, cli.TASK_XML_NAME)
        data = self.read(path)
        root = ET.fromstring(data)
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", TASK_NS).text,
                         subprocess.list2cmdline([os.path.join(self.tt, "app", "dashboard", "server.py"), "--home",
                                                  self.tt, "--host", "127.0.0.1", "--port", str(port)]))
        m = self.manifest()
        self.assertEqual((m["service"]["kind"], m["service"]["path"], m["service"]["label"]),
                         ("schtasks", path, "TokenTier Dashboard"))
        st = self.run_cli("status", check=True).stdout
        self.assertIn("service          : scheduled task", st)
        d = self.run_cli("doctor").stdout
        self.assertRegex(d, r"(?m)^PASS service +scheduled task file present")
        again = self.install("--port", str(port))
        self.assertIn("Nothing to do", again.stdout)
        u = self.uninstall()
        self.assertIn("schtasks /End /TN TokenTier Dashboard", u.stdout)
        self.assertIn("schtasks /Delete /TN TokenTier Dashboard /F", u.stdout)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(sorted(k for k in snapshot(self.tt) if not k.startswith("logs")), [])

    def test_uninstall_with_recorded_run_key(self):
        self.install("--port", str(free_port()))
        mp = os.path.join(self.tt, "install-manifest.json")
        m = self.manifest()
        m["service"]["runkey"] = {"key": "HKCU\\" + cli.RUNKEY_PATH, "name": cli.RUNKEY_NAME, "value": "x"}
        self.write(mp, json.dumps(m, indent=2))
        u = self.uninstall()
        self.assertIn("skipped, TOKENTIER_NO_SERVICE_EXEC is set) delete HKCU", u.stdout)

    def test_no_service_flag(self):
        p = self.install("--no-service")
        self.assertIn("tokentier.cmd dashboard", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tt, cli.TASK_XML_NAME)))
        self.assertIsNone(self.manifest()["service"])


# ------------------------------------------------------------------ launchers
class LauncherTest(unittest.TestCase):
    FILES = ("install.ps1", "uninstall.ps1", "install.cmd", "uninstall.cmd", os.path.join("tests", "windows_smoke.ps1"))

    def read(self, name):
        with open(os.path.join(ROOT, name), "rb") as f:
            return f.read()

    def test_exist_and_ascii_only(self):
        for name in self.FILES:
            data = self.read(name)
            data.decode("ascii")  # raises on any non-ASCII byte
            self.assertNotIn(b"\t", data, name)

    def test_cmd_wrappers(self):
        for verb in ("install", "uninstall"):
            data = self.read(verb + ".cmd")
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""))  # CRLF only
            self.assertIn(('powershell -NoProfile -ExecutionPolicy Bypass -File "%%~dp0%s.ps1" %%*' % verb).encode(),
                          data)
            self.assertTrue(data.startswith(b"@echo off"))

    def test_ps1_wrappers(self):
        for verb in ("install", "uninstall"):
            text = self.read(verb + ".ps1").decode("ascii")
            self.assertIn("$ErrorActionPreference = 'Stop'", text)
            for probe in ("exe = 'py'; pre = @('-3')", "exe = 'python'", "exe = 'python3'"):
                self.assertIn(probe, text)
            self.assertLess(text.index("'py'"), text.index("exe = 'python'"))
            self.assertLess(text.index("exe = 'python'"), text.index("'python3'"))
            self.assertIn("sys.version_info >= (3, 9)", text)
            self.assertIn("python.org/downloads", text)
            self.assertIn("$argv += @($cli, '%s')" % verb, text)
            self.assertIn("if ($args) { $argv += $args }", text)
            self.assertIn("exit $LASTEXITCODE", text)
            low = text.lower()
            for bad in ("invoke-webrequest", "iwr ", "downloadstring", "downloadfile", "start-bitstransfer",
                        "invoke-expression", "iex "):
                self.assertNotIn(bad, low)

    def test_smoke_script_is_sandboxed(self):
        text = self.read(os.path.join("tests", "windows_smoke.ps1")).decode("ascii")
        for needle in ("$env:USERPROFILE = $fakeHome", "TOKENTIER_NO_SERVICE_EXEC = '1'", "--no-service",
                       "Remove-Item Env:TOKENTIER_HOME", "Get-FileHash", "'uninstall', '--yes'", "finally"):
            self.assertIn(needle, text)

    def test_cmd_shim_content(self):
        with mock.patch.object(cli.os, "name", "posix"):
            self.assertEqual(cli.cmd_shim("C:\\Python312\\python.exe"),
                             b'@"C:\\Python312\\python.exe" "%~dp0tokentier" %*\r\n')


# ------------------------------------------------------------------ dashboard
class DashboardPortabilityTest(unittest.TestCase):
    def test_explicit_mime_types(self):
        want = {"a.js": "text/javascript; charset=utf-8", "a.css": "text/css; charset=utf-8",
                "a.html": "text/html; charset=utf-8", "a.json": "application/json; charset=utf-8",
                "a.png": "image/png", "a.svg": "image/svg+xml; charset=utf-8", "A.JS": "text/javascript; charset=utf-8"}
        # a Windows registry that maps .js/.css to text/plain must not matter
        with mock.patch.object(srv.mimetypes, "guess_type", return_value=("text/plain", None)):
            for name, ct in want.items():
                self.assertEqual(srv.content_type(name), ct, name)
            self.assertEqual(srv.content_type("x.weird"), "text/plain; charset=utf-8")
        with mock.patch.object(srv.mimetypes, "guess_type", return_value=(None, None)):
            self.assertEqual(srv.content_type("x.bin"), "application/octet-stream")

    def test_second_instance_on_same_port_fails(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        a = srv.make_server(home, "127.0.0.1", 0, quiet=True, retention_interval=0)
        self.addCleanup(a.server_close)
        self.addCleanup(a.store.stop)
        with self.assertRaises(OSError):
            b = srv.make_server(home, "127.0.0.1", a.server_address[1], quiet=True, retention_interval=0)
            b.server_close()

    def test_windows_bind_uses_exclusive_addr(self):
        class S(object):
            def __init__(self):
                self.opts = []

            def setsockopt(self, *a):
                self.opts.append(a)

            def bind(self, addr):
                self.addr = addr

            def getsockname(self):
                return ("127.0.0.1", 4321)
        fake = S()
        server = srv.DashboardServer.__new__(srv.DashboardServer)
        server.socket = fake
        server.server_address = ("127.0.0.1", 0)
        server.allow_reuse_address = False
        with mock.patch.object(srv, "os", types.SimpleNamespace(name="nt")), \
                mock.patch.object(srv.socket, "SO_EXCLUSIVEADDRUSE", -5, create=True):
            server.server_bind()
        self.assertEqual(fake.opts, [(socket.SOL_SOCKET, -5, 1)])
        self.assertEqual((server.server_name, server.server_port), ("127.0.0.1", 4321))
        if os.name != "nt":
            self.assertTrue(srv.DashboardServer.allow_reuse_address)

    def test_streams_redirected_for_pythonw(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            srv._ensure_streams(home)
            print("hello log")
            sys.stderr.write("err line\n")
            f = sys.stdout
        f.close()
        with open(os.path.join(home, "dashboard.log"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello log\nerr line\n")

    def test_home_flag_and_clean_stop(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        port = free_port()
        env = {k: v for k, v in os.environ.items() if k not in ("TOKENTIER_HOME", "TOKENTIER_PORT")}
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard", "server.py"), "--home", home,
                              "--port", str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        health = None
        for _ in range(100):
            health = cli.http_health(port)
            if health:
                break
            time.sleep(0.1)
        self.assertIsNotNone(health)
        self.assertEqual(os.path.normcase(health["log_dir"]), os.path.normcase(os.path.join(home, "logs")))
        p.terminate()  # SIGTERM on POSIX -> handler -> clean shutdown (TerminateProcess on Windows)
        rc = p.wait(timeout=20)
        if os.name != "nt":
            self.assertEqual(rc, 0, p.stderr.read())
        p.stdout.close()
        p.stderr.close()


if __name__ == "__main__":
    unittest.main()
