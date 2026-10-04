"""Upgrade restarts the running dashboard service (it keeps old python code loaded otherwise).
All service-manager calls are mocked (subprocess.run replaced); CLI runs use a throwaway HOME with
TOKENTIER_NO_SERVICE_EXEC=1. Nothing here touches the real launchctl/systemctl/schtasks."""
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tt_helpers import FakeHome, load_cli

cli = load_cli()


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class RestartService(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, True))
        self.ctx = cli.Ctx(self.home)
        self.ctx.service_exec = True
        self.calls = []

    def go(self, plat, kind, restart=True, reload_=False, running=True, fail=(), svc=None):
        """fail: argv prefixes (tuples) that return rc 1. 'running' drives the 'is running' probe."""
        self.ctx.platform = plat
        self.ctx.dry_run = False

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            self.assertNotIn("shell", kw)
            self.assertIsInstance(cmd, list)
            for f in fail:
                if tuple(cmd[:len(f)]) == f:
                    return cp(1, "", "boom")
            if cmd[:2] == ["launchctl", "print"]:
                return cp(0, "state = running" if running else "state = waiting")
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return cp(0, "active\n" if running else "inactive\n")
            if cmd[0] == "schtasks" and cmd[1] == "/Query":
                return cp(0, '"x","N/A","%s"' % ("Running" if running else "Ready"))
            return cp(0)
        info = {"kind": kind, "path": os.path.join(self.home, "svcfile"), "reload": reload_,
                "restart": restart, "svc": svc if svc is not None else {"kind": kind, "port": 8899}}
        with mock.patch.object(cli.subprocess, "run", fake_run), \
                mock.patch.object(cli, "http_health", return_value=None), \
                mock.patch.object(cli, "_uid", return_value=501), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            cli.ensure_service(self.ctx, info)
        return out.getvalue() + err.getvalue()

    def test_launchd_kickstart(self):
        out = self.go("darwin", "launchd")
        self.assertIn(["launchctl", "kickstart", "-k", "gui/501/dev.tokentier.dashboard"], self.calls)
        self.assertNotIn("bootout", [c[1] for c in self.calls])
        self.assertIn("dashboard restarted to load the new version", out)

    def test_launchd_fallback_bootout_bootstrap(self):
        out = self.go("darwin", "launchd", fail=[("launchctl", "kickstart")])
        names = [c[1] for c in self.calls]
        self.assertLess(names.index("kickstart"), names.index("bootout"))
        self.assertIn(["launchctl", "bootstrap", "gui/501", os.path.join(self.home, "svcfile")], self.calls)
        self.assertIn("dashboard restarted", out)

    def test_launchd_failure_warns_with_manual_command(self):
        out = self.go("darwin", "launchd", fail=[("launchctl", "kickstart"), ("launchctl", "bootstrap")])
        self.assertIn("launchctl kickstart -k gui/501/dev.tokentier.dashboard", out)
        self.assertNotIn("dashboard restarted to load", out)

    def test_systemd_restart(self):
        out = self.go("linux", "systemd")
        self.assertIn(["systemctl", "--user", "restart", "tokentier-dashboard.service"], self.calls)
        self.assertIn("dashboard restarted to load the new version", out)

    def test_systemd_failure_never_raises(self):
        out = self.go("linux", "systemd", fail=[("systemctl", "--user", "restart")])
        self.assertIn("systemctl --user restart tokentier-dashboard.service", out)

    def test_schtasks_end_then_run(self):
        out = self.go("win32", "schtasks")
        sch = [c[1] for c in self.calls if c[0] == "schtasks"]
        self.assertEqual(sch[-2:], ["/End", "/Run"])
        self.assertIn(["schtasks", "/End", "/TN", "TokenTier Dashboard"], self.calls)
        self.assertIn("dashboard restarted to load the new version", out)

    def test_runkey_fallback_prints_relogin(self):
        out = self.go("win32", "schtasks", svc={"kind": "schtasks", "port": 8899, "runkey": {"name": "x"}})
        self.assertNotIn("/End", [c[1] for c in self.calls if c[0] == "schtasks"])
        self.assertIn("log off and on again", out)

    def test_no_restart_when_nothing_changed(self):
        for plat, kind in (("darwin", "launchd"), ("linux", "systemd"), ("win32", "schtasks")):
            self.calls = []
            out = self.go(plat, kind, restart=False)
            self.assertNotIn("kickstart", [c[1] for c in self.calls])
            self.assertNotIn(["systemctl", "--user", "restart", "tokentier-dashboard.service"], self.calls)
            self.assertNotIn("/End", [c[1] for c in self.calls if c[0] == "schtasks"])
            self.assertNotIn("restarted", out)

    def test_no_restart_when_not_running(self):
        for plat, kind in (("darwin", "launchd"), ("linux", "systemd"), ("win32", "schtasks")):
            self.calls = []
            out = self.go(plat, kind, running=False)
            self.assertNotIn("kickstart", [c[1] for c in self.calls])
            self.assertNotIn(["systemctl", "--user", "restart", "tokentier-dashboard.service"], self.calls)
            self.assertNotIn("restarted", out)

    def test_no_service_exec_runs_nothing(self):
        self.ctx.service_exec = False
        with mock.patch.object(cli.subprocess, "run") as run, mock.patch("sys.stdout", new_callable=io.StringIO):
            cli.ensure_service(self.ctx, {"kind": "launchd", "path": "p", "reload": False, "restart": True,
                                          "svc": {}})
        run.assert_not_called()


class UpgradeCli(FakeHome):
    def app_file(self):
        return os.path.join(self.tt, "app", "dashboard", "server.py")

    def read_app(self):
        with open(self.app_file(), "rb") as f:
            return f.read()

    def test_dry_run_upgrade_announces_restart(self):
        self.install()
        with open(self.app_file(), "ab") as f:
            f.write(b"# stale\n")
        p = self.run_cli("install", "--dry-run", check=True)
        self.assertIn("service: will restart (app files changed)", p.stdout)
        self.assertTrue(self.read_app().endswith(b"# stale\n"))  # dry run wrote nothing

    def test_idempotent_rerun_does_not_announce_restart(self):
        self.install()
        p = self.run_cli("install", "--dry-run", check=True)
        self.assertNotIn("will restart", p.stdout)
        p = self.install()
        self.assertNotIn("restart", p.stdout.replace("Restart Claude Code", ""))

    def test_no_service_flag_never_restarts(self):
        self.install()
        with open(self.app_file(), "ab") as f:
            f.write(b"# stale\n")
        p = self.run_cli("install", "--dry-run", "--no-service", check=True)
        self.assertNotIn("will restart", p.stdout)

    def test_upgrade_with_service_exec_disabled_skips_restart(self):
        self.install()
        with open(self.app_file(), "ab") as f:
            f.write(b"# stale\n")
        p = self.install()
        self.assertNotIn("dashboard restarted", p.stdout)
        self.assertIn("skipped, TOKENTIER_NO_SERVICE_EXEC", p.stdout)
        self.assertFalse(self.read_app().endswith(b"# stale\n"))


if __name__ == "__main__":
    unittest.main()
