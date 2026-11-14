#!/usr/bin/env python3
"""Build a Wekan board from a JSON config, or inventory a board, through Wekan's REST API.

  board_setup.py check     --config board.json
        Validate the config offline. Nothing is contacted.
  board_setup.py create    --config board.json [--dry-run] [--out ids.json] [--allow-same-title]
        Create a NEW board with the config's lists, labels, custom fields, card and view
        settings, rules, webhooks and template cards, then copy the listed cards onto it.
        --dry-run reads the API (and the source board of any copied card) but writes nothing.
        The ids of everything created go to --out (default ./board-ids.json), rewritten
        after every step, so a run that stops halfway still records what it made.
  board_setup.py inventory BOARD_ID [--json] [--config board.json]
        Read-only summary of an existing board: swimlanes, lists with card counts, labels,
        custom fields, rules, webhooks, members and every open card.

Safety: every request goes through Wekan.call(). In `create`, a write (any method but GET)
is allowed only for POST /boards (once, to create the board) and for paths under
/boards/<the board this run created>/. Anything else, including any write to the board
cards are copied from, raises WriteRefused before a request is made. `inventory` and
`check` make no writes at all.

Config: see board.example.json next to this file; every key is described there. Python 3.9+, standard library only.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

# Wekan v12 accepts these (models/boards.js, models/lists.js, models/customFields.js).
BOARD_COLORS = [
    "belize", "nephritis", "pomegranate", "pumpkin", "wisteria", "moderatepink", "strongcyan",
    "limegreen", "midnight", "dark", "relax", "corteza", "appleglasspastel", "clearblue",
    "cleargreen", "clearorange", "clearpink", "clearpurple", "clearred", "natural", "modern",
    "moderndark", "exodark", "cleandark", "cleanlight"]
LABEL_COLORS = [
    "white", "green", "yellow", "orange", "red", "purple", "blue", "sky", "lime", "pink", "black",
    "silver", "peachpuff", "crimson", "plum", "darkgreen", "slateblue", "magenta", "gold", "navy",
    "gray", "saddlebrown", "paleturquoise", "mistyrose", "indigo"]
FIELD_TYPES = ["text", "number", "date", "dropdown", "dropdownMultiSelect", "checkbox",
               "currency", "stringtemplate"]
BOARD_VIEWS = [
    "board-view-swimlanes", "board-view-lists", "board-view-table", "board-view-cal",
    "board-view-calendar-mode", "board-view-multiboard-cal", "board-view-time",
    "board-view-timeline", "board-view-stats", "board-view-group-by-assignee", "board-view-gantt",
    "board-view-gantt-frappe", "board-view-gantt-dhtmlx", "board-view-product-backlog",
    "board-view-sprints", "board-view-sprint-report", "board-view-velocity", "board-view-roadmap",
    "board-view-dashboard", "board-view-bigboard", "board-view-burndown", "board-view-burnup",
    "board-view-cumulative-flow", "board-view-control-chart", "board-view-cycle-time",
    "board-view-flow-efficiency", "board-view-lead-time", "board-view-throughput-histogram",
    "board-view-wip-run", "board-view-pulse", "board-view-aging-wip", "board-view-blocker-analysis",
    "board-view-monte-carlo", "board-view-process-behavior", "board-view-size-cycle-time",
    "board-view-map", "board-view-email", "board-view-chat", "board-view-version-control",
    "board-view-column-sets"]
# The card settings PUT /api/boards/:id/cardSettings accepts: these booleans plus 3 day counts.
CARD_SETTING_FLAGS = [
    "allowsSwimlaneDragging", "allowsListDragging", "allowsCardDragging",
    "allowsChecklistDragging", "allowsChecklistItemDragging", "allowsSubtaskDragging",
    "allowsMinicardCollapse", "labelsAboveTitleOnMinicard", "allowsChecklistDueDate",
    "allowsChecklistTitle", "allowsCardCounterList", "cardAging", "askForMoveReason",
    "allowsBoardMemberList", "allowsShowLists", "allowsAttachments", "allowsChecklists",
    "allowsComments", "allowsCommentsOnMinicard", "allowsDescriptionTitle",
    "allowsDescriptionText", "allowsActivities", "allowsLabels", "allowsCreator", "allowsAssignee",
    "allowsMembers", "allowsRequestedBy", "allowsCardSortingByNumber", "allowsCardNumber",
    "allowsAssignedBy", "allowsReceivedDate", "allowsSpentTime", "allowsStartDate",
    "allowsEndDate", "allowsDueDate", "allowsSubtasks", "allowsCreatorOnMinicard",
    "allowsAttachmentsOnMinicard", "allowsChecklistsOnMinicard", "allowsChecklistAtMinicard",
    "allowsCoverAttachmentOnMinicard", "allowsBadgeAttachmentOnMinicard",
    "allowsCardSortingByNumberOnMinicard", "allowsCardNumberOnMinicard",
    "allowsDescriptionTitleOnMinicard", "allowsDescriptionTextOnMinicard",
    "allowsLabelsOnMinicard", "allowsAssigneeOnMinicard", "allowsMembersOnMinicard",
    "allowsRequestedByOnMinicard", "allowsAssignedByOnMinicard", "allowsReceivedDateOnMinicard",
    "allowsSpentTimeOnMinicard", "allowsStartDateOnMinicard", "allowsEndDateOnMinicard",
    "allowsDueDateOnMinicard", "allowsSubtasksOnMinicard", "allowsShowListsOnMinicard",
    "allowsSwimlaneNameOnMinicard", "showLabelText", "allowsStickers", "allowsStickersOnMinicard",
    "allowsLocation", "allowsDependencies", "allowsDependenciesOnMinicard", "allowsFlowtime",
    "allowsPomodoro", "allowsVote", "allowsVoteOnMinicard", "allowsPoker", "allowsPokerOnMinicard",
    "allowsTextNotes", "allowsCommentCountOnMinicard", "allowsIssueTypeOnMinicard",
    "allowsCardColorOnCard", "allowsCardColorOnMinicard", "allowsParentCardOnCard",
    "allowsParentCardOnMinicard", "allowsLinkedCardOnCard", "allowsLinkedCardOnMinicard",
    "allowsCardButtonsOnCard", "allowsDescriptionBadgeOnMinicard",
    "allowsUnreadCommentsOnMinicard", "allowsCardButtonsOnMinicard",
    "allowsDescriptionBadgeOnCard", "allowsUnreadCommentsOnCard", "allowsDueFromTitleOnCard",
    "allowsDueFromTitleOnMinicard", "allowsSimilarCardsOnCard", "allowsSimilarCardsOnMinicard",
    "allowsAtRiskOnCard", "allowsAtRiskOnMinicard", "allowsSuggestedMembersOnCard",
    "allowsForecastOnCard", "allowsForecastOnMinicard", "allowsChecklistFromDescriptionOnCard",
    "allowsActionItemsOnCard", "allowsWhiteboardOnCard", "allowsAttachmentPreviewsOnCard",
    "allowsAttachmentDetailsOnCard", "allowsCardCollapse", "allowsChecklistTitleOnMinicard",
    "allowsLabelTextOnCard", "labelsAboveTitleOnCard", "allowsChecklistDueDateOnMinicard",
    "allowsLocationOnMinicard", "allowsSwimlaneNameOnCard", "allowsFlowtimeOnMinicard",
    "allowsPomodoroOnMinicard", "allowsAttachmentListOnMinicard", "allowsTextNotesOnMinicard",
    "allowsCommentCountOnCard", "allowsActivitiesOnMinicard"]
CARD_SETTING_DAYS = ["cardAgingDays1", "cardAgingDays2", "cardAgingDays3"]
RULE_TRIGGERS = ["moveCard", "createCard", "archivedCard", "restoredCard", "addedLabel",
                 "joinMember", "addComment", "addAttachment"]
RULE_ACTIONS = ["addLabel", "removeLabel", "removeAllLabels", "markCardComplete",
                "markCardIncomplete", "setDate", "updateDate", "removeDate", "setColor",
                "addMember", "removeMember", "addAssignee", "removeAssignee", "archive",
                "unarchive", "addChecklist", "addChecklistWithItems", "addComment"]
DATE_FIELDS = ["receivedAt", "startAt", "dueAt", "endAt"]


class WriteRefused(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


class Wekan:
    """Minimal REST client. Writes are checked against an allow-list before any request."""

    def __init__(self, api, token=None, dry_run=False, log=print):
        self.api = api.rstrip("/")
        self.token = token
        self.dry_run = dry_run
        self.log = log
        self.read_only = True        # inventory/check: no writes at all
        self.may_create_board = False
        self.writable = set()        # board ids this run created
        self._fake = 0
        self.writes = []             # (method, path) of every write made or simulated

    def _check_write(self, method, path):
        if self.read_only:
            raise WriteRefused(f"read-only run: refused {method} {path}")
        if method == "POST" and path == "/boards" and self.may_create_board:
            return
        m = re.match(r"^/boards/([^/]+)(?:/|$)", path)
        if m and m.group(1) in self.writable:
            return
        raise WriteRefused(f"refused {method} {path}: writes are allowed only on the board this run created")

    def call(self, method, path, body=None):
        if method != "GET":
            self._check_write(method, path)
            self.writes.append((method, path))
            if self.dry_run:
                self._fake += 1
                fake = f"DRY{self._fake:03d}"
                self.log(f"  DRY-RUN {method} {path} {json.dumps(body)[:160] if body is not None else ''}")
                if method == "POST" and path == "/boards":
                    return {"_id": fake, "defaultSwimlaneId": fake + "-swimlane"}
                return {"_id": fake}
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            raise ApiError(f"{method} {path}: HTTP {e.code} {detail}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"{method} {path}: {e.reason}") from None
        try:
            out = json.loads(raw) if raw else None
        except ValueError:
            raise ApiError(f"{method} {path}: not JSON: {raw[:200]!r}") from None
        if isinstance(out, dict) and out.get("error") and len(out) <= 3:
            raise ApiError(f"{method} {path}: {out}")
        return out

    def get(self, path):
        return self.call("GET", path)


# ---------- config ----------

def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("api", "http://127.0.0.1:8090/api")
    cfg.setdefault("token_file", None)
    cfg.setdefault("board_url", "")
    cfg.setdefault("permission", "private")
    cfg.setdefault("color", "belize")
    cfg.setdefault("members", [])
    cfg.setdefault("swimlane", None)
    for k in ("lists", "labels", "custom_fields", "rules", "webhooks"):
        cfg.setdefault(k, [])
    cfg.setdefault("card_settings", {})
    cfg.setdefault("board_views", {})
    cfg.setdefault("templates", {"list": None, "cards": []})
    cfg.setdefault("copy", {"from_board": None, "cards": []})
    return cfg


def validate(cfg):
    """Return a list of problems; empty means the config can be applied."""
    errs = []

    def need(cond, msg):
        if not cond:
            errs.append(msg)

    need(isinstance(cfg.get("title"), str) and cfg["title"].strip(), "title: required")
    need(cfg["color"] in BOARD_COLORS, f"color {cfg['color']!r}: use one of {', '.join(BOARD_COLORS)}")
    need(cfg["permission"] in ("private", "public"), "permission: private or public")
    if cfg["board_url"]:
        try:
            cfg["board_url"].format(board="b", slug="s")
        except (KeyError, IndexError, ValueError):
            errs.append("board_url may only use {board} and {slug}")
    for m in cfg["members"]:
        need(isinstance(m, dict) and m.get("userId"), f"members: each needs a userId: {m!r}")

    def keyed(section, items, extra):
        keys = set()
        for it in items:
            if not isinstance(it, dict) or not it.get("key"):
                errs.append(f"{section}: every entry needs a key: {it!r}")
                continue
            if it["key"] in keys:
                errs.append(f"{section}: duplicate key {it['key']!r}")
            keys.add(it["key"])
            extra(it)
        return keys

    def check_list(l):
        need(l.get("title"), f"lists.{l['key']}: title required")
        c = l.get("color")
        need(c is None or c in LABEL_COLORS or re.fullmatch(r"#[0-9a-fA-F]{6}", str(c)),
             f"lists.{l['key']}: color {c!r} is not a palette name or #rrggbb")
        w = l.get("wipLimit")
        need(w is None or (isinstance(w, dict) and isinstance(w.get("value"), int) and w["value"] > 0),
             f"lists.{l['key']}: wipLimit is {{\"value\": N, \"enabled\": true, \"soft\": true}}")

    def check_label(l):
        need(l.get("name"), f"labels.{l['key']}: name required")
        need(l.get("color") in LABEL_COLORS, f"labels.{l['key']}: color {l.get('color')!r} not in {LABEL_COLORS}")

    def check_field(f):
        need(f.get("name"), f"custom_fields.{f['key']}: name required")
        need(f.get("type") in FIELD_TYPES, f"custom_fields.{f['key']}: type {f.get('type')!r} not in {FIELD_TYPES}")
        if f.get("type") in ("dropdown", "dropdownMultiSelect"):
            need(isinstance(f.get("items"), list) and f["items"], f"custom_fields.{f['key']}: dropdown needs items")

    lists = keyed("lists", cfg["lists"], check_list)
    labels = keyed("labels", cfg["labels"], check_label)
    fields = keyed("custom_fields", cfg["custom_fields"], check_field)
    need(lists, "lists: at least one list")
    fdefs = {f["key"]: f for f in cfg["custom_fields"] if isinstance(f, dict)}

    bad = set(cfg["card_settings"]) - set(CARD_SETTING_DAYS) - set(CARD_SETTING_FLAGS)
    need(not bad, f"card_settings: unknown keys {sorted(bad)}")
    for k, v in cfg["card_settings"].items():
        if k in CARD_SETTING_DAYS:
            need(isinstance(v, int) and v >= 0, f"card_settings.{k}: whole number of days")
        else:
            need(isinstance(v, bool), f"card_settings.{k}: true or false")

    bv = cfg["board_views"]
    for v in [bv.get("default")] + list(bv.get("show", [])) + list(bv.get("hide", [])):
        need(v is None or v in BOARD_VIEWS, f"board_views: unknown view {v!r}")
    if bv.get("default") and bv.get("show"):
        need(bv["default"] in bv["show"], "board_views: default must be one of show")

    for r in cfg["rules"]:
        t, a = r.get("trigger", {}), r.get("action", {})
        need(r.get("title"), f"rules: title required: {r!r}")
        need(t.get("activityType") in RULE_TRIGGERS, f"rules {r.get('title')!r}: trigger.activityType in {RULE_TRIGGERS}")
        need(a.get("actionType") in RULE_ACTIONS, f"rules {r.get('title')!r}: action.actionType in {RULE_ACTIONS}")
        for k in ("list", "oldList"):
            need(t.get(k) in (None, "*") or t.get(k) in lists, f"rules {r.get('title')!r}: trigger.{k} {t.get(k)!r} is not a list key")
        for k in ("label",):
            need(t.get(k) is None or t[k] in labels, f"rules {r.get('title')!r}: trigger.label {t.get(k)!r} is not a label key")
            need(a.get(k) is None or a[k] in labels, f"rules {r.get('title')!r}: action.label {a.get(k)!r} is not a label key")
        if a.get("actionType") in ("setDate", "updateDate", "removeDate"):
            need(a.get("dateField") in DATE_FIELDS, f"rules {r.get('title')!r}: action.dateField in {DATE_FIELDS}")
        if a.get("actionType") == "setColor":
            need(a.get("selectedColor") in LABEL_COLORS, f"rules {r.get('title')!r}: action.selectedColor in {LABEL_COLORS}")

    for w in cfg["webhooks"]:
        need(isinstance(w, dict) and str(w.get("url", "")).startswith(("http://", "https://")),
             f"webhooks: each needs an http(s) url: {w!r}")

    def check_card(where, c, list_required):
        need(c.get("title") or c.get("id"), f"{where}: title required")
        if list_required or c.get("list"):
            need(c.get("list") in lists, f"{where}: list {c.get('list')!r} is not a list key")
        for lab in c.get("labels", []):
            need(lab in labels, f"{where}: label {lab!r} is not a label key")
        for fk, val in c.get("fields", {}).items():
            need(fk in fields, f"{where}: field {fk!r} is not a custom field key")
            fd = fdefs.get(fk)
            if fd and fd.get("type") == "dropdown":
                need(val in fd.get("items", []), f"{where}: {val!r} is not an item of {fk}")
        for cl in c.get("checklists", []):
            need(cl.get("title") and isinstance(cl.get("items", []), list), f"{where}: checklist needs title and items")

    tpl = cfg["templates"]
    if tpl.get("cards"):
        need(tpl.get("list") in lists, f"templates.list {tpl.get('list')!r} is not a list key")
    for i, c in enumerate(tpl.get("cards", [])):
        check_card(f"templates.cards[{i}]", c, False)
    cp = cfg["copy"]
    if cp.get("cards"):
        need(cp.get("from_board"), "copy.from_board: required when copy.cards is set")
    for i, c in enumerate(cp.get("cards", [])):
        need(c.get("id"), f"copy.cards[{i}]: id (the source card id) required")
        need(c.get("comments", "digest") in ("digest", "none"), f"copy.cards[{i}]: comments is digest or none")
        check_card(f"copy.cards[{i}]", c, True)
    return errs


# ---------- create ----------

class Recorder:
    """Keeps the ids of everything created and rewrites the --out file after each step."""

    def __init__(self, path, dry_run):
        self.path, self.dry_run = path, dry_run
        self.data = {"created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                     "dry_run": dry_run, "complete": False}

    def save(self):
        if self.dry_run or not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
            f.write("\n")
        os.replace(tmp, self.path)


def ident(x):
    return x["_id"] if isinstance(x, dict) else x


def create(cfg, w, rec, allow_same_title=False):
    log = w.log
    me = w.get("/user")
    my_id = me["_id"]
    existing = w.get(f"/users/{my_id}/boards") or []
    same = [b for b in existing if (b.get("title") or "").strip() == cfg["title"].strip()]
    if same and not allow_same_title:
        raise SystemExit(f"board-setup: a board titled {cfg['title']!r} already exists ({same[0]['_id']}); "
                         f"pick another title or pass --allow-same-title")

    # Read the copy sources first, so a bad source id fails before anything is created.
    sources = read_copy_sources(cfg, w) if cfg["copy"].get("cards") else []

    w.read_only = False
    w.may_create_board = True
    made = w.call("POST", "/boards", {"title": cfg["title"], "color": cfg["color"],
                                      "permission": cfg["permission"]})
    w.may_create_board = False
    board = made["_id"]
    w.writable.add(board)
    swimlane = made.get("defaultSwimlaneId")
    rec.data.update({"api": cfg["api"], "board": {"id": board, "title": cfg["title"], "color": cfg["color"],
                                                  "permission": cfg["permission"]},
                     "agents_user": my_id, "swimlane": {"id": swimlane, "title": "Default"},
                     "members": [], "lists": {}, "labels": {}, "custom_fields": {}, "rules": [],
                     "webhooks": [], "templates": [], "copied": []})
    rec.save()
    log(f"board {board} created ({cfg['title']!r}, color {cfg['color']}, {cfg['permission']})")

    if cfg.get("swimlane"):
        w.call("PUT", f"/boards/{board}/swimlanes/{swimlane}", {"title": cfg["swimlane"]})
        rec.data["swimlane"]["title"] = cfg["swimlane"]
        rec.save()

    for m in cfg["members"]:
        flags = {k: bool(m.get(k, False)) for k in ("isAdmin", "isNoComments", "isCommentOnly", "isWorker",
                                                     "isReadOnly")}
        w.call("POST", f"/boards/{board}/members/{m['userId']}/add", {"action": "add", **flags})
        rec.data["members"].append({"userId": m["userId"], "name": m.get("name"), **flags})
        rec.save()
        log(f"member {m.get('name') or m['userId']} added{' as board admin' if flags['isAdmin'] else ''}")

    lists = {}
    for l in cfg["lists"]:
        lid = ident(w.call("POST", f"/boards/{board}/lists", {"title": l["title"], "swimlaneId": swimlane}))
        extra = {k: l[k] for k in ("color", "starred", "wipLimit") if l.get(k) is not None}
        if extra:
            w.call("PUT", f"/boards/{board}/lists/{lid}", extra)
        lists[l["key"]] = lid
        rec.data["lists"][l["key"]] = {"id": lid, "title": l["title"], **extra}
        rec.save()
    log(f"{len(lists)} lists: {', '.join(l['title'] for l in cfg['lists'])}")

    labels = {}
    for lab in cfg["labels"]:
        lid = w.call("PUT", f"/boards/{board}/labels", {"label": {"name": lab["name"], "color": lab["color"]}})
        lid = ident(lid)
        labels[lab["key"]] = lid
        rec.data["labels"][lab["key"]] = {"id": lid, "name": lab["name"], "color": lab["color"]}
        rec.save()
    log(f"{len(labels)} labels")

    fields, items = {}, {}
    for f in cfg["custom_fields"]:
        body = {"name": f["name"], "type": f["type"], "settings": f.get("settings", {}),
                "showOnCard": f.get("showOnCard", True),
                "automaticallyOnCard": f.get("automaticallyOnCard", False),
                "showLabelOnMiniCard": f.get("showLabelOnMiniCard", False),
                "showSumAtTopOfList": f.get("showSumAtTopOfList", False)}
        if f["type"] in ("dropdown", "dropdownMultiSelect"):
            body["settings"] = {**body["settings"], "dropdownItems": []}
        fid = ident(w.call("POST", f"/boards/{board}/custom-fields", body))
        entry = {"id": fid, "name": f["name"], "type": f["type"]}
        if f["type"] in ("dropdown", "dropdownMultiSelect"):
            w.call("POST", f"/boards/{board}/custom-fields/{fid}/dropdown-items", {"items": f["items"]})
            got = {} if w.dry_run else {
                i["name"]: i["_id"] for i in (w.get(f"/boards/{board}/custom-fields/{fid}").get("settings", {})
                                             .get("dropdownItems", []))}
            items[f["key"]] = {n: got.get(n, f"DRY-{n}") for n in f["items"]}
            entry["items"] = items[f["key"]]
        fields[f["key"]] = fid
        rec.data["custom_fields"][f["key"]] = entry
        rec.save()
    log(f"{len(fields)} custom fields")

    if cfg["card_settings"]:
        w.call("PUT", f"/boards/{board}/cardSettings", cfg["card_settings"])
        rec.data["card_settings"] = cfg["card_settings"]
        rec.save()
        log(f"{len(cfg['card_settings'])} card settings applied")

    bv = cfg["board_views"]
    if bv:
        body = {}
        if bv.get("default"):
            body["defaultPrivateBoardView"] = bv["default"]
            body["defaultPublicBoardView"] = bv["default"]
        show = set(bv.get("show", []))
        hide = set(bv.get("hide", []))
        if bv.get("hide_others") and show:
            hide |= set(BOARD_VIEWS) - show
        settings = {v: {"showOnPrivate": v not in hide, "showOnPublic": v not in hide}
                    for v in sorted(show | hide)}
        if settings:
            body["boardViewSettings"] = settings
        if bv.get("order"):
            body["boardViewOrder"] = bv["order"]
        if body:
            w.call("PUT", f"/boards/{board}/boardViewSettings", body)
            rec.data["board_views"] = {"default": bv.get("default"), "shown": sorted(show) or "all",
                                       "hidden": len(hide)}
            rec.save()
            log(f"board views: default {bv.get('default')}, {len(hide)} hidden")

    titles = {l["key"]: l["title"] for l in cfg["lists"]}
    for r in cfg["rules"]:
        trig = {k: v for k, v in r["trigger"].items() if k not in ("list", "oldList", "label")}
        trig["boardId"] = board
        if "list" in r["trigger"]:
            trig["listName"] = titles.get(r["trigger"]["list"], "*")
        if "oldList" in r["trigger"]:
            trig["oldListName"] = titles.get(r["trigger"]["oldList"], "*")
        if "label" in r["trigger"]:
            trig["labelId"] = labels[r["trigger"]["label"]]
        act = {k: v for k, v in r["action"].items() if k != "label"}
        act["boardId"] = board
        if "label" in r["action"]:
            act["labelId"] = labels[r["action"]["label"]]
        out = w.call("POST", f"/boards/{board}/rules", {"title": r["title"], "trigger": trig, "action": act})
        rid = ident(out)
        if r.get("enabled") is False:
            w.call("PUT", f"/boards/{board}/rules/{rid}", {"enabled": False})
        rec.data["rules"].append({"id": rid, "title": r["title"], "enabled": r.get("enabled", True) is not False})
        rec.save()
    if cfg["rules"]:
        log(f"{len(cfg['rules'])} rules")

    for h in cfg["webhooks"]:
        hid = ident(w.call("POST", f"/boards/{board}/integrations", {"url": h["url"]}))
        extra = {k: h[k] for k in ("title", "activities") if h.get(k) is not None}
        extra["enabled"] = bool(h.get("enabled", False))
        w.call("PUT", f"/boards/{board}/integrations/{hid}", extra)
        rec.data["webhooks"].append({"id": hid, "url": h["url"], "enabled": extra["enabled"]})
        rec.save()
    if cfg["webhooks"]:
        log(f"{len(cfg['webhooks'])} webhooks (enabled only where the config says so)")

    ctx = {"board": board, "swimlane": swimlane, "lists": lists, "labels": labels, "fields": fields,
           "items": items, "fdefs": {f["key"]: f for f in cfg["custom_fields"]}, "me": my_id}
    tpl = cfg["templates"]
    for c in tpl.get("cards", []):
        cid = add_card(w, ctx, lists[tpl["list"]], c, c.get("description", ""))
        rec.data["templates"].append({"id": cid, "title": c["title"]})
        rec.save()
    if tpl.get("cards"):
        log(f"{len(tpl['cards'])} template cards in {titles[tpl['list']]!r}")

    for spec, src in sources:
        copy_card(w, ctx, cfg, spec, src, rec)

    if not w.dry_run:
        b = w.get(f"/boards/{board}")
        rec.data["board"]["slug"] = b.get("slug")
        if cfg["board_url"]:
            rec.data["board"]["url"] = cfg["board_url"].format(board=board, slug=b.get("slug") or "board")
        rec.data["default_labels"] = [{"id": l["_id"], "name": l.get("name", ""), "color": l.get("color")}
                                      for l in (b.get("labels") or []) if l["_id"] not in labels.values()]
    rec.data["complete"] = True
    rec.save()
    return board


def add_card(w, ctx, list_id, c, description):
    board = ctx["board"]
    cid = ident(w.call("POST", f"/boards/{board}/lists/{list_id}/cards",
                       {"authorId": ctx["me"], "title": c["title"], "description": description,
                        "swimlaneId": ctx["swimlane"]}))
    put = {}
    if c.get("labels"):
        put["labelIds"] = [ctx["labels"][k] for k in c["labels"]]
    if c.get("color"):
        put["color"] = c["color"]
    if c.get("fields") or any(f.get("automaticallyOnCard") for f in ctx["fdefs"].values()):
        values = []
        for key, fd in ctx["fdefs"].items():
            if key in c.get("fields", {}):
                v = c["fields"][key]
                if fd["type"] == "dropdown":
                    v = ctx["items"][key][v]
                values.append({"_id": ctx["fields"][key], "value": v})
            elif fd.get("automaticallyOnCard"):
                values.append({"_id": ctx["fields"][key], "value": None})
        if c.get("fields"):
            put["customFields"] = values
    if put:
        w.call("PUT", f"/boards/{board}/lists/{list_id}/cards/{cid}", put)
    for cl in c.get("checklists", []):
        w.call("POST", f"/boards/{board}/cards/{cid}/checklists", {"title": cl["title"], "items": cl.get("items", [])})
    return cid


# ---------- copy ----------

def read_copy_sources(cfg, w):
    """Find each source card on the source board (read-only) with its comments."""
    src_board = cfg["copy"]["from_board"]
    where = {}
    for l in w.get(f"/boards/{src_board}/lists") or []:
        for c in w.get(f"/boards/{src_board}/lists/{l['_id']}/cards") or []:
            where[c["_id"]] = l
    out = []
    for spec in cfg["copy"]["cards"]:
        l = where.get(spec["id"])
        if not l:
            raise SystemExit(f"board-setup: card {spec['id']} is not an open card on board {src_board}")
        card = w.get(f"/boards/{src_board}/lists/{l['_id']}/cards/{spec['id']}")
        comments = [] if spec.get("comments") == "none" else (w.get(f"/boards/{src_board}/cards/{spec['id']}/comments") or [])
        out.append((spec, {"card": card, "list": l, "comments": comments, "board": src_board}))
    return out


def copy_card(w, ctx, cfg, spec, src, rec):
    card, src_board = src["card"], src["board"]
    names = cfg["copy"].get("usernames", {})
    src_url = cfg["copy"].get("card_url", "")
    link = src_url.format(board=src_board, card=card["_id"]) if src_url else f"board {src_board}, card {card['_id']}"
    today = dt.date.today().isoformat()
    desc = (card.get("description") or "").rstrip()
    desc += (f"\n\n---\nCopied on {today} from the {src['list'].get('title')!r} list of the old board: {link}. "
             f"The original was left unchanged.")
    c = dict(spec)
    c["title"] = spec.get("title") or card.get("title") or "(untitled)"
    cid = add_card(w, ctx, ctx["lists"][spec["list"]], c, desc)
    if src["comments"]:
        lines = [f"{len(src['comments'])} comment(s) on the original card, copied as one comment "
                 f"(Wekan's API does not return comment dates, so the order is the API's):"]
        for m in src["comments"]:
            who = names.get(m.get("authorId"), m.get("authorId"))
            lines.append(f"\n— {who}:\n{m.get('comment', '')}")
        w.call("POST", f"/boards/{ctx['board']}/cards/{cid}/comments",
               {"authorId": ctx["me"], "comment": "\n".join(lines)})
    rec.data["copied"].append({"from": card["_id"], "id": cid, "title": c["title"], "list": spec["list"],
                               "comments": len(src["comments"])})
    rec.save()
    w.log(f"copied {card['_id']} -> {cid}: {c['title'][:70]!r}")


# ---------- inventory ----------

def inventory(w, board, done_pattern=r"(?i)\b(done|archive|closed)\b"):
    b = w.get(f"/boards/{board}")
    out = {"board": {k: b.get(k) for k in ("_id", "title", "slug", "color", "permission", "archived",
                                            "createdAt", "modifiedAt")},
           "members": b.get("members", []),
           "labels": b.get("labels") or [],
           "swimlanes": w.get(f"/boards/{board}/swimlanes") or [],
           "custom_fields": w.get(f"/boards/{board}/custom-fields") or [],
           "rules": w.get(f"/boards/{board}/rules") or [],
           "lists": [], "cards": []}
    try:
        out["webhooks"] = w.get(f"/boards/{board}/integrations") or []
    except ApiError as e:
        out["webhooks"] = f"not readable: {e}"
    for l in w.get(f"/boards/{board}/lists") or []:
        cards = w.get(f"/boards/{board}/lists/{l['_id']}/cards") or []
        done = bool(re.search(done_pattern, l.get("title", "")))
        out["lists"].append({"id": l["_id"], "title": l.get("title"), "cards": len(cards), "done_list": done})
        for c in cards:
            full = w.get(f"/boards/{board}/lists/{l['_id']}/cards/{c['_id']}")
            comments = w.get(f"/boards/{board}/cards/{c['_id']}/comments") or []
            out["cards"].append({"id": c["_id"], "list": l.get("title"), "open": not done,
                                 "title": full.get("title"), "createdAt": full.get("createdAt"),
                                 "modifiedAt": full.get("modifiedAt"), "author": full.get("userId"),
                                 "labels": full.get("labelIds") or [], "comments": len(comments),
                                 "description_chars": len(full.get("description") or "")})
    try:
        out["cards_count"] = (w.get(f"/boards/{board}/cards_count") or {}).get("board_cards_count")
    except ApiError:
        out["cards_count"] = None
    return out


def print_inventory(inv):
    b = inv["board"]
    print(f"Board {b['_id']} {b['title']!r} (slug {b.get('slug')}, color {b.get('color')}, {b.get('permission')}, "
          f"archived={b.get('archived')}, created {b.get('createdAt')})")
    print(f"Members: {len(inv['members'])}: " + ", ".join(
        f"{m['userId']}{' (admin)' if m.get('isAdmin') else ''}" for m in inv["members"]))
    print(f"Swimlanes: " + ", ".join(f"{s['title']} ({s['_id']})" for s in inv["swimlanes"]))
    print(f"Labels: {len(inv['labels'])}" + (": " + ", ".join(f"{l.get('name') or '(unnamed)'}/{l.get('color')}" for l in inv["labels"]) if inv["labels"] else ""))
    print(f"Custom fields: {len(inv['custom_fields'])}; rules: {len(inv['rules'])}; webhooks: "
          f"{len(inv['webhooks']) if isinstance(inv['webhooks'], list) else inv['webhooks']}")
    print(f"Cards (API count): {inv['cards_count']}")
    for l in inv["lists"]:
        print(f"  list {l['title']!r} ({l['id']}): {l['cards']} cards{' [done list]' if l['done_list'] else ''}")
    open_cards = [c for c in inv["cards"] if c["open"]]
    print(f"Open cards (not in a done list): {len(open_cards)}")
    for c in sorted(inv["cards"], key=lambda c: (not c["open"], c["list"], c.get("modifiedAt") or "")):
        print(f"  {'OPEN' if c['open'] else 'done'} {c['id']} [{c['list']}] modified {c.get('modifiedAt')} "
              f"comments {c['comments']}: {c['title']}")


# ---------- main ----------

def token_from(cfg):
    if not cfg.get("token_file"):
        return None
    with open(os.path.expanduser(cfg["token_file"]), encoding="utf-8") as f:
        return f.read().strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c1 = sub.add_parser("check")
    c1.add_argument("--config", required=True)
    c2 = sub.add_parser("create")
    c2.add_argument("--config", required=True)
    c2.add_argument("--dry-run", action="store_true")
    c2.add_argument("--out", default="board-ids.json")
    c2.add_argument("--allow-same-title", action="store_true")
    c3 = sub.add_parser("inventory")
    c3.add_argument("board")
    c3.add_argument("--json", action="store_true")
    c3.add_argument("--config")
    c3.add_argument("--api")
    args = ap.parse_args(argv)

    if args.cmd in ("check", "create"):
        cfg = load_config(args.config)
        errs = validate(cfg)
        if errs:
            for e in errs:
                print("config:", e, file=sys.stderr)
            return 2
        if args.cmd == "check":
            print(f"config OK: {len(cfg['lists'])} lists, {len(cfg['labels'])} labels, "
                  f"{len(cfg['custom_fields'])} custom fields, {len(cfg['card_settings'])} card settings, "
                  f"{len(cfg['rules'])} rules, {len(cfg['webhooks'])} webhooks, "
                  f"{len(cfg['templates'].get('cards', []))} templates, {len(cfg['copy'].get('cards', []))} copies")
            return 0
        w = Wekan(cfg["api"], token_from(cfg), dry_run=args.dry_run)
        rec = Recorder(os.path.abspath(args.out), args.dry_run)
        try:
            board = create(cfg, w, rec, args.allow_same_title)
        except (ApiError, WriteRefused) as e:
            print("board-setup: stopped:", e, file=sys.stderr)
            if rec.data.get("board"):
                print(f"board-setup: partial board {rec.data['board']['id']}; ids so far in {rec.path}",
                      file=sys.stderr)
            return 1
        print(f"{'DRY RUN: would create' if args.dry_run else 'created'} board {board}; "
              f"{len(w.writes)} writes{'' if args.dry_run else '; ids in ' + rec.path}")
        if not args.dry_run and rec.data["board"].get("url"):
            print("open:", rec.data["board"]["url"])
        return 0

    cfg = load_config(args.config) if args.config else {"api": args.api or "http://127.0.0.1:8090/api", "token_file": None}
    if args.api:
        cfg["api"] = args.api
    w = Wekan(cfg["api"], token_from(cfg))
    inv = inventory(w, args.board)
    if args.json:
        json.dump(inv, sys.stdout, indent=1, default=str)
        print()
    else:
        print_inventory(inv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
