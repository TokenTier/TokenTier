"""Log retention: find_expired boundaries, `tokentier prune`, server pruning, store tolerance."""
import datetime as dt
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from tt_helpers import FakeHome
from dash_helpers import ROOT, TZ, Clock, at, end, start, write

sys.path.insert(0, os.path.join(ROOT, "dashboard"))
import server as srv  # noqa: E402
from tokentier_dash import config as cfg  # noqa: E402

TODAY = dt.date(2026, 10, 20)


def touch(d, name, content=b'{"x":1}\n'):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as f:
        f.write(content)


class FindExpiredTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def names(self, days, today=TODAY):
        return [os.path.basename(p) for p, _, _ in cfg.find_expired(self.d, days, today)]

    def test_boundary_day(self):
        # days=7, today=10-20: cutoff 10-13. 10-13 is exactly 7 days old -> kept; 10-12 -> deleted
        for day in ("2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14"):
            touch(self.d, day + ".jsonl")
        self.assertEqual(self.names(7), ["2026-10-11.jsonl", "2026-10-12.jsonl"])

    def test_today_and_yesterday_never_deleted(self):
        for day in ("2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21"):  # last one is in the future
            touch(self.d, day + ".jsonl")
        self.assertEqual(self.names(1), ["2026-10-18.jsonl"])  # 10-19 is yesterday, protected
        self.assertEqual(self.names(30), [])

    def test_junk_names_untouched(self):
        junk = ["notes.txt", "2026-10-01.jsonl.bak", "2026-10-01.json", "x2026-10-01.jsonl",
                "2026-13-45.jsonl", "2026-1-1.jsonl", "2026-10-01.JSONL", ".2026-10-01.jsonl.tmp",
                "2026-10-01.jsonl~"]
        for n in junk:
            touch(self.d, n)
        os.mkdir(os.path.join(self.d, "2020-01-01.jsonl"))          # a directory with a log name
        try:
            os.symlink(os.path.join(self.d, "notes.txt"), os.path.join(self.d, "2020-01-02.jsonl"))
        except (OSError, NotImplementedError, AttributeError):
            pass  # Windows without symlink privilege: the symlink case is simply not exercised
        touch(self.d, "2020-01-03.jsonl")
        self.assertEqual(self.names(5), ["2020-01-03.jsonl"])

    def test_sizes_and_order(self):
        touch(self.d, "2026-09-02.jsonl", b"a" * 10)
        touch(self.d, "2026-09-01.jsonl", b"b" * 2048)
        got = cfg.find_expired(self.d, 3, TODAY)
        self.assertEqual([(os.path.basename(p), s) for p, _, s in got],
                         [("2026-09-01.jsonl", 2048), ("2026-09-02.jsonl", 10)])
        self.assertEqual(got[0][1], dt.date(2026, 9, 1))

    def test_missing_dir_and_bad_days(self):
        self.assertEqual(cfg.find_expired(os.path.join(self.d, "nope"), 3, TODAY), [])
        for bad in (0, -1, 1.5, "7", True, None):
            with self.assertRaises(cfg.ConfigError):
                cfg.find_expired(self.d, bad, TODAY)

    def test_delete_files_tolerates_vanished(self):
        touch(self.d, "2026-09-01.jsonl")
        items = cfg.find_expired(self.d, 3, TODAY)
        os.unlink(items[0][0])
        self.assertEqual(cfg.delete_files(items), [])


class PruneCliTest(FakeHome):
    def setUp(self):
        FakeHome.setUp(self)
        self.logs = os.path.join(self.tt, "logs")
        today = dt.date.today()
        self.day = lambda n: (today - dt.timedelta(days=n)).isoformat()
        for n in (0, 1, 2, 9, 10, 11, 40):
            touch(self.logs, self.day(n) + ".jsonl")
        touch(self.logs, "keep.txt")
        touch(self.logs, "2026-13-99.jsonl")

    def left(self):
        return sorted(os.listdir(self.logs))

    def test_not_configured(self):
        before = self.left()
        p = self.run_cli("prune")
        self.assertEqual(p.returncode, 0)
        self.assertIn("retention not configured; nothing to do", p.stdout)
        self.assertEqual(self.left(), before)

    def test_dry_run_deletes_nothing_and_lists_files(self):
        before = self.left()
        p = self.run_cli("prune", "--older-than", "10", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.left(), before)
        self.assertIn(self.day(11) + ".jsonl", p.stdout)
        self.assertIn(self.day(40) + ".jsonl", p.stdout)
        self.assertNotIn(self.day(10) + ".jsonl", p.stdout)   # exactly 10 days old: kept
        self.assertIn("Would delete 2 file(s)", p.stdout)
        self.assertRegex(p.stdout, r"\d+ B")
        self.assertIn("Dry run", p.stdout)

    def test_real_prune_with_yes(self):
        p = self.run_cli("prune", "--older-than", "10", "--yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        want = sorted(["keep.txt", "2026-13-99.jsonl"] + [self.day(n) + ".jsonl" for n in (0, 1, 2, 9, 10)])
        self.assertEqual(self.left(), want)
        self.assertIn("Deleted 2 file(s)", p.stdout)

    def test_today_and_yesterday_protected_even_with_one_day(self):
        self.run_cli("prune", "--older-than", "1", "--yes", check=True)
        left = self.left()
        self.assertIn(self.day(0) + ".jsonl", left)
        self.assertIn(self.day(1) + ".jsonl", left)
        self.assertNotIn(self.day(2) + ".jsonl", left)

    def test_needs_confirmation_without_tty(self):
        before = self.left()
        p = self.run_cli("prune", "--older-than", "10", input="y\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--yes", p.stderr)
        self.assertEqual(self.left(), before)

    def test_uses_config_retention_days(self):
        self.run_cli("config", "set", "retention_days", "10", check=True)
        p = self.run_cli("prune", "--yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(self.day(11) + ".jsonl", self.left())
        self.assertIn(self.day(10) + ".jsonl", self.left())
        self.assertIn("config.json retention_days", p.stdout)

    def test_flag_beats_env_beats_config(self):
        self.run_cli("config", "set", "retention_days", "100", check=True)
        self.run_cli("prune", "--dry-run", env=self.env(TOKENTIER_RETENTION_DAYS="10"), check=True)
        p = self.run_cli("prune", "--dry-run", env=self.env(TOKENTIER_RETENTION_DAYS="10"))
        self.assertIn("Would delete 2", p.stdout)
        p = self.run_cli("prune", "--dry-run", "--older-than", "30", env=self.env(TOKENTIER_RETENTION_DAYS="10"))
        self.assertIn("Would delete 1", p.stdout)

    def test_invalid_days(self):
        self.assertEqual(self.run_cli("prune", "--older-than", "0", "--yes").returncode, 2)
        self.assertEqual(self.run_cli("prune", "--older-than", "x", "--yes").returncode, 2)

    def test_nothing_to_prune(self):
        p = self.run_cli("prune", "--older-than", "400", "--yes")
        self.assertEqual(p.returncode, 0)
        self.assertIn("Nothing to prune", p.stdout)


class ServerPruneTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.today = "2026-10-20"
        write(self.home, "2026-10-20", [start("n", at("2026-10-20", "10:00")), end("n", at("2026-10-20", "10:01"))])
        write(self.home, "2026-10-19", [start("y", at("2026-10-19", "10:00")), end("y", at("2026-10-19", "10:01"))])
        write(self.home, "2026-09-01", [start("old", at("2026-09-01", "10:00")), end("old", at("2026-09-01", "10:01"))])
        self.logs = os.path.join(self.home, "logs")
        self.lines = []

    def serve(self, config, interval=0):
        if config is not None:
            with open(os.path.join(self.home, "config.json"), "w") as f:
                json.dump(config, f)
        self.httpd = srv.make_server(self.home, "127.0.0.1", 0, clock=Clock(at(self.today, "12:00")), tz=TZ,
                                     poll_interval=0.1, quiet=True, retention_interval=interval,
                                     settings=cfg.Settings(self.home, env={}))
        self.httpd.retention_log = self.lines.append
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.stopping = True
        self.httpd.retention_stop.set()
        self.httpd.store.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def api(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        return json.loads(c.getresponse().read().decode())

    def test_no_pruning_when_not_configured(self):
        self.serve(None)
        self.assertEqual(srv.prune_once(self.httpd), [])
        self.assertEqual(len(os.listdir(self.logs)), 3)

    def test_prune_once_removes_old_files_and_logs_them(self):
        self.serve({"retention_days": 7})
        self.assertEqual(self.api("/api/health")["tasks"], 3)  # loaded before pruning
        done = srv.prune_once(self.httpd)
        self.assertEqual([os.path.basename(p) for p, _, _ in done], ["2026-09-01.jsonl"])
        self.assertEqual(sorted(os.listdir(self.logs)), ["2026-10-19.jsonl", "2026-10-20.jsonl"])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("2026-09-01.jsonl", self.lines[0])
        # store tolerates the removed file: rebuilds and drops the pruned data, serves the rest
        h = self.api("/api/health")
        self.assertEqual((h["files"], h["tasks"]), (2, 2))
        self.assertEqual(self.api("/api/overview?from=all")["totals"]["tasks"], 2)

    def test_prunes_at_startup_and_then_periodically(self):
        self.serve({"retention_days": 7}, interval=0.2)
        deadline = time.time() + 5
        while os.path.exists(os.path.join(self.logs, "2026-09-01.jsonl")) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(os.path.exists(os.path.join(self.logs, "2026-09-01.jsonl")))
        write(self.home, "2026-08-01", [start("o2", at("2026-08-01", "10:00"))])  # appears later
        deadline = time.time() + 5
        while os.path.exists(os.path.join(self.logs, "2026-08-01.jsonl")) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(os.path.exists(os.path.join(self.logs, "2026-08-01.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.logs, "2026-10-19.jsonl")))

    def test_config_change_applies_without_restart(self):
        self.serve({"retention_days": 400})
        self.assertEqual(srv.prune_once(self.httpd), [])
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"retention_days": 7}, f)
        self.assertEqual(len(srv.prune_once(self.httpd)), 1)


if __name__ == "__main__":
    unittest.main()
