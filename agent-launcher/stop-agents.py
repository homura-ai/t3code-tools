#!/usr/bin/env python3
"""Stop every thread in a T3 Code project, then settle it.

Usage:
  stop-agents.py --project ID [--dry-run] [--wait SECONDS] [--reason TEXT] [--port PORT]

Talks to T3's MCP endpoint (http://127.0.0.1:PORT/mcp, default port $T3_PORT or 3773) over HTTP with a
short-lived bearer session issued by `t3 auth session issue --subject
mcp-client` and revoked on exit. The token is never printed.

Steps:
  1. List every thread in the project: t3_thread_list (all pages,
     includeSubagents, settled and unsettled), plus any thread filed in a
     sidebar folder (t3_thread_folder list) whose project matches, which
     also catches archived threads the list omits.
  2. For each thread with an active status: t3_thread_interrupt stop=true
     (delegate_task children fall back to task_cancel), then poll
     `t3 thread show` until it is no longer active (timeout --wait).
  3. t3_thread_organize action=settle on every thread not yet settled or
     archived. Never archives.
  4. Re-list, print a summary, exit 1 if anything failed.

Limit: T3 caps a bearer session at runtime mode approval-required and
refuses t3_thread_interrupt / t3_thread_organize on threads with a broader
mode (runtime_mode_escalation_denied). Such threads are reported as
failures; act on them from a full-access agent thread's T3 MCP tools.

Only threads whose projectId equals --project are touched, and never the
caller's own thread (T3CODE_THREAD_ID).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

T3 = os.environ.get("T3_CLI") or shutil.which("t3") or os.path.expanduser("~/.t3/bin/t3")
MCP_URL = "http://127.0.0.1:%s/mcp" % os.environ.get("T3_PORT", "3773")
REASON = "Stopping all threads in this project."
ACTIVE = {"preparing", "queued", "starting", "running", "waiting"}


class McpError(Exception):
    pass


class Mcp:
    def __init__(self, scopes):
        cmd = [T3, "auth", "session", "issue", "--subject", "mcp-client",
               "--ttl", "15m", "--label", "stop-agents", "--json"]
        for s in scopes:
            cmd += ["--scope", s]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise McpError("t3 auth session issue failed: %s" % p.stderr.strip()[:300])
        d = json.loads(p.stdout)
        self._token = d["token"]
        self.session_id = d["sessionId"]
        self._mcp_session = None
        self._id = 0
        try:
            self._rpc("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "stop-agents", "version": "1"}})
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise

    def _post(self, body):
        headers = {"Authorization": "Bearer " + self._token,
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._mcp_session:
            headers["Mcp-Session-Id"] = self._mcp_session
        req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            raise McpError("HTTP %s from /mcp: %s" % (e.code, e.read().decode(errors="replace")[:300]))
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._mcp_session = sid
        text = r.read().decode()
        if "text/event-stream" in (r.headers.get("content-type") or ""):
            text = "".join(l[5:].strip() for l in text.splitlines() if l.startswith("data:"))
        return text

    def _rpc(self, method, params):
        self._id += 1
        text = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        msg = json.loads(text)
        if "error" in msg:
            raise McpError("%s: %s" % (method, json.dumps(msg["error"])[:400]))
        return msg["result"]

    def call(self, tool, args):
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise McpError("%s refused: %s" % (tool, text.strip()[:400]))
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self):
        p = subprocess.run([T3, "auth", "session", "revoke", self.session_id],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print("WARNING: could not revoke session %s: %s"
                  % (self.session_id, p.stderr.strip()[:200]), file=sys.stderr)


def list_threads(mcp, project):
    threads, cursor = [], 0
    while True:
        r = mcp.call("t3_thread_list", {"projectId": project, "includeSubagents": True,
                                        "cursor": cursor, "limit": 100})
        threads += r.get("threads", [])
        nxt = r.get("nextCursor")
        if nxt is None or nxt == cursor:
            break
        cursor = nxt
    # Hard filter: never touch anything outside the project.
    return [t for t in threads if t.get("projectId") == project]


def show(tid):
    p = subprocess.run([T3, "thread", "show", tid, "--json", "--messages", "0"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return json.loads(p.stdout).get("thread")


def folder_threads(mcp, project, known):
    """Threads filed in sidebar folders that the list call didn't return
    (e.g. archived ones), kept only if they belong to the project."""
    extra = []
    for f in mcp.call("t3_thread_folder", {"action": "list"}).get("folders", []):
        for tid in f.get("threadIds", []):
            if tid in known:
                continue
            t = show(tid)
            if t and t.get("projectId") == project:
                t.setdefault("threadId", tid)
                t["_folder"] = f.get("name")
                extra.append(t)
                known.add(tid)
    return extra


def all_threads(mcp, project):
    threads = list_threads(mcp, project)
    return threads + folder_threads(mcp, project, {t["threadId"] for t in threads})


def stop(mcp, t, reason=REASON):
    """Interrupt with stop=true; delegate_task children fall back to task_cancel."""
    tid = t["threadId"]
    try:
        mcp.call("t3_thread_interrupt", {"threadId": tid, "stop": True, "reason": reason})
        return
    except McpError as e:
        prefix = "thread:delegated-task:"
        if not tid.startswith(prefix):
            raise
        first = e
    task_id = urllib.parse.unquote(tid[len(prefix):])
    try:
        mcp.call("task_cancel", {"taskId": task_id, "reason": reason})
    except McpError as e:
        raise McpError("%s; task_cancel %s: %s" % (first, task_id, e))


def label(t):
    tags = [t.get("status") or "?"]
    if t.get("parentThreadId"):
        tags.append("subagent of " + t["parentThreadId"])
    if t.get("archived"):
        tags.append("archived")
    if t.get("_folder"):
        tags.append("folder " + t["_folder"])
    return "%s  [%s]  %s" % (t["threadId"], ", ".join(tags), t.get("title", ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", required=True, help="T3 project id: the projectId in `t3 thread list --project PATH --json`")
    ap.add_argument("--dry-run", action="store_true",
                    help="only list what would be done (still issues and revokes a read-only session)")
    ap.add_argument("--wait", type=float, default=90, help="seconds to wait per interrupted thread")
    ap.add_argument("--reason", default=REASON, help="reason recorded on each interrupt")
    ap.add_argument("--port", type=int, help="T3 backend port (default $T3_PORT or 3773)")
    a = ap.parse_args()
    global MCP_URL
    if a.port:
        MCP_URL = "http://127.0.0.1:%d/mcp" % a.port
    me = os.environ.get("T3CODE_THREAD_ID", "")

    scopes = ["orchestration:read"] if a.dry_run else ["orchestration:read", "orchestration:operate"]
    mcp = Mcp(scopes)
    stopped, settled, already, failed, skipped = [], [], [], [], []
    try:
        threads = all_threads(mcp, a.project)
        print("Project %s: %d thread(s)" % (a.project, len(threads)))
        mine = [t for t in threads if me and t["threadId"] == me]
        if mine:
            skipped.append((mine[0], "caller's own thread"))
        threads = [t for t in threads if not (me and t["threadId"] == me)]

        active = [t for t in threads if t.get("status") in ACTIVE]
        # Archived threads are already filed away; settle only live ones.
        to_settle = [t for t in threads if not t.get("settled") and not t.get("archived")]
        already = [t for t in threads if t.get("settled") or t.get("archived")]

        if a.dry_run:
            print("\nWould interrupt (stop=true) %d:" % len(active))
            for t in active:
                print("  " + label(t))
            print("\nWould settle %d:" % len(to_settle))
            for t in to_settle:
                print("  " + label(t))
            print("\nAlready settled or archived %d:" % len(already))
            for t in already:
                print("  " + label(t))
            for t, why in skipped:
                print("\nSkipped (%s): %s" % (why, label(t)))
            return 0

        bad = set()
        for t in active:
            tid = t["threadId"]
            try:
                stop(mcp, t, a.reason)
            except Exception as e:
                failed.append((t, "interrupt: %s" % e))
                bad.add(tid)
                continue
            deadline = time.time() + a.wait
            st = t.get("status")
            while time.time() < deadline:
                st = (show(tid) or {}).get("status")
                if st not in ACTIVE:
                    break
                time.sleep(2)
            if st in ACTIVE:
                failed.append((t, "still %s after %ds" % (st, a.wait)))
                bad.add(tid)
            else:
                stopped.append(t)
                print("stopped  " + label(t))

        for t in to_settle:
            try:
                mcp.call("t3_thread_organize", {"threadId": t["threadId"], "action": "settle"})
                settled.append(t)
                print("settled  " + label(t))
            except Exception as e:
                failed.append((t, "settle: %s" % e))

        # Verify.
        for t in all_threads(mcp, a.project):
            if me and t["threadId"] == me:
                continue
            if t.get("archived"):
                if t.get("status") in ACTIVE and t["threadId"] not in bad:
                    failed.append((t, "verify: archived but still %s" % t.get("status")))
                continue
            if t.get("status") in ACTIVE and t["threadId"] not in bad:
                failed.append((t, "verify: still %s" % t.get("status")))
            if not t.get("settled") and not any(f[0]["threadId"] == t["threadId"] for f in failed):
                failed.append((t, "verify: not settled"))
    except Exception as e:
        failed.append(({"threadId": "-", "title": "(script)"}, str(e)))
    finally:
        mcp.close()

    print("\nSummary: stopped %d, settled %d (already settled %d), skipped %d, failed %d"
          % (len(stopped), len(settled), len(already), len(skipped), len(failed)))
    for t, why in skipped:
        print("  skipped %s: %s" % (t["threadId"], why))
    for t, why in failed:
        print("  FAILED %s (%s): %s" % (t["threadId"], t.get("title", ""), why))
    if any("runtime_mode_escalation_denied" in why for _, why in failed):
        print("\nNote: a bearer session is capped at approval-required, and T3 refuses\n"
              "interrupt/settle on any thread with a broader runtime mode (these are\n"
              "full-access). Run the same calls from a full-access agent thread's own\n"
              "T3 MCP tools (t3_thread_interrupt stop=true, t3_thread_organize settle)\n"
              "on the ids above, or from the T3 UI.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
