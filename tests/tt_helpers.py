"""Shared helpers for installer / CLI tests.

SAFETY: every CLI run happens with HOME pointing at a fresh temp dir, with
TOKENTIER_NO_SERVICE_EXEC=1 (launchctl/systemctl are never executed) and with
TOKENTIER_HOME / CLAUDE_CONFIG_DIR removed from the environment.
"""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

IS_WINDOWS = os.name == "nt"
# Tests that exercise macOS/Linux-only behaviour (sh -c command strings, symlinks, POSIX modes,
# TZ/tzset) are skipped on real Windows with this reason; Windows behaviour is covered (also on
# macOS/Linux, by simulation) in test_windows.py.
POSIX_ONLY = "macOS/Linux behaviour (POSIX shell form, modes, symlinks or TZ); Windows is covered in test_windows.py"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "tokentier")
FIX = os.path.join(ROOT, "tests", "fixtures")
REAL_HOME = os.path.realpath(os.path.expanduser("~"))
TMP_ROOT = os.path.realpath(tempfile.gettempdir())


def load_cli():
    loader = importlib.machinery.SourceFileLoader("tokentier_cli", BIN)
    spec = importlib.util.spec_from_loader("tokentier_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def snapshot(root):
    """{relpath: sha256 or 'DIR' or 'LINK->target'} for everything under root."""
    out = {}
    if not os.path.exists(root):
        return out
    for dp, dns, fns in os.walk(root):
        for d in dns:
            p = os.path.join(dp, d)
            out[os.path.relpath(p, root)] = "LINK->" + os.readlink(p) if os.path.islink(p) else "DIR"
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.islink(p):
                out[os.path.relpath(p, root)] = "LINK->" + os.readlink(p)
            else:
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


class FakeHome(unittest.TestCase):
    """Each test gets self.home (fake $HOME), self.claude (~/.claude) and self.tt (~/.tokentier)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="tt-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.claude = os.path.join(self.home, ".claude")
        self.tt = os.path.join(self.home, ".tokentier")
        assert self.home.startswith(TMP_ROOT) and not self.home.startswith(REAL_HOME + os.sep + ".")

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TOKENTIER_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "TOKENTIER_PLATFORM",
                            "HOMEDRIVE", "HOMEPATH")
               and not k.startswith("XDG_")}
        # USERPROFILE too: on Windows Python's expanduser("~") ignores HOME and uses USERPROFILE
        env.update(HOME=self.home, USERPROFILE=self.home, TOKENTIER_NO_SERVICE_EXEC="1",
                   PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        assert env["HOME"].startswith(TMP_ROOT) and env["USERPROFILE"].startswith(TMP_ROOT)
        assert env["TOKENTIER_NO_SERVICE_EXEC"] == "1"
        return env

    def run_cli(self, *args, bin_path=BIN, cwd=None, input=None, env=None, check=None):
        p = subprocess.run([sys.executable, bin_path] + list(args), cwd=cwd or self.tmp,
                           env=env or self.env(), input=input, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        if check is True and p.returncode != 0:
            self.fail("tokentier %s failed (%d):\n%s\n%s" % (" ".join(args), p.returncode, p.stdout, p.stderr))
        return p

    def install(self, *args, **kw):
        return self.run_cli("install", "--yes", *args, check=kw.pop("check", True), **kw)

    def uninstall(self, *args, **kw):
        return self.run_cli("uninstall", "--yes", *args, check=kw.pop("check", True), **kw)

    def write(self, path, data, mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        if mode is not None:
            os.chmod(path, mode)

    def read(self, path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def settings(self, path=None):
        return json.loads(self.read(path or os.path.join(self.claude, "settings.json")).decode("utf-8-sig"))

    def manifest(self):
        return json.loads(self.read(os.path.join(self.tt, "install-manifest.json")).decode("utf-8"))
