# SPDX-License-Identifier: Apache-2.0
"""Pricing table + cost maths. $ per million tokens."""
import json
import os
import threading

DEFAULT = {
    "updated": "2026-10-04",
    "source": "built-in defaults",
    "models": {
        "haiku": {"match": ["claude-haiku-4-5"], "label": "Haiku 4.5", "input": 1.00, "output": 5.00,
                  "cache_write": 1.25, "cache_read": 0.10},
        "sonnet": {"match": ["claude-sonnet-5-5"], "label": "Sonnet 5.5", "input": 2.00, "output": 10.00,
                   "cache_write": 2.50, "cache_read": 0.20},
        "opus": {"match": ["claude-opus-5-5"], "label": "Opus 5.5", "input": 4.00, "output": 20.00,
                 "cache_write": 5.00, "cache_read": 0.20},
    },
    "baseline_tier": "opus",
    "baseline_token_multiplier": 1.0,
}

TOKEN_KEYS = ("input", "output", "cache_creation", "cache_read")


def _n(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _valid_table(d):
    return isinstance(d, dict) and isinstance(d.get("models"), dict)


def _valid_mult(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 < v < float("inf")


class Pricing(object):
    """Pricing table with hot reload.

    `path` is the shipped pricing.json. `settings` (tokentier_dash.config.Settings, optional)
    can point at a user-owned pricing file (`pricing_path`) that overrides it and at a
    `baseline_token_multiplier` that overrides the value in the pricing file. An invalid user
    file never replaces a good table: the previous one is kept and a warning is reported."""

    def __init__(self, path=None, settings=None):
        self.path = path
        self.settings = settings
        self._lock = threading.Lock()
        self._sigs = None
        self._table = json.loads(json.dumps(DEFAULT))   # last good table
        self._origin = "default"                          # 'default' | 'file'
        self._have_user = False
        self._warnings = []
        self._view = None
        self.current()

    @staticmethod
    def _stat(path):
        try:
            st = os.stat(path)
            return (path, st.st_mtime_ns, st.st_size)
        except OSError:
            return (path, None, None)

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not _valid_table(d):
            raise ValueError("no \"models\" object")
        return d

    def _user_path(self):
        if not self.settings:
            return None
        return self.settings.value("pricing_path")

    def _refresh(self):
        user = self._user_path()
        mult_val, mult_src = (None, "default")
        if self.settings:
            mult_val, mult_src = self.settings.get()[0]["baseline_token_multiplier"]
        sigs = (self._stat(self.path) if self.path else None, self._stat(user) if user else None,
                mult_val, mult_src)
        if sigs == self._sigs and self._view is not None:
            return
        self._sigs = sigs
        warnings = []
        if self.settings:
            warnings.extend(self.settings.warnings())
        loaded = None
        if user:
            try:
                loaded = self._read(user)
                self._have_user = True
            except (OSError, ValueError) as e:
                warnings.append("pricing_path %s is invalid (%s); keeping the previous pricing table"
                                % (user, e))
                if self._have_user:
                    loaded = self._table  # keep what the user file gave us last time
        else:
            self._have_user = False
        if loaded is None and self.path:
            try:
                loaded = self._read(self.path)
            except (OSError, ValueError):
                loaded = None  # keep previous table
        if loaded is not None and loaded is not self._table:
            self._table = loaded
            self._origin = "file"
        view = json.loads(json.dumps(self._table))
        base = view.get("baseline_token_multiplier")
        if mult_val is not None and mult_src in ("config.json", "env", "flag"):
            view["baseline_token_multiplier"] = float(mult_val)
            view["baseline_source"] = mult_src
        elif self._origin == "file" and _valid_mult(base):
            view["baseline_token_multiplier"] = base
            view["baseline_source"] = "pricing.json"
        else:
            view["baseline_token_multiplier"] = 1.0
            view["baseline_source"] = "default"
        self._warnings = warnings
        self._view = view

    def current(self):
        with self._lock:
            self._refresh()
            return self._view

    def warnings(self):
        with self._lock:
            self._refresh()
            return list(self._warnings)

    # ------------------------------------------------------------------
    def resolve(self, model=None, tier=None, data=None):
        """Pricing key for a model string (prefix match), else the tier name, else None."""
        data = data or self.current()
        models = data.get("models", {})
        m = (model or "").lower()
        if m:
            for key, spec in models.items():
                for pref in spec.get("match", []):
                    if m.startswith(str(pref).lower()):
                        return key
        t = (tier or "").lower()
        if t in models:
            return t
        return None

    def base_tier(self, key, data=None):
        """Canonical tier bucket (haiku/sonnet/opus/lead) for a pricing key.

        Sub-tier entries (e.g. 'sonnet-4') declare their bucket via a 'tier' field.
        Falls back to the key itself when it is already a base tier, else 'unknown'."""
        _BASE = ("haiku", "sonnet", "opus", "lead")
        if not key:
            return "unknown"
        if key in _BASE:
            return key
        data = data or self.current()
        spec = data.get("models", {}).get(key, {})
        t = spec.get("tier", "")
        return t if t in _BASE else "unknown"

    @staticmethod
    def _price(tokens, spec):
        parts = {
            "input": _n(tokens.get("input")) * _n(spec.get("input")) / 1e6,
            "output": _n(tokens.get("output")) * _n(spec.get("output")) / 1e6,
            "cache_write": _n(tokens.get("cache_creation")) * _n(spec.get("cache_write")) / 1e6,
            "cache_read": _n(tokens.get("cache_read")) * _n(spec.get("cache_read")) / 1e6,
        }
        parts["total"] = sum(parts.values())
        return parts

    def cost(self, tokens, key, data=None):
        """Cost breakdown dict for tokens at pricing `key`; None if key unknown."""
        data = data or self.current()
        spec = data.get("models", {}).get(key)
        if not spec:
            return None
        return self._price(tokens, spec)

    def baseline(self, tokens, data=None):
        data = data or self.current()
        spec = data.get("models", {}).get(data.get("baseline_tier"))
        if not spec:
            return None
        mult = data.get("baseline_token_multiplier", 1.0)
        mult = float(mult) if _valid_mult(mult) else 1.0
        return self._price(tokens, spec)["total"] * mult
