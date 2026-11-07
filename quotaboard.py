#!/usr/bin/env python3
"""quotaboard: a local page for LLM spending and quotas, with rule-based advice and confirmed actions.

  quotaboard.py [--port 9998] [--config PATH]   serve http://127.0.0.1:<port>
  quotaboard.py --once                          collect once, print the JSON the page reads, exit

Everything is optional and detected when the tool is installed; ~/.config/quotaboard/config.json
(see config.example.json) only adds what cannot be guessed: Multica, extra Antigravity accounts, stop files, rules.

Sources, each refreshed in its own background thread:
- ccusage (npm i -g ccusage): `blocks --json` (5-hour windows) and `daily --sections daily,weekly` (trend, cost per
  model). `ccusage session` is not used: it reports a session's lifetime total, not what it spent today.
- ~/.claude/projects/**/*.jsonl, read incrementally: tokens per session, hour and model. Cost = tokens weighted by
  Anthropic's price ratios (input 1, output 5, cache write 1.25, cache read 0.1) times each model's unit price,
  fitted from ccusage's own per-model cost, so the totals agree with ccusage.
- Antigravity CLI wrappers (`<cmd> -p /usage`); Codex app-server `account/rateLimits/read`; OpenRouter /api/v1/key
  with opencode's key; the Claude plan tier from ~/.claude.json. Claude's plan percentages are only shown in the
  Claude app; the page lets you type them in.
- Multica (optional): runtimes, agents and runs from its CLI. A Multica session's Claude transcript lives in
  ~/.claude/projects/*multica-workspaces*…-<issue>-<last 12 hex of the run id>-worktree, which ties it to its run.

Actions (POST with the page's token; the server only answers 127.0.0.1 / localhost):
- agent model / thinking level: `multica agent update`, or, when agents are kept as files, rewrite the file and run
  the configured push command;
- cancel a Multica run: `multica issue cancel-task <run id>`;
- toggle a stop file (e.g. a watchdog's STOP flag);
- store the Claude percentages typed in.
Nothing here deletes data or spends money.
"""
import glob
import json
import os
import re
import secrets
import select
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "quotaboard.html")
CONFIG_DIR = os.path.expanduser("~/.config/quotaboard")
WEIGHT = (1.0, 5.0, 1.25, 0.1)  # input, output, cache write, cache read — relative to the input price
CLAUDE_MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
THINKING = ["low", "medium", "high", "xhigh", "max"]
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ACTIVE = ("queued", "dispatched", "running")

LOCK = threading.Lock()
DATA = {"sources": {}}  # source name -> payload; "sources" -> source name -> {ok, at, error}


def x(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


def load_config():
    path = x(os.environ.get("QUOTABOARD_CONFIG") or os.path.join(CONFIG_DIR, "config.json"))
    if "--config" in sys.argv:
        path = x(sys.argv[sys.argv.index("--config") + 1])
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    cfg.setdefault("claude_projects", "~/.claude/projects")
    rules = cfg.setdefault("rules", {})
    rules.setdefault("expensive_models", ["fable"])
    rules.setdefault("opus_allowed_agents", [])
    rules.setdefault("session_share", 0.3)
    rules.setdefault("context_warning", 400_000)
    rules.setdefault("long_run_minutes", 60)
    if "antigravity" not in cfg:  # one plain account when the CLI is installed and nothing is configured
        cfg["antigravity"] = [{"name": "agy", "command": "agy"}] if shutil.which("agy") else []
    m = cfg.get("multica")
    if m is None and shutil.which("multica"):
        cfg["multica"] = m = {}
    if m:
        m.setdefault("command", ["multica"])
        m.setdefault("days", 7)
    return cfg


CFG = load_config()
PROJECTS = x(CFG["claude_projects"])
MANUAL = os.path.join(CONFIG_DIR, "claude-percentages.json")
HOME_DIR = os.path.expanduser("~").replace("/", "-").replace(".", "-")


def now():
    return datetime.now(timezone.utc)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def local(d):
    return d.astimezone() if d else None


def hhmm(d):
    return local(d).strftime("%H:%M") if d else ""


def sh(args, timeout=120, cwd=None):
    r = subprocess.run([x(a) for a in args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or f"exit {r.returncode}")
    return r.stdout


def mx(args, timeout=120):
    return sh(CFG["multica"]["command"] + list(args), timeout=timeout)


def mxjson(args):
    out = mx(list(args) + ["--output", "json"])
    return json.loads(out) if out.strip() else None


def items(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                return v
    return []


class History:
    """Quota readings over time (left %), so a lane can be drawn and a slope projected. One JSON line per reading in
    ~/.config/quotaboard/history.jsonl; kept for 14 days."""
    PATH = os.path.join(CONFIG_DIR, "history.jsonl")
    KEEP = timedelta(days=14)

    def __init__(self):
        self.rows, self.lock = [], threading.Lock()
        try:
            for line in open(self.PATH, encoding="utf-8"):
                try:
                    self.rows.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        self.prune(rewrite=True)

    def prune(self, rewrite=False):
        cut = (now() - self.KEEP).isoformat()
        before = len(self.rows)
        self.rows = [r for r in self.rows if r.get("t", "") >= cut]
        if rewrite and len(self.rows) != before:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(self.PATH, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in self.rows)

    def record(self, readings):
        t = now().isoformat(timespec="seconds")
        new = [{"t": t, "k": k, "v": v} for k, v in readings if isinstance(v, (int, float))]
        if not new:
            return
        with self.lock:
            self.rows += new
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(self.PATH, "a", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in new)
            if len(self.rows) > 50_000:
                self.prune(rewrite=True)

    def since(self, hours):
        cut = (now() - timedelta(hours=hours)).isoformat()
        out = defaultdict(list)
        with self.lock:
            for r in self.rows:
                if r["t"] >= cut:
                    out[r["k"]].append([r["t"], r["v"]])
        return out


def pct_value(s):
    try:
        return float(str(s).rstrip("%"))
    except ValueError:
        return None


def put(name, value=None, error=None):
    with LOCK:
        if error is None:
            DATA[name] = value
        DATA["sources"][name] = {"ok": error is None, "at": now().isoformat(), "error": error}


# ---------- sources ----------

def src_blocks():
    since = local(now() - timedelta(days=8)).strftime("%Y%m%d")
    return json.loads(sh(["ccusage", "blocks", "--json", "--since", since, "-O"]))["blocks"]


def src_daily():
    since = local(now() - timedelta(days=21)).strftime("%Y%m%d")
    d = json.loads(sh(["ccusage", "daily", "--json", "--since", since, "-O", "--sections", "daily,weekly"], timeout=240))
    return {"daily": d.get("daily", []), "weekly": d.get("weekly", [])}


def issue_key(t):
    """ABC-93 from the run's work dir (…/abc-93-6f3a2cc5d9b1/worktree); empty for runs without an issue."""
    m = re.search(r"/([a-z]+-\d+)-[0-9a-f]{12}/", "/" + (t.get("relative_work_dir") or ""))
    return m.group(1).upper() if m else ""


def src_multica():
    """Runtimes, agents and the last N days of runs, straight from the Multica CLI."""
    since = now() - timedelta(days=CFG["multica"]["days"])
    runtimes = {r["id"]: {"name": re.sub(r" \([^()]*\)$", "", r.get("name") or "?"),
                          "provider": r.get("provider") or r.get("kind") or ""}
                for r in items(mxjson(["runtime", "list"]))}
    agents = [a for a in items(mxjson(["agent", "list"])) if not a.get("archived_at")]

    def tasks(a):
        try:
            return a, items(mxjson(["agent", "tasks", a["id"], "--limit", "200"]))
        except Exception:
            return a, []
    runs = []
    with ThreadPoolExecutor(6) as pool:
        for a, tlist in pool.map(tasks, agents):
            for t in tlist:
                c = ts(t.get("created_at"))
                if not c or c < since:
                    continue
                u = (t.get("usage") or [{}])[0] if t.get("usage") else {}
                s, e = ts(t.get("started_at")), ts(t.get("completed_at"))
                runs.append({"agent": a["name"], "model": u.get("model") or a.get("model") or "", "status": t["status"],
                             "reason": t.get("failure_reason") or "", "created": t.get("created_at"),
                             "started": t.get("started_at"), "completed": t.get("completed_at"),
                             "minutes": round((e - s).total_seconds() / 60, 1) if s and e else "",
                             "cache_read": u.get("cache_read_tokens", 0), "cache_write": u.get("cache_write_tokens", 0),
                             "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                             "task_id": t["id"], "issue_key": issue_key(t),
                             "session_id": (t.get("result") or {}).get("session_id", "")})
    return {"runtimes": runtimes, "agents": agents, "runs": runs}


def src_issues():
    out, off = {}, 0
    while True:
        d = mxjson(["issue", "list", "--limit", "100", "--offset", str(off)])
        page = items(d)
        for i in page:
            out[i["identifier"]] = {"title": i.get("title", ""), "status": i.get("status")}
        if not (isinstance(d, dict) and d.get("has_more")) or not page:
            return out
        off += len(page)


def agy_usage(cmd):
    out = sh([cmd, "-p", "/usage", "--print-timeout", "30s"], timeout=90)
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            rows.append({"group": p[0], "limit": p[1], "left": p[2], "reset": p[3].strip() if len(p) > 3 else ""})
    if not rows:
        raise RuntimeError("empty quota table (not signed in?)")
    return rows


def src_antigravity():
    out = {}
    for acc in CFG["antigravity"]:
        try:
            out[acc["name"]] = {"rows": agy_usage(acc["command"])}
        except Exception as e:  # one account failing must not hide the others
            out[acc["name"]] = {"error": str(e)[:200]}
        out[acc["name"]].update({"command": acc["command"], "note": acc.get("note", ""), "runtimes": acc.get("runtimes", [])})
        HIST.record([(f"agy|{acc['name']}|{r['group']}|{limit_name(r['limit'])}", pct_value(r["left"]))
                     for r in out[acc["name"]].get("rows", [])])
    return out


def codex_limits():
    """Codex limits from its own app-server: `account/rateLimits/read` asks the server, no model call."""
    p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    try:
        for i, (method, params) in enumerate([("initialize", {"clientInfo": {"name": "quotaboard", "version": "1"}}),
                                              ("initialized", None), ("account/read", {}), ("account/rateLimits/read", None)]):
            msg = {"jsonrpc": "2.0", "method": method}
            if method != "initialized":
                msg["id"] = i
            if params is not None:
                msg["params"] = params
            p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        got, end = {}, time.time() + 40
        while time.time() < end and len(got) < 2:
            if select.select([p.stdout], [], [], 1)[0]:
                line = p.stdout.readline()
                if not line:
                    break
                o = json.loads(line)
                if o.get("id") in (2, 3):
                    got[o["id"]] = o.get("result") or {}
    finally:
        p.kill()
    acc, lim = (got.get(2) or {}).get("account") or {}, (got.get(3) or {}).get("rateLimits") or {}
    if not lim:
        raise RuntimeError("could not read Codex limits (not signed in?)")
    rows = []
    for key in ("primary", "secondary"):
        w = lim.get(key)
        if w:
            mins = w.get("windowDurationMins") or 0
            name = "5h" if mins == 300 else "weekly" if mins == 10080 else "monthly" if mins >= 40000 else f"{mins}m"
            rows.append({"window": name, "left": 100 - (w.get("usedPercent") or 0),
                         "reset": datetime.fromtimestamp(w["resetsAt"], timezone.utc).isoformat() if w.get("resetsAt") else ""})
    return {"account": (acc.get("email") or "").split("@")[0], "plan": lim.get("planType") or acc.get("planType"),
            "rows": rows}


OPENCODE_AUTH = os.path.expanduser("~/.local/share/opencode/auth.json")


def openrouter_limits():
    """The OpenRouter key opencode signed in with, asked at openrouter.ai's own /key endpoint. The key is sent only
    there and never reaches the page."""
    key = json.load(open(OPENCODE_AUTH))["openrouter"]["key"]
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    d = json.load(urllib.request.urlopen(req, timeout=20))["data"]
    free = d.get("free_model_daily_requests") or {}
    rows = []
    if free.get("limit"):
        rows.append({"kind": "free_requests", "left": round(100 * free.get("remaining", 0) / free["limit"]),
                     "remaining": free.get("remaining", 0), "limit": free["limit"], "reset": ""})
    lim = d.get("limit")
    rows.append({"kind": "credit", "left": None if not lim else round(100 * (d.get("limit_remaining") or 0) / lim),
                 "remaining": d.get("limit_remaining") or 0, "limit": lim, "reset": ""})
    return {"free_tier": bool(d.get("is_free_tier")), "rows": rows, "usage_month": d.get("usage_monthly", 0)}


def claude_plan():
    try:
        acc = json.load(open(os.path.expanduser("~/.claude.json"))).get("oauthAccount") or {}
    except (OSError, ValueError):
        acc = {}
    tier = acc.get("organizationRateLimitTier") or acc.get("userRateLimitTier") or ""
    try:
        manual = json.load(open(MANUAL, encoding="utf-8"))
    except (OSError, ValueError):
        manual = None
    return {"plan": {"default_claude_max_5x": "Max 5x", "default_claude_max_20x": "Max 20x"}.get(tier, tier or "?"),
            "manual": manual}


def src_quotas():
    out = {"claude": claude_plan()}
    jobs = []
    if shutil.which("codex") and CFG.get("codex", True):
        jobs.append(("codex", codex_limits))
    if os.path.exists(OPENCODE_AUTH) and CFG.get("openrouter", True):
        try:
            if "openrouter" in json.load(open(OPENCODE_AUTH)):
                jobs.append(("openrouter", openrouter_limits))
        except (OSError, ValueError):
            pass
    for name, fn in jobs:
        try:
            out[name] = fn()
        except Exception as e:  # one provider failing must not hide the others
            out[name] = {"error": f"{type(e).__name__}: {e}"[:200]}
    readings = [(f"codex|{r['window']}", r["left"]) for r in (out.get("codex") or {}).get("rows", [])]
    readings += [(f"openrouter|{r['kind']}", r["left"]) for r in (out.get("openrouter") or {}).get("rows", [])]
    HIST.record(readings)
    out["installed"] = {k: bool(shutil.which(k)) for k in ("claude", "codex", "opencode", "gemini", "agy", "ccusage", "multica")}
    return out


LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")


def src_catalog():
    """Which models each runtime kind offers and which thinking levels each model takes.
    Claude: a separate thinking level. Antigravity: the level is the model name's suffix (`agy models`).
    Codex: levels per model from its local models cache. Opencode: model names only."""
    cat = {"claude": {"in_name": False, "models": {m: list(THINKING) for m in CLAUDE_MODELS}}}
    cmd = next((a["command"] for a in CFG["antigravity"]), "agy" if shutil.which("agy") else None)
    if cmd:
        try:
            models = {}
            for line in sh([cmd, "models"], timeout=90).splitlines():
                name = line.split("\t")[0].strip()
                if not re.fullmatch(r"[\w.:/-]+", name):
                    continue
                base, _, lvl = name.rpartition("-")
                if base and lvl in LEVELS:
                    models.setdefault(base, []).append(lvl)
                else:
                    models.setdefault(name, [])
            if models:
                cat["antigravity"] = {"in_name": True, "models": {b: sorted(l, key=LEVELS.index) for b, l in models.items()}}
        except Exception:
            pass
    try:
        d = json.load(open(os.path.expanduser("~/.codex/models_cache.json")))
        cat["codex"] = {"in_name": False, "models": {m["slug"]: [l["effort"] for l in m.get("supported_reasoning_levels") or []]
                                                     for m in d.get("models", []) if m.get("slug")}}
    except (OSError, ValueError, KeyError):
        pass
    if shutil.which("opencode"):
        try:
            names = [l.strip() for l in sh(["opencode", "models"], timeout=60).splitlines() if re.fullmatch(r"[\w.:/@+-]+", l.strip())]
            if names:
                cat["opencode"] = {"in_name": False, "models": {n: [] for n in names}}
        except Exception:
            pass
    return cat


def split_model(provider, model, cat):
    """(base, level) for a runtime whose level lives in the model name; (model, None) otherwise."""
    c = cat.get(provider) or {}
    if c.get("in_name"):
        base, _, lvl = model.rpartition("-")
        if base and lvl in (c["models"].get(base) or []):
            return base, lvl
    return model, None


class Scan:
    """Incremental reader of Claude Code transcripts: tokens per (session, UTC hour, model) and session facts."""

    def __init__(self):
        self.offsets, self.seen = {}, set()
        self.use = defaultdict(lambda: [0, 0, 0, 0])  # (sid, hour, model) -> input, output, cache write, cache read
        self.meta = {}

    def session(self, sid, path):
        m = self.meta.get(sid)
        if m is None:
            rel = os.path.relpath(path, PROJECTS).split(os.sep)
            m = self.meta[sid] = {"id": sid, "dir": rel[0], "first": None, "last": None, "title": None,
                                  "first_message": None, "context": 0, "model": None}
        return m

    def run(self):
        cutoff = time.time() - 9 * 86400
        for path in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            off = self.offsets.get(path, 0)
            if st.st_size <= off:
                continue
            with open(path, "rb") as f:
                f.seek(off)
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1
            self.offsets[path] = off + end
            fsid = os.path.basename(path)[:-6]
            for raw in chunk[:end].splitlines():
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                self.line(o, o.get("sessionId") or fsid, path)

    def line(self, o, sid, path):
        typ = o.get("type")
        if typ == "custom-title":
            self.session(sid, path)["title"] = o.get("customTitle")
            return
        if typ not in ("user", "assistant"):
            return
        m = self.session(sid, path)
        t = o.get("timestamp")
        if t:
            m["first"] = min(m["first"] or t, t)
            m["last"] = max(m["last"] or t, t)
        msg = o.get("message") or {}
        if typ == "user" and m["first_message"] is None and not o.get("isMeta"):
            c = msg.get("content")
            text = c if isinstance(c, str) else " ".join(i.get("text", "") for i in c or [] if isinstance(i, dict))
            if text.strip():
                m["first_message"] = text[:600]
            return
        u, model = msg.get("usage"), msg.get("model")
        if typ != "assistant" or not u or not t or not model or model.startswith("<"):
            return
        key = f"{msg.get('id')}:{o.get('requestId')}"
        if key in self.seen:
            return
        self.seen.add(key)
        v = self.use[(sid, t[:13], model)]
        v[0] += u.get("input_tokens") or 0
        v[1] += u.get("output_tokens") or 0
        v[2] += u.get("cache_creation_input_tokens") or 0
        v[3] += u.get("cache_read_input_tokens") or 0
        if not o.get("isSidechain"):
            m["context"] = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
                (u.get("cache_creation_input_tokens") or 0)
            m["model"] = model

    def snapshot(self):
        return {"use": {k: list(v) for k, v in self.use.items()}, "meta": {k: dict(v) for k, v in self.meta.items()}}


SCAN = Scan()
HIST = History()


def src_scan():
    SCAN.run()
    return SCAN.snapshot()


def read_agent_file(path):
    head = open(path, encoding="utf-8").read().split("---\n")[1]
    meta = {}
    for line in head.strip().splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            try:
                meta[k] = json.loads(v)
            except ValueError:
                meta[k] = v.strip()
    return meta


def src_agents():
    """Agents for the model table: from files when Multica's agents are kept as files, else from Multica."""
    m = CFG.get("multica") or {}
    runtimes = (DATA.get("multica") or {}).get("runtimes", {})
    if m.get("agent_files"):
        raw = [{**read_agent_file(p), "file": os.path.basename(p)}
               for p in sorted(glob.glob(os.path.join(x(m["agent_files"]), "*.md")))]
    else:
        raw = (DATA.get("multica") or {}).get("agents") or []
    out = []
    for a in raw:
        rid = a.get("runtime_id") or ""
        rt = runtimes.get(rid, {})
        out.append({"id": a.get("id"), "file": a.get("file"), "fallback": a.get("fallback") if isinstance(a.get("fallback"), list) else None,
                    "name": a.get("name"), "model": a.get("model") or "",
                    "thinking": a.get("thinking_level") or "", "runtime_id": rid, "runtime": rt.get("name", rid[:8] or "?"),
                    "provider": rt.get("provider", "")})
    return out


def src_fallback():
    """Fallback state from the configured status command (contract in README: chains, held, closed, runtimes, events)."""
    fb = (CFG.get("multica") or {}).get("fallback") or {}
    d = json.loads(sh(fb["status_command"], timeout=120))
    events = []
    for line in d.get("events") or []:
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d) (.*)", line)
        if m:
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").astimezone(timezone.utc).isoformat()
            events.append({"at": at, "text": m.group(2)})
    d["events"] = events
    return d


def src_stop_files():
    out = []
    for d in CFG.get("stop_files", []):
        path, log = x(d["path"]), x(d.get("log"))
        lines = []
        if log:
            try:
                lines = open(log, encoding="utf-8", errors="replace").read().splitlines()[-8:]
            except OSError:
                pass
        on = os.path.exists(path)
        out.append({"name": d["name"], "description": d.get("description", ""), "stopped": on, "log": lines,
                    "since": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat() if on else None})
    return out


SOURCES = [("blocks", src_blocks, 60), ("scan", src_scan, 60), ("daily", src_daily, 180), ("quotas", src_quotas, 300),
           ("catalog", src_catalog, 6 * 3600),
           ("stop_files", src_stop_files, 30)]
if CFG["antigravity"]:
    SOURCES.append(("antigravity", src_antigravity, 300))
if CFG.get("multica"):
    SOURCES += [("multica", src_multica, 300), ("agents", src_agents, 30), ("issues", src_issues, 600)]
    if (CFG["multica"].get("fallback") or {}).get("status_command"):
        SOURCES.append(("fallback", src_fallback, 60))
NEXT = {}


def refresh(name, fn):
    try:
        put(name, fn())
    except Exception as e:
        put(name, error=f"{type(e).__name__}: {e}"[:400])


def collector(name, fn, every):
    """One thread per source, so a slow one (ccusage daily, ~30 s) does not hold up the others."""
    while True:
        if time.time() >= NEXT.get(name, 0):
            NEXT[name] = time.time() + every
            refresh(name, fn)
        time.sleep(2)


def collect_once():
    threads = [threading.Thread(target=refresh, args=(n, f)) for n, f, _ in SOURCES if n != "agents"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if CFG.get("multica"):
        refresh("agents", src_agents)


# ---------- the view ----------

def weight(v):
    return sum(a * b for a, b in zip(v, WEIGHT))


def unit_prices(daily):
    """USD per weighted token, per model, fitted from ccusage's per-model cost over the period."""
    cost, w = defaultdict(float), defaultdict(float)
    for d in daily:
        for b in d.get("modelBreakdowns", []):
            if b.get("cost"):
                cost[b["modelName"]] += b["cost"]
                w[b["modelName"]] += weight([b.get("inputTokens", 0), b.get("outputTokens", 0),
                                             b.get("cacheCreationTokens", 0), b.get("cacheReadTokens", 0)])
    return {m: cost[m] / w[m] for m in cost if w[m]}


def is_multica(d):
    return "multica-workspaces" in d or "multica_workspaces" in d


def kind_of(meta, runs_by_short, runs_by_session):
    """(class, label, run) for a session. Explainable: path, then first message, then age."""
    d, first = meta["dir"], meta.get("first_message") or ""
    if is_multica(d):
        m = re.search(r"-([0-9a-f]{12})-(?:worktree|workdir)$", d)
        run = runs_by_session.get(meta["id"]) or (runs_by_short.get(m.group(1)) if m else None)
        if run:
            return "multica", f"{run['agent']} · {run.get('issue_key') or 'no issue'}", run
        key = re.search(r"-([a-z]+-\d+)-[0-9a-f]{12}-", d)
        return "multica", f"Multica · {key.group(1).upper() if key else '?'} (run not matched)", None
    m = re.match(r'\s*<scheduled-task name="([^"]+)"', first)
    if m:
        return "scheduled", f"Scheduled: {m.group(1)}", None
    if "Task (from the session that started this one)" in first:
        return "side", "Side session (started by another session)", None
    a, b = ts(meta.get("first")), ts(meta.get("last"))
    if a and b and b - a > timedelta(hours=24):
        return "long", "Long-running session", None
    return "interactive", "Interactive session", None


def project_of(d):
    if is_multica(d):
        return "Multica"
    s = d[len(HOME_DIR):].lstrip("-") if d.startswith(HOME_DIR) else d.lstrip("-")
    if s.startswith(("private-tmp", "tmp")):
        return "temp folder"
    return s or "~"


def view():
    with LOCK:
        D = dict(DATA)
        sources = json.loads(json.dumps(DATA["sources"]))
    t_now = now()
    today = local(t_now).date().isoformat()
    daily = (D.get("daily") or {}).get("daily", [])
    weekly = (D.get("daily") or {}).get("weekly", [])
    price = unit_prices(daily)
    mu = D.get("multica") or {}
    runs, runtimes = mu.get("runs") or [], mu.get("runtimes") or {}
    issues = D.get("issues") or {}
    agents = [dict(a) for a in D.get("agents") or []]
    for a in agents:  # agents may have been read before the runtimes were
        rt = runtimes.get(a["runtime_id"])
        if rt and not a["provider"]:
            a["provider"], a["runtime"] = rt["provider"], rt["name"]
    scan = D.get("scan") or {"use": {}, "meta": {}}
    runs_by_short = {r["task_id"].replace("-", "")[-12:]: r for r in runs if r.get("task_id")}
    runs_by_session = {r["session_id"]: r for r in runs if r.get("session_id")}

    blocks = [b for b in D.get("blocks") or [] if not b.get("isGap")]
    active = next((b for b in blocks if b.get("isActive")), None)
    w_start = ts(active["startTime"]) if active else None
    sess = {}
    model_today = defaultdict(lambda: {"usd": 0.0, "tokens": 0})
    sess_hours = defaultdict(lambda: defaultdict(float))  # sid -> UTC hour -> usd, last 7 days
    types = {span: {"tokens": [0, 0, 0, 0], "usd": [0.0, 0.0, 0.0, 0.0]} for span in ("today", "week")}
    for (sid, hour, model), v in scan["use"].items():
        h = datetime.fromisoformat(hour + ":00:00+00:00")
        usd = weight(v) * price.get(model, 0)
        s = sess.setdefault(sid, {"today": 0.0, "window": 0.0, "week": 0.0})
        spans = []
        if t_now - h <= timedelta(days=7):
            s["week"] += usd
            sess_hours[sid][hour] += usd
            spans.append("week")
        if local(h).date().isoformat() == today:
            spans.append("today")
        for span in spans:  # what each token type costs: cache reads are most tokens but a tenth of the price
            for i in range(4):
                types[span]["tokens"][i] += v[i]
                types[span]["usd"][i] += v[i] * WEIGHT[i] * price.get(model, 0)
        if local(h).date().isoformat() == today:
            s["today"] += usd
            model_today[model]["usd"] += usd
            model_today[model]["tokens"] += sum(v)
        if w_start and h >= w_start:
            s["window"] += usd
    rows = []
    for sid, s in sess.items():
        meta = scan["meta"].get(sid) or {"id": sid, "dir": "?"}
        if s["week"] < 0.01:
            continue
        cls, label, run = kind_of(meta, runs_by_short, runs_by_session)
        issue = (run or {}).get("issue_key") or ""
        task = re.match(r'\s*<scheduled-task name="([^"]+)"', meta.get("first_message") or "")
        rows.append({"id": sid, "kind": cls, "label": label, "task": task.group(1) if task else None, "title": meta.get("title") or "",
                     "project": project_of(meta.get("dir", "")), "first_message": (meta.get("first_message") or "")[:160],
                     "last": meta.get("last"), "context": meta.get("context", 0), "model": meta.get("model"),
                     "today": round(s["today"], 2), "window": round(s["window"], 2), "week": round(s["week"], 2),
                     "agent": (run or {}).get("agent"), "issue": issue, "issue_title": issues.get(issue, {}).get("title", "")})
    rows.sort(key=lambda r: -r["today"])
    kind_of_sid = {r["id"]: r["kind"] for r in rows}
    hourly = defaultdict(lambda: defaultdict(float))
    for sid, hours in sess_hours.items():
        k = kind_of_sid.get(sid)
        if k:
            for hour, usd in hours.items():
                hourly[hour][k] += usd
    by_kind = defaultdict(lambda: {"today": 0.0, "window": 0.0, "week": 0.0, "sessions": 0})
    for r in rows:
        k = by_kind[r["kind"]]
        for f in ("today", "window", "week"):
            k[f] += r[f]
        k["sessions"] += 1

    run_rows = []
    for r in runs:
        run_rows.append({**{k: r.get(k) for k in ("agent", "model", "status", "reason", "created", "started", "completed",
                                                   "minutes", "task_id", "issue_key")},
                         "tokens": r["cache_read"] + r["cache_write"] + r["input"] + r["output"],
                         "issue_title": issues.get(r.get("issue_key") or "", {}).get("title", "")})
    run_rows.sort(key=lambda r: -r["tokens"])
    live = [r for r in run_rows if r["status"] in ACTIVE]
    for r in live:
        s = ts(r.get("started"))
        r["running_minutes"] = round((t_now - s).total_seconds() / 60) if s else None

    # A window that stopped well before its 5 hours after heavy spending most likely hit the limit
    window = None
    past = [b for b in blocks if not b.get("isActive") and ts(b["startTime"]) > t_now - timedelta(days=7)]
    full = sorted(b["costUSD"] for b in past if b["costUSD"] >= 40 and b.get("actualEndTime")
                  and ts(b["actualEndTime"]) - ts(b["startTime"]) < timedelta(hours=4, minutes=30))
    if CFG.get("window_cap_usd"):
        cap, cap_source = CFG["window_cap_usd"], "config file"
    elif full:
        cap, cap_source = full[len(full) // 2], f"median of {len(full)} windows that look like they hit the limit"
    else:
        cap, cap_source = max((b["costUSD"] for b in past), default=0) or None, "most expensive window of the last 7 days"
    if active:
        rate = (active.get("burnRate") or {}).get("costPerHour") or 0
        end = ts(active["endTime"])
        full_at = t_now + timedelta(hours=max((cap - active["costUSD"]) / rate, 0)) if cap and rate > 0 else None
        window = {"start": active["startTime"], "end": active["endTime"], "usd": round(active["costUSD"], 2),
                  "tokens": active["totalTokens"], "per_hour": round(rate, 2), "models": active.get("models", []),
                  "cap": round(cap, 2) if cap else None, "cap_source": cap_source,
                  "full_at": full_at.isoformat() if full_at else None, "full_before_end": bool(full_at and full_at < end)}
    recent = [{"start": b["startTime"], "end": b.get("actualEndTime"), "close": b["endTime"], "usd": round(b["costUSD"], 2),
               "active": bool(b.get("isActive"))} for b in blocks if ts(b["startTime"]) > t_now - timedelta(days=3)]

    # ccusage also reads Antigravity transcripts and prices some of its models; the Claude plan only cares about claude-*
    def period(d, key):
        bs = d.get("modelBreakdowns", [])
        cl = [b for b in bs if b["modelName"].startswith("claude")]
        toks = lambda b: b.get("inputTokens", 0) + b.get("outputTokens", 0) + b.get("cacheCreationTokens", 0) + b.get("cacheReadTokens", 0)
        return {key: d["period"], "usd": round(sum(b.get("cost", 0) for b in cl), 2), "tokens": sum(toks(b) for b in cl),
                "models": {b["modelName"]: round(b.get("cost", 0), 2) for b in cl},
                "other_tokens": sum(toks(b) for b in bs if not b["modelName"].startswith("claude"))}

    runtime_agents = defaultdict(list)
    for a in agents:
        runtime_agents[a["runtime"]].append(a["name"])
    out = {"now": t_now.isoformat(), "today": today, "window": window, "recent_windows": recent,
           "daily": [period(d, "day") for d in daily], "weekly": [period(w, "week") for w in weekly],
           "by_kind": {k: {f: round(v, 2) if isinstance(v, float) else v for f, v in d.items()} for k, d in by_kind.items()},
           "models_today": {m: {"usd": round(v["usd"], 2), "tokens": v["tokens"]} for m, v in model_today.items()},
           "sessions": rows, "runs": run_rows, "live": live, "antigravity": D.get("antigravity") or {},
           "quotas": D.get("quotas") or {}, "agents": agents, "stop_files": D.get("stop_files") or [],
           "runtimes": {rt["name"]: {"provider": rt["provider"], "agents": runtime_agents.get(rt["name"], [])}
                        for rt in runtimes.values()},
           "multica": bool(CFG.get("multica")), "model_action": model_mode(), "sources": sources,
           "hourly": {h: {k: round(u, 3) for k, u in d.items()} for h, d in sorted(hourly.items())},
           "token_types": {span: {"tokens": d["tokens"], "usd": [round(u, 2) for u in d["usd"]]} for span, d in types.items()},
           "history": HIST.since(72),
           "options": {"catalog": catalog_for(agents, runs, D.get("catalog") or {})}}
    out["fallback"] = fallback_view(D.get("fallback"), agents, runtimes, live, t_now)
    out["today_ccusage"] = next((d["usd"] for d in out["daily"] if d["day"] == today), None)
    out["advice"] = advice(out, runs, issues, t_now)
    return out


def fallback_view(fb, agents, runtimes, live, t_now):
    """Who has a fallback chain, who is on one, and each entry's state, ready for the page."""
    conf = (CFG.get("multica") or {}).get("fallback") or {}
    if not conf.get("status_command"):
        return None
    fb = fb or {"chains": {}, "held": {}, "closed": {}, "runtimes": {}, "events": []}
    rts = {rid: {"name": r["name"], "provider": r["provider"], "online": True} for rid, r in runtimes.items()}
    for rid, r in (fb.get("runtimes") or {}).items():
        rts.setdefault(rid, {"name": r.get("name", rid[:8]), "provider": ""})["online"] = bool(r.get("online"))
    closed = {k: v for k, v in (fb.get("closed") or {}).items() if ts(v) and ts(v) > t_now}

    def entry(h):
        rt = rts.get(h.get("runtime_id") or "", {})
        return {"runtime_id": h.get("runtime_id") or "", "model": h.get("model") or "", "thinking": h.get("thinking_level") or "",
                "runtime": rt.get("name", (h.get("runtime_id") or "?")[:8]), "provider": rt.get("provider", ""),
                "online": rt.get("online", True), "closed_until": closed.get(f"{h.get('runtime_id')}|{h.get('model')}")}
    busy = {r["agent"] for r in live}
    rows = []
    for aid, c in (fb.get("chains") or {}).items():
        held = (fb.get("held") or {}).get(aid)
        rows.append({"id": aid, "name": c.get("name"), "own": entry(c.get("own") or {}), "chain": [entry(h) for h in c.get("fallback") or []],
                     "now": entry(c.get("now") or {}), "on": c.get("on"), "held": held, "busy": c.get("name") in busy})
    known = {(a["runtime_id"], a["model"]) for a in agents if a["runtime_id"] and a["model"]}
    for r in rows:
        known |= {(e["runtime_id"], e["model"]) for e in [r["own"]] + r["chain"] if e["runtime_id"] and e["model"]}
    models = defaultdict(set)
    for rid, m in known:
        models[rid].add(m)
    return {"rows": rows, "events": fb.get("events") or [], "editable": bool((CFG.get("multica") or {}).get("agent_files")),
            "runtimes": [{"id": rid, "name": r["name"], "provider": r["provider"], "online": r["online"], "models": sorted(models.get(rid, []))}
                         for rid, r in sorted(rts.items(), key=lambda x: x[1]["name"])],
            "agents_without": sorted(a["name"] for a in agents if not any(r["id"] == a["id"] for r in rows) and a.get("file"))}


def model_mode():
    m = CFG.get("multica") or {}
    return "file" if m.get("agent_files") and m.get("push_command") else "multica" if m else None


def catalog_for(agents, runs, cat):
    """The catalog, plus whatever model an agent or run already uses, so a current value is always selectable."""
    out = {p: {"in_name": c["in_name"], "models": {m: list(l) for m, l in c["models"].items()}} for p, c in cat.items()}
    out.setdefault("claude", {"in_name": False, "models": {m: list(THINKING) for m in CLAUDE_MODELS}})
    for a in agents:
        p = a["provider"] or "other"
        c = out.setdefault(p, {"in_name": False, "models": {}})
        base, lvl = split_model(p, a["model"], out)
        if a["model"] and base not in c["models"]:
            c["models"][base] = [lvl] if lvl else ([a["thinking"]] if a["thinking"] else [])
    return out


def other_models(agents, runs):
    seen = {a["model"] for a in agents if a["model"] and a["provider"] != "claude"}
    return sorted(seen | {r["model"] for r in runs if r.get("model") and not r["model"].startswith("claude")})


# ---------- advice: each rule says what it saw and why it matters ----------

def advice(v, runs, issues, t_now):
    """Each entry is a rule id plus the numbers it saw; the page words it in the reader's language."""
    out, R = [], CFG["rules"]

    def add(level, rule, action=None, **p):
        out.append({"level": level, "rule": rule, "p": p, "action": action})

    w = v["window"]
    if w and w.get("full_at") and w["full_before_end"]:
        add("high", "window_fills", at=w["full_at"], end=w["end"], usd=w["usd"], per_hour=w["per_hour"], cap=w["cap"])
    elif w and w.get("cap") and w["usd"] >= 0.7 * w["cap"]:
        add("medium", "window_high", pct=round(100 * w["usd"] / w["cap"]), usd=w["usd"], cap=w["cap"])

    total = v["today_ccusage"] or sum(r["today"] for r in v["sessions"]) or 0
    for r in v["sessions"]:
        if r["kind"] == "multica" or total <= 0:
            continue
        name = r["title"] or r["first_message"][:60] or r["id"][:8]
        share = r["today"] / total
        if share >= R["session_share"] and r["today"] >= 10:
            add("high", "session_share", name=name, pct=round(100 * share), usd=r["today"], total=round(total, 2),
                context=r["context"])
        elif r["context"] >= R["context_warning"] and r["today"] >= 3:
            add("medium", "session_context", name=name, context=r["context"], usd=r["today"])

    expensive = [m.lower() for m in R["expensive_models"]]
    allowed = set(R["opus_allowed_agents"])
    for a in v["agents"]:
        claude = a["provider"] == "claude"
        fix = {"type": "model", "agent": a["name"], "model": "claude-sonnet-5-5" if claude else a["model"],
               "thinking": a["thinking"] or "medium"}
        if claude and any(m in a["model"] for m in expensive):
            add("high", "agent_expensive", fix, agent=a["name"], model=a["model"])
        elif claude and allowed and "opus" in a["model"] and a["name"] not in allowed:
            add("medium", "agent_opus", fix, agent=a["name"], allowed=sorted(allowed))
        elif claude and a["thinking"] in ("high", "xhigh", "max") and a["name"] not in allowed:
            add("low", "agent_thinking", {**fix, "model": a["model"]}, agent=a["name"], thinking=a["thinking"])
    used = defaultdict(int)
    for r in runs:
        if any(m in (r.get("model") or "") for m in expensive) and t_now - (ts(r.get("created")) or t_now) <= timedelta(hours=24):
            used[r["agent"]] += 1
    for name, n in used.items():
        if not any(a["name"] == name and any(m in a["model"] for m in expensive) for a in v["agents"]):
            add("low", "runs_expensive", agent=name, n=n)

    by_issue = defaultdict(list)
    for r in runs:
        if r.get("issue_key"):
            by_issue[r["issue_key"]].append(r)
    for key, rs in by_issue.items():
        rs.sort(key=lambda r: r["created"], reverse=True)
        streak = 0
        for r in rs:
            if r["status"] != "failed":
                break
            streak += 1
        last = ts(rs[0]["created"])
        if streak >= 3 and last and t_now - last <= timedelta(hours=24) and \
                issues.get(key, {}).get("status") not in ("done", "cancelled"):
            add("medium", "issue_failing", issue=key, n=streak, agent=rs[0]["agent"],
                reasons=sorted({(r.get("reason") or "?").split(".")[-1] for r in rs[:streak]}))

    for r in v["live"]:
        if (r.get("running_minutes") or 0) >= R["long_run_minutes"]:
            add("medium", "run_long", {"type": "cancel", "run": r["task_id"], "label": f"{r['agent']} · {r['issue_key']}"},
                agent=r["agent"], issue=r["issue_key"], minutes=r["running_minutes"])

    for name, acc in v["antigravity"].items():
        for row in acc.get("rows") or []:
            if row["left"] not in ("0%", "1%"):
                continue
            g = row["group"].lower()
            uses = (lambda m: "argon" in m) if "argon" in g else (lambda m: m.startswith("gemini") and "argon" not in m) \
                if g.startswith("gemini") else (lambda m: m.startswith("claude") or m.startswith("gpt"))
            hit = [a["name"] for a in v["agents"] if a["runtime"] in acc.get("runtimes", []) and uses(a["model"])]
            if hit or not v["multica"]:
                add("medium", "agy_empty", account=name, group=row["group"], window=limit_name(row["limit"]),
                    left=row["left"], reset=row["reset"], agents=hit)

    fb = v.get("fallback") or {}
    for r in fb.get("rows", []):
        if r["on"] is not None:
            held = r["held"] or {}
            add("medium", "on_fallback", {"type": "restore", "agent_id": r["id"], "agent": r["name"]}, agent=r["name"],
                target=f"{r['now']['runtime']} {r['now']['model']}", since=held.get("since"), back=held.get("back_at"),
                manual=held.get("manual", False))
        elif w and w["full_before_end"] and r["own"]["provider"] == "claude" and r["busy"]:
            n = next((k for k, e in enumerate(r["chain"]) if e["online"] and not e["closed_until"]), None)
            if n is not None:
                e = r["chain"][n]
                add("medium", "pre_switch", {"type": "switch", "agent_id": r["id"], "agent": r["name"], "n": n + 1,
                                             "target": f"{e['runtime']} {e['model']}"},
                    agent=r["name"], at=w["full_at"], target=f"{e['runtime']} {e['model']}")

    sch = v["by_kind"].get("scheduled", {})
    if sch.get("today", 0) >= 5:
        add("low", "scheduled_spend", usd=sch["today"])
    for i, d in enumerate(v["stop_files"]):
        if d["stopped"]:
            add("low", "switch_stopped", {"type": "stop_file", "i": i, "stop": False}, name=d["name"],
                description=d["description"])
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda o: order[o["level"]])


def limit_name(s):
    s = s.lower()
    return "weekly" if "weekly" in s else "5h" if "five hour" in s else s


def fmt_reset(s):
    t = local(ts(s)) if s else None
    return t.strftime("%a %d %b %H:%M") if t else "unknown"


# ---------- actions ----------

def act_model(body):
    mode = model_mode()
    if not mode:
        raise ValueError("Multica is not configured")
    agents = {a["name"]: a for a in src_agents()}
    a = agents.get(body.get("agent", ""))
    if not a:
        raise ValueError("no such agent")
    model, thinking = body.get("model", ""), body.get("thinking", "") or ""
    cat = catalog_for(list(agents.values()), [], DATA.get("catalog") or {})
    c = cat.get(a["provider"] or "other") or {"in_name": False, "models": {}}
    if model not in c["models"]:
        raise ValueError(f"{model} is not offered on {a['runtime']}")
    levels = c["models"][model]
    if thinking and thinking not in levels:
        raise ValueError(f"{model} does not take thinking level {thinking}")
    if c["in_name"]:  # Antigravity: the level is part of the model name
        if levels and not thinking:
            raise ValueError(f"pick a thinking level for {model}")
        model, thinking = (f"{model}-{thinking}" if thinking else model), ""
    if (model, thinking) == (a["model"], a["thinking"]):
        return {"message": "Nothing to change."}
    old = a["model"] + (f" / {a['thinking']}" if a["thinking"] else "")
    new = model + (f" / {thinking}" if thinking else "")
    note = ""
    if mode == "multica":
        mx(["agent", "update", a["id"], "--model", model, "--thinking-level", thinking], timeout=60)
    else:
        m = CFG["multica"]
        path = os.path.join(x(m["agent_files"]), a["file"])
        text = open(path, encoding="utf-8").read()
        _, head, rest = text.split("---\n", 2)
        head = re.sub(r'^model: .*$', f"model: {json.dumps(model)}", head, count=1, flags=re.M)
        head = re.sub(r'^thinking_level: .*$', f"thinking_level: {json.dumps(thinking)}", head, count=1, flags=re.M)
        open(path, "w", encoding="utf-8").write(f"---\n{head}---\n{rest}")
        repo = os.path.dirname(path)
        try:
            sh(m["push_command"], timeout=300, cwd=repo)
        except Exception as e:
            open(path, "w", encoding="utf-8").write(text)  # push refused: keep the file and Multica equal
            raise RuntimeError(f"push failed, file restored: {e}")
        if m.get("commit", True):
            c = subprocess.run(["git", "commit", "--only", "-m", f"quotaboard: {a['name']} {old} → {new}", "--", path],
                               capture_output=True, text=True, cwd=repo)
            note = " File committed." if c.returncode == 0 else " Commit failed: " + (c.stderr or c.stdout).strip()[:200]
    NEXT["multica"] = 0
    return {"message": f"{a['name']}: {old} → {new}. Multica updated.{note} To undo, pick the old value and apply again."}


def act_cancel(body):
    rid = body.get("run", "")
    if not CFG.get("multica") or not UUID.match(rid):
        raise ValueError("invalid run id")
    out = mx(["issue", "cancel-task", rid], timeout=60)
    NEXT["multica"] = 0
    return {"message": "Run cancelled. The issue stays; it can be rerun in Multica.", "output": out[-600:]}


def act_stop_file(body):
    i, files = body.get("i"), CFG.get("stop_files", [])
    if not isinstance(i, int) or not 0 <= i < len(files):
        raise ValueError("no such stop file")
    d, path = files[i], x(files[i]["path"])
    if body.get("stop"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"Stopped from quotaboard, {local(now()).isoformat(timespec='minutes')}\n")
        return {"message": f"{d['name']} stopped."}
    if os.path.exists(path):
        os.remove(path)  # only the stop flag itself
    return {"message": f"{d['name']} started."}


def act_claude_percentages(body):
    """The Claude app's percentages, typed in by hand; the page shows them with their age. Local file only."""
    vals = {}
    for k in ("five_hour", "weekly", "weekly_model"):
        val = body.get(k)
        if val not in (None, ""):
            val = float(val)
            if not 0 <= val <= 100:
                raise ValueError("a percentage is between 0 and 100")
            vals[k] = val
    # the weekly reset moment the app shows; the page repeats it every seven days. Kept across saves.
    wr = body.get("weekly_reset")
    if wr:
        vals["weekly_reset"] = datetime.fromisoformat(str(wr).replace("Z", "+00:00")).isoformat()
    elif wr is None:
        try:
            old = json.load(open(MANUAL, encoding="utf-8")).get("weekly_reset")
        except (OSError, ValueError):
            old = None
        if old:
            vals["weekly_reset"] = old
    os.makedirs(CONFIG_DIR, exist_ok=True)
    json.dump({**vals, "at": now().isoformat()}, open(MANUAL, "w", encoding="utf-8"), indent=1)
    with LOCK:
        DATA.setdefault("quotas", {})["claude"] = claude_plan()
    return {"message": "Saved. Type them in again when the app's numbers change; the page shows their age."}


def fb_conf():
    fb = (CFG.get("multica") or {}).get("fallback") or {}
    if not fb.get("status_command"):
        raise ValueError("fallback is not configured")
    return fb


def fb_agent(body):
    aid = body.get("agent_id", "")
    rows = {r["id"]: r for r in (fallback_view(DATA.get("fallback"), DATA.get("agents") or [],
                                                (DATA.get("multica") or {}).get("runtimes") or {}, [], now()) or {}).get("rows", [])}
    if aid not in rows:
        raise ValueError("this agent has no fallback chain")
    return rows[aid]


def fill(cmd, **kw):
    return [str(c).replace("{agent_id}", kw.get("agent_id", "")).replace("{n}", str(kw.get("n", ""))) for c in cmd]


def act_fallback_switch(body):
    fb, r = fb_conf(), fb_agent(body)
    n = body.get("n")
    if not isinstance(n, int) or not 1 <= n <= len(r["chain"]):
        raise ValueError("no such fallback")
    out = sh(fill(fb["switch_command"], agent_id=r["id"], n=n), timeout=120)
    refresh("fallback", src_fallback)
    return {"message": f"{r['name']} moved to fallback {n}. It stays there until you return it or its own quota reopens.",
            "output": out.strip()[-400:]}


def act_fallback_restore(body):
    fb, r = fb_conf(), fb_agent(body)
    out = sh(fill(fb["restore_command"], agent_id=r["id"]), timeout=120)
    refresh("fallback", src_fallback)
    return {"message": f"{r['name']} is back on its own model.", "output": out.strip()[-400:]}


def act_fallback_chain(body):
    """Write an agent's `fallback` front-matter line (agent files only). It is the watchdog's list; nothing is pushed."""
    fb_conf()
    m = CFG["multica"]
    if not m.get("agent_files"):
        raise ValueError("chains can only be edited when agents are kept as files")
    agents = {a["id"]: a for a in src_agents()}
    a = agents.get(body.get("agent_id", ""))
    if not a or not a.get("file"):
        raise ValueError("no such agent file")
    known = (DATA.get("multica") or {}).get("runtimes") or {}
    chain = []
    for e in body.get("chain") or []:
        rid, model, th = str(e.get("runtime_id", "")), str(e.get("model", "")).strip(), str(e.get("thinking", "") or "")
        if rid not in known or not re.fullmatch(r"[\w.:/@+-]{1,120}", model) or th not in ("",) + tuple(THINKING):
            raise ValueError(f"invalid fallback entry: {rid[:8]} {model}")
        chain.append({"runtime_id": rid, "model": model, **({"thinking_level": th} if th else {})})
    path = os.path.join(x(m["agent_files"]), a["file"])
    text = open(path, encoding="utf-8").read()
    _, head, rest = text.split("---\n", 2)
    head = re.sub(r"^fallback: .*\n?", "", head, flags=re.M)
    if chain:
        head = head.rstrip("\n") + "\nfallback: " + json.dumps(chain, ensure_ascii=False) + "\n"
    new = f"---\n{head}---\n{rest}"
    if new == text:
        return {"message": "Nothing to change."}
    open(path, "w", encoding="utf-8").write(new)
    note = ""
    if m.get("commit", True):
        c = subprocess.run(["git", "commit", "--only", "-m", f"quotaboard: {a['name']} fallback chain ({len(chain)})", "--", path],
                           capture_output=True, text=True, cwd=os.path.dirname(path))
        note = " File committed." if c.returncode == 0 else " Commit failed: " + (c.stderr or c.stdout).strip()[:200]
    refresh("agents", src_agents)
    refresh("fallback", src_fallback)
    return {"message": f"{a['name']}: fallback chain saved ({len(chain)}).{note} The watchdog reads it on its next turn."}


ACTIONS = {"/api/fallback/switch": act_fallback_switch, "/api/fallback/restore": act_fallback_restore,
           "/api/fallback/chain": act_fallback_chain, "/api/claude": act_claude_percentages, "/api/model": act_model, "/api/cancel": act_cancel,
           "/api/stop-file": act_stop_file}


class Handler(BaseHTTPRequestHandler):
    token = ""
    port = 9998

    def log_message(self, *a):
        pass

    def host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=list).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "local only"})
        if self.path in ("/", "/index.html"):
            html = open(PAGE, encoding="utf-8").read().replace("__TOKEN__", self.token)
            return self.send(200, html.encode(), "text/html; charset=utf-8")
        if self.path == "/api/data":
            return self.send(200, view())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Quotaboard") != self.token:
            return self.send(403, {"error": "forbidden"})
        fn = ACTIONS.get(self.path)
        if not fn:
            return self.send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            res = fn(body)
            refresh("stop_files", src_stop_files)
            if CFG.get("multica") and self.path == "/api/model":
                refresh("multica", src_multica)
                refresh("agents", src_agents)
            return self.send(200, res)
        except Exception as e:
            return self.send(400, {"error": str(e)[:600]})


def main():
    if "--once" in sys.argv:
        collect_once()
        print(json.dumps(view(), ensure_ascii=False, indent=1, default=list))
        return
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else int(CFG.get("port", 9998))
    if not shutil.which("ccusage"):
        print("Warning: ccusage not found (npm i -g ccusage); the Claude window and trend stay empty.", file=sys.stderr)
    Handler.token, Handler.port = secrets.token_urlsafe(24), port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    for src in SOURCES:
        threading.Thread(target=collector, args=src, daemon=True).start()
    print(f"quotaboard: http://127.0.0.1:{port}  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
