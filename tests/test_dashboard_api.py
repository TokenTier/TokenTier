import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from dash_helpers import ROOT, TZ, Clock, at, end, start, toks, write

sys.path.insert(0, os.path.join(ROOT, "dashboard"))
import server as srv  # noqa: E402
import make_sample_logs  # noqa: E402


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.pub = tempfile.mkdtemp()
        with open(os.path.join(self.pub, "index.html"), "w") as f:
            f.write("<h1>hi</h1>")
        self.clock = Clock(at("2026-10-05", "12:00"))
        write(self.home, "2026-10-05", [
            start("a", at("2026-10-05", "10:00")),
            end("a", at("2026-10-05", "10:01"), tokens={"input": 218, "output": 1153,
                                                        "cache_creation": 63069, "cache_read": 1513669})])
        self.httpd = srv.make_server(self.home, "127.0.0.1", 0, public_dir=self.pub, clock=self.clock, tz=TZ,
                                     poll_interval=0.1, ping_interval=0.5, quiet=True)
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.httpd.stopping = True
        self.httpd.store.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.home, True)
        shutil.rmtree(self.pub, True)

    def raw(self, path, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def get(self, path):
        st, h, b = self.raw(path)
        return st, json.loads(b.decode())


class ApiTest(ApiBase):
    def test_bind_address(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_health(self):
        st, h = self.get("/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(h["ok"])
        self.assertEqual(h["today"], "2026-10-05")
        self.assertEqual(h["tz_offset"], "+05:00")
        self.assertEqual(h["tasks"], 1)
        self.assertEqual(h["bad_lines"], 0)
        self.assertEqual(h["version"], srv.read_version())

    def test_overview(self):
        st, o = self.get("/api/overview?from=today")
        self.assertEqual(st, 200)
        self.assertEqual(o["totals"]["tasks"], 1)
        self.assertAlmostEqual(o["totals"]["cost"], 0.23619, places=5)
        self.assertAlmostEqual(o["totals"]["baseline_cost"], 0.64201, places=5)
        self.assertEqual(o["by_tier"]["haiku"]["tasks"], 1)
        self.assertEqual(o["daily"][0]["date"], "2026-10-05")
        self.assertEqual(self.get("/api/overview?from=nope")[0], 400)
        self.assertEqual(self.get("/api/overview?project=zzz")[1]["totals"]["tasks"], 0)

    def test_legacy_through_api(self):
        write(self.home, "2026-10-05", [
            start("legacy-x", at("2026-10-05", "09:00"), source="legacy"),
            end("legacy-x", at("2026-10-05", "09:01"), source="legacy", model=None, tokens=toks(0, 0, 0, 0),
                tokens_total=777, legacy_tokens_total=777)])
        time.sleep(0.5)
        st, t = self.get("/api/tasks/legacy-x")
        self.assertEqual((t["tokens_total"], t["legacy"], t["cost"]), (777, True, None))
        tot = self.get("/api/overview?from=today")[1]["totals"]
        self.assertEqual((tot["legacy_tasks"], tot["legacy_tokens"], tot["unknown_cost_tasks"]), (1, 777, 0))
        self.assertAlmostEqual(tot["cost"], 0.23619, places=5)

    def test_lists_and_detail(self):
        self.assertEqual(self.get("/api/projects")[1][0]["project"], "proj")
        self.assertEqual(self.get("/api/sessions")[1][0]["session_id"], "s1")
        st, t = self.get("/api/tasks?limit=1")
        self.assertEqual((st, t["total"], len(t["items"])), (200, 1, 1))
        self.assertEqual(self.get("/api/tasks?limit=abc")[0], 400)
        st, d = self.get("/api/tasks/a")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["events"]), 2)
        self.assertEqual(d["same_tool_use"], [])
        st, e = self.get("/api/tasks/missing")
        self.assertEqual(st, 404)
        self.assertIn("error", e)
        self.assertEqual(self.get("/api/pricing")[1]["baseline_tier"], "opus")
        self.assertEqual(self.get("/api/nope")[0], 404)

    def test_methods(self):
        self.assertEqual(self.raw("/api/health", "POST")[0], 405)

    def test_static(self):
        st, h, b = self.raw("/")
        self.assertEqual(st, 200)
        self.assertEqual(b, b"<h1>hi</h1>")
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertEqual(self.raw("/index.html")[0], 200)
        self.assertEqual(self.raw("/missing.js")[0], 404)

    def test_traversal(self):
        for p in ("/../server.py", "/%2e%2e/server.py", "/%2e%2e/", "/..%2fserver.py",
                  "/a/../../server.py", "/%2e%2e%2f%2e%2e%2fVERSION", "/%00"):
            st, _, body = self.raw(p)
            self.assertIn(st, (400, 403, 404), p)
            self.assertNotIn(b"ThreadingHTTPServer", body, p)

    def test_sse(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/events")
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        first = r.fp.readline().decode()
        self.assertTrue(first.startswith("data: "), first)
        r.fp.readline()
        write(self.home, "2026-10-05", [end("b", at("2026-10-05", "10:10"))])
        deadline = time.time() + 6
        got = None
        while time.time() < deadline:
            line = r.fp.readline().decode()
            if line.startswith("data: "):
                got = line
                break
        self.assertIsNotNone(got)
        self.assertNotEqual(got, first)
        # ping arrives when idle
        pinged = False
        deadline = time.time() + 5
        while time.time() < deadline:
            if r.fp.readline().decode().startswith(": ping"):
                pinged = True
                break
        self.assertTrue(pinged)
        c.close()
        self.assertEqual(self.get("/api/health")[1]["tasks"], 2)


class SampleLogTest(unittest.TestCase):
    def test_sample_logs_through_api(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        n = make_sample_logs.generate(home, days=5)
        self.assertGreater(n, 50)
        httpd = srv.make_server(home, "127.0.0.1", 0, quiet=True, poll_interval=0.2)
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            port = httpd.server_address[1]

            def get(p):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", p)
                return json.loads(c.getresponse().read().decode())
            h = get("/api/health")
            o = get("/api/overview?from=7d")
            t = get("/api/tasks?limit=1000")
            dates = [x["started_at"] for x in t["items"]]
            self.assertEqual(dates, sorted(dates, reverse=True))
            self.assertEqual(h["bad_lines"], 0)
            self.assertEqual(o["totals"]["tasks"], t["total"])
            self.assertEqual(o["totals"]["unknown_cost_tasks"], 0)
            self.assertGreaterEqual(o["totals"]["saved"], 0)
            self.assertEqual(o["daily"][0]["date"], h["today"])
            ld = o["totals"]["lead"]
            self.assertGreater(ld["tokens"]["total"], 0)
            self.assertGreater(ld["cost"], 0)
            self.assertEqual(ld["unknown_cost"], 0)
            self.assertAlmostEqual(o["totals"]["total_spend"], o["totals"]["cost"] + ld["cost"], places=5)
            self.assertAlmostEqual(sum(x["lead_cost"] for x in o["daily"]), ld["cost"], places=4)
            rows = get("/api/sessions?from=7d")
            self.assertAlmostEqual(sum(r["lead_cost"] for r in rows), ld["cost"], places=4)
            self.assertEqual(sum(r["lead_tokens"] for r in rows), ld["tokens"]["total"])
            print("\n[sample] files=%d events=%d tasks=%d completed=%d running=%d failed=%d esc=%d "
                  "cost=$%.2f baseline=$%.2f saved=%.1f%% projects=%d" % (
                      h["files"], h["events"], o["totals"]["tasks"], o["totals"]["completed"],
                      o["totals"]["running"], o["totals"]["failed"], o["totals"]["escalations"],
                      o["totals"]["cost"], o["totals"]["baseline_cost"], o["totals"]["saved_pct"],
                      len(get("/api/projects"))))
        finally:
            httpd.stopping = True
            httpd.store.stop()
            httpd.shutdown()
            httpd.server_close()


class CliBindTest(unittest.TestCase):
    def test_default_host(self):
        with open(os.path.join(ROOT, "dashboard", "server.py")) as f:
            src = f.read()
        self.assertIn('"--host", default="127.0.0.1"', src)


if __name__ == "__main__":
    unittest.main()
