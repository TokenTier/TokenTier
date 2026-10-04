# SPDX-License-Identifier: Apache-2.0
"""Incremental JSONL log loader + aggregator. Thread-safe, stdlib only."""
import datetime as dt
import glob
import json
import os
import re
import threading
import time

from .pricing import Pricing

STALE_AFTER_S = 2 * 3600        # no transcript to look at: stale after 2 h
IDLE_AFTER_S = 15 * 60          # transcript known: stale after 15 min without a write
STAT_TTL_S = 2.0
# Only ever stat paths of this exact shape; nothing else from the log is touched.
_TRANSCRIPT_RE = re.compile(r"[/\\]subagents[/\\]agent-[A-Za-z0-9]+\.jsonl\Z")
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
TIERS = ("haiku", "sonnet", "opus")
TOKEN_KEYS = ("input", "output", "cache_creation", "cache_read")
STATUSES = ("pass", "fail", "escalated", "running", "stale", "unknown")
SESSION_ACTIVE_S = 30 * 60      # a session without session_end counts as active this long after its last activity
SESSION_IDLE_S = 6 * 3600       # ... then idle, then finished


def parse_ts(s):
    """ISO-8601 -> aware datetime (naive treated as UTC). None if unparseable."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if m:
        frac = ((m.group(2) or "")[1:] + "000000")[:6]
        s = m.group(1) + "." + frac + m.group(3)
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def _r(x, n=6):
    return None if x is None else round(x, n)


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class RangeError(ValueError):
    pass


class Store(object):
    def __init__(self, home, pricing=None, clock=None, tz=None, poll_interval=1.0):
        self.home = home
        self.log_dir = os.path.join(home, "logs")
        self.pricing = pricing or Pricing(None)
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.tz = tz  # None -> system local timezone
        self.poll_interval = poll_interval
        self._lock = threading.RLock()
        self._offsets = {}
        self._stat_cache = {}
        self._stat_ttl = STAT_TTL_S
        self._reset_state()
        self._thread = None
        self._stop = threading.Event()
        self.extra_warnings = []  # e.g. retention problems, reported in /api/health

    # ------------------------------------------------------------ clock
    def now(self):
        n = self._clock()
        if n.tzinfo is None:
            n = n.replace(tzinfo=dt.timezone.utc)
        return n

    def local(self, d):
        return d.astimezone(self.tz) if self.tz else d.astimezone()

    def today(self):
        return self.local(self.now()).strftime("%Y-%m-%d")

    def _transcript_mtime(self, path):
        """mtime (epoch) of a worker transcript, or None. Stat only, never reads; never raises."""
        try:
            if (not isinstance(path, str) or "\0" in path or len(path) > 4096
                    or not os.path.isabs(path) or not _TRANSCRIPT_RE.search(path)
                    or ".." in re.split(r"[/\\]", path)):
                return None
            t = time.monotonic()
            hit = self._stat_cache.get(path)
            if hit is not None and 0 <= t - hit[0] < self._stat_ttl:
                return hit[1]
            try:
                m = os.stat(path).st_mtime
            except (OSError, ValueError):
                m = None
            if len(self._stat_cache) > 5000:
                self._stat_cache.clear()
            self._stat_cache[path] = (t, m)
            return m
        except Exception:
            return None

    def _iso_local(self, epoch):
        return self.local(dt.datetime.fromtimestamp(epoch, dt.timezone.utc)).isoformat(timespec="seconds")

    # ------------------------------------------------------------ state
    def _reset_state(self):
        self._offsets = {}
        self.events = 0
        self.bad_lines = 0
        self.files = 0
        self._tasks = {}      # task_id -> {"start","end","events":[...]}  (events: (ev, filedate))
        self._sessions = {}   # sid -> {"start","end"}
        self._esc = []        # (ev, filedate)
        self._lead = []       # parsed lead_usage events (main session's own usage), in log order
        self._lead_cache = None
        self._proj = {}       # project -> {"path","last"(epoch),"first","sessions":set()}
        self._task_proj = {}  # task_id -> project, from the latest event of that task carrying one
        self._sid_proj = {}   # session_id -> project, from the first event of that session carrying one
        self.generation = 0
        self._base_cache = None
        self._lead_cache = None

    def token(self):
        return "%d" % self.events

    # ---------------------------------------------------------- loading
    def refresh(self):
        with self._lock:
            paths = sorted(glob.glob(os.path.join(self.log_dir, "*.jsonl")))
            sizes = {}
            for p in paths:
                try:
                    sizes[p] = os.stat(p).st_size
                except OSError:
                    pass
            # truncated or removed file -> rebuild from scratch
            for p, off in list(self._offsets.items()):
                if p not in sizes or sizes[p] < off:
                    self._reset_state()
                    break
            changed = False
            for p in paths:
                if p not in sizes:
                    continue
                off = self._offsets.get(p, 0)
                if sizes[p] <= off:
                    continue
                try:
                    with open(p, "rb") as f:
                        f.seek(off)
                        chunk = f.read(sizes[p] - off)
                except OSError:
                    continue
                cut = chunk.rfind(b"\n")
                if cut < 0:
                    continue  # only a partial line so far
                filedate = os.path.basename(p)[:-6]
                for raw in chunk[:cut].split(b"\n"):
                    if self._ingest(raw, filedate):
                        changed = True
                self._offsets[p] = off + cut + 1
            self.files = len(paths)
            if changed:
                self.generation += 1
                self._base_cache = None
                self._lead_cache = None
            return changed

    def _ingest(self, raw, filedate):
        raw = raw.strip()
        if not raw:
            return False
        try:
            ev = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self.bad_lines += 1
            return False
        if not isinstance(ev, dict) or not isinstance(ev.get("event"), str):
            self.bad_lines += 1
            return False
        kind = ev["event"]
        item = (ev, filedate)
        if kind in ("task_start", "task_end", "task_update"):
            tid = ev.get("task_id")
            if not tid or not isinstance(tid, str):
                self.bad_lines += 1
                return False
            ent = self._tasks.setdefault(tid, {"start": None, "end": None, "events": [],
                                               "ends": [], "updates": []})
            ent["events"].append(item)
            if kind == "task_start":
                ent["start"] = item
            elif kind == "task_end":
                ent["end"] = item
                ent["ends"].append(item)
            else:
                ent["updates"].append(item)
        elif kind in ("session_start", "session_end"):
            sid = ev.get("session_id")
            if sid:
                ent = self._sessions.setdefault(sid, {"start": None, "end": None})
                ent["start" if kind == "session_start" else "end"] = item
        elif kind == "escalation":
            self._esc.append(item)
        elif kind == "lead_usage":
            rec = self._parse_lead(ev, filedate)
            if rec is None:
                self.bad_lines += 1
                return False
            self._lead.append(rec)
        self.events += 1
        # project activity. An event without project fields (task_update, for one) never creates a project or a
        # session entry: it is attributed to the project already known for its task or session, else ignored.
        pn = ev.get("project")
        if not pn or not isinstance(pn, str):
            pn = None
            if kind in ("task_start", "task_end", "task_update"):
                pn = self._task_proj.get(ev.get("task_id"))
            if pn is None and ev.get("session_id"):
                pn = self._sid_proj.get(ev.get("session_id"))
            if pn is None:
                return True
        else:
            if kind in ("task_start", "task_end", "task_update"):
                self._task_proj[ev["task_id"]] = pn
            if ev.get("session_id") and isinstance(ev.get("session_id"), str):
                self._sid_proj.setdefault(ev["session_id"], pn)
        d = parse_ts(ev.get("ts"))
        ep = d.timestamp() if d else 0
        pr = self._proj.setdefault(pn, {"path": None, "last": 0, "last_ts": None, "first": None,
                                        "first_ts": None, "sessions": set()})
        if ev.get("project") and ev.get("project_path"):
            pr["path"] = ev.get("project_path")
        if ep >= pr["last"]:
            pr["last"] = ep
            pr["last_ts"] = ev.get("ts")
        if d and (pr["first"] is None or ep < pr["first"]):
            pr["first"] = ep
            pr["first_ts"] = ev.get("ts")
        sid = ev.get("session_id")
        if sid and isinstance(sid, str) and (ev.get("project") or self._sid_proj.get(sid) == pn):
            pr["sessions"].add(sid)
        return True

    @staticmethod
    def _parse_lead(ev, filedate):
        """lead_usage event -> compact record, or None when the token block is unusable."""
        t = ev.get("tokens")
        if not isinstance(t, dict):
            return None
        vals = {k: _num(t.get(k)) for k in TOKEN_KEYS}
        if any(v is None or v < 0 for v in vals.values()):
            return None
        return {"session_id": ev.get("session_id"), "project": ev.get("project") or "unknown",
                "has_project": bool(ev.get("project")),
                "project_path": ev.get("project_path"), "model": ev.get("model"), "tier": ev.get("tier"),
                "tokens": vals, "turns": max(0, _num(ev.get("turns")) or 0), "ts": ev.get("ts"),
                "from_ts": ev.get("from_ts") or ev.get("ts"), "filedate": filedate}

    # ---------------------------------------------------- poll thread
    def start(self):
        self.refresh()
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tokentier-poll", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(self.poll_interval):
            try:
                self.refresh()
            except Exception:  # never die
                pass

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ dates
    def _date_of(self, ts, filedate=None):
        d = parse_ts(ts)
        if d:
            return self.local(d).strftime("%Y-%m-%d")
        return filedate

    def resolve_range(self, frm, to, min_date=None, default="today"):
        today = self.today()
        td = dt.datetime.strptime(today, "%Y-%m-%d").date()

        def one(v, is_from):
            v = (v or default).strip().lower()
            if v == "today":
                return td
            if v == "yesterday":
                return td - dt.timedelta(days=1)
            if v in ("7d", "30d"):
                n = int(v[:-1])
                return td - dt.timedelta(days=n - 1) if is_from else td
            if v == "all":
                if is_from and min_date:
                    return min(dt.datetime.strptime(min_date, "%Y-%m-%d").date(), td)
                return td
            try:
                return dt.datetime.strptime(v, "%Y-%m-%d").date()
            except ValueError:
                raise RangeError("bad date %r (use YYYY-MM-DD, today, yesterday, 7d, 30d, all)" % v)

        f = one(frm, True)
        if not to and (frm or "").strip().lower() == "yesterday":
            to = "yesterday"
        t = one(to if to else "today", False)
        if f > t:
            f, t = t, f
        return f.strftime("%Y-%m-%d"), t.strftime("%Y-%m-%d")

    # ------------------------------------------------------------ tasks
    @staticmethod
    def _status_rank(ev):
        """status_line (explicit STATUS) > inferred > unknown. Legacy events without a source: known = inferred."""
        src = ev.get("status_source")
        if src == "status_line":
            return 2
        if src == "inferred":
            return 1
        return 0 if ev.get("status") in (None, "", "unknown") else 1

    @staticmethod
    def _tokens_of(e):
        if e and isinstance(e.get("tokens"), dict):
            t = e["tokens"]
            vals = {k: _num(t.get(k)) for k in ("input", "output", "cache_creation", "cache_read")}
            if all(v is not None for v in vals.values()):
                return vals
        return None

    def _merge_ends(self, ends):
        """Field-wise merge of every task_end of one task (Claude Code fires SubagentStop twice).

        status: highest rank wins, never downgraded (ties: latest); tokens/duration/model/ended_at: latest event
        carrying them; result: latest non-empty (the longer one on equal timestamps)."""
        if not ends:
            return None
        order = sorted(range(len(ends)), key=lambda i: (parse_ts(ends[i][0].get("ts")) or _EPOCH, i))
        evs = [ends[i][0] for i in order]
        best = None
        for e in evs:
            if best is None or self._status_rank(e) >= self._status_rank(best):
                best = e

        def latest(pred, val):
            for e in reversed(evs):
                if pred(e):
                    return val(e)
            return None

        res = None
        res_ts = None
        for e in evs:
            r = e.get("result")
            if isinstance(r, str) and r.strip():
                ts = parse_ts(e.get("ts"))
                if res is None or ts != res_ts or len(r) >= len(res):
                    res, res_ts = r, ts
        src = best.get("status_source")
        merged = {
            "status": best.get("status"),
            "status_source": src if src in ("status_line", "inferred") else None,
            "tokens": latest(lambda e: self._tokens_of(e) is not None, self._tokens_of),
            "duration_ms": latest(lambda e: _num(e.get("duration_ms")) is not None, lambda e: _num(e.get("duration_ms"))),
            "model": latest(lambda e: e.get("model") not in (None, ""), lambda e: e.get("model")),
            "ended_at": latest(lambda e: bool(e.get("ts")), lambda e: e.get("ts")),
            "result": res,
            "last": evs[-1],
        }
        return merged

    def _base_tasks(self):
        if self._base_cache is not None:
            return self._base_cache
        out = []
        for tid, ent in self._tasks.items():
            s = ent["start"][0] if ent["start"] else None
            m = self._merge_ends(ent["ends"])
            e = m["last"] if m else None  # latest end event: identity / legacy fields
            if s is None and e is None:
                continue  # only task_update events so far
            sfd = ent["start"][1] if ent["start"] else None
            efd = ent["end"][1] if ent["end"] else None
            src = e or s
            seq = [x[0] for x in ent["ends"]] + ([s] if s else []) + [x[0] for x in ent["updates"]]

            def pick(k):
                for x in seq:
                    if x.get(k) not in (None, ""):
                        return x.get(k)
                return None

            if s:
                started = s.get("ts")
                fd = sfd
            else:
                started = e.get("started_at") or e.get("ts")
                fd = efd
            sd = parse_ts(started)
            if sd is None and e and not s:
                sd = parse_ts(e.get("ts"))
                started = e.get("ts") if sd else started
            toks = m["tokens"] if m else None
            legacy_total = None
            if (e and e.get("source") == "legacy" and (toks is None or not any(toks.values()))
                    and _num(e.get("legacy_tokens_total")) is not None):
                legacy_total = max(0, _num(e.get("legacy_tokens_total")))
            out.append({
                "_legacy_total": legacy_total,
                "task_id": tid,
                "session_id": src.get("session_id") or pick("session_id"),
                "project": src.get("project") or pick("project") or "unknown",
                "project_path": src.get("project_path") or pick("project_path"),
                "label": pick("label"),
                "agent_type": pick("agent_type"),
                "tier_raw": pick("tier"),
                "router_worker": bool(pick("router_worker")),
                "tool_use_id": pick("tool_use_id"),
                "_transcript": pick("subagent_transcript"),
                "model": (m["model"] if m else None) or pick("model"),
                "_has_end": e is not None,
                "_status": (m["status"] if m else None),
                "status_source": (m["status_source"] if m else None),
                "date": self._date_of(started, fd) if sd or fd else None,
                "started_at": started if sd else None,
                "_start_epoch": sd.timestamp() if sd else 0.0,
                "ended_at": m["ended_at"] if m else None,
                "_duration": m["duration_ms"] if m else None,
                "_tokens": toks,
                "result": m["result"] if m else None,
            })
        out.sort(key=lambda t: (t["_start_epoch"], t["task_id"]), reverse=True)
        self._base_cache = out
        return out

    def _view(self, b, now_ep, pdata):
        t = {k: v for k, v in b.items() if not k.startswith("_") and k != "tier_raw"}
        status = b["_status"] if b["_has_end"] else "running"
        dur = b["_duration"]
        t["stale_reason"] = None
        t["last_activity"] = None
        if not b["_has_end"]:
            se = b["_start_epoch"]
            age = now_ep - se if se else 0
            mt = self._transcript_mtime(b["_transcript"])
            if mt is not None and se:
                last = max(se, mt)
                t["last_activity"] = self._iso_local(last)
                if now_ep - last > IDLE_AFTER_S:
                    status = "stale"
                    t["stale_reason"] = "no activity for %d min" % int((now_ep - last) // 60)
                    dur = int(max(0, last - se) * 1000)
                else:
                    dur = int(max(0, age) * 1000)
            elif age > STALE_AFTER_S:
                status = "stale"
                t["stale_reason"] = "no activity for %d min" % int(age // 60)
                t["last_activity"] = self._iso_local(se)
                dur = None
            else:
                dur = int(max(0, age) * 1000)
        elif dur is None and b["ended_at"] and b["_start_epoch"]:
            ed = parse_ts(b["ended_at"])
            if ed:
                dur = max(0, int((ed.timestamp() - b["_start_epoch"]) * 1000))
        t["_start_epoch"] = b["_start_epoch"]
        t["status"] = status or "unknown"
        if not b["_has_end"]:
            t["status_source"] = None
        t["duration_ms"] = dur
        key = self.pricing.resolve(b["model"], b["tier_raw"], pdata)
        t["tier"] = key or (b["tier_raw"] if b["tier_raw"] in ("lead",) else "unknown")
        t["pricing_key"] = key
        toks = b["_tokens"]
        t["tokens"] = dict(toks) if toks else {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
        t["tokens_total"] = sum(t["tokens"].values())
        legacy = b["_legacy_total"] is not None
        t["legacy"] = legacy
        if legacy:
            # old router-kit log: only a single token number, no breakdown -> never priced
            t["tokens_total"] = b["_legacy_total"]
            t["legacy_tokens_total"] = b["_legacy_total"]
        cost = None
        if toks is not None and key and not legacy:
            cost = self.pricing.cost(toks, key, pdata)
        t["cost_known"] = cost is not None
        if cost is not None:
            t["cost"] = {k: _r(v) for k, v in cost.items()}
            base = self.pricing.baseline(toks, pdata)
            t["baseline_cost"] = _r(base)
            t["saved"] = _r(base - cost["total"]) if base is not None else None
            t["_cost"] = cost["total"]
            t["_base"] = base
        else:
            t["cost"] = None
            t["baseline_cost"] = None
            t["saved"] = None
            t["_cost"] = None
            t["_base"] = None
        return t

    def _views(self):
        pdata = self.pricing.current()
        now_ep = self.now().timestamp()
        return [self._view(b, now_ep, pdata) for b in self._base_tasks()]

    @staticmethod
    def _public(t):
        return {k: v for k, v in t.items() if not k.startswith("_")}

    def _in_range(self, d, f, t):
        return d is not None and f <= d <= t

    def _filter(self, views, p):
        project = p.get("project")
        session = p.get("session")
        tier = p.get("tier")
        status = p.get("status")
        statuses = set(x.strip() for x in str(status).split(",") if x.strip()) if status else None
        q = (p.get("q") or "").lower()
        router_only = str(p.get("router_only") or "").lower() in ("1", "true", "yes")
        f, t = p.get("_range", (None, None))
        out = []
        for v in views:
            if project and v["project"] != project:
                continue
            if session and v["session_id"] != session:
                continue
            if tier and v["tier"] != tier:
                continue
            if statuses and v["status"] not in statuses:
                continue
            if router_only and not v["router_worker"]:
                continue
            if f and not self._in_range(v["date"], f, t):
                continue
            if q and not (q in (v["label"] or "").lower() or q in (v["result"] or "").lower()
                          or q in (v["project"] or "").lower()):
                continue
            out.append(v)
        return out

    def _min_date(self):
        ds = [b["date"] for b in self._base_tasks() if b["date"]]
        for ent in self._sessions.values():
            x = ent["start"] or ent["end"]
            if x:
                d = self._date_of(x[0].get("ts"), x[1])
                if d:
                    ds.append(d)
        ds.extend(r["date"] for r in self._lead_views() if r["date"])
        return min(ds) if ds else None

    # ------------------------------------------------- lead session usage
    def _lead_views(self):
        """Lead records with date, pricing key and cost. Cached until new events or a pricing reload.

        Lead usage is priced with the same maths as tasks (model prefix -> pricing key). Its cost is
        reported next to the routing numbers but never enters the savings comparison: the lead
        model is the user's own choice, not something the router decided."""
        pdata = self.pricing.current()
        c = self._lead_cache
        if c is not None and c[0] is pdata:
            return c[1]
        out = []
        for r in self._lead:
            key = self.pricing.resolve(r["model"], r["tier"], pdata)
            cost = self.pricing.cost(r["tokens"], key, pdata)["total"] if key else None
            v = dict(r)
            v["date"] = self._date_of(r["ts"], r["filedate"])
            v["pricing_key"] = key
            v["cost"] = cost
            v["total"] = sum(r["tokens"].values())
            out.append(v)
        self._lead_cache = (pdata, out)
        return out

    # -------------------------------------------------------- queries
    def health(self, version=None):
        pdata = self.pricing.current()
        warnings = list(self.pricing.warnings()) + list(self.extra_warnings)
        with self._lock:
            n = self.local(self.now())
            off = n.strftime("%z")
            return {
                "baseline_token_multiplier": pdata.get("baseline_token_multiplier"),
                "baseline_source": pdata.get("baseline_source"),
                "warnings": warnings,
                "ok": True, "version": version, "now": n.isoformat(timespec="seconds"),
                "today": n.strftime("%Y-%m-%d"), "tz": n.tzname(),
                "tz_offset": (off[:3] + ":" + off[3:]) if off else "+00:00",
                "log_dir": self.log_dir, "files": self.files, "events": self.events,
                "tasks": len(self._tasks), "bad_lines": self.bad_lines,
            }

    def overview(self, frm=None, to=None, project=None):
        with self._lock:
            f, t = self.resolve_range(frm, to, self._min_date())
            views = self._filter(self._views(), {"project": project, "_range": (f, t)})
            tot = {"tasks": 0, "completed": 0, "running": 0, "failed": 0, "escalations": 0,
                   "tokens": {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "total": 0},
                   "cost": 0.0, "baseline_cost": 0.0, "saved": 0.0, "saved_pct": 0.0,
                   "unknown_cost_tasks": 0, "legacy_tasks": 0, "legacy_tokens": 0}
            by_tier = {k: {"tasks": 0, "tokens": 0, "cost": 0.0} for k in TIERS + ("unknown",)}
            daily = {}
            d = dt.datetime.strptime(t, "%Y-%m-%d").date()
            start = dt.datetime.strptime(f, "%Y-%m-%d").date()
            n = 0
            while d >= start and n < 3700:
                ds = d.strftime("%Y-%m-%d")
                daily[ds] = {"date": ds, "tasks": 0, "cost": 0.0, "baseline_cost": 0.0, "tokens": 0}
                d -= dt.timedelta(days=1)
                n += 1
            byproj = {}
            esc_ids = set()
            lead = {"tokens": {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "total": 0},
                    "cost": 0.0, "turns": 0, "sessions": 0, "unknown_cost": 0, "by_model": {}}
            lead_sids = set()
            for x in daily.values():
                x["lead_cost"] = 0.0
                x["lead_tokens"] = 0
            for lv in self._lead_views():
                if not self._in_range(lv["date"], f, t) or (project and lv["project"] != project):
                    continue
                for k in TOKEN_KEYS:
                    lead["tokens"][k] += lv["tokens"][k]
                lead["tokens"]["total"] += lv["total"]
                lead["turns"] += lv["turns"]
                if lv["session_id"]:
                    lead_sids.add(lv["session_id"])
                bm = lead["by_model"].setdefault(lv["model"] or "unknown", {
                    "tokens": 0, "cost": 0.0, "turns": 0, "pricing_key": lv["pricing_key"]})
                bm["tokens"] += lv["total"]
                bm["turns"] += lv["turns"]
                dd = daily.get(lv["date"])
                if dd is not None:
                    dd["lead_tokens"] += lv["total"]
                if lv["cost"] is None:
                    lead["unknown_cost"] += 1
                else:
                    lead["cost"] += lv["cost"]
                    bm["cost"] += lv["cost"]
                    if dd is not None:
                        dd["lead_cost"] += lv["cost"]
            lead["sessions"] = len(lead_sids)
            for v in views:
                tot["tasks"] += 1
                st = v["status"]
                if st == "pass":
                    tot["completed"] += 1
                elif st == "running":
                    tot["running"] += 1
                elif st == "fail":
                    tot["failed"] += 1
                elif st == "escalated":
                    esc_ids.add(v["task_id"])
                for k in ("input", "output", "cache_creation", "cache_read"):
                    tot["tokens"][k] += v["tokens"][k]
                tot["tokens"]["total"] += v["tokens_total"]
                tk = v["tier"] if v["tier"] in TIERS else "unknown"
                bt = by_tier[tk]
                bt["tasks"] += 1
                bt["tokens"] += v["tokens_total"]
                dd = daily.get(v["date"])
                if dd is not None:
                    dd["tasks"] += 1
                    dd["tokens"] += v["tokens_total"]
                pj = byproj.setdefault(v["project"], {
                    "project": v["project"], "project_path": v["project_path"], "tasks": 0,
                    "tokens": 0, "cost": 0.0, "baseline_cost": 0.0, "saved": 0.0, "_last": 0.0})
                pj["tasks"] += 1
                pj["tokens"] += v["tokens_total"]
                if v["cost_known"]:
                    c, b = v["_cost"], v["_base"]
                    tot["cost"] += c
                    tot["baseline_cost"] += b
                    bt["cost"] += c
                    pj["cost"] += c
                    pj["baseline_cost"] += b
                    if dd is not None:
                        dd["cost"] += c
                        dd["baseline_cost"] += b
                elif v["legacy"]:
                    tot["legacy_tasks"] += 1
                    tot["legacy_tokens"] += v["tokens_total"]
                elif st not in ("running", "stale"):
                    tot["unknown_cost_tasks"] += 1
            # project activity ordering uses started epoch via base tasks
            epoch = {b["project"]: 0.0 for b in self._base_tasks()}
            for b in self._base_tasks():
                epoch[b["project"]] = max(epoch[b["project"]], b["_start_epoch"])
            tmap = {b["task_id"]: b for b in self._base_tasks()}
            for e, fd in self._esc:
                tid = e.get("task_id")
                bt_ = tmap.get(tid) if tid else None
                if bt_:
                    dte, pj_ = bt_["date"], bt_["project"]
                else:
                    dte, pj_ = self._date_of(e.get("ts"), fd), e.get("project") or "unknown"
                if not self._in_range(dte, f, t) or (project and pj_ != project):
                    continue
                esc_ids.add(tid if tid else id(e))
            tot["escalations"] = len(esc_ids)
            tot["saved"] = tot["baseline_cost"] - tot["cost"]
            tot["saved_pct"] = (tot["saved"] / tot["baseline_cost"] * 100) if tot["baseline_cost"] else 0.0
            for k in ("cost", "baseline_cost", "saved"):
                tot[k] = _r(tot[k])
            tot["saved_pct"] = round(tot["saved_pct"], 2)
            for v in by_tier.values():
                v["cost"] = _r(v["cost"])
            lead["cost"] = _r(lead["cost"])
            for bm in lead["by_model"].values():
                bm["cost"] = _r(bm["cost"])
            total_spend = _r((tot["cost"] or 0.0) + lead["cost"])
            dl = sorted(daily.values(), key=lambda x: x["date"], reverse=True)
            for x in dl:
                x["lead_cost"] = _r(x["lead_cost"])
                x["cost"] = _r(x["cost"])
                x["baseline_cost"] = _r(x["baseline_cost"])
            plist = sorted(byproj.values(), key=lambda x: (epoch.get(x["project"], 0), x["project"]), reverse=True)
            for x in plist:
                x.pop("_last", None)
                for k in ("cost", "baseline_cost", "saved"):
                    x[k] = _r(x[k]) if k != "saved" else _r(x["baseline_cost"] - x["cost"])
            tot["lead"] = lead                # the main session's own usage (not part of the savings maths)
            tot["total_spend"] = total_spend  # delegated tasks + lead session
            return {"range": {"from": f, "to": t}, "totals": tot, "by_tier": by_tier,
                    "daily": dl, "by_project": plist}

    # ------------------------------------------------- per-card helpers
    @staticmethod
    def _mix():
        return {"haiku": 0, "sonnet": 0, "opus": 0, "unknown": 0}

    @staticmethod
    def _status_counts():
        return {k: 0 for k in STATUSES}

    def _count(self, a, v):
        a["tier_mix"][v["tier"] if v["tier"] in TIERS else "unknown"] += 1
        st = v["status"] if v["status"] in STATUSES else "unknown"
        a["status_counts"][st] += 1

    def _task_epochs(self, v):
        """(first, last) activity epochs of one task view: start, end and last observed activity."""
        eps = [v["_start_epoch"]] if v["_start_epoch"] else []
        for k in ("ended_at", "last_activity"):
            d = parse_ts(v.get(k))
            if d:
                eps.append(d.timestamp())
        return (min(eps), max(eps)) if eps else (None, None)

    def _iso_or(self, epoch, fallback=None):
        return self._iso_local(epoch) if epoch else fallback

    def projects(self, days=14):
        """All-time project rows (one pass over the cached task views) for the project cards."""
        with self._lock:
            views = self._views()
            today = dt.datetime.strptime(self.today(), "%Y-%m-%d").date()
            keys = [(today - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
            kidx = {k: i for i, k in enumerate(keys)}
            agg = {}

            def row(n):
                a = agg.get(n)
                if a is None:
                    a = agg[n] = {"tasks": 0, "cost": 0.0, "base": 0.0, "sessions": set(), "running": 0,
                                  "tier_mix": self._mix(), "status_counts": self._status_counts(),
                                  "daily": [[0.0, 0.0, 0] for _ in keys], "first": None, "last": None,
                                  "lead_cost": 0.0, "lead_tokens": 0, "lead_unpriced": 0, "path": None}
                return a

            def seen(a, lo, hi):
                if lo is not None and (a["first"] is None or lo < a["first"]):
                    a["first"] = lo
                if hi is not None and (a["last"] is None or hi > a["last"]):
                    a["last"] = hi

            for v in views:
                a = row(v["project"])
                a["tasks"] += 1
                self._count(a, v)
                if v["status"] == "running":
                    a["running"] += 1
                if v["session_id"]:
                    a["sessions"].add(v["session_id"])
                if not a["path"] and v["project_path"]:
                    a["path"] = v["project_path"]
                i = kidx.get(v["date"])
                if i is not None:
                    a["daily"][i][2] += 1
                if v["cost_known"]:
                    a["cost"] += v["_cost"]
                    a["base"] += v["_base"]
                    if i is not None:
                        a["daily"][i][0] += v["_cost"]
                seen(a, *self._task_epochs(v))
            for lv in self._lead_views():
                if not lv.get("has_project", True) and lv["project"] not in agg and lv["project"] not in self._proj:
                    continue
                a = row(lv["project"])
                a["lead_tokens"] += lv["total"]
                if lv["session_id"]:
                    a["sessions"].add(lv["session_id"])
                if not a["path"] and lv.get("project_path"):
                    a["path"] = lv["project_path"]
                i = kidx.get(lv["date"])
                if lv["cost"] is None:
                    a["lead_unpriced"] += 1
                else:
                    a["lead_cost"] += lv["cost"]
                    if i is not None:
                        a["daily"][i][1] += lv["cost"]
                d1, d2 = parse_ts(lv.get("from_ts")), parse_ts(lv.get("ts"))
                seen(a, d1.timestamp() if d1 else None, d2.timestamp() if d2 else None)
            out = []
            for n in set(agg) | set(self._proj):
                a = row(n)
                p = self._proj.get(n, {"path": None, "last": 0, "last_ts": None, "first": None,
                                       "first_ts": None, "sessions": set()})
                seen(a, p.get("first"), p.get("last") or None)
                last_e = a["last"] or 0
                out.append({
                    "project": n, "project_path": p["path"] or a["path"], "tasks": a["tasks"],
                    "sessions": len(a["sessions"] | p["sessions"]), "cost": _r(a["cost"]),
                    "baseline_cost": _r(a["base"]), "saved": _r(a["base"] - a["cost"]),
                    "lead_cost": _r(a["lead_cost"]), "lead_tokens": a["lead_tokens"],
                    "lead_unpriced": a["lead_unpriced"], "spend": _r(a["cost"] + a["lead_cost"]),
                    "running": a["running"], "tier_mix": a["tier_mix"], "status_counts": a["status_counts"],
                    "daily": [{"date": k, "cost": _r(d[0]), "lead_cost": _r(d[1]), "tasks": d[2]}
                              for k, d in zip(keys, a["daily"])],
                    "first_active": p.get("first_ts") if p.get("first") and p["first"] == a["first"]
                    else self._iso_or(a["first"]),
                    "last_active": p["last_ts"] if p.get("last") and p["last"] >= last_e
                    else self._iso_or(a["last"], p["last_ts"]),
                    "_e": max(last_e, p.get("last") or 0)})
            out.sort(key=lambda x: (x["_e"], x["project"]), reverse=True)
            for o in out:
                o.pop("_e")
            return out

    def sessions(self, project=None, frm=None, to=None):
        with self._lock:
            rng = None
            if frm or to:
                rng = self.resolve_range(frm or "all", to, self._min_date())
            views = self._views()
            now_ep = self.now().timestamp()
            by_s = {}
            for v in views:
                if v["session_id"]:
                    by_s.setdefault(v["session_id"], []).append(v)
            lead_by_s = {}
            for lv in self._lead_views():
                if lv["session_id"]:
                    lead_by_s.setdefault(lv["session_id"], []).append(lv)
            sids = set(by_s) | set(self._sessions) | set(lead_by_s)
            out = []
            for sid in sids:
                ent = self._sessions.get(sid, {"start": None, "end": None})
                ts = by_s.get(sid, [])
                s_ev = ent["start"][0] if ent["start"] else None
                e_ev = ent["end"][0] if ent["end"] else None
                first = min(ts, key=lambda x: x["_start_epoch"]) if ts else None
                if s_ev:
                    started, fd = s_ev.get("ts"), ent["start"][1]
                elif first:
                    started, fd = first["started_at"], first["date"]
                elif e_ev:
                    started, fd = e_ev.get("ts"), ent["end"][1]
                elif lead_by_s.get(sid):
                    lv0 = min(lead_by_s[sid], key=lambda x: (parse_ts(x["from_ts"]) or parse_ts(x["ts"])
                                                             or dt.datetime.max.replace(tzinfo=dt.timezone.utc)))
                    started, fd = lv0["from_ts"], lv0["filedate"]
                else:
                    continue
                date = self._date_of(started, fd)
                lv_list = lead_by_s.get(sid, [])
                proj = ((s_ev or {}).get("project") or (e_ev or {}).get("project")
                        or (ts[0]["project"] if ts else None)
                        or next((x["project"] for x in lv_list if x.get("has_project", True)), None) or "unknown")
                if project and proj != project:
                    continue
                if rng and not self._in_range(date, rng[0], rng[1]):
                    continue
                cost = base = 0.0
                a = {"tier_mix": self._mix(), "status_counts": self._status_counts()}
                last = None
                latest = None
                running = 0
                for v in ts:
                    self._count(a, v)
                    if v["status"] == "running":
                        running += 1
                    if v["cost_known"]:
                        cost += v["_cost"]
                        base += v["_base"]
                    hi = self._task_epochs(v)[1]
                    if hi is not None and (last is None or hi > last):
                        last = hi
                    if latest is None or (v["_start_epoch"], v["task_id"]) > (latest["_start_epoch"], latest["task_id"]):
                        latest = v
                for x in (s_ev, e_ev):
                    d = parse_ts(x.get("ts")) if x else None
                    if d and (last is None or d.timestamp() > last):
                        last = d.timestamp()
                for lv in lv_list:
                    d = parse_ts(lv.get("ts"))
                    if d and (last is None or d.timestamp() > last):
                        last = d.timestamp()
                sd = parse_ts(started)
                if e_ev:
                    state = "finished"
                elif running or (last is not None and now_ep - last < SESSION_ACTIVE_S):
                    state = "active"
                elif last is not None and now_ep - last < SESSION_IDLE_S:
                    state = "idle"
                else:
                    state = "finished"
                top = max((v for v in ts if v["cost_known"]), key=lambda v: v["_cost"], default=None)
                lead_cost = sum(v["cost"] for v in lv_list if v["cost"] is not None)
                out.append({
                    "session_id": sid, "project": proj, "started_at": started,
                    "ended_at": e_ev.get("ts") if e_ev else None, "tasks": len(ts),
                    "running": running,
                    "cost": _r(cost), "baseline_cost": _r(base), "saved": _r(base - cost), "date": date,
                    "lead_tokens": sum(v["total"] for v in lv_list),
                    "lead_cost": _r(lead_cost), "spend": _r(cost + lead_cost),
                    "tier_mix": a["tier_mix"], "status_counts": a["status_counts"], "state": state,
                    "first_active": started if sd else None,
                    "last_active": self._iso_or(last),
                    "preview_label": (latest["label"] or latest["agent_type"]) if latest else None,
                    "preview_task_id": latest["task_id"] if latest else None,
                    "top_label": (top["label"] or top["agent_type"]) if top else None,
                    "top_cost": _r(top["_cost"]) if top else None,
                    "_e": sd.timestamp() if sd else 0})
            out.sort(key=lambda x: (x["_e"], x["session_id"]), reverse=True)
            for o in out:
                o.pop("_e")
            return out

    _SORTS = {
        "cost": lambda v: v["_cost"] if v["_cost"] is not None else -1.0,
        "duration": lambda v: v["duration_ms"] if v["duration_ms"] is not None else -1,
        "tokens": lambda v: v["tokens_total"] or 0,
    }

    def tasks(self, params):
        with self._lock:
            p = dict(params)
            if p.get("from") or p.get("to"):
                p["_range"] = self.resolve_range(p.get("from") or "all", p.get("to"), self._min_date())
            views = self._views()
            items = self._filter(views, p)
            # live counts for the filter chips: each ignores its own facet
            sc = self._status_counts()
            for v in (self._filter(views, dict(p, status=None)) if p.get("status") else items):
                sc[v["status"] if v["status"] in STATUSES else "unknown"] += 1
            tc = {}
            for v in (self._filter(views, dict(p, tier=None)) if p.get("tier") else items):
                tc[v["tier"]] = tc.get(v["tier"], 0) + 1
            key = self._SORTS.get(str(p.get("sort") or "").lower())
            if key:
                items = sorted(items, key=lambda v: (key(v), v["_start_epoch"], v["task_id"]), reverse=True)
            total = len(items)
            limit = max(0, min(int(p.get("limit", 100)), 1000))
            offset = max(0, int(p.get("offset", 0)))
            return {"total": total, "items": [self._public(v) for v in items[offset:offset + limit]],
                    "counts": {"status": sc, "tier": tc}}

    def task(self, task_id):
        with self._lock:
            ent = self._tasks.get(task_id)
            if not ent:
                return None
            views = self._views()
            me = next((v for v in views if v["task_id"] == task_id), None)
            if me is None:
                return None
            out = self._public(me)
            out["events"] = [e for e, _ in ent["events"]]
            tu = me.get("tool_use_id")
            out["same_tool_use"] = [self._public(v) for v in views
                                    if tu and v["tool_use_id"] == tu and v["task_id"] != task_id]
            return out
