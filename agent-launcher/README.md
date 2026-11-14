# agent-launcher

Scripts for running a team of T3 Code agents, one thread per role file.

| Script | What it does |
| --- | --- |
| `start-agents.py REPO` | Starts one top-level T3 thread per `roles/**/*.md`, with the model from `REPO/agents.json`. Records each in `REPO/threads.tsv`; a rerun starts only what is missing. |
| `stop-agents.py --project ID` | Stops every active thread in one T3 project and settles them, using T3's MCP endpoint and a short-lived session it revokes on exit. |
| `revive-agents.py --repo REPO` | Model-free loop. When a usage limit stops every agent on a model category, it wakes them once the category is back: one probe agent first, then the rest. |
| `make-run-repo.sh SRC DEST` | Builds a clean run repo from a prep repo's HEAD, leaving out top-level `__*` files (and anything passed to `--exclude`). Exits 2 if the result still mentions SRC's path or a left-out `__` file. `--replace` only wipes a run repo it built itself, never one where `threads.tsv` exists. |
| `tcp-relay.py ADDRESS PORT` | Forwards `ADDRESS:PORT` to `127.0.0.1:PORT`, so another machine (for example on a tailnet) can reach a loopback-only T3. |

## Status

- **Earlier hard-coded versions: worked.** They started and ran a team of
  dozens of threads.
- **These general versions: tested offline only.** 14 tests pass (`python3 -m unittest -v test_launcher`). They run every script
  against a fake `t3` CLI, a fake MCP HTTP server and temporary copies. They
  cover ordering, ids, reruns, dry runs, model and settings checks, the
  `--all-model` id guard, revive's probe, stop's dry run, the relay, and the
  repo builder's literal path rewrite and leftover check.
- `start-agents.py --dry-run` on `examples/project` with the real `t3` CLI
  passed T3's capability check for all three example models.
- Nothing was started, stopped or sent on the real T3.

## Requirements

Python 3.9+ standard library, the T3 Code `t3` CLI (`$T3_CLI`, else `t3` on
`PATH`, else `~/.t3/bin/t3`), and a running T3. `make-run-repo.sh` needs
bash, git and GNU sed.

## Usage

Run the scripts from this repo's folder; REPO can be anywhere. If `t3` is not
on your `PATH`, use `~/.t3/bin/t3` in the `t3` commands below.

```sh
cp -r examples/project ~/code/myteam      # then edit its roles/, AGENTS.md, agents.json
t3 project add ~/code/myteam --title "My team"            # REPO must be a T3 project
python3 start-agents.py ~/code/myteam --dry-run
# Optional cheap test run, in its own copy (also a T3 project): threads.tsv is per REPO,
# and roles a test run records there would be skipped by the real run.
cp -r ~/code/myteam ~/code/myteam-test && rm -rf ~/code/myteam-test/threads.tsv ~/code/myteam-test/.t3-agent-launcher
t3 project add ~/code/myteam-test --title "My team test"
python3 start-agents.py ~/code/myteam-test --all-model "Sonnet low" --id-prefix test-1-
python3 start-agents.py ~/code/myteam [--start-at 2026-12-01T09:00]   # no UTC offset: local time
python3 revive-agents.py --repo ~/code/myteam [--report-to Planner] [--once|--dry-run]
python3 revive-agents.py --repo ~/code/myteam --back Codex   # for limits only a human can lift
t3 thread list --project ~/code/myteam --json   # each thread's "projectId" is the project id
python3 stop-agents.py --project <project id> --dry-run
```

`agents.json` (see `agents.example.json`):

- `request_id_prefix` (required): makes each start idempotent. Use a new
  prefix for every separate run. `--all-model` refuses to run without an
  `--id-prefix` that differs from it, so a test run can't get the real run's
  threads back from T3.
- `models`: maps a short name to `[provider, model id, effort option id]`.
- `roles`: maps a role name to `"<model name> <effort>"`, optionally with
  ` ultracode` (Claude only).
- `first`: roles to start first. `roles_dir` defaults to `roles`.
- `message`: the start message, with `{name}`, `{path}` and `{repo}` filled
  in. Write literal braces as `{{` and `}}`.
- `models_doc` (optional): a file whose `## Role models` list must match
  `roles`, so the agents' instructions and the launcher can't disagree.

Role files: any `.md` under the roles folder; the name is the file name. Any
path part starting with `_` is skipped (drafts). Subfolders only group files.

Files written into REPO: `threads.tsv` (start-agents) and
`.t3-agent-launcher/` (revive's state and lock, unless `--state-dir`). Ignore
them in the project's git if the agents commit there.

Dry runs: `start-agents.py --dry-run` only calls `t3 thread capabilities`;
`revive-agents.py --dry-run` only reads threads. `stop-agents.py --dry-run`
touches no thread but still issues (and revokes) a read-only
`orchestration:read` session, because listing goes through MCP.

## Limits

- `start-agents.py` gives every role a top-level thread. It can't file
  threads into sidebar folders (the CLI has no folder command).
- `stop-agents.py` uses a bearer session, which T3 caps at the
  approval-required runtime mode. T3 refuses to interrupt or settle
  full-access threads from it (`runtime_mode_escalation_denied`). Stop those
  from a full-access agent's own T3 tools or the UI.
- `revive-agents.py` only reads the `[Usage] USAGE CATEGORY DEATH` notices
  that the `claude-usage` tool (usage-meters, in the companion `agent-tools`
  repo) posts into threads. Times in them are
  Eastern Time, as that tool writes them. Categories are inferred from model
  names (Fable, Opus, Sonnet, Haiku, Codex).
- `tcp-relay.py` adds no authentication. Bind it only to a private address,
  not `0.0.0.0` (that address would also clash with T3's own loopback
  listener).
