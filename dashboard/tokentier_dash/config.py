# SPDX-License-Identifier: Apache-2.0
"""TokenTier configuration (config.json) and log retention. Python 3.9+, stdlib only.

Shared by bin/tokentier (CLI) and dashboard/server.py. Precedence for every key:
CLI flag > environment variable > config.json > built-in default.

config.json lives in $TOKENTIER_HOME (default ~/.tokentier). All keys are optional:

    {"port": 8899, "retention_days": null, "pricing_path": null, "baseline_token_multiplier": null}

The dashboard always binds 127.0.0.1; there is deliberately no "host" key.
"""
import datetime as dt
import json
import math
import os
import re
import threading

CONFIG_NAME = "config.json"
DEFAULT_PORT = 8899

# key -> (environment variable, default, help)
KEYS = {
    "port": ("TOKENTIER_PORT", DEFAULT_PORT, "dashboard port, 1-65535 (default 8899)"),
    "retention_days": ("TOKENTIER_RETENTION_DAYS", None,
                       "delete daily logs older than N days; null keeps everything (default null)"),
    "pricing_path": ("TOKENTIER_PRICING_PATH", None,
                     "your own pricing.json that overrides the shipped one; null = shipped"),
    "baseline_token_multiplier": ("TOKENTIER_BASELINE_TOKEN_MULTIPLIER", None,
                                  "number > 0 overriding pricing.json's multiplier; null = use pricing.json"),
}
KEY_ORDER = tuple(KEYS)


class ConfigError(ValueError):
    """A key or value that is not acceptable."""


def config_path(home):
    return os.path.join(home, CONFIG_NAME)


# ------------------------------------------------------------------- validation
def _is_nullish(text):
    return isinstance(text, str) and text.strip().lower() in ("null", "none", "")


def validate_pricing_file(path):
    """Raise ConfigError unless `path` is a readable pricing.json with a 'models' object."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except OSError as e:
        raise ConfigError("cannot read pricing file %s: %s" % (path, e.strerror or e))
    except ValueError as e:
        raise ConfigError("pricing file %s is not valid JSON: %s" % (path, e))
    if not isinstance(d, dict) or not isinstance(d.get("models"), dict) or not d["models"]:
        raise ConfigError("pricing file %s must be a JSON object with a non-empty \"models\" object" % path)
    return d


def validate_value(key, value, check_files=True):
    """Return the normalised value for `key` or raise ConfigError. `value` is already JSON-typed
    (int/float/str/None); use parse_cli_value() for text from a command line or environment."""
    if key == "host":
        raise ConfigError("\"host\" is not configurable: the dashboard always binds 127.0.0.1")
    if key not in KEYS:
        raise ConfigError("unknown key %r (valid keys: %s)" % (key, ", ".join(KEY_ORDER)))
    if key == "port":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ConfigError("port must be an integer from 1 to 65535 (got %r)" % (value,))
        return value
    if key == "retention_days":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError("retention_days must be null or an integer >= 1 (got %r)" % (value,))
        return value
    if key == "baseline_token_multiplier":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            raise ConfigError("baseline_token_multiplier must be null or a finite number > 0 (got %r)"
                              % (value,))
        return value
    if key == "pricing_path":
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("pricing_path must be null or a file path (got %r)" % (value,))
        p = os.path.abspath(os.path.expanduser(value))
        if check_files:
            validate_pricing_file(p)
        return p
    raise ConfigError("unknown key %r" % key)  # pragma: no cover


def parse_cli_value(key, text):
    """Parse text typed on a command line (or read from the environment) for `key`."""
    if key == "host":
        raise ConfigError("\"host\" is not configurable: the dashboard always binds 127.0.0.1")
    if key not in KEYS:
        raise ConfigError("unknown key %r (valid keys: %s)" % (key, ", ".join(KEY_ORDER)))
    t = text.strip()
    if key == "port" or key == "retention_days":
        if key == "retention_days" and _is_nullish(t):
            return validate_value(key, None)
        if not re.match(r"^[0-9]+$", t):
            raise ConfigError("%s must be an integer%s (got %r)" % (
                key, " >= 1 or null" if key == "retention_days" else " from 1 to 65535", text))
        return validate_value(key, int(t))
    if key == "baseline_token_multiplier":
        if _is_nullish(t):
            return validate_value(key, None)
        try:
            v = float(t)
        except ValueError:
            raise ConfigError("baseline_token_multiplier must be a number > 0 or null (got %r)" % text)
        return validate_value(key, v)
    if key == "pricing_path":
        return validate_value(key, None if _is_nullish(t) else t)
    raise ConfigError("unknown key %r" % key)  # pragma: no cover


# ----------------------------------------------------------------------- reading
def read_config(home):
    """(data_dict, error_or_None). A missing file is ({}, None). A broken file is ({}, message)."""
    path = config_path(home)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, None
    except OSError as e:
        return {}, "cannot read %s: %s" % (path, e.strerror or e)
    try:
        text = raw.decode("utf-8-sig")
        if not text.strip():
            return {}, None
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as e:
        return {}, "%s is not valid JSON (%s)" % (path, e)
    if not isinstance(data, dict):
        return {}, "%s must contain a JSON object, found %s" % (path, type(data).__name__)
    return data, None


def check_config(data, check_files=True):
    """(errors, unknown_keys) for a parsed config dict. errors: [(key, message)]."""
    errors, unknown = [], []
    for k, v in data.items():
        if k == "host":
            unknown.append(k)
            continue
        if k not in KEYS:
            unknown.append(k)
            continue
        try:
            validate_value(k, v, check_files=check_files)
        except ConfigError as e:
            errors.append((k, str(e)))
    return errors, unknown


def resolve(home, cli=None, env=None):
    """Effective settings: ({key: (value, source)}, warnings).

    source is 'flag', 'env', 'config.json' or 'default'. Invalid env/config values are skipped
    (with a warning) and the next source in the precedence chain is used."""
    cli = cli or {}
    env = os.environ if env is None else env
    data, err = read_config(home)
    warnings = []
    if err:
        warnings.append("config: " + err + "; ignoring it")
    if "host" in data:
        warnings.append("config: \"host\" is ignored; the dashboard always binds 127.0.0.1")
    out = {}
    for key in KEY_ORDER:
        envvar, default, _ = KEYS[key]
        val, src = default, "default"
        # lowest first, so later (higher precedence) assignments win only when valid
        if key in data:
            try:
                v = validate_value(key, data[key], check_files=False)
                if v is not None:
                    val, src = v, "config.json"
            except ConfigError as e:
                warnings.append("config: %s; ignoring it" % e)
        if env.get(envvar, "").strip():
            try:
                v = parse_cli_value(key, env[envvar])
                if v is not None:
                    val, src = v, "env"
            except ConfigError as e:
                warnings.append("%s: %s; ignoring it" % (envvar, e))
        if cli.get(key) is not None:
            val, src = cli[key], "flag"
        out[key] = (val, src)
    return out, warnings


class Settings(object):
    """Hot-reloading view of resolve(): re-reads config.json when its mtime/size changes."""

    def __init__(self, home, cli=None, env=None):
        self.home = home
        self.cli = dict(cli or {})
        self.env = dict(os.environ if env is None else env)
        self._lock = threading.Lock()
        self._sig = object()
        self._cache = None

    def _signature(self):
        try:
            st = os.stat(config_path(self.home))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def get(self):
        """({key: (value, source)}, warnings)"""
        with self._lock:
            sig = self._signature()
            if self._cache is None or sig != self._sig:
                self._cache = resolve(self.home, self.cli, self.env)
                self._sig = sig
            return self._cache

    def value(self, key):
        return self.get()[0][key][0]

    def source(self, key):
        return self.get()[0][key][1]

    def warnings(self):
        return list(self.get()[1])


# ---------------------------------------------------------------------- retention
LOG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def log_file_date(name):
    """datetime.date for a name like 2026-10-04.jsonl, else None (junk / impossible dates)."""
    m = LOG_RE.match(name)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def find_expired(log_dir, days, today=None):
    """[(path, date, size)] of daily log files older than `days` days, oldest first.

    A file is expired when its filename date < today - days. Today's and yesterday's files are
    never returned, nor anything that is not a plain file named YYYY-MM-DD.jsonl."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ConfigError("retention days must be an integer >= 1 (got %r)" % (days,))
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=days)
    protect_from = today - dt.timedelta(days=1)
    out = []
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        return out
    for name in names:
        d = log_file_date(name)
        if d is None or d >= cutoff or d >= protect_from:
            continue
        p = os.path.join(log_dir, name)
        try:
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            out.append((p, d, os.stat(p).st_size))
        except OSError:
            continue
    return out


def delete_files(items):
    """Delete the files from find_expired(); returns the list of (path, date, size) removed."""
    done = []
    for p, d, size in items:
        try:
            os.unlink(p)
            done.append((p, d, size))
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return done


def fmt_size(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1024.0 / 1024.0)
