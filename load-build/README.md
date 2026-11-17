# load-build

`t3-load-build` loads a staged T3 Code build: it verifies the file, restarts T3 on it, wakes an agent thread, and rolls back by itself if the new build doesn't come up or the configured wake command returns failure twice. It replaces hand-written one-off restart scripts.

```sh
t3-load-build <staged-file> <sha256> <wake-message-file> [--detach] [--live PATH] [--settle SECONDS]
```
Needs a launcher at `~/.local/bin/t3code` (not part of T3 Code) that starts the newest
`*-x86_64.AppImage` in the staged file's folder, as a process named `t3code`: the loader finds
and stops T3 with `pgrep`/`pkill -x t3code` and starts T3, and rolls back, by running that
launcher. Without it, the loader still stops T3 but cannot start it again, and leaves the new
build renamed to `.off`.

Use `--detach`: the loader stops T3, which stops the agent that started it, so it re-runs itself under `systemd-run --user`.

## Steps
1. **Refuses** (exit 3) while `~/.local/state/t3-load-build/no-restart-until`, or any file listed (colon-separated) in `T3_LOAD_GUARDS`, holds a UTC ISO time in the future. Write a time there to block restarts until then (for example during quiet hours).
2. Verifies the staged file and its sha256, and that the wake file is readable; otherwise exit 1 and nothing is touched.
3. If `T3_LOAD_PAUSE_FILE` is set, writes now + 8 minutes there so a T3 watchdog holds off.
4. Renames the file so the launcher picks it: `...-next.AppImage.staged` becomes `...-x86_64.AppImage`, other `.staged` files lose the suffix (`--live` overrides).
5. Stops T3 (TERM, 8 s, KILL), starts it with `systemd-run --user ... ~/.local/bin/t3code`, waits `--settle` seconds (90).
6. If T3 runs, sends the wake file's text to `T3_LOAD_WAKE_THREAD` with `t3 thread send <thread> - --mode auto` (skipped when unset) (text on stdin; one retry after 30 s). Exit 0 only if the wake succeeds or is skipped.
7. If T3 is not running or both wake attempts return failure, rolls back by itself: renames the new build to `.off`, restarts T3 on the previous newest build, wakes that thread with a rollback notice. Exit 2. If T3 still does not come up it only logs; a watchdog, if you run one, has to alert you.

A wake-command timeout or launch error is not caught: it aborts the loader without
retrying or rolling back.

Log: `~/.local/state/t3-load-build/load.log` (no message text). Python 3 standard library only.

## Check and install
```sh
python3 -m unittest discover -s load-build -p 'test_*.py' -v    # from the repo root
install -m 755 load-build/t3-load-build ~/.local/bin/
```
Tests use fakes only; they never stop or start T3.

`--detach` passes `T3_LOAD_GUARDS`, `T3_LOAD_PAUSE_FILE` and `T3_LOAD_WAKE_THREAD` on to
the `systemd-run` child with `--setenv`, because that child starts from the user
manager's environment. Unset, there are no extra guards, no watchdog pause and no wake.
