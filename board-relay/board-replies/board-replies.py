#!/usr/bin/env python3
"""Deliver human comments on a Wekan board to the T3 thread of the agent that posted the card.

Every few seconds it reads every card in the board's lists and each card's
comments. A comment whose author is not the agents user and whose id is not in
the state file is new: it is sent to the agent named before the first colon of
the card title. Delivered comment ids are saved (atomically) only after a
successful send, so each comment is delivered exactly once across restarts.

  --config PATH  JSON settings (default $BOARD_RELAY_CONFIG or
                 ~/.config/board-relay/config.json); see config.example.json
  --dry-run      one pass: print what would be sent; no sends, no card comments,
                 no state writes, no lock.
  --once         one real pass, then exit (for testing and cron-style use).
  --seed         record every human comment now on the board as delivered (and every
                 card without a name prefix as noticed), sending and posting nothing;
                 then exit. Run it once before the first real start on a board that
                 already has comments.
  --deliver-existing  start with no state file and deliver every comment already there.

With no readable state file a real run refuses to start (exit 2) unless --seed or
--deliver-existing is given: otherwise every old comment on the board would be sent
to agent threads again.
"""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULTS = {
    "api": "http://127.0.0.1:8090/api",  # a local proxy may add the agents' bearer token
    "token_file": None,     # optional file holding a Wekan API token (sent as a Bearer header)
    "board": None,          # required: board id
    "lists": {},            # {list id: name}; empty means every list on the board
    "only_agent_cards": False,  # true: skip cards a human created (author is not agents_user)...
    "any_author_lists": [],     # ...except in these list ids, where every card is handled
    "agents_user": None,    # required: user id agents post as; its comments are not relayed
    "usernames": {},        # {user id: display name} for the message text
    "card_url": "",         # e.g. "https://board.example:8445/b/{board}/board/{card}"
    "t3": "~/.t3/bin/t3",
    "project": None,        # required: T3 project directory whose threads receive comments
    "threads_tsv": None,    # optional "name<TAB>threadId" file; default <project>/threads.tsv
    "state_dir": "~/.local/state/board-replies",
    "poll_seconds": 5,
    "retry_seconds": 30,    # wait between delivery attempts for one undelivered comment
    "timezone": None,       # e.g. "America/New_York"; default: the system's local zone
    "reply_hint": ("Act on this, and move the card to the right list yourself through the "
                   "board API; you can answer by commenting on the card."),
}
REQUIRED = ("board", "agents_user", "project")
CFG = dict(DEFAULTS)
PROJECT = THREADS_TSV = T3 = API = BOARD = AGENTS_USER = CARD_URL = TOKEN = None
LISTS, USERNAMES = {}, {}
ONLY_AGENT_CARDS, ANY_AUTHOR_LISTS = False, set()
AUTHORS = {}  # card id -> the user who created it (never changes, so it is fetched once per card)
STATE_DIR = STATE = LOCK = None
POLL_SECONDS = RETRY_SECONDS = 0
TZ = None


def configure(path):
    """Load the JSON config over DEFAULTS and set the module settings."""
    global PROJECT, THREADS_TSV, T3, API, BOARD, AGENTS_USER, CARD_URL, TOKEN, LISTS, USERNAMES
    global STATE_DIR, STATE, LOCK, POLL_SECONDS, RETRY_SECONDS, TZ, ONLY_AGENT_CARDS, ANY_AUTHOR_LISTS
    try:
        with open(path, encoding="utf-8") as f:
            CFG.update(json.load(f))
    except FileNotFoundError:
        sys.exit(f"board-relay: no config at {path} (copy config.example.json there)")
    missing = [k for k in REQUIRED if not CFG.get(k)]
    if missing:
        sys.exit(f"board-relay: {path} is missing {', '.join(missing)}")
    x = os.path.expanduser
    PROJECT = x(CFG["project"])
    THREADS_TSV = x(CFG["threads_tsv"]) if CFG["threads_tsv"] else os.path.join(PROJECT, "threads.tsv")
    T3, API, BOARD = x(CFG["t3"]), CFG["api"].rstrip("/"), CFG["board"]
    AGENTS_USER, LISTS = CFG["agents_user"], dict(CFG["lists"])
    USERNAMES = {AGENTS_USER: "agents", **CFG["usernames"]}
    CARD_URL = CFG["card_url"]
    ONLY_AGENT_CARDS, ANY_AUTHOR_LISTS = bool(CFG["only_agent_cards"]), set(CFG["any_author_lists"])
    TOKEN = open(x(CFG["token_file"]), encoding="utf-8").read().strip() if CFG["token_file"] else None
    STATE_DIR = x(CFG["state_dir"])
    STATE = os.path.join(STATE_DIR, "board-replies.json")
    LOCK = os.path.join(STATE_DIR, "board-replies.lock")
    POLL_SECONDS, RETRY_SECONDS = CFG["poll_seconds"], CFG["retry_seconds"]
    TZ = ZoneInfo(CFG["timezone"]) if CFG["timezone"] else None
    if CARD_URL:
        try:
            CARD_URL.format(board="b", card="c")
        except (KeyError, IndexError, ValueError):
            sys.exit(f"board-relay: card_url may only use {{board}} and {{card}}: {CARD_URL}")


def local_now():
    return datetime.now(TZ) if TZ else datetime.now().astimezone()


def log(*parts):
    print(local_now().strftime("%H:%M:%S"), *parts, flush=True)


def short(text, n=80):
    return " ".join(str(text).split())[:n]


# ---------- Wekan API ----------

def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def board_lists():
    if LISTS:
        return LISTS
    return {l["_id"]: l.get("title", l["_id"]) for l in api("GET", f"/boards/{BOARD}/lists")}


def card_author(list_id, card_id):
    if card_id not in AUTHORS:
        card = api("GET", f"/boards/{BOARD}/lists/{list_id}/cards/{card_id}") or {}
        AUTHORS[card_id] = card.get("userId")
    return AUTHORS[card_id]


def board_cards():
    """Yield (card, list name) for every card in the configured (or all) lists.

    With only_agent_cards, a card a human created is skipped (no relay, no notices)
    unless it is in one of any_author_lists: on a board shared with a person's own
    cards, only the agents' cards are the relay's business."""
    for list_id, list_name in board_lists().items():
        for card in api("GET", f"/boards/{BOARD}/lists/{list_id}/cards"):
            if (ONLY_AGENT_CARDS and list_id not in ANY_AUTHOR_LISTS
                    and card_author(list_id, card["_id"]) != AGENTS_USER):
                continue
            yield card, list_name


def card_comments(card_id):
    return api("GET", f"/boards/{BOARD}/cards/{card_id}/comments")


def comment_on_card(card_id, text):
    api("POST", f"/boards/{BOARD}/cards/{card_id}/comments",
        {"authorId": AGENTS_USER, "comment": text})


# ---------- T3 ----------

def t3(args, stdin=None):
    return subprocess.run([T3] + args, cwd=PROJECT, input=stdin, capture_output=True,
                          text=True, timeout=30)


def find_thread(name):
    """Return (threadId, None) or (None, reason). Never cached: agents may be restarted."""
    try:
        with open(THREADS_TSV, encoding="utf-8") as f:
            found = None
            for line in f:
                fields = line.rstrip("\n").split("\t")
                if len(fields) >= 2 and fields[0].strip() == name and fields[1].strip():
                    found = fields[1].strip()
            if found:
                return found, None
    except FileNotFoundError:
        pass
    except OSError as e:
        log("WARN cannot read", THREADS_TSV, e)
    try:
        r = t3(["thread", "list", "--project", PROJECT, "--json"])
    except Exception as e:
        return None, f"t3 thread list failed: {e}"
    if r.returncode != 0:
        return None, "t3 thread list failed: " + short((r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1], 200)
    try:
        threads = json.loads(r.stdout)
    except ValueError:
        return None, "t3 thread list printed invalid JSON"
    # The title equals the name, starts with "<name>:" or "<name> ", or starts with
    # "You are <name>."; newest createdAt wins; no status filter (idle agents show completed).
    def matches(title):
        return (title == name or title.startswith(name + ":") or title.startswith(name + " ")
                or title.startswith(f"You are {name}."))
    hits = [t for t in threads if matches((t.get("title") or "").strip())]
    if hits:
        return max(hits, key=lambda t: t.get("createdAt") or "")["threadId"], None
    return None, f'no T3 thread titled "{name}" in {THREADS_TSV} or the {PROJECT} thread list'


def send(thread_id, message):
    """Queue message to the thread. Returns None on success, else the failure reason."""
    reason = None
    for attempt in range(3):
        if attempt:
            time.sleep(2)
        try:
            r = t3(["thread", "send", "--mode", "queue", thread_id, "-"], stdin=message)
        except Exception as e:
            reason = f"t3 thread send failed: {e}"
            continue
        if r.returncode == 0:
            return None
        last = (r.stderr.strip().splitlines() or [r.stdout.strip() or f"exit {r.returncode}"])[-1]
        reason = "t3 thread send failed: " + short(last, 200)
    return reason


# ---------- state ----------

def load_state():
    """Return (state, ok); ok is False when the state file is missing or unreadable."""
    state = {"delivered": {}, "fail_noticed": {}, "no_prefix_noticed": {}, "recovery_pending": {}}
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        for k in state:
            if isinstance(data.get(k), dict):
                state[k] = data[k]
        return state, True
    except FileNotFoundError:
        log("no state file at", STATE)
    except (OSError, ValueError) as e:
        log("WARN state file unreadable:", e)
    return state, False


def seed(state):
    """Mark everything now on the board as handled, without sending or posting anything."""
    comments = cards = 0
    for card, _ in list(board_cards()):
        card_id, title = card["_id"], card.get("title", "")
        name = agent_name(title)
        if name is None and card_id not in state["no_prefix_noticed"]:
            state["no_prefix_noticed"][card_id] = title
            cards += 1
        for c in card_comments(card_id):
            if c.get("authorId") != AGENTS_USER and c["_id"] not in state["delivered"]:
                state["delivered"][c["_id"]] = {"card": card_id, "agent": name, "thread": None, "seeded": True,
                                                "at": local_now().isoformat(timespec="seconds")}
                comments += 1
    save_state(state)
    log(f"seeded: {comments} existing comments marked delivered, {cards} unprefixed cards marked noticed; "
        f"nothing sent")


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


# ---------- main loop ----------

def agent_name(title):
    name, sep, _ = (title or "").partition(":")
    name = name.strip()
    return name if sep and name else None


def build_message(who, comment, title, list_name, card_id):
    return (
        f"{who} commented on your board card.\n\n"
        f"Card: {title}\n"
        f"List: {list_name}\n"
        f"Card id: {card_id}\n"
        + (f"Link: {CARD_URL.format(board=BOARD, card=card_id)}\n" if CARD_URL else "")
        + f"\nComment from {who}, word for word:\n"
        f"{comment}\n\n"
        + CFG["reply_hint"]
    )


def post_recovery_notices(state):
    for cid, r in list(state["recovery_pending"].items()):
        try:  # a failing notice must not block deliveries; it is retried next pass
            comment_on_card(r["card"], f"board-replies: {r['who']}'s comment {cid} has now been delivered to "
                                       f"agent \"{r['agent']}\"; ignore the earlier couldn't-deliver notice for it.")
        except Exception as e:
            log("WARN recovery notice for comment", cid, "not posted yet:", e)
            continue
        del state["recovery_pending"][cid]
        save_state(state)


def run_pass(state, next_try, last_reason, dry_run):
    now = time.monotonic()
    if not dry_run:
        post_recovery_notices(state)
    for card, list_name in list(board_cards()):
        card_id, title = card["_id"], card.get("title", "")
        comments = [c for c in card_comments(card_id)
                    if c.get("authorId") != AGENTS_USER and c["_id"] not in state["delivered"]]
        name = agent_name(title)
        if name is None:
            if card_id not in state["no_prefix_noticed"]:
                note = ('board-replies: this card title has no "Agent Name:" prefix, so comments on it '
                        'cannot be delivered to an agent. Retitle it "<agent name>: <matter>" and new '
                        'and earlier undelivered comments will be delivered.')
                if dry_run:
                    log("WOULD COMMENT", card_id, short(note))
                else:
                    comment_on_card(card_id, note)
                    state["no_prefix_noticed"][card_id] = title
                    save_state(state)
                    log("NO PREFIX", card_id, short(title))
            continue
        for c in comments:
            cid = c["_id"]
            if next_try.get(cid, 0) > now:
                continue
            who = USERNAMES.get(c.get("authorId"), "user " + str(c.get("authorId")))
            message = build_message(who, c.get("comment", ""), title, list_name, card_id)
            thread_id, reason = find_thread(name)
            if dry_run:
                log("WOULD SEND", name, thread_id or "NOT FOUND", short(message))
                if reason:
                    log("  (would fail:", reason + ")")
                print(message, flush=True)
                continue
            if thread_id:
                reason = send(thread_id, message)
            if reason is None:
                state["delivered"][cid] = {"card": card_id, "agent": name, "thread": thread_id,
                                           "at": local_now().isoformat(timespec="seconds")}
                if state["fail_noticed"].pop(cid, None) is not None:
                    state["recovery_pending"][cid] = {"card": card_id, "who": who, "agent": name}
                save_state(state)  # delivery is recorded before any board call that could fail
                next_try.pop(cid, None)
                last_reason.pop(cid, None)
                log("SENT", name, thread_id, short(message))
                post_recovery_notices(state)
                continue
            next_try[cid] = now + RETRY_SECONDS
            if last_reason.get(cid) != reason:  # log only when the reason changes, not every retry
                log("FAILED", name, thread_id or "NOT FOUND", f"comment {cid} on {card_id}:", reason,
                    f"(retrying every {RETRY_SECONDS}s)")
                last_reason[cid] = reason
            if cid not in state["fail_noticed"]:
                note = (f"board-replies: couldn't deliver {who}'s comment {cid} to agent \"{name}\": {reason}. "
                        f"It will keep retrying and deliver it once the agent's thread is reachable.")
                comment_on_card(card_id, note)
                state["fail_noticed"][cid] = reason
                save_state(state)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.environ.get(
        "BOARD_RELAY_CONFIG", os.path.expanduser("~/.config/board-relay/config.json")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--deliver-existing", action="store_true")
    args = ap.parse_args()
    if args.seed and (args.dry_run or args.deliver_existing):
        ap.error("--seed cannot be combined with --dry-run or --deliver-existing")
    configure(args.config)
    dry_run = args.dry_run
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if dry_run:
        state, ok = load_state()
        if not ok:
            log("NOTE: with no state file a real run refuses to start; use --seed (mark what is there "
                "as delivered) or --deliver-existing (send all of it, as below)")
        run_pass(state, {}, {}, True)
        return 0
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("already running", flush=True)
        return 3
    state, ok = load_state()
    if args.seed:
        seed(state)
        return 0
    if not ok:
        if not args.deliver_existing:
            log(f"REFUSING to start without state at {STATE}: every comment on the board would be "
                f"delivered again. Run once with --seed (mark existing comments delivered, send nothing) "
                f"or --deliver-existing (send them all).")
            return 2
        save_state(state)  # from now on the state file exists, so later starts need no flag
    log(f"started: board {BOARD}, project {PROJECT}, {len(state['delivered'])} comments already delivered")
    next_try, last_reason = {}, {}
    last_error = None
    while True:
        try:
            run_pass(state, next_try, last_reason, False)
            if args.once:
                return 0
            if last_error:
                log("recovered")
                last_error = None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if args.once:
                log("ERROR", err)
                return 1
            if err != last_error:
                log("ERROR", err)
                last_error = err
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
