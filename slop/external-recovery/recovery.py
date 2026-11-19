#!/usr/bin/env python3
"""One persistent Claude Code recovery session for T3 Code, shown in a supervised Kitty window.

Settings come from a JSON file ($T3_RECOVERY_CONFIG, default
~/.config/t3-external-recovery/config.json); every key is optional. Run
`recovery.py config` to print the effective settings.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get('T3_RECOVERY_CONFIG') or HOME / '.config/t3-external-recovery/config.json')
DEFAULTS = {
    'state_dir': '~/.local/state/t3-external-recovery',  # private state, pause file, host log
    'workdir': '~',                    # Claude Code session's working directory
    'port': 3773,                      # T3 backend port to watch
    'backend_marker': 'apps/server/dist/bin.mjs',  # argv fragment that identifies T3's backend
    'launcher': 't3code',              # command that starts T3 Code (PATH, then ~/.local/bin)
    'launcher_env': {'T3CODE_BACKGROUND_GIT': '0'},
    'recovered_unit': 't3code-recovered',  # transient systemd unit T3 is started in
    'claude': 'claude',
    'model': 'claude-opus-5-5',
    'effort': 'low',
    'session_name': 'T3 Recovery',
    'prompt_file': None,               # replaces the built-in PROMPT; {recovery}, {scan}, {port} are filled in
    'pause_files': [],                 # extra markers: present = paused, or paused until the ISO time inside
    'usage_events': None,              # optional JSONL of usage category events to relay
    'anthropic_base_url': None,        # set to route Claude Code through a local proxy
    'retired_threads_file': None,      # scan_stopped.py: thread ids to skip, one per line
    'usage_holds_file': None,          # scan_stopped.py: usage latch file; missing then fails closed
}


def expand(value):
    return Path(os.path.expanduser(str(value)))


def load_config(path=CONFIG_PATH):
    try:
        loaded = json.loads(Path(path).read_text())
    except FileNotFoundError:
        loaded = {}
    if not isinstance(loaded, dict):
        raise SystemExit(f'{path}: expected a JSON object')
    unknown = sorted(set(loaded) - set(DEFAULTS))
    if unknown:
        raise SystemExit(f'{path}: unknown settings {", ".join(unknown)}')
    return {**DEFAULTS, **loaded}


def program(name):
    """Absolute path for a configured command; systemd user units often lack ~/.local/bin on PATH."""
    if '/' in name or name.startswith('~'):
        return str(expand(name))
    return shutil.which(name) or str(HOME / '.local/bin' / name)


CONFIG = load_config()
STATE = expand(CONFIG['state_dir'])
SERVICE = 't3-external-recovery.service'
HERE = Path(__file__).resolve().parent
PROMPT = '''You are the T3 Code recovery agent. You run in a Kitty window outside T3 Code,
supervised by systemd, so you keep working while T3 Code is down. Your one job is to
keep the local T3 Code app running.

Lines that begin "HOST OBSERVATION" are typed in by the host monitor, not by the user.
They carry metadata about the T3 backend on local port {port}. Anything else is the
user typing to you directly.

When an observation says T3 is down and recovery is not paused:
1. Check the actual current state first; it may already be back.
2. Look briefly for why it stopped (logs, crash output) and note it.
3. Start it with: python3 {recovery} start-t3
   That rechecks the pause and the backend, then starts T3 in its own systemd unit,
   so closing this window cannot kill it.
4. Confirm the backend on port {port} is healthy.
5. List threads whose latest run was stopped with: python3 {scan} --eligible
   Tell the user which may need resuming. Do not resume them unless the user asks.

Rules:
- Never stop, kill or restart a T3 Code that is running.
- Never start T3 while recovery is paused (python3 {recovery} status shows it).
- Do not change builds, databases, models or credentials. Ask the user before
  anything destructive, outward-facing, spending money or needing a sign-in.
- Do not poll from your own tool calls; the host tells you about changes. When T3
  is healthy, finish your turn and wait for the next notice.
- If the user asks, run python3 {recovery} pause, unpause or status.
- Closing this window resumes this same session. Reconcile any interrupted action
  before retrying it; never repeat an action that may already have taken effect.
'''


def prompt_text(config=CONFIG):
    template = expand(config['prompt_file']).read_text() if config['prompt_file'] else PROMPT
    return template.format(recovery=HERE / 'recovery.py', scan=HERE / 'scan_stopped.py', port=config['port'])


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)


def read_state(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def paused(state=None, pause_files=None, now=None):
    state = STATE if state is None else state
    pause_files = CONFIG['pause_files'] if pause_files is None else pause_files
    if (state / 'pause').exists():
        return True
    for marker in pause_files:
        try:
            value = expand(marker).read_text().strip()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        try:
            until = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return True  # empty or malformed marker: treat as paused until someone looks
        if until.tzinfo is None or until.timestamp() > (time.time() if now is None else now):
            return True
    return False


def observe(port=None, marker=None):
    """Pin the listener to the native local backend; never accept an unrelated listener."""
    port = CONFIG['port'] if port is None else port
    marker = (CONFIG['backend_marker'] if marker is None else marker).encode()
    result = subprocess.run(['ss', '-H', '-lptn', f'sport = :{int(port)}'],
                            text=True, capture_output=True, timeout=5, check=True)
    if not result.stdout.strip():
        return {'status': 'down'}
    for value in re.findall(r'pid=(\d+)', result.stdout):
        pid = int(value)
        try:
            proc = Path('/proc') / str(pid)
            argv = proc.joinpath('cmdline').read_bytes().split(b'\0')
            if not any(marker in arg for arg in argv):
                continue
            stat = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return {'status': 'up', 'identity': f'{boot}:{pid}:{stat[19]}', 'pid': pid}
        except FileNotFoundError:
            continue
    return {'status': 'unknown', 'reason': f'port {port} listener is not a verified T3 backend'}


class Monitor:
    def __init__(self, state):
        self.path = state / 'monitor.json'
        self.last = read_state(self.path).get('observation')
        self.down_samples = 0

    def update(self, observation, is_paused):
        value = {**observation, 'paused': is_paused}
        self.down_samples = self.down_samples + 1 if value['status'] == 'down' else 0
        if value['status'] == 'down' and self.down_samples < 2:
            return None
        if value == self.last:
            return None
        self.last = value
        # Claim before sending; a lost notification is reconciled by the resume prompt,
        # never automatically replayed as an already attempted repair.
        save(self.path, {'observation': value})
        return ('HOST OBSERVATION (metadata only): ' + json.dumps(value) +
                '. Inspect current state. ' +
                ('Planned pause: do not launch T3.' if is_paused else
                 'If T3 is stopped, diagnose and restart it under your recovery remit, then list the threads its stop interrupted.'))


def start_t3(state=None, observe_fn=observe, run_fn=subprocess.run, config=CONFIG):
    state = STATE if state is None else state
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'start.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if paused(state, config['pause_files']):
            return {'status': 'held', 'reason': 'planned pause'}
        observed = observe_fn()
        if observed['status'] != 'down':
            return {'status': 'held', 'reason': 'backend is up or uncertain', 'observation': observed}
        # Separate cgroup: closing the recovery Kitty must not kill the app it recovered.
        unit = config['recovered_unit']
        command = ['systemd-run', '--user', '--collect', '--unit=' + unit]
        for key, value in sorted((config['launcher_env'] or {}).items()):
            command.append(f'--setenv={key}={value}')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
            if os.environ.get(key):
                command.append('--setenv=' + key + '=' + os.environ[key])
        command += ['--', '/usr/bin/env', '-u', 'ELECTRON_RUN_AS_NODE', program(config['launcher'])]
        result = run_fn(command, text=True, capture_output=True, timeout=15)
        return {'status': 'requested' if result.returncode == 0 else 'held',
                'unit': unit + '.service', 'exit': result.returncode,
                'healthy': False}


def claude_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or HOME / '.claude')


def claude_environment(config=CONFIG, environ=None, settings=None):
    """Environment for the recovery session, without variables inherited from a parent agent session."""
    env = dict(os.environ if environ is None else environ)
    # This tool's own settings stay, so the agent's `recovery.py start-t3|pause|status` and
    # `scan_stopped.py` read the same config (port, state dir, pause files) as this host loop.
    keep = {'T3_RECOVERY_CONFIG', 'T3_STATE_DB'}
    if not config['anthropic_base_url']:
        keep.add('CLAUDE_CODE_OAUTH_TOKEN')
    for key in list(env):
        if key.startswith(('CLAUDECODE', 'CLAUDE_CODE_', 'T3_', 'T3CODE_', 'MCP_')) and key not in keep:
            env.pop(key)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    if config['anthropic_base_url']:
        # Proxy mode: reuse the token already in Claude Code's settings, never start a new login.
        settings = claude_dir() / 'settings.json' if settings is None else settings
        try:
            configured = json.loads(settings.read_text()).get('env', {})
        except FileNotFoundError:
            configured = {}
        for key in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'):
            if isinstance(configured.get(key), str):
                env[key] = configured[key]
        if not (env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')):
            raise RuntimeError('anthropic_base_url is set but no proxy token is available; no login attempted')
        env['ANTHROPIC_BASE_URL'] = config['anthropic_base_url']
    return env


def project_slug(path):
    """Claude Code's per-project folder name: every non-alphanumeric character becomes '-'."""
    return re.sub(r'[^A-Za-z0-9]', '-', str(path))


def transcript(session, workdir=None):
    workdir = expand(CONFIG['workdir'] if workdir is None else workdir).resolve()
    return claude_dir() / 'projects' / project_slug(workdir) / (session['id'] + '.jsonl')


def claude_argv(session, opening, config=CONFIG):
    """Interactive Claude Code TUI owns the terminal; the role prompt persists on resume."""
    return [program(config['claude']),
            '--model', config['model'], '--effort', config['effort'], '--name', config['session_name'],
            '--permission-mode', 'bypassPermissions', '--allow-dangerously-skip-permissions',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--append-system-prompt', prompt_text(config),
            '--resume' if session.get('initialized') else '--session-id', session['id'],
            opening]


def log(line):
    with (STATE / 'host.log').open('a') as out:
        out.write(datetime.datetime.now().isoformat(timespec='seconds') + ' ' + line + '\n')


def kitty_target(env):
    return ['kitty', '@', '--to', env['KITTY_LISTEN_ON']]


ANSI = re.compile(r'\x1b\[([0-9;:]*)m')


def grey(codes):
    """True if a single SGR colour spec (list of ints) is grey/hint-like."""
    if codes[:2] == [38, 2] and len(codes) >= 5:
        return codes[2] == codes[3] == codes[4]
    if codes[:2] == [38, 5] and len(codes) >= 3:
        return codes[2] in (7, 8) or 232 <= codes[2] <= 255
    return codes[0] in (30, 37, 90, 97)


def visible_typed(line):
    """Text drawn in a non-grey, non-dim style (grey/dim = hints, borders, placeholder)."""
    out, dim, muted_colour = [], False, False
    for index, part in enumerate(ANSI.split(line)):
        if not index % 2:
            if not (dim or muted_colour):
                out.append(part)
            continue
        try:
            codes = [int(c) if c else 0 for c in part.replace(':', ';').split(';')] if part else [0]
        except ValueError:
            dim = muted_colour = True  # unknown styling: treat as hint, then rely on the box check
            continue
        i = 0
        while i < len(codes):  # apply SGR operations in order
            c = codes[i]
            if c == 0:
                dim = muted_colour = False
            elif c == 2:
                dim = True
            elif c == 22:
                dim = False
            elif c == 39:
                muted_colour = False
            elif c == 38:
                width = 5 if codes[i + 1:i + 2] == [2] else 3
                muted_colour = grey(codes[i:i + width])
                i += width - 1
            elif 30 <= c <= 37 or 90 <= c <= 97:
                muted_colour = grey([c])
            i += 1
    return ''.join(out).strip()


def draft(screen):
    """Contents of Claude Code's input box (between the last two separators); None if unknown."""
    lines = screen.splitlines()
    plain = [ANSI.sub('', line) for line in lines]
    seps = [i for i, line in enumerate(plain)
            if line.strip().startswith('─') and line.count('─') >= 10]
    if len(seps) < 2:
        return None
    box = lines[seps[-2] + 1:seps[-1]]
    if not box or not ANSI.sub('', box[0]).lstrip().startswith('❯'):
        return None
    box[0] = box[0].replace('❯', '', 1)
    return ' '.join(filter(None, (visible_typed(line) for line in box)))


def read_screen(env=os.environ, run_fn=subprocess.run):
    return run_fn(kitty_target(env) + ['get-text', '--ansi', '--extent', 'screen', '--match',
                                       'id:' + env['KITTY_WINDOW_ID']],
                  text=True, capture_output=True, timeout=10, check=True).stdout


def submit(env=os.environ, run_fn=subprocess.run):
    run_fn(kitty_target(env) + ['send-text', '--match', 'id:' + env['KITTY_WINDOW_ID'], '--stdin'],
           input='\r', text=True, capture_output=True, timeout=10, check=True)


def paste(text, env=os.environ, run_fn=subprocess.run):
    run_fn(kitty_target(env) + ['send-text', '--match', 'id:' + env['KITTY_WINDOW_ID'],
                                '--bracketed-paste', 'enable', '--stdin'],
           input=text, text=True, capture_output=True, timeout=10, check=True)


class Courier:
    """Holds the newest undelivered notice; never merges it into the user's unfinished draft.

    Submit (CR) is sent only when the input box holds exactly the notice, so a paste that
    landed earlier is submitted rather than pasted twice, and text the user types between the
    paste and the CR is never submitted by the host.
    """
    def __init__(self, screen_fn=read_screen, paste_fn=paste, submit_fn=submit, settle=.4):
        # One slot per source: newest T3 observation wins its slot; each usage event
        # has its own slot, so neither overwrites the other. Delivered oldest first.
        self.slots = {}
        self.screen_fn, self.paste_fn, self.submit_fn = screen_fn, paste_fn, submit_fn
        self.settle = settle

    @property
    def pending(self):
        return next(iter(self.slots.values()), None)

    def offer(self, notice, key='t3'):
        if notice:
            self.slots[key] = ' '.join(notice.split())  # one line: a newline must not submit early

    def _done(self):
        self.slots.pop(next(iter(self.slots)), None)

    def attempt(self):
        if not self.pending:
            return 'idle'
        try:
            typed = draft(self.screen_fn())
            if typed is None:
                return 'unknown-screen'
            if typed == self.pending:  # earlier paste landed but its CR did not
                self.submit_fn()
                self._done()
                return 'sent'
            if typed:
                # Never type into the user's draft (the cursor may be mid-text). Wait for an empty box;
                # the notice stays pending, newest state wins.
                return 'deferred'
            self.paste_fn(self.pending)
            time.sleep(self.settle)
            now_typed = draft(self.screen_fn())
            if now_typed != self.pending:
                # The user typed meanwhile (or screen unreadable): leave it for them; no CR, no re-paste.
                self._done()
                return 'pasted-unsubmitted'
            self.submit_fn()
        except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
            return 'failed ' + type(exc).__name__
        self._done()
        return 'sent'


USAGE_BACKLOG = 900  # on first start, deliver only events confirmed in the last 15 minutes


class UsageInbox:
    """Usage category limit-hit / available-again events from a JSONL file (one JSON object per line,
    each with an "id"; see README). Claimed before offering (like Monitor): a host restart never
    replays an already offered event.
    """
    def __init__(self, state, events, now=time.time):
        self.path = state / 'usage-inbox.json'
        self.events = events
        saved = read_state(self.path)
        self.offset, self.seen = saved.get('offset'), saved.get('seen', [])
        self.identity = saved.get('identity')
        self.now = now

    def _identity(self, stat):
        # Inode plus a hash of the first line detects replacement or truncate-and-regrow
        # even when the new file is as long as the old offset; seen ids prevent duplicates.
        with self.events.open('rb') as handle:
            head = handle.readline(4096)
        return [stat.st_dev, stat.st_ino, hashlib.sha256(head).hexdigest()]

    def _save(self):
        save(self.path, {'offset': self.offset, 'seen': self.seen, 'identity': self.identity})

    def poll(self):
        try:
            stat = self.events.stat()
            identity = self._identity(stat)
        except FileNotFoundError:
            return []
        size = stat.st_size
        first = self.offset is None
        if first or size < self.offset or identity != self.identity:
            self.offset = 0  # first start, or the producer's file was replaced/truncated
        self.identity = identity
        if size == self.offset:
            self._save()
            return []
        with self.events.open('rb') as handle:
            handle.seek(self.offset)
            data = handle.read(size - self.offset)
        end = data.rfind(b'\n') + 1  # leave a partially written line for next time
        notices = []
        for raw in data[:end].splitlines():
            try:
                event = json.loads(raw)
                key = str(event['id'])
            except (ValueError, KeyError, TypeError):
                continue
            if key in self.seen:
                continue
            self.seen = (self.seen + [key])[-200:]
            if first and self.now() - float(event.get('confirmed_at') or 0) > USAGE_BACKLOG:
                continue
            notices.append((key, usage_notice(event)))
        self.offset += end
        self._save()
        return notices


def _local(epoch):
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(float(epoch)).astimezone().strftime('%a %m/%d %H:%M %Z')


def usage_notice(event):
    label = event.get('label') or event.get('category')
    if event.get('transition') == 'limit-hit':
        what = (f'{label} category LIMIT HIT: every {label} account is at its usage limit '
                f'(first seen {_local(event.get("first_seen"))}). ')
        back = _local(event.get('earliest_return'))
        what += (f'Earliest scheduled return {back}. ' if back else 'Return time unknown. ')
        what += 'Agents running on this category will fail until it returns. '
    elif event.get('transition') == 'capped':
        what = (f'{label} category CAPPED by a usage limit ({", ".join(event.get("boxes") or [])}; first seen '
                f'{_local(event.get("first_seen"))}). Only a human can lift it. '
                'Agents running on this category will fail until then. ')
    elif event.get('transition') == 'cap-cleared':
        what = f'{label} category usage cap CLEARED (seen {_local(event.get("first_seen"))}). '
    else:
        what = (f'{label} category AVAILABLE AGAIN: at least one {label} account is below its '
                f'limit (first seen {_local(event.get("first_seen"))}). ')
    meta = {k: event.get(k) for k in ('id', 'category', 'transition', 'first_seen', 'confirmed_at', 'earliest_return')}
    return ('HOST OBSERVATION (usage category, metadata only): ' + what + json.dumps(meta, sort_keys=True) +
            '. Inspect current state. This is a usage observation, not a T3 stop; it grants no '
            'new authority to lift limits, stop agents or switch providers or models. Act only within your existing remit.')


def watch(monitor, child, stop, courier=None, inbox=None):
    courier = courier or Courier()
    if inbox is None and CONFIG['usage_events']:
        inbox = UsageInbox(STATE, expand(CONFIG['usage_events']))
    stop.wait(15)  # let the Claude Code interface start before typing into it
    last = None
    while not stop.is_set() and child.poll() is None:
        try:
            notice = monitor.update(observe(), paused())
        except (OSError, subprocess.SubprocessError) as exc:
            notice = monitor.update({'status': 'unknown', 'reason': type(exc).__name__}, paused())
        if notice:
            log(notice)
        courier.offer(notice)
        if inbox is not None:
            try:
                for key, usage in inbox.poll():
                    log(usage)
                    courier.offer(usage, key='usage:' + key)
            except (OSError, ValueError) as exc:
                log('USAGE INBOX failed ' + type(exc).__name__)
        result = courier.attempt()
        if result != last and result != 'idle':
            log('DELIVERY ' + result)
        last = result
        stop.wait(3)


def run():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'session.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session_path = STATE / 'session.json'
        session = read_state(session_path) or {'id': str(uuid.uuid4()), 'initialized': False}
        # Only an existing transcript proves the session can be resumed.
        session['initialized'] = transcript(session).exists()
        save(session_path, session)
        monitor = Monitor(STATE)
        initial = observe()
        monitor.update(initial, paused())
        opening = ('This window has started/resumed. Check actual T3 state now. ' +
                   json.dumps(initial) + ' ' +
                   ('Recovery is PAUSED.' if paused() else 'Recovery is enabled.'))
        if not session['initialized']:
            opening = prompt_text() + '\n' + opening
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C belongs to Claude Code
        child = subprocess.Popen(
            claude_argv(session, opening), cwd=expand(CONFIG['workdir']), env=claude_environment(),
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        stop = threading.Event()
        threading.Thread(target=watch, args=(monitor, child, stop), daemon=True).start()
        try:
            return child.wait()
        finally:
            stop.set()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'pause', 'unpause', 'status', 'start-t3', 'config'])
    args = parser.parse_args()
    if args.command == 'run':
        raise SystemExit(run())
    if args.command == 'start-t3':
        print(json.dumps(start_t3()))
        raise SystemExit(0)
    if args.command == 'config':
        print(json.dumps({'config_file': str(CONFIG_PATH), **CONFIG}, indent=2))
        raise SystemExit(0)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == 'pause':
        (STATE / 'pause').touch(mode=0o600)
    elif args.command == 'unpause':
        (STATE / 'pause').unlink(missing_ok=True)
    print(json.dumps({'paused': paused(), 'monitor': read_state(STATE / 'monitor.json')}))
