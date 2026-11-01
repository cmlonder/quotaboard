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
            name = "5-hour" if mins == 300 else "weekly" if mins == 10080 else "monthly" if mins >= 40000 else f"{mins}-minute"
            rows.append({"name": name, "left": 100 - (w.get("usedPercent") or 0),
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
        rows.append({"name": "free models, daily requests", "left": round(100 * free.get("remaining", 0) / free["limit"]),
                     "text": f"{free.get('remaining', 0)} / {free['limit']}", "reset": ""})
    lim = d.get("limit")
    rows.append({"name": "credit", "left": None if not lim else round(100 * (d.get("limit_remaining") or 0) / lim),
                 "text": f"${d.get('limit_remaining') or 0:.2f} left" if lim else "no credit (free tier)", "reset": ""})
    return {"plan": "free tier" if d.get("is_free_tier") else "paid", "rows": rows,
            "usage": f"${d.get('usage_monthly', 0):.2f} this month"}


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
    out["installed"] = {k: bool(shutil.which(k)) for k in ("claude", "codex", "opencode", "gemini", "agy", "ccusage", "multica")}
    return out


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
        out.append({"id": a.get("id"), "file": a.get("file"), "name": a.get("name"), "model": a.get("model") or "",
                    "thinking": a.get("thinking_level") or "", "runtime_id": rid, "runtime": rt.get("name", rid[:8] or "?"),
                    "provider": rt.get("provider", "")})
    return out


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
           ("stop_files", src_stop_files, 30)]
if CFG["antigravity"]:
    SOURCES.append(("antigravity", src_antigravity, 300))
if CFG.get("multica"):
    SOURCES += [("multica", src_multica, 300), ("agents", src_agents, 30), ("issues", src_issues, 600)]
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
    agents = D.get("agents") or []
    scan = D.get("scan") or {"use": {}, "meta": {}}
    runs_by_short = {r["task_id"].replace("-", "")[-12:]: r for r in runs if r.get("task_id")}
    runs_by_session = {r["session_id"]: r for r in runs if r.get("session_id")}

    blocks = [b for b in D.get("blocks") or [] if not b.get("isGap")]
    active = next((b for b in blocks if b.get("isActive")), None)
    w_start = ts(active["startTime"]) if active else None
    sess = {}
    model_today = defaultdict(lambda: {"usd": 0.0, "tokens": 0})
    for (sid, hour, model), v in scan["use"].items():
        h = datetime.fromisoformat(hour + ":00:00+00:00")
        usd = weight(v) * price.get(model, 0)
        s = sess.setdefault(sid, {"today": 0.0, "window": 0.0, "week": 0.0})
        if t_now - h <= timedelta(days=7):
            s["week"] += usd
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
        rows.append({"id": sid, "kind": cls, "label": label, "title": meta.get("title") or "",
                     "project": project_of(meta.get("dir", "")), "first_message": (meta.get("first_message") or "")[:160],
                     "last": meta.get("last"), "context": meta.get("context", 0), "model": meta.get("model"),
                     "today": round(s["today"], 2), "window": round(s["window"], 2), "week": round(s["week"], 2),
                     "agent": (run or {}).get("agent"), "issue": issue, "issue_title": issues.get(issue, {}).get("title", "")})
    rows.sort(key=lambda r: -r["today"])
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
    recent = [{"start": b["startTime"], "end": b.get("actualEndTime"), "usd": round(b["costUSD"], 2),
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
           "options": {"claude": CLAUDE_MODELS, "thinking": THINKING, "other": other_models(agents, runs)}}
    out["today_ccusage"] = next((d["usd"] for d in out["daily"] if d["day"] == today), None)
    out["advice"] = advice(out, runs, issues, t_now)
    return out


def model_mode():
    m = CFG.get("multica") or {}
    return "file" if m.get("agent_files") and m.get("push_command") else "multica" if m else None


def other_models(agents, runs):
    seen = {a["model"] for a in agents if a["model"] and a["provider"] != "claude"}
    return sorted(seen | {r["model"] for r in runs if r.get("model") and not r["model"].startswith("claude")})


# ---------- advice: each rule says what it saw and why it matters ----------

def advice(v, runs, issues, t_now):
    out, R = [], CFG["rules"]

    def add(level, text, why, action=None):
        out.append({"level": level, "text": text, "why": why, "action": action})

    w = v["window"]
    if w and w.get("full_at") and w["full_before_end"]:
        add("high", f"At this pace the Claude window fills at {hhmm(ts(w['full_at']))}; it resets at {hhmm(ts(w['end']))}.",
            f"${w['usd']} so far, ${w['per_hour']} per hour. Estimated cap ${w['cap']} ({w['cap_source']}). "
            "The real plan percentage is only shown in the Claude app.")
    elif w and w.get("cap") and w["usd"] >= 0.7 * w["cap"]:
        add("medium", f"{round(100 * w['usd'] / w['cap'])}% of the estimated Claude window cap is used.",
            f"${w['usd']} / ${w['cap']} (cap estimate: {w['cap_source']}).")

    total = v["today_ccusage"] or sum(r["today"] for r in v["sessions"]) or 0
    for r in v["sessions"]:
        if r["kind"] == "multica" or total <= 0:
            continue
        name = r["title"] or r["first_message"][:50] or r["id"][:8]
        share = r["today"] / total
        if share >= R["session_share"] and r["today"] >= 10:
            add("high", f"“{name}” is {round(100 * share)}% of today's spend. Start a new session.",
                f"${r['today']} of ${round(total, 2)} today. Its context is {r['context'] // 1000}k tokens and every message "
                "re-reads it. Asking for a short hand-over note and starting fresh resets that.")
        elif r["context"] >= R["context_warning"] and r["today"] >= 3:
            add("medium", f"“{name}” has a {r['context'] // 1000}k-token context.",
                "Every message re-reads it; the same work is much cheaper in a new session.")

    expensive = [m.lower() for m in R["expensive_models"]]
    allowed = set(R["opus_allowed_agents"])
    for a in v["agents"]:
        claude = a["provider"] == "claude"
        fix = {"type": "model", "agent": a["name"], "model": "claude-sonnet-5-5" if claude else a["model"],
               "thinking": a["thinking"] or "medium"}
        if claude and any(m in a["model"] for m in expensive):
            add("high", f"{a['name']} uses an expensive model ({a['model']}).",
                "Listed in rules.expensive_models. Sonnet is enough for most work.", fix)
        elif claude and allowed and "opus" in a["model"] and a["name"] not in allowed:
            add("medium", f"{a['name']} uses Opus; the rules say Sonnet.",
                f"Opus is allowed only for: {', '.join(sorted(allowed))} (rules.opus_allowed_agents).", fix)
        elif claude and a["thinking"] in ("high", "xhigh", "max") and a["name"] not in allowed:
            add("low", f"{a['name']} runs at {a['thinking']} thinking.",
                "Higher thinking means more output tokens; medium is enough for most work.", {**fix, "model": a["model"]})
    used = defaultdict(int)
    for r in runs:
        if any(m in (r.get("model") or "") for m in expensive) and t_now - (ts(r.get("created")) or t_now) <= timedelta(hours=24):
            used[r["agent"]] += 1
    for name, n in used.items():
        if not any(a["name"] == name and any(m in a["model"] for m in expensive) for a in v["agents"]):
            add("low", f"{name} ran {n} times on an expensive model in the last 24 hours.",
                "The agent is no longer set to it; these may be runs from before the change.")

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
            reasons = sorted({(r.get("reason") or "?").split(".")[-1] for r in rs[:streak]})
            add("medium", f"{key} has failed {streak} runs in a row ({rs[0]['agent']}).",
                f"Reason: {', '.join(reasons)}. Every retry burns tokens again.")

    for r in v["live"]:
        if (r.get("running_minutes") or 0) >= R["long_run_minutes"]:
            add("medium", f"{r['agent']} · {r['issue_key'] or 'no issue'} has been running for {r['running_minutes']} minutes.",
                "A long run may be stuck; check the issue and cancel if needed.",
                {"type": "cancel", "run": r["task_id"], "label": f"{r['agent']} · {r['issue_key']}"})

    for name, acc in v["antigravity"].items():
        for row in acc.get("rows") or []:
            if row["left"] not in ("0%", "1%"):
                continue
            g = row["group"].lower()
            uses = (lambda m: "argon" in m) if "argon" in g else (lambda m: m.startswith("gemini") and "argon" not in m) \
                if g.startswith("gemini") else (lambda m: m.startswith("claude") or m.startswith("gpt"))
            hit = [a["name"] for a in v["agents"] if a["runtime"] in acc.get("runtimes", []) and uses(a["model"])]
            if hit or not v["multica"]:
                add("medium", f"Antigravity {name}: {row['group']} {limit_name(row['limit'])} quota is used up ({row['left']} left).",
                    f"Resets {fmt_reset(row['reset'])}." + (f" Agents on this account using it: {', '.join(hit)}; "
                                                          "they can move to another account's runtime." if hit else ""))

    sch = v["by_kind"].get("scheduled", {})
    if sch.get("today", 0) >= 5:
        add("low", f"Scheduled tasks spent ${sch['today']} today.", "Running them less often would save that.")
    for i, d in enumerate(v["stop_files"]):
        if d["stopped"]:
            add("low", f"{d['name']} is stopped.", d["description"] or "Its stop file exists.",
                {"type": "stop_file", "i": i, "stop": False})
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda o: order[o["level"]])


def limit_name(s):
    s = s.lower()
    return "weekly" if "weekly" in s else "5-hour" if "five hour" in s else s


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
    model, thinking = body.get("model", ""), body.get("thinking", "")
    if a["provider"] == "claude":
        if model not in CLAUDE_MODELS or thinking not in THINKING:
            raise ValueError("invalid model or thinking level for a Claude runtime")
    else:
        if model not in other_models(list(agents.values()), (DATA.get("multica") or {}).get("runs") or []):
            raise ValueError("invalid model for this runtime")
        thinking = a["thinking"]  # other runtimes carry the level in the model name
    if (model, thinking) == (a["model"], a["thinking"]):
        return {"message": "Nothing to change."}
    old = a["model"] + (f" / {a['thinking']}" if a["thinking"] else "")
    new = model + (f" / {thinking}" if thinking else "")
    note = ""
    if mode == "multica":
        args = ["agent", "update", a["id"], "--model", model]
        if a["provider"] == "claude":
            args += ["--thinking-level", thinking]
        mx(args, timeout=60)
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
    os.makedirs(CONFIG_DIR, exist_ok=True)
    json.dump({**vals, "at": now().isoformat()}, open(MANUAL, "w", encoding="utf-8"), indent=1)
    with LOCK:
        DATA.setdefault("quotas", {})["claude"] = claude_plan()
    return {"message": "Saved. Type them in again when the app's numbers change; the page shows their age."}


ACTIONS = {"/api/claude": act_claude_percentages, "/api/model": act_model, "/api/cancel": act_cancel,
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
