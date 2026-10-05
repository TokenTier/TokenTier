"""config.json: validation, precedence, `tokentier config`, installer integration, pricing overrides.

Every CLI run uses a throwaway HOME (see tt_helpers). Servers run on 127.0.0.1 port 0.
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tt_helpers import FakeHome, free_port, snapshot
from dash_helpers import ROOT, TZ, Clock, at, end, start, write

sys.path.insert(0, os.path.join(ROOT, "dashboard"))
import server as srv  # noqa: E402
from tokentier_dash import config as cfg  # noqa: E402
from tokentier_dash.pricing import Pricing  # noqa: E402


def j(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


class ValidationTest(unittest.TestCase):
    def test_port(self):
        for bad in ("0", "65536", "-1", "abc", "80.5", "", " "):
            with self.assertRaises(cfg.ConfigError, msg=bad):
                cfg.parse_cli_value("port", bad)
        self.assertEqual(cfg.parse_cli_value("port", "1"), 1)
        self.assertEqual(cfg.parse_cli_value("port", "65535"), 65535)
        with self.assertRaises(cfg.ConfigError):
            cfg.validate_value("port", True)
        with self.assertRaises(cfg.ConfigError):
            cfg.validate_value("port", "8899")

    def test_retention(self):
        self.assertIsNone(cfg.parse_cli_value("retention_days", "null"))
        self.assertEqual(cfg.parse_cli_value("retention_days", "30"), 30)
        for bad in ("0", "-5", "1.5", "x"):
            with self.assertRaises(cfg.ConfigError, msg=bad):
                cfg.parse_cli_value("retention_days", bad)

    def test_multiplier(self):
        self.assertEqual(cfg.parse_cli_value("baseline_token_multiplier", "1.5"), 1.5)
        self.assertEqual(cfg.parse_cli_value("baseline_token_multiplier", "2"), 2.0)
        self.assertIsNone(cfg.parse_cli_value("baseline_token_multiplier", "null"))
        for bad in ("0", "-1", "nan", "inf", "-inf", "abc"):
            with self.assertRaises(cfg.ConfigError, msg=bad):
                cfg.parse_cli_value("baseline_token_multiplier", bad)

    def test_pricing_path(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        good, bad = os.path.join(tmp, "p.json"), os.path.join(tmp, "b.json")
        j(good, {"models": {"opus": {"input": 1}}})
        with open(bad, "w") as f:
            f.write("{nope")
        self.assertEqual(cfg.parse_cli_value("pricing_path", good), good)
        for p in (bad, os.path.join(tmp, "missing.json")):
            with self.assertRaises(cfg.ConfigError):
                cfg.parse_cli_value("pricing_path", p)
        j(bad, {"models": []})
        with self.assertRaises(cfg.ConfigError):
            cfg.parse_cli_value("pricing_path", bad)

    def test_host_and_unknown(self):
        for k in ("host", "nope"):
            with self.assertRaises(cfg.ConfigError):
                cfg.parse_cli_value(k, "x")
            with self.assertRaises(cfg.ConfigError):
                cfg.validate_value(k, "x")


class PrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_chain(self):
        v, w = cfg.resolve(self.home, {}, {})
        self.assertEqual(v["port"], (8899, "default"))
        j(os.path.join(self.home, "config.json"), {"port": 9100})
        self.assertEqual(cfg.resolve(self.home, {}, {})[0]["port"], (9100, "config.json"))
        self.assertEqual(cfg.resolve(self.home, {}, {"TOKENTIER_PORT": "9200"})[0]["port"], (9200, "env"))
        self.assertEqual(cfg.resolve(self.home, {"port": 9300}, {"TOKENTIER_PORT": "9200"})[0]["port"],
                         (9300, "flag"))

    def test_invalid_values_fall_through_with_warning(self):
        j(os.path.join(self.home, "config.json"), {"port": 9100, "retention_days": 0, "host": "0.0.0.0"})
        v, w = cfg.resolve(self.home, {}, {"TOKENTIER_PORT": "banana"})
        self.assertEqual(v["port"], (9100, "config.json"))      # bad env ignored
        self.assertEqual(v["retention_days"], (None, "default"))  # bad config value ignored
        text = " ".join(w)
        self.assertIn("TOKENTIER_PORT", text)
        self.assertIn("retention_days", text)
        self.assertIn("host", text)

    def test_broken_file(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("{oops")
        v, w = cfg.resolve(self.home, {}, {})
        self.assertEqual(v["port"], (8899, "default"))
        self.assertTrue(any("not valid JSON" in x for x in w))

    def test_settings_hot_reload(self):
        s = cfg.Settings(self.home, env={})
        self.assertEqual(s.value("port"), 8899)
        j(os.path.join(self.home, "config.json"), {"port": 9101})
        self.assertEqual(s.value("port"), 9101)


class ConfigCliTest(FakeHome):
    def cfgpath(self):
        return os.path.join(self.tt, "config.json")

    def test_set_get_list_unset_roundtrip(self):
        p = self.run_cli("config", "set", "port", "9001")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(self.read(self.cfgpath())), {"port": 9001})
        self.assertEqual(self.run_cli("config", "get", "port").stdout.strip(), "9001")
        self.run_cli("config", "set", "baseline_token_multiplier", "1.5", check=True)
        lst = self.run_cli("config", "list").stdout
        self.assertRegex(lst, r"port\s+9001\s+\(config.json\)")
        self.assertRegex(lst, r"baseline_token_multiplier\s+1\.5\s+\(config.json\)")
        self.assertRegex(lst, r"retention_days\s+null\s+\(default\)")
        data = json.loads(self.run_cli("config", "list", "--json").stdout)
        self.assertEqual(data["effective"]["port"], {"value": 9001, "source": "config.json"})
        self.run_cli("config", "unset", "port", check=True)
        self.assertEqual(json.loads(self.read(self.cfgpath())), {"baseline_token_multiplier": 1.5})
        self.assertEqual(self.run_cli("config", "get", "port").stdout.strip(), "8899")
        self.assertEqual(self.run_cli("config", "path").stdout.strip(), self.cfgpath())
        self.assertEqual(self.run_cli("config", "unset", "port").returncode, 0)  # idempotent

    def test_env_wins_over_file_in_get(self):
        self.run_cli("config", "set", "port", "9001", check=True)
        p = self.run_cli("config", "get", "port", env=self.env(TOKENTIER_PORT="9555"))
        self.assertEqual(p.stdout.strip(), "9555")
        self.assertIn("env", p.stderr)

    def test_invalid_values_rejected_exit_2_and_file_untouched(self):
        self.run_cli("config", "set", "port", "9001", check=True)
        before = self.read(self.cfgpath())
        for key, val in (("port", "0"), ("port", "70000"), ("port", "abc"), ("retention_days", "0"),
                         ("retention_days", "2.5"), ("baseline_token_multiplier", "0"),
                         ("baseline_token_multiplier", "nan"), ("baseline_token_multiplier", "-2"),
                         ("pricing_path", os.path.join(self.tmp, "missing.json")),
                         ("host", "0.0.0.0"), ("bogus", "1")):
            p = self.run_cli("config", "set", key, val)
            self.assertEqual(p.returncode, 2, (key, val, p.stdout, p.stderr))
            self.assertIn("ERROR:", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.run_cli("config", "get", "bogus").returncode, 2)
        self.assertEqual(self.read(self.cfgpath()), before)

    def test_unknown_keys_preserved_and_style_kept(self):
        self.write(self.cfgpath(), '{\n    "custom": {"a": [1, 2]},\n    "port": 9000\n}\n')
        self.run_cli("config", "set", "retention_days", "14", check=True)
        raw = self.read(self.cfgpath()).decode()
        self.assertEqual(json.loads(raw), {"custom": {"a": [1, 2]}, "port": 9000, "retention_days": 14})
        self.assertIn('\n    "custom"', raw)  # 4-space indent kept
        self.assertTrue(raw.endswith("\n"))

    def test_pricing_path_set_validates_and_stores_absolute(self):
        pp = os.path.join(self.tmp, "my-pricing.json")
        j(pp, {"models": {"opus": {"input": 1, "output": 2}}})
        self.run_cli("config", "set", "pricing_path", pp, check=True)
        self.assertEqual(json.loads(self.read(self.cfgpath()))["pricing_path"], pp)

    def test_null_clears_value(self):
        self.run_cli("config", "set", "retention_days", "10", check=True)
        self.run_cli("config", "set", "retention_days", "null", check=True)
        self.assertIsNone(json.loads(self.read(self.cfgpath()))["retention_days"])

    def test_broken_config_is_never_overwritten(self):
        self.write(self.cfgpath(), "{not json")
        p = self.run_cli("config", "set", "port", "9001")
        self.assertEqual(p.returncode, 1)
        self.assertIn("Refusing to overwrite", p.stderr)
        self.assertEqual(self.read(self.cfgpath()), b"{not json")

    def test_help_lists_subcommands(self):
        out = self.run_cli("config", "--help").stdout
        for w in ("list", "get", "set", "unset", "path", "port", "baseline_token_multiplier"):
            self.assertIn(w, out)
        self.assertEqual(self.run_cli("config").returncode, 0)  # prints help

    def test_doctor_config_checks(self):
        self.install("--no-service")
        self.assertRegex(self.run_cli("doctor").stdout, r"PASS\s+config\s+no .*config.json")
        self.write(self.cfgpath(), '{"port": 9001}')
        self.assertRegex(self.run_cli("doctor").stdout, r"PASS\s+config\s+.*is valid")
        self.write(self.cfgpath(), '{"port": 9001, "mystery": 1}')
        out = self.run_cli("doctor").stdout
        self.assertRegex(out, r"WARN\s+config\s+unknown key 'mystery'")
        self.write(self.cfgpath(), '{"host": "0.0.0.0"}')
        self.assertRegex(self.run_cli("doctor").stdout, r"WARN\s+config\s+unknown key \"host\"")
        self.write(self.cfgpath(), '{"port": 0}')
        p = self.run_cli("doctor")
        self.assertEqual(p.returncode, 1)
        self.assertRegex(p.stdout, r"FAIL\s+config\s+port:")
        self.write(self.cfgpath(), "{broken")
        p = self.run_cli("doctor")
        self.assertEqual(p.returncode, 1)
        self.assertRegex(p.stdout, r"FAIL\s+config\s+.*not valid JSON")


class InstallerConfigTest(FakeHome):
    def cfgpath(self):
        return os.path.join(self.tt, "config.json")

    def test_install_port_writes_config_and_uninstall_removes_it(self):
        before = snapshot(self.home)
        port = free_port()
        self.install("--port", str(port))
        self.assertEqual(json.loads(self.read(self.cfgpath())), {"port": port})
        self.assertTrue(self.manifest()["config"]["created"])
        self.uninstall()
        self.assertIsNone(self.read(self.cfgpath()))
        after = {k: v for k, v in snapshot(self.home).items() if not k.startswith(".tokentier")}
        self.assertEqual(after, {k: v for k, v in before.items() if not k.startswith(".tokentier")})
        self.assertEqual(sorted(k for k in snapshot(self.tt) if not k.startswith("logs")), [])

    def test_install_without_port_creates_no_config(self):
        self.install("--no-service")
        self.assertIsNone(self.read(self.cfgpath()))
        self.assertNotIn("config", self.manifest())

    def test_service_file_still_gets_port_arg(self):
        port = free_port()
        self.install("--port", str(port), env=self.env(TOKENTIER_PLATFORM="linux"))
        unit = self.read(os.path.join(self.home, ".config", "systemd", "user",
                                      "tokentier-dashboard.service")).decode()
        self.assertIn("--host 127.0.0.1 --port %d" % port, unit)
        self.assertEqual(json.loads(self.read(self.cfgpath()))["port"], port)

    def test_reinstall_uses_config_port_when_no_flag(self):
        self.write(self.cfgpath(), '{"port": 9444}')
        self.install(env=self.env(TOKENTIER_PLATFORM="linux"))
        unit = self.read(os.path.join(self.home, ".config", "systemd", "user",
                                      "tokentier-dashboard.service")).decode()
        self.assertIn("--port 9444", unit)

    def test_uninstall_keeps_modified_config_and_says_so(self):
        self.install("--port", str(free_port()), "--no-service")
        self.run_cli("config", "set", "retention_days", "30", check=True)
        p = self.uninstall()
        self.assertIsNotNone(self.read(self.cfgpath()))
        self.assertIn("kept", p.stdout + p.stderr)
        self.assertIn("config.json", p.stdout)
        self.assertEqual(json.loads(self.read(self.cfgpath()))["retention_days"], 30)

    def test_preexisting_config_is_kept_and_unknown_keys_preserved(self):
        self.write(self.cfgpath(), '{"retention_days": 7, "extra": true}\n')
        before = self.read(self.cfgpath())
        port = free_port()
        self.install("--port", str(port), "--no-service")
        self.assertEqual(json.loads(self.read(self.cfgpath())),
                         {"retention_days": 7, "extra": True, "port": port})
        self.assertFalse(self.manifest()["config"]["created"])
        self.uninstall()
        self.assertIsNotNone(self.read(self.cfgpath()))  # not ours: never deleted
        self.assertNotEqual(self.read(self.cfgpath()), before)  # port stays (documented)

    def test_untouched_preexisting_config_without_port_flag(self):
        self.write(self.cfgpath(), '{"retention_days": 7}\n')
        before = self.read(self.cfgpath())
        self.install("--no-service")
        self.uninstall()
        self.assertEqual(self.read(self.cfgpath()), before)

    def test_install_aborts_on_broken_config_when_port_given(self):
        self.write(self.cfgpath(), "{broken")
        p = self.install("--port", "9555", "--no-service", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.read(self.cfgpath()), b"{broken")

    def test_install_rejects_bad_port(self):
        for bad in ("0", "70000"):
            self.assertEqual(self.install("--port", bad, "--no-service", check=False).returncode, 1)

    def test_dry_run_writes_no_config(self):
        p = self.run_cli("install", "--dry-run", "--port", str(free_port()), "--no-service")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIsNone(self.read(self.cfgpath()))
        self.assertIn("config.json", p.stdout)


class ServerBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        write(self.home, "2026-10-05", [start("a", at("2026-10-05", "10:00")),
                                        end("a", at("2026-10-05", "10:01"))])
        self.httpd = None

    def serve(self, env=None, pricing_path=None):
        kw = {"pricing_path": pricing_path} if pricing_path else {}
        self.httpd = srv.make_server(self.home, "127.0.0.1", 0, clock=Clock(at("2026-10-05", "12:00")), tz=TZ,
                                     poll_interval=0.1, quiet=True, retention_interval=0,
                                     settings=cfg.Settings(self.home, env=env or {}), **kw)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.stopping = True
        self.httpd.store.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        return json.loads(r.read().decode())

    def status(self, path, method):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=b"{}")
        return c.getresponse().status


class PricingConfigTest(ServerBase):
    def test_default_source_is_pricing_json_with_shipped_file(self):
        self.serve()
        p, h = self.get("/api/pricing"), self.get("/api/health")
        self.assertEqual((p["baseline_token_multiplier"], p["baseline_source"]), (1.0, "pricing.json"))
        self.assertEqual((h["baseline_token_multiplier"], h["baseline_source"]), (1.0, "pricing.json"))
        self.assertEqual(h["warnings"], [])

    def test_default_source_when_pricing_json_has_no_multiplier(self):
        pp = os.path.join(self.home, "plain.json")
        j(pp, {"models": {"opus": {"input": 4, "output": 20, "cache_write": 5, "cache_read": 0.2}},
               "baseline_tier": "opus"})
        self.serve(pricing_path=pp)
        p = self.get("/api/pricing")
        self.assertEqual((p["baseline_token_multiplier"], p["baseline_source"]), (1.0, "default"))

    def test_config_multiplier_overrides_and_hot_reloads(self):
        self.serve()
        base = self.get("/api/overview?from=all")["totals"]["baseline_cost"]
        j(os.path.join(self.home, "config.json"), {"baseline_token_multiplier": 1.5})
        p = self.get("/api/pricing")
        self.assertEqual((p["baseline_token_multiplier"], p["baseline_source"]), (1.5, "config.json"))
        self.assertEqual(self.get("/api/health")["baseline_source"], "config.json")
        self.assertAlmostEqual(self.get("/api/overview?from=all")["totals"]["baseline_cost"], base * 1.5, 5)
        os.unlink(os.path.join(self.home, "config.json"))
        self.assertEqual(self.get("/api/pricing")["baseline_source"], "pricing.json")

    def test_env_multiplier(self):
        self.serve(env={"TOKENTIER_BASELINE_TOKEN_MULTIPLIER": "2"})
        p = self.get("/api/pricing")
        self.assertEqual((p["baseline_token_multiplier"], p["baseline_source"]), (2.0, "env"))

    def test_user_pricing_file_overrides_and_invalid_keeps_previous(self):
        user = os.path.join(self.home, "mine.json")
        spec = {"match": ["claude-haiku-4-5"], "label": "H", "input": 9.0, "output": 9.0,
                "cache_write": 9.0, "cache_read": 9.0}
        j(user, {"models": {"haiku": spec}, "baseline_tier": "haiku", "baseline_token_multiplier": 3})
        j(os.path.join(self.home, "config.json"), {"pricing_path": user})
        self.serve()
        p = self.get("/api/pricing")
        self.assertEqual(list(p["models"]), ["haiku"])
        self.assertEqual((p["baseline_token_multiplier"], p["baseline_source"]), (3, "pricing.json"))
        self.assertEqual(self.get("/api/health")["warnings"], [])
        # break the file: previous (user) table stays, warning appears
        with open(user, "w") as f:
            f.write("{broken")
        os.utime(user, (1, 1))
        p = self.get("/api/pricing")
        self.assertEqual(list(p["models"]), ["haiku"])
        w = self.get("/api/health")["warnings"]
        self.assertEqual(len(w), 1)
        self.assertIn(user, w[0])
        # fix it again: hot reload and the warning goes away
        j(user, {"models": {"haiku": spec, "opus": spec}})
        self.assertEqual(sorted(self.get("/api/pricing")["models"]), ["haiku", "opus"])
        self.assertEqual(self.get("/api/health")["warnings"], [])

    def test_invalid_user_file_at_startup_uses_shipped_table(self):
        user = os.path.join(self.home, "bad.json")
        with open(user, "w") as f:
            f.write("nope")
        j(os.path.join(self.home, "config.json"), {"pricing_path": user})
        self.serve()
        self.assertEqual(sorted(self.get("/api/pricing")["models"]), ["haiku", "opus", "opus-4", "sonnet", "sonnet-4"])
        self.assertEqual(len(self.get("/api/health")["warnings"]), 1)

    def test_config_warnings_in_health(self):
        j(os.path.join(self.home, "config.json"), {"port": "x", "host": "0.0.0.0"})
        self.serve()
        w = " ".join(self.get("/api/health")["warnings"])
        self.assertIn("port", w)
        self.assertIn("host", w)

    def test_api_stays_read_only(self):
        self.serve()
        for path in ("/api/pricing", "/api/health", "/api/config"):
            for m in ("POST", "PUT", "DELETE", "PATCH"):
                self.assertEqual(self.status(path, m), 405, (path, m))
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_pricing_class_without_settings_still_works(self):
        pr = Pricing(os.path.join(ROOT, "dashboard", "pricing.json"))
        self.assertEqual(pr.current()["baseline_source"], "pricing.json")


class ServerArgsTest(unittest.TestCase):
    def test_host_must_be_loopback(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            srv.main(["--host", "0.0.0.0", "--port", "1"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
