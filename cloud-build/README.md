# cloud-build

Builds or tests one committed revision of the T3 Code repository on a
disposable [boat.dev](https://boat.dev) box, brings back the artifacts and
logs, and deletes the box. It never stages, installs or launches what it
builds.

Modes:

- `build` (default): runs the repo's `pnpm run dist:desktop:linux` and returns
  the AppImage with a `SHA256SUMS` that is checked locally.
- `test`: runs focused Vitest files (`--test-file`, `--test-case FILE::REGEX`,
  optional `--test-name`, `--no-isolate`), plus scoped typechecks for any
  `--package`. `run-tests-for-branch` is an alias.
- `typecheck`: `effect-tsgo tsrs --noEmit` per `--package` (exact workspace
  names, checked against the archive first).

## Requirements

- Python 3.11+ (uses `hashlib.file_digest`), `git`, `sha256sum`.
- The `boat` CLI on `PATH`, signed in, on a plan that allows the machine
  type you ask for (`large` = 8 vCPU / 16 GB).
- A T3 Code checkout whose `package.json` pins an exact `engines.node` and
  `packageManager: pnpm@X.Y.Z`, with `pnpm-lock.yaml` committed.

## Usage

```sh
python3 boat_t3_build.py --repo ~/code/t3code --revision <sha-or-branch> --output ~/builds/t3-<sha>
python3 boat_t3_build.py --repo ~/code/t3code --revision main --output /tmp/t3-test \
    --mode test --test-file apps/server/src/foo.test.ts --package t3
```

`--output` must not exist yet. Other options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--name-prefix` | `t3-build-` | Box name; the first 12 characters of the commit are appended |
| `--machine-type` | `large` | Boat machine type |
| `--ttl` | `7200` | Box lifetime backstop, seconds. It must outlast the whole job: the build step alone may run 70 minutes, plus uploads and downloads |
| `--min-included-hours` | `4` | Refuse to start with less included compute left |
| `--dependency-cache` | `~/.cache/t3-boat/pnpm` | Local pnpm store cache |
| `--use-dependency-cache` | off | Reuse and refresh the local pnpm store archive. It downloads the box's whole store (over 700 MB at the time of writing), so leave it off on metered networks |
| `--no-dependency-cache` | on | Plain `pnpm install --frozen-lockfile` on the box |

## What it sends and keeps

- Sends only `git archive` of the exact commit (no working tree, history,
  `.repos` or `.claude`). The archive is rejected if it contains symlinks,
  `.git`, `.t3`, `.codex`, `.claude`, `.ssh`, `credentials`, `.env*` or
  common key or auth file names. This is a guard, not a secret scanner.
- The box is created with `--no-env --no-snapshots` and no ports.
- It refuses to start unless the next start costs $0, at least 2 starts are
  left in each window, and `--min-included-hours` remain.
- Writes to `--output`: `provenance.json`, every CLI log, `box-id` as soon as
  the box exists, `source.sha256`, `dependency-cache.json` (unless
  `--no-dependency-cache`), `deletion.json`, `deleted`, `completed`, and
  `artifacts/` in build mode.

## Limits

- Specific to boat.dev and to T3 Code's scripts and workspace layout
  (`apps/`, `packages/`, `vp test`, `effect-tsgo`). Another project needs a
  new `remote_script`.
- The box installs Ubuntu packages, Node (checked against nodejs.org
  SHASUMS), pnpm and Rust stable from the internet, so builds are not
  hermetic.
- Deletion runs in `finally`. A SIGKILL or power loss skips it: read
  `<output>/box-id` and run `boat --no-update --json delete <id> --yes`. If
  creation failed before an ID came back, look for the name prefix in
  `boat --no-update --json list`.
