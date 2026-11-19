# external-recovery

A Claude Code session that lives outside T3 Code and restarts T3 when it dies.

- A systemd user unit keeps a Kitty window open. Inside it, `recovery.py run`
  starts one Claude Code session and resumes the same session whenever the
  window is closed and reopened.
- Next to it, a host loop checks every 3 seconds whether T3's backend is
  listening on its port (default 3773), and whether the process there really
  is T3's backend. When that changes (two "down" samples in a row count as
  down), it types one `HOST OBSERVATION …` line into the Claude window through
  Kitty's remote-control socket. It never types into a half-written message:
  it waits until the input box is empty.
- The agent restarts T3 with `recovery.py start-t3`. That command rechecks the
  pause and the backend, then starts the launcher in its own transient systemd
  unit, so closing Kitty cannot kill the recovered app. It never stops a
  running T3.
- `scan_stopped.py` lists threads whose latest run stopped (cancelled,
  interrupted or failed), read-only, with the reason to skip each one, so the
  agent can tell you what may need resuming.
- Optional: relay usage-category events (limit hit or available again) from a
  JSONL file, such as the one the `claude-usage` tool (usage-meters, in the companion
  `homustack` repo) writes.

## Requirements

Linux with systemd user services, `ss` (iproute2), Kitty, Python 3.9+
standard library, Claude Code (`claude`) already signed in, and T3 Code with a
launcher command (default `t3code`).

## Setup

```sh
mkdir -p ~/.local/lib/t3-external-recovery ~/.config/t3-external-recovery
cp recovery.py scan_stopped.py ~/.local/lib/t3-external-recovery/
cp config.example.json ~/.config/t3-external-recovery/config.json   # edit, or delete keys you keep at default
cp t3-external-recovery.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now t3-external-recovery.service
```

Check the `DISPLAY` and `WAYLAND_DISPLAY` lines in the unit match your
session.

## Usage

```sh
python3 recovery.py config    # effective settings and which file they came from
python3 recovery.py status    # paused?, last observation
python3 recovery.py pause     # also: unpause
python3 recovery.py start-t3  # what the agent runs; refuses if paused or T3 is up/uncertain
python3 scan_stopped.py [--eligible] [--db PATH]
systemctl --user stop t3-external-recovery.service   # stop the whole thing
```

You can also type to the agent in its window.

## Settings

All in `~/.config/t3-external-recovery/config.json`, or the file named by
`$T3_RECOVERY_CONFIG`. Every key is optional and unknown keys are an error.
See `config.example.json` for the defaults.

| Key | Meaning |
| --- | --- |
| `state_dir` | Private state: `pause`, `session.json`, `monitor.json`, `host.log` |
| `workdir` | Claude Code session's working directory |
| `port`, `backend_marker` | Port to watch, and an argv fragment that identifies T3's backend |
| `launcher`, `launcher_env`, `recovered_unit` | How `start-t3` starts T3 |
| `claude`, `model`, `effort`, `session_name` | The Claude Code session (no fallback model) |
| `prompt_file` | Your own system prompt instead of the built-in one. `{recovery}`, `{scan}` and `{port}` are filled in; write literal braces as `{{ }}` |
| `pause_files` | Extra markers. Present means paused, or paused until the ISO time (with zone) inside it |
| `usage_events` | JSONL of usage events to relay (`id`, `category`, `label`, `transition`, `first_seen`, `confirmed_at`, `earliest_return`) |
| `anthropic_base_url` | Route Claude Code through a local proxy. The token comes from `env` in Claude Code's `settings.json`; no login is attempted |
| `retired_threads_file` | `scan_stopped.py`: thread ids to skip, one per line (`#` starts a comment). When set, a missing file stops the scan |
| `usage_holds_file` | `scan_stopped.py`: usage latch file (`{"latches": {…: {"category": …}}}`). When set, a missing file stops the scan |

`T3_STATE_DB` sets the T3 database path for `scan_stopped.py`. To use another
settings file, set `T3_RECOVERY_CONFIG` in the unit (`Environment=`). Both are
passed on to the Claude Code session, so the agent's own `recovery.py` and
`scan_stopped.py` commands use the same settings as the host loop.

## Limits

- Runs Claude Code with `--permission-mode bypassPermissions` and no MCP
  servers, so the agent can repair things without T3. It is a capable agent
  on your machine. The built-in prompt limits it to restarting T3, but
  nothing else enforces that.
- Reading the input box depends on how Claude Code draws its prompt
  (separator lines, `❯`, grey hint text). A Claude Code UI change can make it
  report `unknown-screen`, and then it stops delivering notices.
- Relies on T3's internal database tables and backend argv; T3 updates can
  break the scan or the backend check. The backend check fails safe: the
  status becomes `unknown`, and `start-t3` refuses to launch.
- A SIGKILL or crash of the host loop can lose one notice. The agent is told
  to check the actual state when the window starts.
- Usage categories in `scan_stopped.py` are hard-coded to `fable`,
  `non-fable` and `codex`, by model name. Category checks only run when
  usage holds are configured: `usage_holds_file`, or a
  `category-holds.json` (`{"codex": <until epoch seconds>}`) in `state_dir`,
  which nothing in this repo writes.
