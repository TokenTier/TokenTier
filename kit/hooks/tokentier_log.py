#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""TokenTier logging hook.

Usage: tokentier_log.py <EventName>   (hook JSON payload on stdin)

Appends event schema v1 lines to $TOKENTIER_HOME/logs/YYYY-MM-DD.jsonl
(default ~/.tokentier). Always exits 0 and never prints to stdout.
`Stop` / `SessionEnd` also log the lead (main) session's own token usage as `lead_usage` events,
incrementally, using per-session state in $TOKENTIER_HOME/state/.
Python 3.9+, stdlib only. Runs on macOS, Linux and Windows.

Windows: registered in exec form (python.exe + args, no shell), optionally with
`--home DIR` after the event name (exec form cannot set environment variables).
Locking uses fcntl.flock on POSIX and msvcrt.locking on Windows; if a lock cannot be taken
within ~1 s the event is appended anyway (never lost, never blocking Claude Code).
"""
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
import traceback

try:
    import fcntl
except ImportError:  # pragma: no cover (Windows)
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no CRLF translation in os.write
APPEND_LOCK_WAIT = 1.0                 # seconds; after that we append without the lock
APPEND_LOCK_NAME = ".tokentier-append.lock"
REPLACE_RETRIES = 10
REPLACE_DELAY = 0.05

ROUTER_WORKERS = {"fast-worker": "haiku", "mid-worker": "sonnet", "deep-worker": "opus"}
STATUS_MAP = {"done": "pass", "escalate": "escalated", "blocked": "fail"}
_STATUS_RE = re.compile(r"^\W*STATUS\s*:\s*(done|escalate|blocked)\b", re.I)
_ESCALATE_RE = re.compile(r"ESCALATE\s*:\s*(.*)", re.I)


# ----------------------------------------------------------------- helpers
def tokentier_home():
    return os.environ.get("TOKENTIER_HOME") or os.path.join(os.path.expanduser("~"), ".tokentier")


def _tzset():
    if hasattr(time, "tzset"):
        time.tzset()


def parse_ts(s):
    """Parse an ISO-8601 timestamp (UTC 'Z' or offset) into an aware datetime."""
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if m:
        frac = (m.group(2) or "")[1:]
        frac = (frac + "000000")[:6]
        s = m.group(1) + "." + frac + m.group(3)
    d = _dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def local_iso(dt):
    """Aware (or naive-UTC) datetime -> local ISO-8601, ms precision, UTC offset."""
    _tzset()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    d = dt.astimezone()
    off = d.strftime("%z")  # +0500
    off = off[:3] + ":" + off[3:]
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (d.microsecond // 1000) + off


def tier_for(agent_type, model=None):
    if agent_type in ROUTER_WORKERS:
        return ROUTER_WORKERS[agent_type]
    m = (model or "").lower()
    for name in ("haiku", "sonnet", "opus"):
        if name in m:
            return name
    return "unknown"


NEGATIVE_MARKERS = ("escalate", "blocked", "unable to", "could not", "couldn't", "cannot", "can't",
                    "failed", "failure", "not able", "error:")
_STATUS_RANK = {"status_line": 2, "inferred": 1}


def classify_report(text):
    """Return (status, status_source) for a worker report.

    An explicit trailing 'STATUS: x' line wins (source 'status_line'). Without one, a non-empty report
    that mentions no problem marker is inferred 'pass' (source 'inferred'); otherwise 'unknown'.
    """
    if text and str(text).strip():
        for line in reversed([l for l in str(text).splitlines() if l.strip()]):
            m = _STATUS_RE.match(line.strip())
            if m:
                return STATUS_MAP[m.group(1).lower()], "status_line"
            break  # only look at the final non-empty line
        low = str(text).lower()
        if not any(mk in low for mk in NEGATIVE_MARKERS):
            return "pass", "inferred"
    return "unknown", None


def parse_status(text, router_worker=False):
    """Return pass|escalated|fail|unknown (see classify_report)."""
    return classify_report(text)[0]


def subagent_paths(transcript_path, agent_id):
    if not transcript_path or not agent_id:
        return None, None
    base = transcript_path[:-6] if transcript_path.endswith(".jsonl") else transcript_path
    d = os.path.join(base, "subagents")
    return (os.path.join(d, "agent-%s.jsonl" % agent_id),
            os.path.join(d, "agent-%s.meta.json" % agent_id))


def parse_transcript_usage(path):
    """Sum usage over a transcript, taking the LAST line per message.id."""
    out = {"input": None, "output": None, "cache_creation": None, "cache_read": None,
           "model": None, "first_ts": None, "last_ts": None, "duration_ms": None}
    last = {}
    first_ts = last_ts = None
    anon = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = obj.get("timestamp")
            if ts:
                try:
                    d = parse_ts(ts)
                    if first_ts is None or d < first_ts:
                        first_ts = d
                    if last_ts is None or d > last_ts:
                        last_ts = d
                except ValueError:
                    pass
            msg = obj.get("message")
            if obj.get("type") != "assistant" or not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            mid = msg.get("id")
            if not mid:
                anon += 1
                mid = "_anon%d" % anon
            last[mid] = (usage, msg.get("model"))
    tot = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    model = None
    for usage, mdl in last.values():
        tot["input"] += int(usage.get("input_tokens") or 0)
        tot["output"] += int(usage.get("output_tokens") or 0)
        tot["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
        tot["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        if mdl and mdl != "<synthetic>":
            model = mdl
    if last:
        out.update(tot)
    out["model"] = model
    out["first_ts"] = first_ts
    out["last_ts"] = last_ts
    if first_ts and last_ts:
        out["duration_ms"] = int(round((last_ts - first_ts).total_seconds() * 1000))
    return out


# ------------------------------------------------------------- locking
def lock_fd(fd, wait, poll=0.02, clock=None, sleep=None):
    """Exclusive lock on an open fd, polling until `wait` seconds have passed. Returns True if
    locked. POSIX: fcntl.flock. Windows: msvcrt.locking of byte 0 (works past EOF too).
    Never raises."""
    clock = clock or time.time
    sleep = sleep or time.sleep
    deadline = clock() + max(0.0, wait)
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            if msvcrt is not None:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            return False  # no locking primitive at all: caller proceeds unlocked
        except (IOError, OSError, ValueError):
            pass
        except Exception:
            return False
        if clock() >= deadline:
            return False
        sleep(poll)


def unlock_fd(fd):
    """Release a lock taken by lock_fd. Never raises."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass


def replace_retry(src, dst, retries=REPLACE_RETRIES, delay=REPLACE_DELAY, sleep=None):
    """os.replace, retried briefly when Windows reports the target as in use (WinError 5/32/33)."""
    sleep = sleep or time.sleep
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            retry = isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32, 33)
            if attempt == retries - 1 or not retry:
                raise
            sleep(delay)


def append_event(home, event, when=None):
    """Append one JSON line (binary, LF) to logs/<local date of `when`>.jsonl under a lock.

    POSIX: flock on the log file itself. Windows: msvcrt lock on logs/.tokentier-append.lock
    (the Windows CRT emulates O_APPEND with seek+write, so concurrent writers need it). If the lock
    is not available within APPEND_LOCK_WAIT seconds the line is appended anyway."""
    _tzset()
    when = when or _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    day = when.astimezone().strftime("%Y-%m-%d")
    logdir = os.path.join(home, "logs")
    os.makedirs(logdir, exist_ok=True)
    data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(os.path.join(logdir, day + ".jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT | O_BINARY,
                 0o644)
    lfd, locked = None, False
    try:
        if fcntl is not None:
            lfd = fd
        else:
            try:
                lfd = os.open(os.path.join(logdir, APPEND_LOCK_NAME), os.O_RDWR | os.O_CREAT | O_BINARY, 0o644)
            except OSError:
                lfd = None
        if lfd is not None:
            locked = lock_fd(lfd, APPEND_LOCK_WAIT)
        os.write(fd, data)
    finally:
        try:
            if locked:
                unlock_fd(lfd)
        finally:
            if lfd is not None and lfd != fd:
                try:
                    os.close(lfd)
                except OSError:
                    pass
            os.close(fd)


def log_error(home, msg):
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "hook-errors.log"), "a", encoding="utf-8", newline="\n") as f:
            f.write("%s %s\n" % (local_iso(_dt.datetime.now(_dt.timezone.utc)), msg))
    except Exception:
        pass


def _oneline(text, n=300):
    if text is None:
        return None
    return re.sub(r"\s+", " ", str(text)).strip()[:n]


# ------------------------------------------------- lead (main session) usage
# The lead session's own tokens live in the MAIN transcript (payload.transcript_path), not in
# any subagent transcript. A `Stop` hook (after every lead turn) and `SessionEnd` read only the
# bytes added since the last flush and append `lead_usage` events holding DELTAS.
STATE_KEEP_IDS = 300          # most recent message ids kept in the state file
STATE_PRUNE_DAYS = 14         # state/lock files of sessions untouched this long are removed
_TOKEN_FIELDS = (("input", "input_tokens"), ("output", "output_tokens"),
                 ("cache_creation", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens"))
_ZERO4 = [0, 0, 0, 0]


def _state_paths(home, session_id):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))[:120] or "_"
    d = os.path.join(home, "state")
    return d, os.path.join(d, safe + ".json"), os.path.join(d, safe + ".lock")


def _usage4(usage):
    out = []
    for _, key in _TOKEN_FIELDS:
        try:
            out.append(max(0, int(usage.get(key) or 0)))
        except (TypeError, ValueError):
            out.append(0)
    return out


def _read_state(path):
    """Valid state dict or None (missing/corrupt)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            return None
        off = st.get("offset")
        em = st.get("emitted", {})
        if not isinstance(off, int) or isinstance(off, bool) or off < 0 or not isinstance(em, dict):
            return None
        clean = {}
        for k, v in em.items():
            if isinstance(v, list) and len(v) == 4 and all(isinstance(x, int) for x in v):
                clean[str(k)] = v
        return {"offset": off, "emitted": clean, "history_skipped": bool(st.get("history_skipped"))}
    except Exception:
        return None


def _write_state(path, st):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(st, f, separators=(",", ":"))
        replace_retry(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _Lock(object):
    """Exclusive lock on a lock file (flock / msvcrt.locking); gives up after `wait` seconds
    (acquired stays False). Without any locking primitive it reports acquired=True."""

    def __init__(self, path, wait):
        self.path, self.wait, self.fd, self.acquired = path, wait, None, False

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | O_BINARY, 0o644)
        if fcntl is None and msvcrt is None:
            self.acquired = True
            return self
        self.acquired = lock_fd(self.fd, self.wait)
        return self

    def __exit__(self, *exc):
        try:
            if self.acquired:
                unlock_fd(self.fd)
        finally:
            os.close(self.fd)
        return False


def _prune_state(statedir, now_ts):
    try:
        cutoff = now_ts - STATE_PRUNE_DAYS * 86400
        for fn in os.listdir(statedir):
            if fn.endswith((".json", ".lock", ".tmp")):
                p = os.path.join(statedir, fn)
                try:
                    if os.stat(p).st_mtime < cutoff:
                        os.unlink(p)
                except OSError:
                    pass
    except OSError:
        pass


def lead_session_start(home, payload, now):
    """Create the per-session state file (offset 0) so a normal session is logged from turn one."""
    sid = payload.get("session_id")
    if not sid:
        return
    statedir, spath, lpath = _state_paths(home, sid)
    os.makedirs(statedir, exist_ok=True)
    with _Lock(lpath, 1.0) as lk:
        if not lk.acquired or _read_state(spath):
            return  # keep an existing state file (resume/compact, or a repeated SessionStart)
        source = str(payload.get("source") or "startup")
        st = {"offset": 0, "emitted": {}, "history_skipped": False}
        tpath = payload.get("transcript_path")
        if source in ("resume", "compact") and tpath and os.path.exists(tpath):
            # the transcript may already hold earlier work we never saw: do not count it
            st["offset"] = os.path.getsize(tpath)
            st["history_skipped"] = True
        _write_state(spath, st)
    _prune_state(statedir, now.timestamp())


def lead_flush(home, payload, now, wait=3.0):
    """Append lead_usage deltas (one event per model) for transcript bytes added since the last flush."""
    sid = payload.get("session_id")
    tpath = payload.get("transcript_path")
    if not sid or not tpath or not os.path.isfile(tpath):
        return []
    statedir, spath, lpath = _state_paths(home, sid)
    os.makedirs(statedir, exist_ok=True)
    out = []
    with _Lock(lpath, wait) as lk:
        if not lk.acquired:
            return []  # another flush is running; the next Stop picks the rest up (offset based)
        size = os.path.getsize(tpath)
        st = _read_state(spath)
        if st is None:
            # no state: hooks were installed mid-session (or the state is corrupt). Start from now.
            _write_state(spath, {"offset": size, "emitted": {}, "history_skipped": True})
            return []
        offset = st["offset"] if st["offset"] <= size else 0  # transcript was rewritten: re-read, ids dedupe
        emitted = st["emitted"]
        if size > offset:
            with open(tpath, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                latest = {}   # message id -> (usage4, model, ts); last line wins (streaming)
                anon = 0
                for raw in chunk[:cut].split(b"\n"):
                    if b'"usage"' not in raw or b'"assistant"' not in raw:
                        continue
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "assistant" or obj.get("isSidechain"):
                        continue
                    msg = obj.get("message")
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    model = msg.get("model")
                    if not model or model == "<synthetic>":
                        continue
                    mid = msg.get("id")
                    if not mid:
                        anon += 1
                        mid = "_anon@%d.%d" % (offset, anon)
                    latest[str(mid)] = (_usage4(usage), model, obj.get("timestamp"))
                per_model = {}
                for mid, (cur, model, ts) in latest.items():
                    prev = emitted.pop(mid, None)
                    base = prev if prev is not None else _ZERO4
                    delta = [max(0, c - b) for c, b in zip(cur, base)]
                    emitted[mid] = [max(c, b) for c, b in zip(cur, base)]  # re-inserted: most recent last
                    if not any(delta):
                        continue
                    pm = per_model.setdefault(model, {"tok": [0, 0, 0, 0], "turns": 0, "ts": []})
                    for i in range(4):
                        pm["tok"][i] += delta[i]
                    if prev is None:
                        pm["turns"] += 1
                    d = None
                    try:
                        d = parse_ts(ts) if ts else None
                    except ValueError:
                        d = None
                    if d:
                        pm["ts"].append(d)
                for model in sorted(per_model):
                    pm = per_model[model]
                    ev = base_event("lead_usage", payload, now)
                    toks = dict((name, pm["tok"][i]) for i, (name, _) in enumerate(_TOKEN_FIELDS))
                    ev.update({"model": model, "tier": tier_for(None, model), "tokens": toks,
                               "tokens_total": sum(pm["tok"]), "turns": pm["turns"],
                               "from_ts": local_iso(min(pm["ts"])) if pm["ts"] else ev["ts"],
                               "to_ts": local_iso(max(pm["ts"])) if pm["ts"] else ev["ts"]})
                    out.append(ev)
                offset += cut + 1
        for ev in out:
            append_event(home, ev, now)
        while len(emitted) > STATE_KEEP_IDS:
            emitted.pop(next(iter(emitted)))
        _write_state(spath, {"offset": offset, "emitted": emitted, "history_skipped": st["history_skipped"]})
    return out


# ------------------------------------------------------------------ events
def base_event(name, payload, now):
    project_path = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
    project = os.path.basename(project_path.rstrip("/\\")) if project_path else None
    return {
        "v": 1,
        "ts": local_iso(now),
        "event": name,
        "session_id": payload.get("session_id"),
        "project": project,
        "project_path": project_path,
        "user_id": None,
        "workspace_id": None,
    }


def _read_meta(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _strings_of(v):
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [x for k in sorted(v) for x in _strings_of(v[k])]
    if isinstance(v, list):
        return [x for i in v for x in _strings_of(i)]
    return []


def report_from_transcript(path):
    """Final report text of a subagent transcript, or None.

    Prefers the last SubagentHandback tool_use input, else the last assistant
    text block, else the last tool_use string input.
    """
    handback = text = tool = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                if not isinstance(msg, dict) or obj.get("type") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    if content.strip():
                        text = content
                    continue
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and str(b.get("text") or "").strip():
                        text = b["text"]
                    elif b.get("type") == "tool_use":
                        strs = [x for x in _strings_of(b.get("input")) if x.strip()]
                        if not strs:
                            continue
                        joined = "\n".join(strs)
                        if b.get("name") == "SubagentHandback":
                            handback = joined
                        else:
                            tool = joined
    except Exception:
        return None
    return handback or text or tool


def _has_status(text):
    lines = [l for l in str(text or "").splitlines() if l.strip()]
    return bool(lines) and bool(_STATUS_RE.match(lines[-1].strip()))


def summarize_report(text):
    """Single-line result: skip leading markdown titles, first sentence, <=300."""
    lines = [l.strip() for l in str(text).splitlines() if l.strip()]
    body = [l for l in lines if not _STATUS_RE.match(l)]
    while len(body) > 1 and body[0].startswith("#"):
        body.pop(0)
    if not body:
        body = lines
    first = re.sub(r"^[#>\s*_`-]+", "", body[0]).strip()
    m = re.match(r"^(.{10,300}?[.!?])(\s|$)", first)
    return _oneline(m.group(1) if m else first)


def _ignore(home, event_name, payload, reason, now):
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "hook-ignored.log"), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": local_iso(now), "event": event_name,
                                "session_id": payload.get("session_id"),
                                "agent_id": payload.get("agent_id"), "reason": reason}) + "\n")
    except Exception:
        pass


def _rank(status, source):
    if source in _STATUS_RANK:
        return _STATUS_RANK[source]
    return 0 if status in (None, "", "unknown", "running") else 1


def _tail_lines(path, limit=200 * 1024):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - limit))
            chunk = f.read()
        if size > limit:
            chunk = chunk.split(b"\n", 1)[-1]  # drop the partial first line
        return chunk.decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _prior_end_state(home, task_id, now):
    """Summary of task_end events already logged for task_id (today's and yesterday's file tails), or None."""
    if not task_id:
        return None
    try:
        _tzset()
        local = now.astimezone()
        days = [(local - _dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in (1, 0)]
        st = None
        for day in days:
            for line in _tail_lines(os.path.join(home, "logs", day + ".jsonl")):
                if '"task_end"' not in line or task_id not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("event") != "task_end" or e.get("task_id") != task_id:
                    continue
                st = st or {"rank": -1, "best_status": None, "tokens": -1, "result_len": 0}
                r = _rank(e.get("status"), e.get("status_source"))
                if r > st["rank"]:
                    st["rank"], st["best_status"] = r, e.get("status")
                tt = e.get("tokens_total")
                if isinstance(tt, int) and tt > st["tokens"]:
                    st["tokens"] = tt
                st["result_len"] = max(st["result_len"], len(e.get("result") or ""))
        return st
    except Exception:
        return None


def _adds_something(ev, prior):
    """True if the new task_end is stronger or carries new tokens / a longer result than what is logged."""
    if _rank(ev.get("status"), ev.get("status_source")) > prior["rank"]:
        return True
    tt = ev.get("tokens_total")
    if isinstance(tt, int) and tt > prior["tokens"]:
        return True
    return len(ev.get("result") or "") > prior["result_len"]


# ------------------------------------------------- detached label updater
LABEL_TIMEOUT_S = 15.0
LABEL_MODEL_GRACE_S = 4.0


def child_python(exe=None, isfile=os.path.isfile):
    """Interpreter for the detached child: pythonw.exe next to python.exe on Windows (no console
    window can flash), else the current interpreter. The hook itself must stay python.exe:
    pythonw has no stdin, and Claude Code pipes the payload to stdin."""
    exe = exe or sys.executable
    d, b = os.path.split(exe)
    m = re.match(r"(?i)^python([0-9.]*)\.exe$", b)
    if m:
        w = os.path.join(d, "pythonw%s.exe" % m.group(1))
        if isfile(w):
            return w
    return exe


WIN_DETACHED_PROCESS = 0x00000008
WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
WIN_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def spawn_label_child(home, payload, agent_type, popen=None, windows=None):
    """Start a detached copy of this script (`_label`) that fills in label/model later. Never blocks.

    The child gets TOKENTIER_HOME explicitly (the Windows exec-form hook passes the home as
    `--home`, not via the environment), no inherited handles and stdio on DEVNULL. Windows: no
    console (DETACHED_PROCESS, pythonw.exe), own process group, and it tries to break away from
    Claude Code's job object so it can finish its <=15 s of work; if breakaway is not allowed it
    starts inside the job."""
    popen = popen or subprocess.Popen
    windows = (os.name == "nt") if windows is None else windows
    args = [child_python() if windows else sys.executable, os.path.abspath(__file__), "_label",
            str(payload.get("session_id") or ""), str(payload.get("agent_id") or ""),
            str(payload.get("transcript_path") or ""),
            str(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""),
            str(agent_type or "")]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "close_fds": True, "env": dict(os.environ, TOKENTIER_HOME=home)}
    if windows:
        base = WIN_DETACHED_PROCESS | WIN_CREATE_NEW_PROCESS_GROUP
        try:
            popen(args, creationflags=base | WIN_CREATE_BREAKAWAY_FROM_JOB, **kw)
            return
        except OSError:
            pass  # job does not allow breakaway: start inside it
        popen(args, creationflags=base, **kw)
    else:
        kw["start_new_session"] = True
        popen(args, **kw)


def label_worker(home, argv, sleep=time.sleep, timeout=None):
    """argv: [session_id, agent_id, transcript_path, project_path, agent_type].

    Appends task_update(s): label/tool_use_id/agent_type immediately when meta is found,
    then a second task_update with model if it appears later.
    """
    argv = list(argv) + [""] * (5 - len(argv))
    sid, agent_id, tpath, project_path, agent_type = argv[:5]
    if timeout is None:
        try:
            timeout = float(os.environ.get("TOKENTIER_LABEL_TIMEOUT") or LABEL_TIMEOUT_S)
        except ValueError:
            timeout = LABEL_TIMEOUT_S
    transcript, meta_path = subagent_paths(tpath, agent_id)
    if not meta_path:
        return None
    start = time.time()
    meta, model, found_at = {}, None, None
    label_sent = False

    while True:
        if not os.path.isdir(home):
            return None  # home vanished (e.g. uninstall / test cleanup): do not recreate it
        if not meta:
            meta = _read_meta(meta_path)
            if meta:
                found_at = time.time()
                # Append task_update with label/tool_use_id/agent_type immediately when meta is found
                label, tu = meta.get("description"), meta.get("toolUseId")
                if label or tu:
                    now = _dt.datetime.now(_dt.timezone.utc)
                    ev = {"v": 1, "ts": local_iso(now), "event": "task_update", "task_id": agent_id,
                          "session_id": sid or None, "label": label, "tool_use_id": tu, "model": None,
                          "agent_type": agent_type or meta.get("agentType")}
                    pp = project_path or None
                    ev["project"] = os.path.basename(pp.rstrip("/\\")) if pp else None
                    ev["project_path"] = pp
                    ev["subagent_transcript"] = os.path.abspath(transcript) if transcript else None
                    append_event(home, ev, now)
                    label_sent = True
        if not model and transcript and os.path.exists(transcript):
            try:
                model = parse_transcript_usage(transcript).get("model")
            except Exception:
                pass
        now_t = time.time()
        # If we have both meta and model, or if we've found meta and exceeded grace period, we can stop
        if meta and model:
            # If we haven't sent the label yet, send it now with model
            if not label_sent:
                label, tu = meta.get("description"), meta.get("toolUseId")
                if label or tu:
                    now = _dt.datetime.now(_dt.timezone.utc)
                    ev = {"v": 1, "ts": local_iso(now), "event": "task_update", "task_id": agent_id,
                          "session_id": sid or None, "label": label, "tool_use_id": tu, "model": model,
                          "agent_type": agent_type or meta.get("agentType")}
                    pp = project_path or None
                    ev["project"] = os.path.basename(pp.rstrip("/\\")) if pp else None
                    ev["project_path"] = pp
                    ev["subagent_transcript"] = os.path.abspath(transcript) if transcript else None
                    append_event(home, ev, now)
                    return ev
            else:
                # Label already sent, send model-only update
                now = _dt.datetime.now(_dt.timezone.utc)
                ev = {"v": 1, "ts": local_iso(now), "event": "task_update", "task_id": agent_id,
                      "session_id": sid or None, "label": None, "tool_use_id": None, "model": model,
                      "agent_type": None}
                pp = project_path or None
                ev["project"] = os.path.basename(pp.rstrip("/\\")) if pp else None
                ev["project_path"] = pp
                ev["subagent_transcript"] = os.path.abspath(transcript) if transcript else None
                append_event(home, ev, now)
            break
        if found_at is not None and now_t - found_at >= LABEL_MODEL_GRACE_S:
            # Grace period exceeded; if we sent the label, we're done; otherwise stop waiting
            break
        if now_t - start >= timeout:
            break
        sleep(0.2)

    return None


def handle(event_name, payload, home, now=None, sleep=time.sleep, spawn_label=None):
    if spawn_label is None:
        spawn_label = spawn_label_child
    now = now or _dt.datetime.now(_dt.timezone.utc)
    events = []
    agent_id = payload.get("agent_id")
    agent_type = payload.get("agent_type")
    router = agent_type in ROUTER_WORKERS

    if event_name == "Stop":
        try:
            return lead_flush(home, payload, now)
        except Exception:
            log_error(home, traceback.format_exc())
            return []
    if event_name in ("SessionStart", "SessionEnd"):
        try:
            if event_name == "SessionStart":
                lead_session_start(home, payload, now)
            else:
                lead_flush(home, payload, now, wait=0.5)  # best effort, before session_end is logged
        except Exception:
            log_error(home, traceback.format_exc())
        ev = base_event("session_start" if event_name == "SessionStart" else "session_end", payload, now)
        events.append(ev)
    elif event_name == "SubagentStart":
        ev = base_event("task_start", payload, now)
        tpath, meta_path = subagent_paths(payload.get("transcript_path"), agent_id)
        meta = {}
        if meta_path:
            # Claude Code can write the meta file 1-2 s AFTER this hook runs: poll only briefly here, then
            # hand the rest to a detached background updater (task_update event) so the hook stays fast.
            for attempt in range(5):
                meta = _read_meta(meta_path)
                if meta:
                    break
                if attempt < 4:
                    sleep(0.1)
        model = None
        has_transcript = bool(tpath and os.path.exists(tpath))
        if has_transcript:
            try:
                model = parse_transcript_usage(tpath).get("model")
            except Exception:
                pass
        if not agent_type and not meta and not has_transcript:
            _ignore(home, event_name, payload, "empty agent_type, no transcript or meta", now)
            return []
        ev.update({
            "task_id": agent_id, "agent_type": agent_type,
            "tier": tier_for(agent_type, model), "router_worker": router,
            "label": meta.get("description"), "tool_use_id": meta.get("toolUseId"),
            "status": "running",
            # lets the dashboard tell a killed worker (transcript stops changing) from a busy one
            "subagent_transcript": os.path.abspath(tpath) if tpath else None,
        })
        if model:
            ev["model"] = model
        events.append(ev)
        if not meta and meta_path and agent_id and spawn_label:
            try:
                spawn_label(home, payload, agent_type)
            except Exception:
                log_error(home, traceback.format_exc())
    elif event_name == "SubagentStop":
        ev = base_event("task_end", payload, now)
        tpath, meta_path = subagent_paths(payload.get("transcript_path"), agent_id)
        usage = None
        if tpath:
            for attempt in range(5):
                if os.path.exists(tpath):
                    try:
                        usage = parse_transcript_usage(tpath)
                    except Exception:
                        log_error(home, traceback.format_exc())
                    break
                if attempt < 4:
                    sleep(0.2)
        if not agent_type and usage is None:
            _ignore(home, event_name, payload, "empty agent_type, no subagent transcript", now)
            return []
        meta = _read_meta(meta_path) if meta_path else {}
        usage = usage or {}
        model = usage.get("model")
        toks = {k: usage.get(k) for k in ("input", "output", "cache_creation", "cache_read")}
        total = sum(toks.values()) if all(v is not None for v in toks.values()) else None
        last_msg = payload.get("last_assistant_message")
        derived = None
        if not _has_status(last_msg) and tpath and os.path.exists(tpath):
            derived = report_from_transcript(tpath)
            if derived and not _has_status(derived) and last_msg:
                derived = None  # keep payload text when transcript has nothing better
        report = derived if derived else last_msg
        status, status_source = classify_report(report)
        first_ts = usage.get("first_ts")
        ev.update({
            "task_id": agent_id, "agent_type": agent_type,
            "tier": tier_for(agent_type, model), "router_worker": router,
            "label": meta.get("description"), "tool_use_id": meta.get("toolUseId"),
            "model": model,
            "started_at": local_iso(first_ts) if first_ts else None,
            "duration_ms": usage.get("duration_ms"),
            "tokens": toks, "tokens_total": total,
            "status": status, "status_source": status_source,
            "result": summarize_report(derived) if derived else _oneline(last_msg),
        })
        prior = _prior_end_state(home, agent_id, now)
        if prior and not _adds_something(ev, prior):
            return []  # a stronger/equal task_end was already logged for this task
        events.append(ev)
        if status == "escalated" and not (prior and prior["best_status"] == "escalated"):
            esc = base_event("escalation", payload, now)
            reason = None
            m = _ESCALATE_RE.search(str(report or ""))
            if m:
                reason = _oneline(m.group(1))
            esc.update({"task_id": agent_id, "from_tier": ev["tier"], "reason": reason})
            events.append(esc)
    # unknown events: ignore
    for ev in events:
        append_event(home, ev, now)
    return events


def split_home_arg(argv):
    """(argv without it, home or None) for `script EVENT --home DIR` (Windows exec form)."""
    argv = list(argv)
    if len(argv) > 3 and argv[2] == "--home" and argv[1] != "_label":
        return argv[:2] + argv[4:], argv[3]
    return argv, None


def read_stdin():
    """The hook payload as text. Read as bytes and decode UTF-8 (Claude Code sends UTF-8 JSON):
    on Windows sys.stdin would otherwise decode with the ANSI code page (cp1252)."""
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return buf.read().decode("utf-8-sig", "replace")
    return sys.stdin.read() if sys.stdin is not None else ""


def main(argv=None):
    argv = sys.argv if argv is None else argv
    argv, home_arg = split_home_arg(argv)
    home = os.path.abspath(home_arg) if home_arg else tokentier_home()
    try:
        event_name = argv[1] if len(argv) > 1 else ""
        if event_name == "_label":
            label_worker(home, argv[2:])
            return 0
        raw = read_stdin()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            log_error(home, "bad JSON on stdin for %s: %r" % (event_name, raw[:200]))
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        now = None
        override = os.environ.get("TOKENTIER_NOW")
        if override:
            now = parse_ts(override)
        if event_name in ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "Stop"):
            handle(event_name, payload, home, now)
    except BaseException:
        log_error(home, traceback.format_exc())
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
