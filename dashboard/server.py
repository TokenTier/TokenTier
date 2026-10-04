#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""TokenTier dashboard server: JSON API + SSE + static UI. Python 3.9+, stdlib only.

    python3 dashboard/server.py [--port N] [--host 127.0.0.1] [--home DIR]

Reads $TOKENTIER_HOME (default ~/.tokentier)/logs/*.jsonl and $TOKENTIER_HOME/config.json.
Always binds to 127.0.0.1 (--host only accepts 127.0.0.1/localhost). Port precedence:
--port > TOKENTIER_PORT > config.json "port" > 8899. The API is read-only (GET only).
--home overrides $TOKENTIER_HOME (Windows Task Scheduler cannot set environment variables).
Runs on macOS, Linux and Windows; on Windows a second instance on the same port fails to start
(SO_EXCLUSIVEADDRUSE) instead of silently sharing the port.
"""
import argparse
import datetime as dt
import json
import mimetypes
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from tokentier_dash import config as cfg  # noqa: E402
from tokentier_dash.pricing import Pricing  # noqa: E402
from tokentier_dash.store import RangeError, Store  # noqa: E402

PUBLIC = os.path.join(HERE, "public")
PRICING_PATH = os.path.join(HERE, "pricing.json")

# Explicit types: on Windows mimetypes reads HKEY_CLASSES_ROOT, where .js is often registered as
# text/plain, and browsers refuse to run a module script served as text/plain.
MIME_TYPES = {
    ".html": "text/html", ".htm": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".txt": "text/plain", ".map": "application/json",
}


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    ctype = MIME_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
        ctype += "; charset=utf-8"
    return ctype


class DashboardServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to share its port.

    POSIX: SO_REUSEADDR (as before) only allows re-binding a port in TIME_WAIT. Windows:
    SO_REUSEADDR would let a second dashboard bind a port that is in use, so it is off and
    SO_EXCLUSIVEADDRUSE is set instead. server_bind also skips the reverse-DNS getfqdn() lookup
    that can stall startup on Windows."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if excl is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def tokentier_home():
    return os.environ.get("TOKENTIER_HOME") or os.path.join(os.path.expanduser("~"), ".tokentier")


def read_version():
    for p in (os.path.join(os.path.dirname(HERE), "VERSION"), os.path.join(HERE, "VERSION")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return "unknown"


class ApiError(Exception):
    def __init__(self, status, msg):
        Exception.__init__(self, msg)
        self.status = status
        self.msg = msg


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenTier"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        BaseHTTPRequestHandler.log_message(self, fmt, *args)

    # ------------------------------------------------------------ output
    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, status, msg):
        self._json({"error": msg}, status)

    # ----------------------------------------------------------- methods
    def do_GET(self):
        try:
            u = urlparse(self.path)
            path = u.path
            if path == "/events":
                return self._sse()
            if path.startswith("/api/") or path == "/api":
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=False).items()}
                return self._json(self._api(path, q))
            return self._static(path)
        except ApiError as e:
            self._err(e.status, e.msg)
        except RangeError as e:
            self._err(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # pragma: no cover
            try:
                self._err(500, "internal error: %s" % e)
            except Exception:
                pass

    def _method_not_allowed(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        body = b'{"error":"method not allowed"}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_not_allowed

    # --------------------------------------------------------------- api
    def _api(self, path, q):
        store = self.server.store
        store.refresh()
        if path == "/api/health":
            return store.health(self.server.version)
        if path == "/api/overview":
            return store.overview(q.get("from"), q.get("to"), q.get("project"))
        if path == "/api/projects":
            return store.projects()
        if path == "/api/sessions":
            return store.sessions(q.get("project"), q.get("from"), q.get("to"))
        if path == "/api/pricing":
            return store.pricing.current()
        if path == "/api/tasks":
            for k in ("limit", "offset"):
                if k in q:
                    try:
                        int(q[k])
                    except ValueError:
                        raise ApiError(400, "%s must be an integer" % k)
            return store.tasks(q)
        if path.startswith("/api/tasks/"):
            t = store.task(unquote(path[len("/api/tasks/"):]))
            if t is None:
                raise ApiError(404, "task not found")
            return t
        raise ApiError(404, "unknown endpoint")

    # --------------------------------------------------------------- sse
    def _sse(self):
        store = self.server.store
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last, beat = None, time.time()
        try:
            while not getattr(self.server, "stopping", False):
                tok = store.token()  # no lock held while writing
                if tok != last:
                    last = tok
                    beat = time.time()
                    self.wfile.write(("data: %s\n\n" % tok).encode())
                    self.wfile.flush()
                elif time.time() - beat >= self.server.ping_interval:
                    beat = time.time()
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------------------------------------------ static
    def _static(self, path):
        root = os.path.realpath(self.server.public_dir)
        nroot = os.path.normcase(root)
        try:
            rel = unquote(path, errors="strict")
        except UnicodeDecodeError:
            return self._err(400, "bad path")
        if "\x00" in rel:
            return self._err(400, "bad path")
        if rel in ("", "/"):
            rel = "/index.html"
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        ntarget = os.path.normcase(target)
        if ntarget != nroot and not ntarget.startswith(nroot.rstrip(os.sep) + os.sep):
            return self._err(403, "forbidden")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return self._err(404, "not found")
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError:
            return self._err(404, "not found")
        self._send(200, body, content_type(target))


RETENTION_INTERVAL = 24 * 3600


def prune_once(httpd):
    """Delete daily logs older than the configured retention_days (no-op when not configured).
    Returns the list of removed (path, date, size). Never raises."""
    try:
        days = httpd.settings.value("retention_days")
        if not days:
            return []
        today = dt.datetime.strptime(httpd.store.today(), "%Y-%m-%d").date()
        done = cfg.delete_files(cfg.find_expired(httpd.store.log_dir, days, today))
    except Exception as e:  # pragma: no cover - defensive, the server must keep running
        httpd.retention_log("retention: prune failed: %s" % e)
        return []
    for path, d, size in done:
        httpd.retention_log("retention: removed %s (%s, older than %d days)"
                            % (path, cfg.fmt_size(size), days))
    return done


def _retention_loop(httpd, interval):
    while not httpd.retention_stop.is_set():
        prune_once(httpd)
        if httpd.retention_stop.wait(interval):
            break


def make_server(home=None, host="127.0.0.1", port=8899, public_dir=PUBLIC, clock=None, tz=None,
                poll_interval=1.0, ping_interval=15.0, quiet=False, pricing_path=PRICING_PATH,
                settings=None, retention_interval=RETENTION_INTERVAL):
    home = home or tokentier_home()
    settings = settings or cfg.Settings(home)
    store = Store(home, Pricing(pricing_path, settings), clock=clock, tz=tz, poll_interval=poll_interval)
    httpd = DashboardServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.store = store
    httpd.settings = settings
    httpd.public_dir = public_dir
    httpd.version = read_version()
    httpd.ping_interval = ping_interval
    httpd.quiet = quiet
    httpd.stopping = False
    httpd.retention_stop = threading.Event()
    httpd.retention_log = (lambda m: None) if quiet else (lambda m: print(m, flush=True))
    store.start()
    if retention_interval:
        t = threading.Thread(target=_retention_loop, args=(httpd, retention_interval),
                             name="tokentier-retention", daemon=True)
        t.start()
        httpd.retention_thread = t
    return httpd


def _ensure_streams(home):
    """pythonw.exe (the Windows service) has no stdout/stderr: send output to dashboard.log so
    print() and the request log neither vanish nor crash."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(home, exist_ok=True)
        f = open(os.path.join(home, "dashboard.log"), "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def _safe_console():
    """A non-UTF-8 console (Windows cp1252/cp437) must not crash the server when a path or log
    line contains characters it cannot show: print '?' instead."""
    for st in (sys.stdout, sys.stderr):
        enc = (getattr(st, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if st is not None and enc not in ("utf8", "utf8sig") and hasattr(st, "reconfigure"):
            try:
                st.reconfigure(errors="replace")
            except Exception:
                pass


def _install_stop_signals():
    """SIGTERM (systemd/launchd stop) and SIGBREAK (Ctrl+Break in a Windows console) stop the
    server cleanly through the same path as Ctrl+C. (schtasks /End terminates the process.)"""
    def stop(signum, frame):
        raise KeyboardInterrupt()
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, stop)
        except (OSError, ValueError, RuntimeError):
            pass  # not in the main thread / not supported


def main(argv=None):
    ap = argparse.ArgumentParser(description="TokenTier dashboard server")
    ap.add_argument("--port", type=int, default=None,
                    help="port (default: TOKENTIER_PORT, then config.json, then 8899)")
    ap.add_argument("--host", default="127.0.0.1", help="only 127.0.0.1 is allowed")
    ap.add_argument("--home", default=None, help="TokenTier home (default: $TOKENTIER_HOME or ~/.tokentier)")
    if sys.stdout is None or sys.stderr is None:  # pythonw: make argparse errors land in dashboard.log
        pre, _ = ap.parse_known_args(argv)
        _ensure_streams(os.path.abspath(os.path.expanduser(pre.home)) if pre.home else tokentier_home())
    a = ap.parse_args(argv)
    if a.host not in ("127.0.0.1", "localhost"):
        ap.error("the dashboard always binds 127.0.0.1; --host %s is not allowed" % a.host)
    if a.port is not None and not 1 <= a.port <= 65535:
        ap.error("--port must be 1-65535")
    if a.home:
        os.environ["TOKENTIER_HOME"] = os.path.abspath(os.path.expanduser(a.home))
    home = tokentier_home()
    _ensure_streams(home)
    _safe_console()
    _install_stop_signals()
    settings = cfg.Settings(home, cli={"port": a.port})
    for w in settings.warnings():
        print("WARNING: " + w, flush=True)
    port = settings.value("port")
    try:
        httpd = make_server(home=home, host="127.0.0.1", port=port, settings=settings)
    except OSError as e:
        sys.stderr.write("ERROR: cannot listen on 127.0.0.1:%d (%s). Pick another port with --port N or "
                         "`tokentier config set port N`.\n" % (port, e.strerror or e))
        return 1
    host, port = httpd.server_address[:2]
    print("TokenTier dashboard: http://%s:%d/  (logs: %s)" % (host, port, httpd.store.log_dir), flush=True)
    days = settings.value("retention_days")
    if days:
        print("Log retention: %d days (pruning at startup and every 24h)" % days, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stopping = True
        httpd.retention_stop.set()
        httpd.store.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
