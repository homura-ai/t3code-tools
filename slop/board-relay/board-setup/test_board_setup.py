"""Offline tests for board_setup.py against a fake Wekan API on 127.0.0.1.

Nothing real is contacted. Run: python3 test_board_setup.py
"""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("board_setup", HERE / "board_setup.py")
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

ME, OLD = "UAGENTS", "OLDBOARD"


class FakeWekan:
    """Just enough of Wekan's REST API for board_setup.py. Records every request."""

    def __init__(self):
        self.requests = []
        self.n = 0
        self.boards = {OLD: {"_id": OLD, "title": "Old board", "slug": "old-board", "labels": [],
                             "members": [{"userId": ME, "isAdmin": False}]}}
        self.lists = {OLD: [{"_id": "OL1", "title": "Human"}, {"_id": "OL2", "title": "Done"}]}
        self.cards = {"OL1": [{"_id": "OC1", "title": "Builder: which branch?", "description": "old desc",
                               "userId": ME, "createdAt": "2025-01-03T00:00:00Z"}],
                      "OL2": [{"_id": "OC2", "title": "Done thing", "userId": ME}]}
        self.comments = {"OC1": [{"_id": "M1", "authorId": "UHUMAN", "comment": "stop the other one"}]}
        self.fields, self.swimlanes, self.rules, self.settings = {}, {}, {}, {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n) or b"null")

            def handle_any(self, method):
                b = self.body() if method != "GET" else None
                fake.requests.append((method, self.path, b))
                p = self.path[len("/api"):].strip("/").split("/")
                try:
                    out = fake.route(method, p, b)
                except KeyError:
                    return self.reply({"error": "Not found"}, 404)
                self.reply(out)

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PUT(self):
                self.handle_any("PUT")

            def do_DELETE(self):
                self.handle_any("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def new(self, prefix):
        self.n += 1
        return f"{prefix}{self.n}"

    def route(self, m, p, b):
        if p == ["user"]:
            return {"_id": ME, "username": "agents"}
        if p[0] == "users" and p[2:] == ["boards"]:
            return [{"_id": k, "title": v["title"]} for k, v in self.boards.items()]
        if p == ["boards"] and m == "POST":
            bid, sid = self.new("B"), self.new("S")
            self.boards[bid] = {"_id": bid, "title": b["title"], "color": b["color"], "slug": "new-board",
                                "labels": [{"_id": "dflt", "name": "", "color": "green"}], "members": [{"userId": ME}]}
            self.lists[bid] = []
            self.swimlanes[sid] = {"_id": sid, "title": "Default", "boardId": bid}
            return {"_id": bid, "defaultSwimlaneId": sid}
        bid = p[1]
        board = self.boards[bid]
        rest = p[2:]
        if not rest and m == "GET":
            return board
        if rest == ["lists"]:
            if m == "GET":
                return self.lists[bid]
            lid = self.new("L")
            self.lists[bid].append({"_id": lid, "title": b["title"], "swimlaneId": b.get("swimlaneId")})
            self.cards[lid] = []
            return {"_id": lid}
        if rest[0] == "lists" and len(rest) == 2 and m == "PUT":
            l = next(x for x in self.lists[bid] if x["_id"] == rest[1])
            l.update(b)
            return {"_id": rest[1]}
        if rest[0] == "lists" and rest[2:] == ["cards"]:
            if m == "GET":
                return [{"_id": c["_id"], "title": c["title"]} for c in self.cards[rest[1]]]
            cid = self.new("C")
            self.cards[rest[1]].append({"_id": cid, "userId": ME, **b})
            return {"_id": cid}
        if rest[0] == "lists" and rest[2] == "cards" and len(rest) == 4:
            c = next(x for x in self.cards[rest[1]] if x["_id"] == rest[3])
            if m == "GET":
                return c
            c.update(b)
            return {"_id": c["_id"]}
        if rest[0] == "swimlanes" and m == "PUT":
            self.swimlanes[rest[1]]["title"] = b["title"]
            return {"_id": rest[1]}
        if rest[0] == "members":
            board["members"].append({"userId": rest[1], **{k: v for k, v in b.items() if k != "action"}})
            return [{"_id": bid}]
        if rest == ["labels"]:
            lid = self.new("lab")
            board["labels"].append({"_id": lid, **b["label"]})
            return lid
        if rest[0] == "custom-fields":
            if len(rest) == 1 and m == "GET":
                return [f for f in self.fields.values() if f.get("boardId") == bid]
            if len(rest) == 1:
                fid = self.new("F")
                self.fields[fid] = {"_id": fid, "boardId": bid, **b}
                return {"_id": fid}
            f = self.fields[rest[1]]
            if m == "GET":
                return f
            f["settings"]["dropdownItems"] += [{"_id": self.new("i"), "name": n} for n in b["items"]]
            return {"_id": rest[1]}
        if rest in (["cardSettings"], ["boardViewSettings"]):
            self.settings[(bid, rest[0])] = b
            return b
        if rest[0] == "rules":
            if len(rest) == 1 and m == "GET":
                return [r for r in self.rules.values() if r["boardId"] == bid]
            if len(rest) == 1:
                rid = self.new("R")
                self.rules[rid] = dict(b, boardId=bid)
                return {"_id": rid}
            self.rules[rest[1]].update(b)
            return {"_id": rest[1]}
        if rest[0] == "cards" and rest[2] == "comments":
            if m == "GET":
                return self.comments.get(rest[1], [])
            self.comments.setdefault(rest[1], []).append(b)
            return {"_id": self.new("M")}
        if rest[0] == "cards" and rest[2] == "checklists":
            return {"_id": self.new("K")}
        if rest == ["swimlanes"]:
            return [s for s in self.swimlanes.values() if s["boardId"] == bid] or [{"_id": "OS", "title": "Default"}]
        if rest == ["integrations"]:
            return []
        if rest == ["cards_count"]:
            return {"board_cards_count": sum(len(self.cards[l["_id"]]) for l in self.lists[bid])}
        raise KeyError(p)

    def writes(self):
        return [(m, p) for m, p, _ in self.requests if m != "GET"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def example_config(api):
    cfg = json.loads((HERE / "board.example.json").read_text())
    cfg["api"] = api
    cfg["copy"]["from_board"] = OLD
    cfg["copy"]["cards"] = [{"id": "OC1", "list": "inbox", "labels": ["src-agent", "p2"],
                             "fields": {"kind": "Decision"}, "comments": "digest"}]
    return cfg


class BoardSetupTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWekan()
        self.addCleanup(self.fake.close)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))

    def run_create(self, cfg, *extra):
        path = self.tmp / "cfg.json"
        path.write_text(json.dumps(cfg))
        out = self.tmp / "ids.json"
        r = subprocess.run([sys.executable, str(HERE / "board_setup.py"), "create", "--config", str(path),
                            "--out", str(out), *extra], capture_output=True, text=True, timeout=60)
        return r, out

    def test_example_configs_validate(self):
        cfg = bs.load_config(HERE / "board.example.json")
        self.assertEqual(bs.validate(cfg), [])

    def test_validate_catches_mistakes(self):
        cfg = bs.load_config(HERE / "board.example.json")
        bad = copy.deepcopy(cfg)
        bad["color"] = "neon"
        bad["labels"][0]["color"] = "chartreuse"
        bad["custom_fields"][0]["type"] = "emoji"
        bad["rules"][0]["trigger"]["list"] = "nope"
        bad["templates"]["cards"][0]["labels"] = ["missing"]
        bad["card_settings"]["allowsTeleport"] = True
        bad["board_views"]["show"].append("board-view-hologram")
        errs = "\n".join(bs.validate(bad))
        for needle in ("color 'neon'", "chartreuse", "emoji", "'nope'", "'missing'", "allowsTeleport",
                       "board-view-hologram"):
            self.assertIn(needle, errs)

    def test_create_builds_board_and_never_writes_the_source(self):
        r, out = self.run_create(example_config(self.fake.url))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        ids = json.loads(out.read_text())
        self.assertTrue(ids["complete"])
        bid = ids["board"]["id"]
        # Every write targets the new board (or creates it); the source board is only read.
        for m, p in self.fake.writes():
            self.assertTrue(p == "/api/boards" or p.startswith(f"/api/boards/{bid}/"), (m, p))
        self.assertFalse(any(OLD in p for _, p in self.fake.writes()))
        self.assertEqual(self.fake.boards[OLD]["title"], "Old board")
        self.assertEqual(self.fake.cards["OL1"][0]["title"], "Builder: which branch?")
        # Lists in config order with colors and the WIP limit.
        lists = self.fake.lists[bid]
        self.assertEqual([l["title"] for l in lists],
                         ["Inbox", "Agent asks", "Waiting on me", "Doing", "Done", "Templates"])
        self.assertEqual(lists[3]["wipLimit"], {"value": 5, "enabled": True, "soft": True})
        self.assertEqual(lists[0]["color"], "sky")
        self.assertEqual(self.fake.swimlanes[ids["swimlane"]["id"]]["title"], "Main")
        # Labels, members, dropdown items mapped to ids.
        self.assertEqual(len(ids["labels"]), 8)
        self.assertIn({"userId": "HUMAN_USER_ID", "isAdmin": True, "isNoComments": False, "isCommentOnly": False,
                       "isWorker": False, "isReadOnly": False}, self.fake.boards[bid]["members"])
        kind = ids["custom_fields"]["kind"]
        self.assertEqual(sorted(kind["items"]), sorted(["Decision", "Blocker", "Result", "Task", "Idea"]))
        # Rules: list keys became list titles, label keys became label ids, boardId filled in.
        rules = list(self.fake.rules.values())
        self.assertEqual(len(rules), 5)
        doing = next(x for x in rules if x["title"].startswith("Doing"))
        self.assertEqual(doing["trigger"]["listName"], "Doing")
        self.assertEqual(doing["trigger"]["boardId"], bid)
        lab = next(x for x in rules if x["action"]["actionType"] == "addLabel")
        self.assertEqual(lab["action"]["labelId"], ids["labels"]["src-agent"]["id"])
        out_of_done = next(x for x in rules if x["title"].startswith("Out of Done"))
        self.assertEqual(out_of_done["trigger"]["oldListName"], "Done")
        self.assertNotIn("listName", out_of_done["trigger"])
        # Views: hide_others hid every view not in show.
        views = self.fake.settings[(bid, "boardViewSettings")]
        self.assertEqual(views["defaultPrivateBoardView"], "board-view-lists")
        self.assertFalse(views["boardViewSettings"]["board-view-gantt"]["showOnPrivate"])
        self.assertTrue(views["boardViewSettings"]["board-view-table"]["showOnPrivate"])
        self.assertEqual(self.fake.settings[(bid, "cardSettings")]["allowsPoker"], False)
        # Templates: 5 cards in Templates, with labels and the dropdown value as an item id.
        tpl_list = lists[5]["_id"]
        self.assertEqual(len(self.fake.cards[tpl_list]), 5)
        first = self.fake.cards[tpl_list][0]
        self.assertEqual(first["labelIds"], [ids["labels"]["src-agent"]["id"], ids["labels"]["p2"]["id"]])
        self.assertEqual(first["customFields"], [{"_id": kind["id"], "value": kind["items"]["Decision"]}])
        # Copy: one card in Inbox with the old description, a back link, and a comment digest.
        inbox = self.fake.cards[lists[0]["_id"]]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["title"], "Builder: which branch?")
        self.assertIn("old desc", inbox[0]["description"])
        self.assertIn("The original was left unchanged", inbox[0]["description"])
        digest = self.fake.comments[inbox[0]["_id"]][0]["comment"]
        self.assertIn("stop the other one", digest)
        self.assertEqual(ids["copied"][0]["from"], "OC1")
        self.assertEqual(ids["default_labels"], [{"id": "dflt", "name": "", "color": "green"}])

    def test_dry_run_writes_nothing(self):
        r, out = self.run_create(example_config(self.fake.url), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.fake.writes(), [])
        self.assertFalse(out.exists())
        self.assertIn("DRY-RUN POST /boards", r.stdout)
        # The copy source was still read, so a bad card id shows up in a dry run.
        self.assertIn(("GET", "/api/boards/OLDBOARD/cards/OC1/comments", None), self.fake.requests)

    def test_same_title_refused(self):
        cfg = example_config(self.fake.url)
        cfg["title"] = "Old board"
        r, _ = self.run_create(cfg)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(self.fake.writes(), [])

    def test_bad_copy_source_stops_before_creating(self):
        cfg = example_config(self.fake.url)
        cfg["copy"]["cards"][0]["id"] = "OC2-not-open"
        r, _ = self.run_create(cfg)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.fake.writes(), [])

    def test_write_guard(self):
        w = bs.Wekan(self.fake.url, log=lambda *a: None)
        with self.assertRaises(bs.WriteRefused):  # read-only by default
            w.call("POST", "/boards", {"title": "x"})
        w.read_only = False
        w.writable.add("NEW")
        with self.assertRaises(bs.WriteRefused):
            w.call("PUT", f"/boards/{OLD}/title", {"title": "x"})
        with self.assertRaises(bs.WriteRefused):
            w.call("POST", "/boards", {"title": "x"})  # board creation is allowed only once, inside create()
        with self.assertRaises(bs.WriteRefused):
            w.call("POST", "/boards/NEWER/lists", {"title": "x"})  # prefix of another id
        self.assertEqual(self.fake.writes(), [])

    def test_inventory_is_read_only(self):
        w = bs.Wekan(self.fake.url, log=lambda *a: None)
        inv = bs.inventory(w, OLD)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual([c["open"] for c in inv["cards"]], [True, False])
        self.assertEqual(inv["cards"][0]["comments"], 1)
        self.assertEqual(inv["cards_count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
