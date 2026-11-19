# t3code-tools

Tools for running many coding agents at once in [T3 Code](https://github.com/pingdotgg/t3code):
starting and reviving a team of agent threads, building T3 Code in the cloud, restarting it when
it dies, loading new builds, measuring autoreviewers, and letting humans answer agents from a
kanban board. Each folder has its own README with setup and usage.

The scripts are Python 3 standard library (plus one bash script); `slop/board-relay/wekan/` is Podman
and Caddy config. Most tools talk to T3 Code
through its `t3` CLI (`$T3_CLI`, `t3` on `PATH`, or `~/.t3/bin/t3`) or read its state database
(`~/.t3/userdata/statev2.sqlite`, or `$T3_STATE_DB`) read-only. Several rely on T3 Code's
internal table layout and CLI output, so a T3 Code update can break them; each README lists
its limits.

## Folders

**`cloud-build/`** builds or tests one committed T3 Code revision on a disposable
[boat.dev](https://boat.dev) box, brings back the AppImage (checked against `SHA256SUMS`) or the
test logs, and deletes the box. Tests run with `cd cloud-build && python3 -m unittest -v test_boat_t3_build`.

**`load-build/`** has `t3-load-build`, which loads a staged T3 Code build: it verifies the file
and checksum, restarts T3 on it, wakes an agent thread, and rolls back to the previous build by
itself if T3 doesn't come up or the configured wake command returns failure twice. Wake-command timeouts
or launch errors abort without rollback. It needs a `~/.local/bin/t3code` launcher that starts the newest
AppImage in the build folder. Run its tests with
`python3 -m unittest discover -s load-build -p 'test_*.py' -v`.

## slop/

Unmaintained tools, kept for reference. They are not part of HomuCode and are not recommended.

**`slop/agent-launcher/`** starts one top-level T3 thread per role file in a project, with the model
and effort set in `agents.json` (`start-agents.py`); stops every thread in a project
(`stop-agents.py`); wakes agents again after a usage limit lifts (`revive-agents.py`); builds a
clean run repo from a prep repo (`make-run-repo.sh`); and relays a loopback-only T3 to a private
address (`tcp-relay.py`). Tests run with `cd slop/agent-launcher && python3 -m unittest -v test_launcher`.

**`slop/external-recovery/`** keeps a Claude Code session in a Kitty window outside T3 Code, under a
systemd user unit. A host loop watches T3's backend port and tells the agent when T3 goes down,
and the agent restarts it in its own transient unit. Also lists threads whose last run stopped.
Run its tests with
`cd slop/external-recovery && python3 -m unittest discover -p 'test_*.py'`.

**`slop/autoreview-bench/`** gives two read-only reports on the autoreviewers attached to a T3 thread:
a per-reviewer table of answers, findings, turnaround and tokens (`autoreview-bench`), and a file
with every reviewer's brief so any can be re-added (`autoreview-briefs`). Tests run with
`cd slop/autoreview-bench && python3 -m unittest discover -p 'test_*.py'`.

**`slop/board-relay/`** lets humans answer agents on a self-hosted Wekan board: `board-replies.py`
delivers each human comment, once, to the T3 thread of the agent named in the card title.
Also Podman quadlets for Wekan, MongoDB and an auto-login Caddy proxy (`wekan/`), a board
builder (`board-setup/`), and rules for agents (`BOARD-RULES.md`). Run its tests with
`python3 slop/board-relay/board-replies/test_board_replies.py` and
`python3 slop/board-relay/board-setup/test_board_setup.py`.

## Install

Clone the repo, then follow the README for the tool you want. The systemd units
expect the paths given in those READMEs. The offline tests use fake services
and fixture databases.
