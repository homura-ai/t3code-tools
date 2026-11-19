#!/usr/bin/env python3
"""Start one T3 Code thread per role file, once.

Usage:
  start-agents.py REPO [--config FILE] [--dry-run] [--start-at ISO] [--id-prefix PREFIX]
                  [--all-model "Model effort"]

REPO is the project the agents work in (it must be a T3 project). Settings come from
REPO/agents.json unless --config names another file; see agents.example.json:

  roles_dir          folder of role files, relative to REPO (default "roles")
  request_id_prefix  prefix of each start's --client-request-id (required)
  first              role names to start first, in this order (default none)
  models             {"Name": [provider, model id, effort option id]}
  roles              {"Role name": "Name effort"} or "Name effort ultracode" (Claude only)
  models_doc         optional file in REPO whose "## Role models" list must equal "roles"
  message            optional start message; {name}, {path} and {repo} are filled in

Rules:
- Eligible roles: every .md file under the roles folder, skipping any path part that starts
  with "_". The name is the file name without .md. A file with no "roles" entry, an entry with
  no file, and duplicate names are failures and are not started.
- Order: "first" roles, then the rest by name. One start at a time, each a top-level thread.
- Each start uses a stable --client-request-id (prefix + name in lowercase with hyphens), so a
  retry never starts a duplicate; a failed start is retried once with the same id.
- REPO/threads.tsv (name TAB thread id) is append-only, fsynced after each start. Names already
  in it are skipped, so a rerun only starts what is missing.
- --start-at adds a wait-until-then instruction to each message: the agent prepares, runs a
  shell command that sleeps until that time, then starts its work in the same turn.
- --all-model runs every agent on one model (for a cheap test run). It needs an --id-prefix
  other than request_id_prefix, so a test run can never reuse the real run's request ids.
  Run it in a separate copy of REPO: threads.tsv is per REPO, and the real run skips any
  name a test run recorded there.
- Prints STARTED, SKIPPED and FAILED; exits 0 only if nothing failed. --dry-run starts and
  writes nothing.

The t3 CLI is $T3_CLI, else `t3` on PATH, else ~/.t3/bin/t3.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MESSAGE = "You are {name}. Follow the instructions in {path}."


def t3_cli():
    return os.environ.get("T3_CLI") or shutil.which("t3") or os.path.expanduser("~/.t3/bin/t3")


def now():
    return dt.datetime.now().astimezone()


def log(msg):
    print(f"{now():%H:%M:%S} {msg}", flush=True)


def last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def run_t3(args, repo, stdin=None):
    """Return (exit code or None, stdout, stderr). None means timeout or exception."""
    try:
        p = subprocess.run([t3_cli(), *args], cwd=repo, input=stdin, capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out after 30 seconds"
    except Exception as e:  # noqa: BLE001
        return None, "", f"{type(e).__name__}: {e}"


# ---------- config ----------

def load_config(path):
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    if not isinstance(c, dict):
        raise ValueError("expected a JSON object")
    known = {"roles_dir", "request_id_prefix", "first", "models", "roles", "models_doc", "message"}
    unknown = set(c) - known
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    # A missing or null optional setting takes its default.
    for key, default in (("roles_dir", "roles"), ("first", []), ("models_doc", None), ("message", DEFAULT_MESSAGE)):
        if c.get(key) is None:
            c[key] = default
    for key in ("request_id_prefix", "models", "roles"):
        if not c.get(key):
            raise ValueError(f"missing setting: {key}")
    if not valid_prefix(c["request_id_prefix"]):
        raise ValueError("request_id_prefix must be lowercase letters, digits and hyphens and end with '-'")
    if not isinstance(c["models"], dict) or not isinstance(c["roles"], dict):
        raise ValueError("models and roles must be JSON objects")
    for name, value in c["models"].items():
        if not (isinstance(value, list) and len(value) == 3 and all(isinstance(v, str) for v in value)):
            raise ValueError(f"model {name}: expected [provider, model id, effort option id]")
    for name, spec in c["roles"].items():
        if not isinstance(spec, str):
            raise ValueError(f"role {name}: expected a string like 'Opus high'")
        model_args(spec, c["models"])  # raises on a bad spec
    if not (isinstance(c["first"], list) and all(isinstance(n, str) for n in c["first"])):
        raise ValueError("first must be a list of role names")
    if not isinstance(c["message"], str):
        raise ValueError("message must be a string")
    try:
        c["message"].format(name="", path="", repo="")
    except (KeyError, IndexError, AttributeError, ValueError) as e:
        raise ValueError(f"message: only {{name}}, {{path}} and {{repo}} are filled in; "
                         f"write literal braces as {{{{ }}}} ({type(e).__name__}: {e})")
    return c


def valid_prefix(prefix):
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*-", prefix or ""))


def model_args(spec, models):
    """'Model effort' or 'Model effort ultracode' (Claude only: adds --option ultracode=true)."""
    parts = spec.split()
    if len(parts) not in (2, 3) or parts[0] not in models or parts[1] not in EFFORTS \
            or parts[2:] not in ([], ["ultracode"]):
        raise ValueError(f"bad model spec {spec!r}: expected '<model name> <effort> [ultracode]'")
    provider, model_id, option = models[parts[0]]
    if parts[2:] and provider != "claudeAgent":
        raise ValueError(f"bad model spec {spec!r}: ultracode is Claude only")
    return provider, model_id, option, parts[1], bool(parts[2:])


# ---------- threads.tsv ----------

def read_threads(path, failed):
    """Return {name: id} for well-formed lines (last one wins). Missing file means empty."""
    recorded = {}
    if not os.path.exists(path):
        return recorded
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                failed.append((f"threads.tsv line {i}", f"malformed line {line.rstrip()!r}; left untouched"))
                continue
            recorded[parts[0].strip()] = parts[1].strip()
    return recorded


def append_line(path, name, value):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}\t{value}\n")
        f.flush()
        os.fsync(f.fileno())


# ---------- roles ----------

def find_roles(root, role_models, failed):
    """Return list of (name, abs path) for unambiguous matched roles."""
    eligible = {}
    listed = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: failed.append(("roles", f"cannot list: {e}"))):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            parts = rel.split(os.sep)
            listed.append(rel)
            if not fn.endswith(".md") or any(p.startswith("_") for p in parts):
                continue
            eligible.setdefault(fn[:-3], []).append(full)
    print(f"Files under {root} ({len(listed)}):", flush=True)
    for rel in listed:
        print(f"  {rel}", flush=True)
    roles = []
    for name in sorted(eligible):
        files = eligible[name]
        if len(files) > 1:
            failed.append((name, "duplicate role name: " + "; ".join(files)))
            continue
        if name not in role_models:
            failed.append((name, f"no model entry for {files[0]}"))
            continue
        roles.append((name, files[0]))
    for name in sorted(role_models):
        if name not in eligible:
            failed.append((name, "model entry with no eligible role file"))
    return roles


def check_models_doc(path, role_models):
    """Return an error string if the doc's '## Role models' list differs from role_models."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        return f"cannot read {path}: {e}"
    m = re.search(r"^## Role models\s*$(.*?)(?=^#)", text + "\n#", re.M | re.S)
    if not m:
        return f"{path} has no '## Role models' section"
    found = {}
    for line in m.group(1).splitlines():
        # Model names are config keys (any non-space text), e.g. "GPT-5 high".
        lm = re.match(r"^\s*- (.+?): (\S+ \w+(?: ultracode)?)\s*$", line)
        if lm:
            found[lm.group(1)] = lm.group(2)
    if found != role_models:
        diff = sorted(set(found.items()) ^ set(role_models.items()))
        return f"{path} Role models differ from the launcher config: {diff}"
    return None


def check_capabilities(specs, models, repo):
    """Return {spec: error} for model mappings the running T3 does not offer."""
    errors = {}
    cache = {}
    for spec in sorted(set(specs)):
        provider, model_id, option, effort, ultracode = model_args(spec, models)
        ok = False
        for search in (None, model_id):
            if search not in cache:
                args = ["thread", "capabilities", "--json"] + (["--search", search] if search else [])
                code, out, err = run_t3(args, repo)
                try:
                    cache[search] = json.loads(out) if code == 0 else None
                except ValueError:
                    cache[search] = None
            data = cache[search]
            if data is None:
                continue
            for p in data.get("providers", []):
                if p.get("providerInstanceId") != provider:
                    continue
                for m in p.get("models", []):
                    if m.get("id") != model_id:
                        continue
                    opts = {o.get("id"): o.get("values", []) for o in m.get("options", [])}
                    if effort in opts.get(option, []) and (not ultracode or True in opts.get("ultracode", [])):
                        ok = True
            if ok:
                break
        if not ok:
            errors[spec] = (f"capabilities do not offer provider {provider} model {model_id} "
                            f"with {option}={effort}" + (" and ultracode=true" if ultracode else ""))
    return errors


# ---------- start ----------

def request_id(prefix, name):
    return prefix + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def start_message(template, name, path, repo, start_at=None):
    msg = template.format(name=name, path=path, repo=repo)
    if start_at is None:
        return msg
    when = f"{start_at:%H:%M:%S}" if start_at.second or start_at.microsecond else f"{start_at:%H:%M}"
    when += f" {start_at:%Z} on {start_at:%Y-%m-%d}"
    return msg + (
        f" Work starts at {when}. Get ready now: read your role file and what it points to. Until then "
        f"you may read, list and check things, but don't start your role's work, write files or send "
        f"messages. When you are ready, run exactly this bash command, which waits until the start time "
        f"and then prints the time: "
        f"`while [ \"$(date +%s)\" -lt {int(start_at.timestamp())} ]; do sleep 1; done; date`. "
        f"If your tool stops the command before it finishes, run the same command again. When it "
        f"finishes, start your role's work in the same turn; no message will tell you the time has come. "
        f"If the time has already passed, the command returns at once and you start now.")


def resolve_id(code, out, name, repo):
    if code == 0:
        try:
            data = json.loads(out)
            if isinstance(data, dict) and isinstance(data.get("threadId"), str) and data["threadId"].strip():
                return data["threadId"].strip()
        except ValueError:
            pass
        m = re.fullmatch(r"\s*(\S+)\s*", out or "")
        if m and not m.group(1).startswith(("{", "[")):
            return m.group(1)
    lc, lout, _ = run_t3(["thread", "list", "--project", repo, "--json"], repo)
    if lc != 0:
        return None
    try:
        threads = json.loads(lout)
    except ValueError:
        return None
    if not isinstance(threads, list):
        return None
    matches = [t for t in threads if isinstance(t, dict) and t.get("title") == name
               and isinstance(t.get("threadId"), str) and isinstance(t.get("createdAt"), str)]
    if not matches:
        return None
    newest = max(t["createdAt"] for t in matches)
    return sorted(t["threadId"] for t in matches if t["createdAt"] == newest)[0]


def start_phase(a, repo, config, start_at):
    started, skipped, failed = [], [], []
    tsv = os.path.join(repo, "threads.tsv")
    prefix = a.id_prefix or config["request_id_prefix"]
    models, role_models = config["models"], config["roles"]

    if config["models_doc"]:
        err = check_models_doc(os.path.join(repo, config["models_doc"]), role_models)
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
    try:
        recorded = read_threads(tsv, failed)
        if not a.dry_run:
            open(tsv, "a", encoding="utf-8").close()
    except OSError as e:
        print(f"ERROR: threads.tsv cannot be read or opened for append: {e}", file=sys.stderr)
        return 1

    roles = find_roles(os.path.join(repo, config["roles_dir"]), role_models, failed)
    ids = {}
    for name, _ in roles:
        ids.setdefault(request_id(prefix, name), []).append(name)
    collide = {n for names in ids.values() if len(names) > 1 for n in names}
    for n in sorted(collide):
        failed.append((n, f"request id {request_id(prefix, n)} collides with another role"))
    roles = [r for r in roles if r[0] not in collide]
    first = config["first"]
    roles.sort(key=lambda r: (first.index(r[0]) if r[0] in first else len(first), r[0]))

    spec_of = {name: (a.all_model or role_models[name]) for name, _ in roles}
    todo = [r for r in roles if r[0] not in recorded]
    cap_errors = check_capabilities([spec_of[n] for n, _ in todo], models, repo) if todo else {}

    for idx, (name, path) in enumerate(roles):
        if name in recorded:
            skipped.append((name, f"already recorded as {recorded[name]}"))
            continue
        spec = spec_of[name]
        if spec in cap_errors:
            failed.append((name, cap_errors[spec]))
            continue
        provider, model_id, option, effort, ultracode = model_args(spec, models)
        msg = start_message(config["message"], name, path, repo, start_at)
        rid = request_id(prefix, name)
        args = ["thread", "start", "--title", name, "--provider", provider, "--model", model_id,
                "--option", f"{option}={effort}", *(["--option", "ultracode=true"] if ultracode else []),
                "--json", "--client-request-id", rid, repo, "-"]
        if a.dry_run:
            print(f"WOULD START {name} [{spec}] id={rid}", flush=True)
            print(f"  t3 {' '.join(args)}", flush=True)
            print(f"  message: {msg}", flush=True)
            recorded[name] = "<dry-run>"
            started.append((name, "<dry-run>"))
            continue
        tid, last_err = None, ""
        for attempt in (1, 2):
            if attempt == 2:
                time.sleep(2)
            code, out, errtxt = run_t3(args, repo, stdin=msg)
            last_err = last_line(errtxt) or (f"exit {code}" if code else "no id in output")
            tid = resolve_id(code, out, name, repo)
            if tid:
                break
        if not tid:
            failed.append((name, f"start failed or id unresolved after retry: {last_err}"))
            log(f"FAILED {name}: {last_err}")
            continue
        try:
            append_line(tsv, name, tid)
        except OSError as e:
            print(f"ERROR: started {name} as {tid} but could not record it in threads.tsv: {e}",
                  file=sys.stderr)
            failed.append((name, f"started as {tid} but NOT recorded: {e}"))
            for later in roles[idx + 1:]:
                if later[0] not in recorded:
                    failed.append((later[0], "not attempted: threads.tsv append failed"))
            summary(started, skipped, failed)
            return 1
        recorded[name] = tid
        started.append((name, tid))
        log(f"STARTED {name} {tid}")

    summary(started, skipped, failed)
    return 0 if not failed else 1


def summary(started, skipped, failed):
    for label, items in (("STARTED", started), ("SKIPPED", skipped), ("FAILED", failed)):
        print(f"{label}:", flush=True)
        if not items:
            print("  none", flush=True)
        for name, detail in items:
            print(f"  {name}: {detail}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", help="project folder holding the roles folder and threads.tsv")
    ap.add_argument("--config", help="launcher settings (default REPO/agents.json)")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen; start and write nothing")
    ap.add_argument("--start-at", help="ISO time every agent waits for before starting work "
                    "(local time zone if none given)")
    ap.add_argument("--id-prefix", help="request id prefix instead of the configured one, for a separate run")
    ap.add_argument("--all-model", metavar="SPEC", help="run every agent on this model, e.g. 'Sonnet low'")
    a = ap.parse_args()
    if a.id_prefix is not None and not valid_prefix(a.id_prefix):
        ap.error("--id-prefix must be lowercase letters, digits and hyphens and end with '-'")
    start_at = None
    if a.start_at:
        try:
            start_at = dt.datetime.fromisoformat(a.start_at)
        except ValueError:
            ap.error(f"--start-at: not an ISO time: {a.start_at!r}")
        start_at = start_at.astimezone() if start_at.tzinfo is None else start_at
    repo = os.path.realpath(a.repo)
    try:
        config = load_config(a.config or os.path.join(repo, "agents.json"))
        if a.all_model:
            model_args(a.all_model, config["models"])
    except (OSError, ValueError) as e:
        print(f"ERROR: launcher settings: {e}", file=sys.stderr)
        return 1
    if a.all_model and (a.id_prefix or config["request_id_prefix"]) == config["request_id_prefix"]:
        # Same request ids as the real run: T3 would hand back the real run's threads.
        ap.error("--all-model needs its own --id-prefix, different from request_id_prefix")
    if not os.path.isdir(os.path.join(repo, config["roles_dir"])):
        print(f"ERROR: {os.path.join(repo, config['roles_dir'])} is not a directory", file=sys.stderr)
        return 1
    print(f"Repo {repo}{'; DRY RUN' if a.dry_run else ''}"
          f"{f'; all agents on {a.all_model}' if a.all_model else ''}"
          f"{f'; start at {start_at.isoformat()}' if start_at else ''}"
          f"; request ids {a.id_prefix or config['request_id_prefix']}<name>", flush=True)
    return start_phase(a, repo, config, start_at)


if __name__ == "__main__":
    sys.exit(main())
