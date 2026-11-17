#!/usr/bin/env python3
"""Bring agents back after a usage-limit category death.

Usage:
  revive-agents.py --repo REPO [--dry-run] [--once] [--back CATEGORY]
                   [--state-dir DIR] [--report-to NAME]

Needs no model, so it keeps running when a whole model category (Codex,
Fable, Opus) hits its usage limit and every agent on it stops. It reads the
`[Usage] USAGE CATEGORY DEATH` notices that usage-meters' claude-usage posts
into threads; times in them are Eastern Time ("ET"), as that tool writes them.

Every minute (one pass with --once or --dry-run):
  1. Read <repo>/threads.tsv afresh (<name>\t<thread id>, last line per name).
  2. Read each agent's newest messages (`t3 thread show --json`). An agent is
     dead when they hold T3's `[Usage] USAGE CATEGORY DEATH: <category>.`
     notice for its own category (from the thread's provider/model) and no
     assistant message without a usage error came after it. Deaths are
     recorded with category, `since <time> ET`, `Earliest scheduled return
     <time> ET` (or human-uncap-only) and the agents taken out.
  3. A category is back when a human runs `--back <category>`, or when its
     scheduled return has passed. Human-uncap-only deaths wait for --back.
  4. Probe: wake the agent with the oldest death first. If it reports a usage
     limit again within 5 minutes, wait 15 minutes and probe again; otherwise
     wake the others, at most one every 5 seconds.
  5. Each agent is woken once per death; state lives in
     --state-dir/revive-agents.json (default REPO/.t3-agent-launcher;
     temp file + os.replace).
  6. When done, it prints one report, and sends it to the --report-to agent
     (a name in threads.tsv) if given.

Sends with `t3 thread send --mode auto <id> -` (queue fallback, three tries),
the run's standard path. It never switches models, interrupts, settles or
archives. No token is ever read, printed or stored.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

T3 = os.environ.get("T3_CLI") or shutil.which("t3") or os.path.expanduser("~/.t3/bin/t3")
ET = ZoneInfo("America/New_York")  # the notices' time zone
NAME = "revive-agents"
PROBE_WINDOW = 5 * 60
PROBE_RETRY = 15 * 60
WAKE_GAP = 5
N_MESSAGES = 30

NOTICE_RE = re.compile(r"\[Usage\]\s*USAGE CATEGORY DEATH:\s*([^.\n]+)\.", re.I)
SINCE_RE = re.compile(r"\bsince\s+(.+?)\s+ET\b", re.I)
RETURN_RE = re.compile(r"Earliest scheduled return\s+(.+?)\s+ET\b", re.I)
USAGE_ERR_RE = re.compile(r"usage limit|USAGE CATEGORY DEATH|rate.?limit|quota exceeded|"
                          r"hit your limit|limit reached", re.I)


def now():
    return dt.datetime.now(ET)


def log(msg):
    print(f"{now():%H:%M:%S} ET {msg}", flush=True)


def short(text):
    return text.replace("\n", " ")[:80]


def last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def run_t3(args, repo, stdin=None):
    try:
        p = subprocess.run([T3, *args], cwd=repo, input=stdin, capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out after 30 seconds"
    except Exception as e:  # noqa: BLE001
        return None, "", f"{type(e).__name__}: {e}"


# ---------- categories ----------

def category_of(thread):
    """Canonical category of a thread from its provider and model."""
    prov = (thread.get("providerInstanceId") or thread.get("provider") or "").lower()
    model = (thread.get("model") or "").lower()
    if "fable" in model:
        return "Fable"
    if "opus" in model:
        return "Opus"
    if "sonnet" in model:
        return "Sonnet"
    if "haiku" in model:
        return "Haiku"
    if prov.startswith("codex") or model.startswith("gpt"):
        return "Codex"
    return prov.capitalize() or None


ALIASES = {"sol": "Codex", "astra": "Codex", "terra": "Codex", "gpt": "Codex", "openai": "Codex",
           "codex": "Codex", "fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}


def canon(name):
    n = name.strip().lower()
    for k, v in ALIASES.items():
        if k in n:
            return v
    return name.strip()


def parse_et(raw, ref):
    """Parse an ET time string; a bare time is taken as the latest such time not after ref+12h."""
    s = re.sub(r"\s+", " ", raw.strip().rstrip(",;"))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%b %d %H:%M", "%b %d, %H:%M", "%b %d %I:%M %p", "%b %d, %I:%M %p"):
        try:
            d = dt.datetime.strptime(s, fmt)
            if d.year == 1900:
                d = d.replace(year=ref.year)
            return d.replace(tzinfo=ET)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p", "%I:%M%p", "%I %p"):
        try:
            t = dt.datetime.strptime(s.upper() if "m" in s.lower() else s, fmt).time()
        except ValueError:
            continue
        return dt.datetime.combine(ref.date(), t, ET)
    return None


def parse_notice(text, ref):
    m = NOTICE_RE.search(text)
    if not m:
        return None
    since_m, ret_m = SINCE_RE.search(text), RETURN_RE.search(text)
    since_raw = since_m.group(1) if since_m else ""
    since = parse_et(since_raw, ref) if since_raw else None
    if since and since > ref + dt.timedelta(minutes=5):
        since -= dt.timedelta(days=1)
    ret = None
    if ret_m:
        ret = parse_et(ret_m.group(1), since or ref)
        if ret and since and ret < since:
            ret += dt.timedelta(days=1)
    return {"category": canon(m.group(1)), "since_raw": since_raw,
            "since": since.isoformat() if since else None,
            "return_raw": ret_m.group(1) if ret_m else None,
            "return_at": ret.isoformat() if ret else None,
            "human_only": not ret_m}


# ---------- threads ----------

def read_threads(path):
    rec = {}
    if not os.path.exists(path):
        return rec
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                rec[parts[0].strip()] = parts[1].strip()
    return rec


def show(repo, tid):
    code, out, err = run_t3(["thread", "show", tid, "--messages", str(N_MESSAGES), "--json"], repo)
    if code != 0:
        raise RuntimeError(f"t3 thread show {tid}: {last_line(err) or code}")
    return json.loads(out)


def death_in(data, ref):
    """The death notice for the thread's own category if still in force, else None."""
    cat = category_of(data.get("thread") or {})
    msgs = data.get("messages") or []
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") != "user":
            continue
        n = parse_notice(m.get("text") or "", ref)
        if not n or n["category"] != cat:
            continue
        for later in msgs[i + 1:]:
            if later.get("role") == "assistant" and not USAGE_ERR_RE.search(later.get("text") or ""):
                return None  # finished a turn since without a usage error
        return n
    return None


def hit_limit_after(data, marker, cat, ref):
    """True if, after the newest user message starting with marker, the thread shows a usage error."""
    msgs = data.get("messages") or []
    idx = None
    for i, m in enumerate(msgs):
        if m.get("role") == "user" and (m.get("text") or "").startswith(marker):
            idx = i
    if idx is None:
        return False
    for m in msgs[idx + 1:]:
        t = m.get("text") or ""
        if m.get("role") == "user":
            n = parse_notice(t, ref)
            if n and n["category"] == cat:
                return True
        elif USAGE_ERR_RE.search(t):
            return True
    return False


def send(repo, name, tid, text, dry):
    if dry:
        log(f"WOULD SEND {name} {tid or 'NOT FOUND'} {short(text)}")
        print(text, flush=True)
        return True
    if not tid:
        log(f"FAILED {name} NOT FOUND {short(text)}")
        return False
    err = ""
    for attempt in range(3):
        code, _, err = run_t3(["thread", "send", "--mode", "auto", tid, "-"], repo, text)
        if code != 0:
            code, _, err = run_t3(["thread", "send", "--mode", "queue", tid, "-"], repo, text)
        if code == 0:
            log(f"SENT {name} {tid} {short(text)}")
            return True
        if attempt < 2:
            time.sleep(2)
    log(f"FAILED {name} {tid} {short(text)} | {last_line(err)}")
    return False


# ---------- state ----------

def state_paths(repo, state_dir=None):
    d = state_dir or os.path.join(repo, ".t3-agent-launcher")
    return d, os.path.join(d, NAME + ".json"), os.path.join(d, NAME + ".lock")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    except Exception as e:  # noqa: BLE001
        log(f"state file unreadable ({e}); treating as missing")
        s = {}
    s.setdefault("deaths", [])
    s.setdefault("back", {})
    return s


def save_state(path, s):
    """Atomic write. Under a short lock, merge --back marks a human may have added meanwhile."""
    with open(path + ".w", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        disk = load_state(path) if os.path.exists(path) else {"back": {}}
        for k, v in disk.get("back", {}).items():
            if v > s["back"].get(k, ""):
                s["back"][k] = v
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def open_death(s, cat):
    for d in s["deaths"]:
        if d["category"] == cat and not d.get("reported"):
            return d
    return None


def wake_text(d, name):
    a = d["agents"][name]
    when = a.get("since_raw") or d.get("since_raw") or "an unknown time"
    return (f"You were stopped by a {d['category']} usage limit at {when} ET. It is back now. "
            "Re-read your role file and your state, check what you were doing when you stopped, "
            "redo anything that may not have finished, and carry on.")


def fmt_dur(sec):
    sec = int(max(sec, 0))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


# ---------- one pass ----------

def one_pass(repo, s, dry, report_to=None):
    ref = now()
    threads = read_threads(os.path.join(repo, "threads.tsv"))
    cache = {}

    # Detect deaths.
    for name, tid in threads.items():
        try:
            data = show(repo, tid)
        except Exception as e:  # noqa: BLE001
            log(f"READ FAILED {name} {tid} {e}")
            continue
        cache[name] = data
        n = death_in(data, ref)
        if not n:
            continue
        # A death already woken for this notice is not a new one.
        if any(d["category"] == n["category"] and d.get("reported") and name in d["agents"]
               and d["agents"][name].get("since_raw") == n["since_raw"] for d in s["deaths"]):
            continue
        d = open_death(s, n["category"])
        if d is None:
            d = {"category": n["category"], "since": n["since"], "since_raw": n["since_raw"],
                 "return_at": n["return_at"], "return_raw": n["return_raw"],
                 "human_only": n["human_only"], "detected": ref.isoformat(),
                 "agents": {}, "back_at": None, "probes": [], "woken": {}, "failed": {},
                 "reported": False}
            s["deaths"].append(d)
            log(f"DEATH {n['category']} since {n['since_raw'] or '?'} ET, "
                + (f"earliest return {n['return_raw']} ET" if not n["human_only"]
                   else "human uncap only"))
        else:
            if n["return_at"] and (not d.get("return_at") or n["return_at"] > d["return_at"]):
                d["return_at"], d["return_raw"], d["human_only"] = n["return_at"], n["return_raw"], False
        if name not in d["agents"]:
            d["agents"][name] = {"thread": tid, "since": n["since"], "since_raw": n["since_raw"]}
            log(f"DEAD {name} {tid} ({n['category']} since {n['since_raw'] or '?'} ET)")

    # Returns, probes, wakes.
    for d in s["deaths"]:
        if d.get("reported"):
            continue
        cat = d["category"]
        if not d.get("back_at"):
            mark = s["back"].get(cat)
            if mark:
                d["back_at"] = mark
                log(f"BACK {cat} (human --back at {mark})")
            elif not d.get("human_only") and d.get("return_at") and ref >= dt.datetime.fromisoformat(d["return_at"]):
                d["back_at"] = ref.isoformat()
                log(f"BACK {cat} (scheduled return {d.get('return_raw')} ET passed)")
            else:
                log(f"WAITING {cat}: {len(d['agents'])} dead, "
                    + (f"return {d.get('return_raw')} ET" if not d.get("human_only") else "needs --back"))
                continue
        pending = sorted((n for n in d["agents"] if n not in d["woken"] and n not in d["failed"]),
                         key=lambda n: d["agents"][n].get("since") or "")
        probe = d["probes"][-1] if d["probes"] else None
        if probe and probe["result"] == "pending":
            age = (ref - dt.datetime.fromisoformat(probe["at"])).total_seconds()
            data = cache.get(probe["agent"])
            if data and hit_limit_after(data, "You were stopped by a", cat, ref):
                probe["result"] = "limit"
                d["woken"].pop(probe["agent"], None)
                log(f"PROBE LIMIT {cat} {probe['agent']}: still dead, next probe in 15 min")
                continue
            if age < PROBE_WINDOW:
                log(f"PROBE WAIT {cat} {probe['agent']} ({int(age)}s of {PROBE_WINDOW}s)")
                continue
            probe["result"] = "ok"
            log(f"PROBE OK {cat} {probe['agent']}")
            pending = [n for n in pending if n != probe["agent"]]
        elif probe and probe["result"] == "limit":
            if (ref - dt.datetime.fromisoformat(probe["at"])).total_seconds() < PROBE_WINDOW + PROBE_RETRY:
                continue
            probe = None
        if not probe or probe["result"] == "limit":
            pending = sorted((n for n in d["agents"] if n not in d["woken"] and n not in d["failed"]),
                             key=lambda n: d["agents"][n].get("since") or "")
            if pending:
                who = pending[0]
                tid = read_threads(os.path.join(repo, "threads.tsv")).get(who) or d["agents"][who]["thread"]
                log(f"PROBE {cat} {who}")
                ok = send(repo, who, tid, wake_text(d, who), dry)
                if dry:
                    for other in pending[1:]:
                        log(f"(after a good probe) would wake {other}")
                        send(repo, other, d["agents"][other]["thread"], wake_text(d, other), True)
                    continue
                if ok:
                    d["woken"][who] = now().isoformat()
                    d["probes"].append({"agent": who, "at": now().isoformat(), "result": "pending"})
                else:
                    d["failed"][who] = now().isoformat()
                continue
        # Probe good: wake the rest.
        tsv = read_threads(os.path.join(repo, "threads.tsv"))
        for i, who in enumerate(pending):
            if i:
                time.sleep(WAKE_GAP)
            tid = tsv.get(who) or d["agents"][who]["thread"]
            if send(repo, who, tid, wake_text(d, who), dry):
                d["woken"][who] = now().isoformat()
            else:
                d["failed"][who] = now().isoformat()
            if not dry:
                save_state(state_path, s)
        if dry:
            continue
        # Report.
        since = d.get("since") or d["detected"]
        dead_for = (dt.datetime.fromisoformat(d["back_at"]) - dt.datetime.fromisoformat(since)).total_seconds()
        msg = (f"Revive report: {cat} usage limit, dead {fmt_dur(dead_for)} "
               f"(since {d.get('since_raw') or '?'} ET, back {dt.datetime.fromisoformat(d['back_at']).astimezone(ET):%H:%M} ET). "
               f"Woken ({len(d['woken'])}): {', '.join(sorted(d['woken'])) or 'none'}. "
               f"Failed ({len(d['failed'])}): {', '.join(sorted(d['failed'])) or 'none'}.")
        print(msg, flush=True)
        if report_to:
            send(repo, report_to, tsv.get(report_to), msg, dry)
        d["reported"] = True
        s["back"].pop(cat, None)
        save_state(state_path, s)


state_path = None


def main():
    global state_path
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="project folder with threads.tsv")
    ap.add_argument("--dry-run", action="store_true", help="one pass; send and write nothing")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--back", metavar="CATEGORY", help="mark a category as back (for human-uncap-only deaths)")
    ap.add_argument("--state-dir", help="state folder (default REPO/.t3-agent-launcher)")
    ap.add_argument("--report-to", metavar="NAME", help="agent in threads.tsv that gets the report")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sdir, state_path, lock_path = state_paths(a.repo, a.state_dir)

    if a.back:
        cat = canon(a.back)
        if a.dry_run:
            log(f"WOULD MARK {cat} back")
            return 0
        os.makedirs(sdir, exist_ok=True)
        s = load_state(state_path)
        s["back"][cat] = now().isoformat()
        save_state(state_path, s)
        log(f"MARKED {cat} back; the running copy wakes its agents on its next pass")
        return 0

    if not a.dry_run:
        os.makedirs(sdir, exist_ok=True)
        lk = open(lock_path, "a")
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("already running", flush=True)
            return 3

    try:
        while True:
            try:
                s = load_state(state_path)
                one_pass(a.repo, s, a.dry_run, a.report_to)
                if not a.dry_run:
                    save_state(state_path, s)
            except Exception as e:  # noqa: BLE001
                log(f"ERROR {type(e).__name__}: {e}")
            if a.dry_run or a.once:
                return 0
            time.sleep(60)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
