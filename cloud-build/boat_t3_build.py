#!/usr/bin/env python3
"""Build a committed T3 revision on a disposable Boat VM; never load it."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone


def dependency_cache_key(lockfile, node, pnpm):
    digest = hashlib.sha256(lockfile).hexdigest()
    return f'v1-{digest}-node{node}-pnpm{pnpm}-linux-x64'


def file_sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def validate_dependency_archive(path):
    with tarfile.open(path) as archive:
        for member in archive:
            if (not Path(member.name).parts or Path(member.name).parts[0] != 'store' or
                    member.name.startswith('/') or '..' in Path(member.name).parts or
                    not (member.isfile() or member.isdir())):
                raise RuntimeError(f'Unsafe dependency cache member: {member.name}')


class DependencyCache:
    """Only public dependency-store files; entries become visible by atomic rename."""
    def __init__(self, root, key):
        self.root = root
        self.key = key
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.entry = root / key

    def lookup(self):
        try:
            manifest = json.loads((self.entry / 'manifest.json').read_text())
            archive = self.entry / 'store.tar.gz'
            if (manifest['key'] == self.key and not archive.is_symlink() and
                    manifest['sha256'] == file_sha256(archive)):
                return archive, manifest['sha256']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def publish(self, archive, rejected_sha256=None):
        validate_dependency_archive(archive)
        with (self.root / f'.{self.key}.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = self.lookup()
            if existing and existing[1] != rejected_sha256:
                return
            with tempfile.TemporaryDirectory(prefix=f'.{self.key}.', dir=self.root) as temp:
                stage = Path(temp) / 'entry'
                stage.mkdir()
                target = stage / 'store.tar.gz'
                shutil.copyfile(archive, target)
                digest = file_sha256(target)
                (stage / 'manifest.json').write_text(json.dumps({'key': self.key, 'sha256': digest}))
                if self.entry.is_symlink():
                    self.entry.unlink()
                elif self.entry.exists():
                    shutil.rmtree(self.entry)
                stage.rename(self.entry)


def dependency_install_script(cache_sha256=None):
    if cache_sha256 is None:
        return 'pnpm install --frozen-lockfile'
    # The empty checksum denotes a cold cache, not disabled caching. Every restore is verified
    # before extraction; only regular files/directories rooted at "store" are accepted.
    return r'''cache_restored=false
cache_exported=false
mkdir -p /home/user/pnpm-cache/store
if [ -f /tmp/pnpm-store.tar.gz ]; then
  if python3 - /tmp/pnpm-store.tar.gz CACHE_SHA256 <<'PY'
import hashlib, pathlib, sys, tarfile
path = pathlib.Path(sys.argv[1])
with path.open('rb') as source:
    if hashlib.file_digest(source, 'sha256').hexdigest() != sys.argv[2]:
        raise ValueError('Dependency cache checksum mismatch')
with tarfile.open(path) as archive:
    for member in archive:
        parts = pathlib.PurePosixPath(member.name).parts
        if not parts or parts[0] != 'store' or '..' in parts or not (member.isfile() or member.isdir()):
            raise ValueError('Unsafe dependency cache member')
    archive.extractall('/home/user/pnpm-cache', filter='data')
PY
  then cache_restored=true
  else rm -rf /home/user/pnpm-cache/store; mkdir -p /home/user/pnpm-cache/store
  fi
fi
if ! pnpm install --frozen-lockfile --store-dir /home/user/pnpm-cache/store --side-effects-cache=false --verify-store-integrity=true; then
  if [ "$cache_restored" != true ]; then exit 1; fi
  # A rejected/corrupt restored package must not poison later builds. Reinstall from a clean store.
  cache_restored=false
  rm -rf /home/user/pnpm-cache/store
  find . -type d -name node_modules -prune -exec rm -rf {} +
  pnpm install --frozen-lockfile --store-dir /home/user/pnpm-cache/store --side-effects-cache=false --verify-store-integrity=true
fi
if [ "$cache_restored" != true ]; then
  if tar -C /home/user/pnpm-cache -czf /tmp/pnpm-store-export.tar.gz store; then cache_exported=true; fi
fi
printf '{"restored":%s,"exported":%s}\n' "$cache_restored" "$cache_exported" > /tmp/pnpm-cache-status.json
'''.replace('CACHE_SHA256', shlex.quote(cache_sha256))


def frames(raw):
    return [json.loads(line) for line in raw.splitlines() if line.startswith('{')]


def check_limits(limits, hours=4, machine_type='large'):
    if not limits.get('canStart') or limits.get('nextStartDollars') != 0:
        raise RuntimeError('No included start available')
    if any(limits['starts'][window]['remaining'] < 2 for window in ('minute', 'hour', 'day')):
        raise RuntimeError('Insufficient included start headroom')
    if limits['subscriptionRemainingSeconds'] < hours * 3600:
        raise RuntimeError('Insufficient included compute time')
    if not any(t['type'] == machine_type and t['available'] for t in limits['machineTypes']):
        raise RuntimeError(f'{machine_type} machine unavailable')


def validate_archive(path):
    with tarfile.open(path) as archive:
        for member in archive:
            parts = Path(member.name).parts
            if member.name.startswith('/') or '..' in parts or member.issym() or member.islnk():
                raise RuntimeError(f'Unsafe source member: {member.name}')
            if not member.isfile() and not member.isdir():
                raise RuntimeError(f'Unsupported source member: {member.name}')
            if any(p in {'.git', '.t3', '.codex', '.claude', 'credentials', '.ssh'} for p in parts):
                raise RuntimeError(f'Private state in source: {member.name}')
            name = Path(member.name).name
            if name == '.env' or name.startswith('.env.') and not name.endswith(('example', 'sample')):
                raise RuntimeError(f'Environment file in source: {member.name}')
            if name in {'auth.json', 'config.json.secret', 'id_rsa', 'id_ed25519'}:
                raise RuntimeError(f'Credential filename in source: {member.name}')


def validate_packages(path, requested):
    names = set()
    with tarfile.open(path) as archive:
        for member in archive:
            parts = Path(member.name).parts
            workspace = (len(parts) == 3 and parts[0] in {'apps', 'packages', 'infra'} or
                         len(parts) == 2 and parts[0] in {'scripts', 'evals', 'oxlint-plugin-t3code'})
            if member.isfile() and workspace and parts[-1] == 'package.json':
                names.add(json.load(archive.extractfile(member))['name'])
    unknown = set(requested) - names
    if unknown:
        raise RuntimeError('Unknown exact workspace packages: ' + ', '.join(sorted(unknown)))


def remote_script(node_version, pnpm_version, mode, test_files, packages=(), test_name=None, test_cases=(), no_isolate=False, dependency_cache_sha256=None):
    paths = [*test_files, *(path for path, _ in test_cases)]
    web_browser = mode == 'test' and any(
        re.fullmatch(r'apps/web/.+\.browser\.test\.[jt]sx?', path) for path in paths)
    browser_setup = ('node apps/web/node_modules/playwright/cli.js install --with-deps --only-shell chromium'
                     if web_browser else '')
    commands = []
    if mode == 'build':
        commands.append(('build', 'pnpm run dist:desktop:linux --output-dir /home/user/artifacts'))
    if mode == 'test':
        groups = [(test_files, test_name)] if test_files else []
        groups += [([path], pattern or None) for path, pattern in test_cases]
        for index, (files, pattern) in enumerate(groups):
            command = 'pnpm exec vp test run --maxWorkers=1 '
            if no_isolate:
                command += '--no-isolate '
            command += ' '.join(shlex.quote(p) for p in files)
            if pattern:
                command += ' -t ' + shlex.quote(pattern)
            commands.append((f'test-{index + 1}', command))
    if packages and mode in {'test', 'typecheck'}:
        for package in packages:
            command = ('pnpm --fail-if-no-match --filter ' + shlex.quote(package) +
                       ' exec effect-tsgo tsrs --noEmit')
            commands.append((f'typecheck:{package}', command))
    lines = ['status=0']
    for label, command in commands:
        lines += ['code=0', command + ' || code=$?',
                  f'printf "CHECK_STATUS {label} %s\\n" "$code"',
                  'if [ "$code" -ne 0 ]; then status="$code"; fi']
    lines += ['exit "$status"']
    task = '/usr/bin/time -v bash -c ' + shlex.quote('\n'.join(lines))
    return f'''#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if [ "$(id -u)" = 0 ]; then privilege=(); else privilege=(sudo -n); fi
"${{privilege[@]}}" apt-get update
"${{privilege[@]}}" apt-get install -y build-essential curl ca-certificates xz-utils git python3 pkg-config libsecret-1-dev imagemagick libgtk-3-0t64 libnss3 libasound2t64 libgbm1 patchelf time
mkdir -p /home/user/toolchain /home/user/src /home/user/artifacts
cd /home/user/toolchain
curl -fsSLO https://nodejs.org/dist/v{node_version}/node-v{node_version}-linux-x64.tar.xz
curl -fsSLO https://nodejs.org/dist/v{node_version}/SHASUMS256.txt
awk '$2 == "node-v{node_version}-linux-x64.tar.xz"' SHASUMS256.txt | sha256sum -c -
tar -xf node-v{node_version}-linux-x64.tar.xz
export PATH="/home/user/toolchain/node-v{node_version}-linux-x64/bin:$PATH"
npm install --global pnpm@{pnpm_version}
curl --proto '=https' --tlsv1.2 -fsS https://sh.rustup.rs -o /home/user/toolchain/rustup-init.sh
sh /home/user/toolchain/rustup-init.sh -y --profile minimal --default-toolchain stable
source "$HOME/.cargo/env"
export CARGO_BUILD_JOBS=4
export npm_config_jobs=4
export NODE_OPTIONS=--max-old-space-size=4096
export CSC_IDENTITY_AUTO_DISCOVERY=false
export APPIMAGE_EXTRACT_AND_RUN=1
cd /home/user/src
tar -xf /tmp/source.tar.gz
node --version
pnpm --version
rustc --version
{dependency_install_script(dependency_cache_sha256)}
{browser_setup}
{task}
cd /home/user/artifacts
if [ {shlex.quote(mode)} = build ]; then
  sha256sum ./*.AppImage > SHA256SUMS
fi
'''


class Boat:
    def __init__(self, output):
        self.output = output
        self.box = None
        self.serial = 0

    def call(self, *args, timeout=180):
        self.serial += 1
        log = self.output / f'{self.serial:02d}-{args[0]}.log'
        command = ['boat', '--no-update', '--json', *args]
        if args[0] == 'new':
            # File output survives interruption and preserves the created ID for recovery.
            try:
                with log.open('w') as output:
                    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
                    try:
                        code = process.wait(timeout=timeout)
                    except BaseException:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise
            finally:
                raw = log.read_text() if log.exists() else ''
                for frame in frames(raw):
                    if frame.get('event') == 'created':
                        self.box = frame['id']
                        (self.output / 'box-id').write_text(self.box + '\n')
            if code:
                raise subprocess.CalledProcessError(code, command)
            return raw
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
        log.write_text(result.stdout)
        result.check_returncode()
        return result.stdout

    def execute(self, command, timeout):
        raw = self.call('exec', self.box, '--timeout', str(timeout), '--', 'bash', '-lc', command,
                        timeout=timeout + 90)
        data = frames(raw)
        # Non-streaming Boat exec can return local exit 0 with remote failure.
        statuses = [f for f in data if 'exitCode' in f]
        if not statuses or statuses[-1]['exitCode'] != 0 or statuses[-1].get('timedOut') or statuses[-1].get('oomKilled'):
            raise RuntimeError('Remote command failed; see exec log')

    def cleanup(self):
        if self.box:
            deletion = frames(self.call('delete', self.box, '--yes'))[-1]
            (self.output / 'deletion.json').write_text(json.dumps(deletion, indent=2))
            remaining = frames(self.call('list'))[-1]['sandboxes']
            if any(b['id'] == self.box for b in remaining):
                raise RuntimeError(f'Cleanup incomplete: {self.box}; inspect deletion.json')
            (self.output / 'deleted').write_text(self.box + '\n')


def build(args):
    repo = args.repo.resolve()
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--verify', args.revision + '^{commit}'], text=True).strip()
    package = json.loads(subprocess.check_output(['git', '-C', str(repo), 'show', revision + ':package.json']))
    node = package['engines']['node']
    pnpm = package['packageManager'].removeprefix('pnpm@')
    if not re.fullmatch(r'\d+\.\d+\.\d+', node) or not re.fullmatch(r'\d+\.\d+\.\d+', pnpm):
        raise RuntimeError('Expected exact Node/pnpm versions')
    args.output.mkdir(parents=True, exist_ok=False)
    cache = None
    cached = None
    cache_status = {'enabled': not args.no_dependency_cache}
    if not args.no_dependency_cache:
        lockfile = subprocess.check_output(['git', '-C', str(repo), 'show', revision + ':pnpm-lock.yaml'])
        key = dependency_cache_key(lockfile, node, pnpm)
        cache_status['key'] = key
        try:
            cache = DependencyCache(args.dependency_cache, key)
            cached = cache.lookup()
            cache_status['lookup'] = 'hit' if cached else 'miss'
        except OSError as error:
            cache_status['warning'] = str(error)
    boat = Boat(args.output)
    (args.output / 'provenance.json').write_text(json.dumps({'revision': revision, 'repo': str(repo),
        'started': datetime.now(timezone.utc).isoformat(), 'mode': args.mode,
        'test_files': args.test_file, 'test_name': args.test_name, 'test_cases': args.test_case,
        'packages': args.package, 'no_isolate': args.no_isolate,
        'dependency_cache': cache_status}, indent=2))
    with tempfile.TemporaryDirectory(prefix='t3-cloud-') as temp:
        source = Path(temp) / 'source.tar.gz'
        subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar.gz', '-1', '-o', str(source), revision, '.', ':(exclude).repos', ':(exclude).claude'], check=True)
        validate_archive(source)
        validate_packages(source, args.package)
        (args.output / 'source.sha256').write_text(file_sha256(source) + '\n')
        setup = Path(temp) / 'build.sh'
        setup.write_text(remote_script(node, pnpm, args.mode, args.test_file, args.package,
                                       args.test_name, args.test_case, args.no_isolate,
                                       cached[1] if cached else '' if cache else None))
        check_limits(frames(boat.call('limits', '--all'))[-1], args.min_included_hours, args.machine_type)
        try:
            boat.call('new', '--personal', '--type', args.machine_type, '--no-env', '--no-snapshots',
                      '--ttl', str(args.ttl), '--name', args.name_prefix + revision[:12], timeout=180)
            if not boat.box:
                raise RuntimeError('No created sandbox ID received')
            boat.call('scp', str(source), boat.box + ':/tmp/source.tar.gz', timeout=900)
            boat.call('scp', str(setup), boat.box + ':/tmp/build.sh')
            if cached:
                try:
                    boat.call('scp', str(cached[0]), boat.box + ':/tmp/pnpm-store.tar.gz', timeout=900)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    cache_status['upload_warning'] = str(error)
            try:
                boat.call('ssh', boat.box, '--', 'timeout', '4200', 'bash', '/tmp/build.sh', timeout=4290)
            finally:
                if cache:
                    try:
                        status_file = Path(temp) / 'pnpm-cache-status.json'
                        boat.call('scp', boat.box + ':/tmp/pnpm-cache-status.json', str(status_file))
                        remote_status = json.loads(status_file.read_text())
                        cache_status['remote'] = remote_status
                        if remote_status.get('exported') is True:
                            export = Path(temp) / 'pnpm-store.tar.gz'
                            boat.call('scp', boat.box + ':/tmp/pnpm-store-export.tar.gz', str(export), timeout=900)
                            rejected = cached[1] if cached and remote_status.get('restored') is False else None
                            cache.publish(export, rejected_sha256=rejected)
                            cache_status['published'] = True
                    except (OSError, EOFError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as error:
                        cache_status['save_warning'] = str(error)
                    (args.output / 'dependency-cache.json').write_text(json.dumps(cache_status, indent=2))
            if args.mode == 'build':
                boat.call('scp', '-r', boat.box + ':/home/user/artifacts', str(args.output), timeout=1800)
                artifacts = args.output / 'artifacts'
                subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=artifacts, check=True)
        finally:
            if boat.box:
                try:
                    boat.call('usage', boat.box)
                finally:
                    boat.cleanup()
    (args.output / 'completed').write_text(datetime.now(timezone.utc).isoformat() + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--revision', required=True, help='Committed revision; uncommitted edits never copied')
    parser.add_argument('--output', type=Path, required=True, help='New directory for artifact, logs and provenance')
    parser.add_argument('--mode', choices=['build', 'test', 'run-tests-for-branch', 'typecheck'], default='build')
    parser.add_argument('--test-file', action='append', default=[])
    parser.add_argument('--test-name', help='Optional focused test name filter')
    parser.add_argument('--no-isolate', action='store_true', help='Pass requested Vitest --no-isolate')
    parser.add_argument('--dependency-cache', type=Path, default=Path.home() / '.cache/t3-boat/pnpm',
                        help='Local pnpm content-store archive cache; no node_modules or credentials')
    # Off by default: the warm path downloads the box's whole pnpm store (over 700 MB) to this machine.
    parser.add_argument('--use-dependency-cache', dest='no_dependency_cache', action='store_false',
                        help='Reuse and refresh the local pnpm store archive (downloads it, several hundred MB)')
    parser.add_argument('--no-dependency-cache', dest='no_dependency_cache', action='store_true',
                        help='Plain pnpm install on the box (the default)')
    parser.set_defaults(no_dependency_cache=True)
    parser.add_argument('--test-case', action='append', default=[], help='One file optionally followed by ::title regex; repeat for distinct groups')
    parser.add_argument('--package', action='append', default=[], help='Exact workspace package name for typecheck; repeat')
    parser.add_argument('--name-prefix', default='t3-build-',
                        help='Box name prefix; the first 12 characters of the commit are appended (default %(default)s)')
    parser.add_argument('--machine-type', default='large', help='Boat machine type (default %(default)s)')
    parser.add_argument('--ttl', type=int, default=7200,
                        help='Box lifetime backstop in seconds; must outlast the whole job, and the build step '
                             'alone may take 70 minutes (default %(default)s)')
    parser.add_argument('--min-included-hours', type=float, default=4,
                        help='Refuse to start unless this many included compute hours remain (default %(default)s)')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,40}', args.name_prefix):
        parser.error('name prefix must be lowercase letters, digits and hyphens')
    if args.ttl <= 0 or args.min_included_hours < 0:
        parser.error('--ttl must be positive and --min-included-hours not negative')
    if args.mode == 'run-tests-for-branch':
        args.mode = 'test'
    if args.mode == 'typecheck' and not args.package:
        parser.error('typecheck requires explicit --package scope')
    if any(not re.fullmatch(r'@?[a-zA-Z0-9][a-zA-Z0-9_./-]*', p) for p in args.package):
        parser.error('invalid package name')
    args.test_case = [tuple(value.split('::', 1)) if '::' in value else (value, '') for value in args.test_case]
    paths = args.test_file + [path for path, pattern in args.test_case]
    if any(not p or p.startswith('-') or Path(p).is_absolute() or '..' in Path(p).parts for p in paths):
        parser.error('test paths must be repository-relative and cannot be options')
    if args.mode == 'test' and not (args.test_file or args.test_case):
        parser.error('test mode requires at least one focused --test-file')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    build(args)


if __name__ == '__main__':
    main()
