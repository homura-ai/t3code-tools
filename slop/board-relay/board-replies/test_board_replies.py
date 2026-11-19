"""Offline end-to-end tests: a fake Wekan API on 127.0.0.1 and a fake t3 script.

Nothing real is contacted: no real board, no real T3 thread, no real state.
Run: python3 test_board_replies.py
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = Path(__file__).with_name("board-replies.py")
BOARD, AGENTS, HUMAN = "B1", "UA", "UH"


class FakeWekan:
    def __init__(self):
        self.lists = [{"_id": "L1", "title": "Inbox"}, {"_id": "L2", "title": "Done"}]
        self.cards = {"L1": [{"_id": "C1", "title": "Builder: ship it?", "userId": AGENTS},
                             {"_id": "C2", "title": "no prefix here", "userId": HUMAN}], "L2": []}
        self.card_gets = []
        self.comments = {"C1": [{"_id": "M1", "authorId": HUMAN, "comment": "Yes, ship it."},
                                {"_id": "M0", "authorId": AGENTS, "comment": "agent note"}],
                         "C2": [{"_id": "M2", "authorId": HUMAN, "comment": "hello"}]}
        self.posts = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = self.path.strip("/").split("/")  # api boards B1 ...
                if parts[3:] == ["lists"]:
                    return self.reply(fake.lists)
                if parts[3] == "lists" and parts[5] == "cards" and len(parts) == 7:
                    card = next((c for c in fake.cards.get(parts[4], []) if c["_id"] == parts[6]), None)
                    fake.card_gets.append(parts[6])
                    return self.reply(card) if card else self.send_error(404)
                if parts[3] == "lists" and parts[5] == "cards":
                    return self.reply([{k: v for k, v in c.items() if k != "userId"}
                                       for c in fake.cards.get(parts[4], [])])
                if parts[3] == "cards" and parts[5] == "comments":
                    return self.reply(fake.comments.get(parts[4], []))
                self.send_error(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.posts.append((self.path, body))
                self.reply({"_id": "new"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class BoardRepliesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.wekan = FakeWekan()
        self.addCleanup(self.wekan.close)
        project = self.tmp / "project"
        project.mkdir()
        (project / "threads.tsv").write_text("Builder\tthread-123\n")
        self.sent = self.tmp / "sent.jsonl"
        t3 = self.tmp / "t3"
        t3.write_text("#!/usr/bin/python3\nimport json,os,sys\n"
                      "if os.environ.get('FAKE_T3_FAIL'): sys.exit('fake failure')\n"
                      f"open({str(self.sent)!r},'a').write(json.dumps({{'argv':sys.argv[1:],'stdin':sys.stdin.read()}})+'\\n')\n")
        t3.chmod(0o700)
        self.state_dir = self.tmp / "state"
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({
            "api": self.wekan.url, "board": BOARD, "agents_user": AGENTS,
            "usernames": {HUMAN: "alex"}, "card_url": "https://board.test/b/{board}/x/{card}",
            "t3": str(t3), "project": str(project), "state_dir": str(self.state_dir),
            "retry_seconds": 0, "timezone": "UTC"}))
        self.env = {**os.environ}
        self.env.pop("FAKE_T3_FAIL", None)

    def run_relay(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--config", str(self.config), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def sends(self):
        return [json.loads(l) for l in self.sent.read_text().splitlines()] if self.sent.exists() else []

    def test_dry_run_sends_and_writes_nothing(self):
        r = self.run_relay("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WOULD SEND Builder thread-123", r.stdout)
        self.assertIn("WOULD COMMENT C2", r.stdout)
        self.assertIn("Yes, ship it.", r.stdout)
        self.assertNotIn("agent note", r.stdout)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.wekan.posts, [])
        self.assertFalse(self.state_dir.exists())

    def test_delivers_once_and_marks_unprefixed_card(self):
        r = self.run_relay("--once", "--deliver-existing")
        self.assertEqual(r.returncode, 0, r.stderr)
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["argv"], ["thread", "send", "--mode", "queue", "thread-123", "-"])
        self.assertIn("alex commented", sends[0]["stdin"])
        self.assertIn("https://board.test/b/B1/x/C1", sends[0]["stdin"])
        state = json.loads((self.state_dir / "board-replies.json").read_text())
        self.assertIn("M1", state["delivered"])
        self.assertIn("C2", state["no_prefix_noticed"])
        self.assertEqual([p for p, _ in self.wekan.posts], [f"/api/boards/{BOARD}/cards/C2/comments"])
        # A second pass delivers nothing new.
        self.assertEqual(self.run_relay("--once").returncode, 0)
        self.assertEqual(len(self.sends()), 1)

    def test_failed_send_posts_notice_then_recovery(self):
        self.env["FAKE_T3_FAIL"] = "1"
        self.assertEqual(self.run_relay("--once", "--deliver-existing").returncode, 0)
        notes = [b["comment"] for p, b in self.wekan.posts if "/C1/" in p]
        self.assertTrue(notes and "couldn't deliver" in notes[0])
        state = json.loads((self.state_dir / "board-replies.json").read_text())
        self.assertNotIn("M1", state["delivered"])
        del self.env["FAKE_T3_FAIL"]
        self.assertEqual(self.run_relay("--once").returncode, 0)
        self.assertEqual(len(self.sends()), 1)
        notes = [b["comment"] for p, b in self.wekan.posts if "/C1/" in p]
        self.assertIn("has now been delivered", notes[-1])

    def test_no_state_refuses_to_start(self):
        r = self.run_relay("--once")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("REFUSING", r.stdout)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.wekan.posts, [])
        self.assertFalse((self.state_dir / "board-replies.json").exists())

    def test_seed_sends_nothing_then_only_new_comments_go(self):
        r = self.run_relay("--seed")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 existing comments marked delivered, 1 unprefixed cards", r.stdout)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.wekan.posts, [])
        state = json.loads((self.state_dir / "board-replies.json").read_text())
        self.assertEqual(sorted(state["delivered"]), ["M1", "M2"])
        self.wekan.comments["C1"].append({"_id": "M3", "authorId": HUMAN, "comment": "One more thing."})
        self.assertEqual(self.run_relay("--once").returncode, 0)
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        self.assertIn("One more thing.", sends[0]["stdin"])
        self.assertNotIn("Yes, ship it.", sends[0]["stdin"])
        self.assertEqual(self.wekan.posts, [])  # C2 was marked noticed by --seed

    def test_bad_card_url_refuses(self):
        cfg = json.loads(self.config.read_text())
        cfg["card_url"] = "https://board.test/b/{}/x"
        self.config.write_text(json.dumps(cfg))
        r = self.run_relay("--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("card_url", r.stderr)

    def test_only_agent_cards_skips_human_cards(self):
        cfg = json.loads(self.config.read_text())
        cfg["only_agent_cards"] = True
        self.config.write_text(json.dumps(cfg))
        # A human's own card with a colon in its title: not an agent card, so no relay and no notice.
        self.wekan.cards["L1"].append({"_id": "C3", "title": "Taxes: file by April", "userId": HUMAN})
        self.wekan.comments["C3"] = [{"_id": "M4", "authorId": HUMAN, "comment": "note to self"}]
        r = self.run_relay("--once", "--deliver-existing")
        self.assertEqual(r.returncode, 0, r.stderr)
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        self.assertIn("Yes, ship it.", sends[0]["stdin"])
        self.assertEqual(self.wekan.posts, [])  # no notice on C2 or C3
        # Authors are fetched once per card, not on every pass.
        self.assertEqual(self.run_relay("--once").returncode, 0)
        self.assertEqual(sorted(self.wekan.card_gets), ["C1", "C1", "C2", "C2", "C3", "C3"])  # 2 processes
        # In an any_author_lists list, human cards are handled again (C2 gets its notice).
        cfg["any_author_lists"] = ["L1"]
        self.config.write_text(json.dumps(cfg))
        self.assertEqual(self.run_relay("--once").returncode, 0)
        self.assertIn(f"/api/boards/{BOARD}/cards/C2/comments", [p for p, _ in self.wekan.posts])

    def test_missing_config_fields_refuse(self):
        self.config.write_text("{}")
        r = self.run_relay("--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing board, agents_user, project", r.stderr)


if __name__ == "__main__":
    unittest.main()
