"""Offline tests: every command runs against a fake t3 CLI (T3_CLI) and temporary copies.

Nothing here talks to a real T3 server or touches a real project.
"""
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent

FAKE_T3 = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_T3_LOG"]
args = sys.argv[1:]
stdin = sys.stdin.read() if args[-1:] == ["-"] else ""
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "stdin": stdin}) + "\n")
state = json.load(open(os.environ["FAKE_T3_STATE"])) if os.environ.get("FAKE_T3_STATE") else {}
if args[:2] == ["thread", "capabilities"]:
    print(json.dumps({"providers": [
        {"providerInstanceId": "claudeAgent", "models": [
            {"id": m, "options": [{"id": "effort", "values": ["low", "medium", "high"]}]}
            for m in ("claude-opus-5-5", "claude-sonnet-5-5")]},
        {"providerInstanceId": "codex", "models": [
            {"id": "gpt-6.1-sol", "options": [{"id": "reasoningEffort", "values": ["low", "high"]}]}]}]}))
elif args[:2] == ["thread", "start"]:
    rid = args[args.index("--client-request-id") + 1]
    print(json.dumps({"threadId": "thread-" + rid}))
elif args[:2] == ["thread", "show"]:
    print(json.dumps(state["show"][args[2]]))
elif args[:2] == ["thread", "send"]:
    print("ok")
elif args[:3] == ["auth", "session", "issue"]:
    print(json.dumps({"token": "fixture-not-a-token", "sessionId": "sess-1"}))
elif args[:3] == ["auth", "session", "revoke"]:
    pass
else:
    sys.exit("fake t3: unsupported " + " ".join(args))
'''


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.t3 = self.dir / "t3"
        self.t3.write_text(FAKE_T3)
        self.t3.chmod(0o755)
        self.log = self.dir / "t3.log"
        self.env = {**os.environ, "T3_CLI": str(self.t3), "FAKE_T3_LOG": str(self.log), "HOME": str(self.dir)}

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run_py(self, script, *args, env=None, check=False):
        return subprocess.run([sys.executable, str(HERE / script), *map(str, args)], capture_output=True,
                              text=True, env={**self.env, **(env or {})}, timeout=60, check=check)


class StartAgents(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.dir / "project"
        shutil.copytree(HERE / "examples/project", self.repo)

    def starts(self):
        return [c for c in self.calls() if c["args"][:2] == ["thread", "start"]]

    def test_starts_each_role_once_in_order_and_records_threads(self):
        r = self.run_py("start-agents.py", self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        starts = self.starts()
        titles = [c["args"][c["args"].index("--title") + 1] for c in starts]
        self.assertEqual(titles, ["Planner", "Builder", "Code Reviewer"])  # "first", then by name; _drafts skipped
        reviewer = starts[2]["args"]
        self.assertIn("--client-request-id", reviewer)
        self.assertEqual(reviewer[reviewer.index("--client-request-id") + 1], "example-run-1-code-reviewer")
        self.assertIn("reasoningEffort=high", reviewer)
        self.assertIn(f"Read {self.repo}/AGENTS.md first.", starts[0]["stdin"])
        lines = (self.repo / "threads.tsv").read_text().splitlines()
        self.assertEqual(lines[0], "Planner\tthread-example-run-1-planner")
        self.assertEqual(len(lines), 3)
        again = self.run_py("start-agents.py", self.repo)
        self.assertEqual(again.returncode, 0)
        self.assertEqual(len(self.starts()), 3)  # nothing new started
        self.assertIn("already recorded", again.stdout)

    def test_dry_run_writes_nothing_and_start_at_adds_wait(self):
        r = self.run_py("start-agents.py", self.repo, "--dry-run", "--start-at", "2030-01-02T09:30:00+00:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.starts(), [])
        self.assertFalse((self.repo / "threads.tsv").exists())
        self.assertIn("WOULD START Planner [Opus high] id=example-run-1-planner", r.stdout)
        self.assertIn('-lt 1893576600 ]', r.stdout)

    def test_models_doc_mismatch_and_unknown_model_fail_before_starting(self):
        agents = self.repo / "AGENTS.md"
        agents.write_text(agents.read_text().replace("Builder: Sonnet medium", "Builder: Sonnet low"))
        r = self.run_py("start-agents.py", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Role models differ", r.stderr)
        self.assertEqual(self.starts(), [])
        agents.write_text(agents.read_text().replace("Builder: Sonnet low", "Builder: Sonnet medium"))
        r = self.run_py("start-agents.py", self.repo, "--all-model", "Fable xhigh", "--id-prefix", "test-1-")
        self.assertEqual(r.returncode, 1)
        self.assertIn("capabilities do not offer", r.stdout)
        self.assertEqual(self.starts(), [])

    def test_all_model_never_reuses_the_real_request_ids(self):
        for extra in ([], ["--id-prefix", "example-run-1-"]):
            r = self.run_py("start-agents.py", self.repo, "--all-model", "Sonnet low", *extra)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("--all-model needs its own --id-prefix", r.stderr)
        self.assertEqual(self.calls(), [])
        r = self.run_py("start-agents.py", self.repo, "--all-model", "Sonnet low", "--id-prefix", "test-1-")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ids = [c["args"][c["args"].index("--client-request-id") + 1] for c in self.starts()]
        self.assertEqual(ids, ["test-1-planner", "test-1-builder", "test-1-code-reviewer"])
        self.assertTrue(all("effort=low" in c["args"] for c in self.starts()))

    def test_bad_settings_fail_cleanly_before_any_t3_call(self):
        config = json.loads((self.repo / "agents.json").read_text())
        for change, needle in (({"message": "You are {name}. Example: {\"a\": 1}"}, "literal braces"),
                               ({"roles": {**config["roles"], "Builder": 5}}, "role Builder"),
                               ({"first": "Planner"}, "first must be a list")):
            (self.repo / "agents.json").write_text(json.dumps({**config, **change}))
            r = self.run_py("start-agents.py", self.repo)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn(needle, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
        (self.repo / "agents.json").write_text(json.dumps({**config, "message": None, "first": None}))
        r = self.run_py("start-agents.py", self.repo, "--dry-run")  # null means default
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("message: You are Builder. Follow the instructions in", r.stdout)
        (self.repo / "agents.json").write_text("[]")
        r = self.run_py("start-agents.py", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("expected a JSON object", r.stderr)
        (self.repo / "agents.json").write_text(json.dumps(config))
        r = self.run_py("start-agents.py", self.repo, "--start-at", "tomorrow")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not an ISO time", r.stderr)
        self.assertFalse(any(c["args"][:2] == ["thread", "start"] for c in self.calls()))

    def test_models_doc_accepts_any_configured_model_name(self):
        config = json.loads((self.repo / "agents.json").read_text())
        config["models"]["Sol-6.1"] = config["models"].pop("Sol")
        config["roles"]["Code Reviewer"] = "Sol-6.1 high"
        (self.repo / "agents.json").write_text(json.dumps(config))
        agents = self.repo / "AGENTS.md"
        agents.write_text(agents.read_text().replace("Code Reviewer: Sol high", "Code Reviewer: Sol-6.1 high"))
        r = self.run_py("start-agents.py", self.repo, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WOULD START Code Reviewer [Sol-6.1 high]", r.stdout)

    def test_missing_role_file_is_a_failure_but_others_start(self):
        (self.repo / "roles/Builder.md").unlink()
        r = self.run_py("start-agents.py", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Builder: model entry with no eligible role file", r.stdout)
        self.assertEqual(len(self.starts()), 2)


class ReviveAgents(Base):
    def test_dry_run_probes_oldest_dead_agent_after_scheduled_return(self):
        repo = self.dir / "project"
        repo.mkdir()
        (repo / "threads.tsv").write_text("Builder\tt-b\nPlanner\tt-p\n")
        dead = {"thread": {"providerInstanceId": "codex", "model": "gpt-6.1-sol"}, "messages": [
            {"role": "assistant", "text": "working"},
            {"role": "user", "text": "[Usage] USAGE CATEGORY DEATH: Codex. All accounts out since 2020-01-01 10:00 ET. "
                                     "Earliest scheduled return 2020-01-01 12:00 ET."}]}
        alive = {"thread": {"providerInstanceId": "claudeAgent", "model": "claude-opus-5-5"},
                 "messages": [{"role": "assistant", "text": "fine"}]}
        state = self.dir / "show.json"
        state.write_text(json.dumps({"show": {"t-b": dead, "t-p": alive}}))
        r = self.run_py("revive-agents.py", "--repo", repo, "--dry-run", env={"FAKE_T3_STATE": str(state)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DEAD Builder t-b (Codex since 2020-01-01 10:00 ET)", r.stdout)
        self.assertIn("PROBE Codex Builder", r.stdout)
        self.assertIn("WOULD SEND Builder t-b You were stopped by a Codex usage limit", r.stdout)
        self.assertFalse(any(c["args"][:2] == ["thread", "send"] for c in self.calls()))
        self.assertFalse((repo / ".t3-agent-launcher").exists())


class StopAgents(Base):
    def test_dry_run_lists_only_the_project_and_revokes_its_session(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.headers.get("Authorization"), body.get("method"), body.get("params", {}).get("name")))
                result = {}
                if body.get("method") == "tools/call":
                    name = body["params"]["name"]
                    payload = {"t3_thread_list": {"threads": [
                        {"threadId": "a", "projectId": "p1", "status": "running", "title": "Builder"},
                        {"threadId": "b", "projectId": "p1", "status": "idle", "settled": True, "title": "Planner"},
                        {"threadId": "c", "projectId": "other", "status": "running", "title": "Elsewhere"}]},
                        "t3_thread_folder": {"folders": []}}[name]
                    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
                data = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        r = self.run_py("stop-agents.py", "--project", "p1", "--dry-run", "--port", server.server_port)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Would interrupt (stop=true) 1:", r.stdout)
        self.assertIn("a  [running]  Builder", r.stdout)
        self.assertNotIn("Elsewhere", r.stdout)
        self.assertNotIn("fixture-not-a-token", r.stdout + r.stderr)
        self.assertEqual(seen[0][0], "Bearer fixture-not-a-token")
        self.assertNotIn("t3_thread_interrupt", [s[2] for s in seen])
        issued = [c["args"] for c in self.calls() if c["args"][:2] == ["auth", "session"]]
        self.assertEqual(issued[-1], ["auth", "session", "revoke", "sess-1"])
        self.assertIn("orchestration:read", issued[0])
        self.assertNotIn("orchestration:operate", issued[0])

    def test_project_is_required_and_port_is_a_number(self):
        self.assertEqual(self.run_py("stop-agents.py", "--dry-run").returncode, 2)
        r = self.run_py("stop-agents.py", "--project", "p1", "--dry-run", "--port", "x")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_project_help_names_a_command_the_t3_cli_has(self):
        r = self.run_py("stop-agents.py", "--help", env={"COLUMNS": "200"})  # no line wrap
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("t3 thread list --project PATH --json", r.stdout)  # the CLI has no `t3 project list`
        self.assertNotIn("t3 project list", r.stdout)


class TcpRelay(unittest.TestCase):
    def test_forwards_to_loopback_port(self):
        echo = socket.create_server(("127.0.0.1", 0))
        port = echo.getsockname()[1]
        self.addCleanup(echo.close)

        def serve():
            conn, _ = echo.accept()
            with conn:
                conn.sendall(conn.recv(100).upper())
        threading.Thread(target=serve, daemon=True).start()
        relay = subprocess.Popen([sys.executable, str(HERE / "tcp-relay.py"), "127.0.0.2", str(port)])
        self.addCleanup(relay.wait)
        self.addCleanup(relay.terminate)
        for _ in range(50):
            try:
                client = socket.create_connection(("127.0.0.2", port), timeout=2)
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("relay did not start")
        with client:
            client.sendall(b"hello")
            self.assertEqual(client.recv(100), b"HELLO")


class MakeRunRepo(unittest.TestCase):
    def test_copies_run_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest = Path(d) / "prep", Path(d) / "run"
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": d}
            files = {"__start.py": "launcher", "AGENTS_REAL.md": "Real rules. Work in /srv/run/project.\n",
                     "AGENTS_DRAFT.md": "draft", "AGENTS.md": "symlink stand-in",
                     "roles/Builder.md": "Builder. See /srv/run/project/NOTES.md and /srv/run/projects.\n",
                     "roles/__keep.md": "nested __ names are not prep-only"}
            for name, text in files.items():
                (src / name).parent.mkdir(parents=True, exist_ok=True)
                (src / name).write_text(text)
            git = lambda *a: subprocess.run(["git", "-C", str(src), *a], env=env, check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            git("add", "-A")
            git("commit", "-q", "-m", "prep")
            r = subprocess.run(["bash", str(HERE / "make-run-repo.sh"), str(src), str(dest), "--agents-from",
                                "AGENTS_REAL.md", "--exclude", "AGENTS_DRAFT.md", "--rewrite", "/srv/run/project"],
                               env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if ".git" not in p.parts and p.is_file())
            self.assertEqual(got, ["AGENTS.md", "roles/Builder.md", "roles/__keep.md"])
            self.assertEqual((dest / "AGENTS.md").read_text(), f"Real rules. Work in {dest}.\n")
            self.assertEqual((dest / "roles/Builder.md").read_text(),
                             f"Builder. See {dest}/NOTES.md and /srv/run/projects.\n")
            again = subprocess.run(["bash", str(HERE / "make-run-repo.sh"), str(src), str(dest)],
                                   env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("not empty", again.stderr)
            rebuild = lambda target: subprocess.run(
                ["bash", str(HERE / "make-run-repo.sh"), str(src), str(target), "--replace"],
                env=env, capture_output=True, text=True, timeout=60)
            r = rebuild(dest)  # a run repo this script built: wiped and rebuilt
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("Replacing contents", r.stdout)
            other = Path(d) / "other"
            (other / "keep").mkdir(parents=True)
            (other / "keep/data.txt").write_text("not a run repo")
            r = rebuild(other)  # anything else is never wiped
            self.assertEqual(r.returncode, 1)
            self.assertIn("not a run repo built by this script", r.stderr)
            self.assertEqual((other / "keep/data.txt").read_text(), "not a run repo")
            (dest / "threads.tsv").write_text("Builder\tt-1\n")
            r = rebuild(dest)
            self.assertEqual(r.returncode, 1)
            self.assertIn("a run has started there", r.stderr)

    def test_rewrite_is_literal_and_mentions_of_prep_files_are_leftovers(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest = Path(d) / "prep", Path(d) / "run"
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": d}
            files = {"__notes.md": "prep only", "__start.py": "launcher",
                     "README.md": "Work in /srv/c++ (run)/x and /srv/cc (run)/x. See __notes.md first.\n",
                     "other.md": "Uses my__start.py and __start.pyc, not the prep file.\n"}
            for name, text in files.items():
                (src / name).parent.mkdir(parents=True, exist_ok=True)
                (src / name).write_text(text)
            git = lambda *a: subprocess.run(["git", "-C", str(src), *a], env=env, check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            git("add", "-A")
            git("commit", "-q", "-m", "prep")
            r = subprocess.run(["bash", str(HERE / "make-run-repo.sh"), str(src), str(dest), "--rewrite",
                                "/srv/c++ (run)"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertEqual((dest / "README.md").read_text(),
                             f"Work in {dest}/x and /srv/cc (run)/x. See __notes.md first.\n")
            self.assertIn("LEFTOVER", r.stdout)
            self.assertIn("README.md:1:", r.stdout)
            self.assertNotIn("other.md", r.stdout)
            self.assertNotIn("warning", r.stderr)


if __name__ == "__main__":
    unittest.main()
