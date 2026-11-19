# autoreview-bench

Two read-only reports on the autoreviewers attached to a T3 Code thread, read
from T3's SQLite state database.

- `autoreview-bench` prints a Markdown table per reviewer: model, effort,
  attached or removed, batches answered, LGTM answers, estimated findings and
  forwarded posts, turnaround (median, p90, max), recorded input and output
  tokens, and meta-review verdict counts. It prints aggregates only, never
  message text.
- `autoreview-briefs` writes a Markdown file listing every reviewer ever
  attached to the thread (or to one of its reviewers), with model, effort,
  state, custom preamble, `minBatchChars` and brief, so any of them can be
  re-added. Hand-written `Note (` lines under an entry survive a rerun.

## Status

Working. Evidence:

- 15 offline tests pass (`python3 -m unittest discover -p 'test_*.py'`). They
  use in-memory or temporary SQLite files only; `test_cli.py` runs both
  commands end to end and checks the fixture file is byte-for-byte unchanged.
- Run read-only against a real T3 database on a thread with several
  reviewers: both commands exited 0, and the bench attributed nearly all
  posts.

## Requirements

Python 3.8+ standard library. T3 Code's state database
(`~/.t3/userdata/statev2.sqlite`, `orchestration_v2_projection_*` tables).

## Usage

```sh
./autoreview-bench  --thread <parent thread id> [--db PATH] [--meta-prefix 'Meta-review ']
./autoreview-briefs --thread <parent thread id> [--db PATH] [--out autoreviewer-briefs.md]
```

The database defaults to `$T3_STATE_DB`, else `~/.t3/userdata/statev2.sqlite`.
It is opened with `mode=ro` inside one read transaction; nothing is written to
it. `autoreview-briefs` writes only `--out` (default `./autoreviewer-briefs.md`).

To find a thread that has reviewers, the reviewer children have
`autoreview-add` in their thread id and their parent in
`payload_json.lineage.parentThreadId`.

## How things are counted

- **Finding posts** in the parent thread, in four formats: `Autoreview from
  "<title>"`; the combined `Autoreview findings from reviewers…` post with
  `--- Autoreview from "<title>" (thread <id>) ---` blocks; `Delegated task
  …autoreview-findings… [completed]:`; and (newer builds) a post sent by the
  reviewer whose first line is its short name followed by `:`. LGTM-only
  posts are skipped.
- **Findings≈** is an estimate: top-level numbered items, else bold lead
  lines, else top-level bullets, else paragraphs.
- **Turnaround** is the final assistant message's `updated_at` minus the batch
  message's `created_at`, in completed runs. These are T3 timestamps and
  include queue time. p90 is nearest rank.
- **Verdicts** (H/W/S/D/O/N = HOLDS, WRONG, STALE, DUPLICATE, OUT OF REMIT,
  NIT) are read from table rows in the answers of reviewers whose title starts
  with `--meta-prefix` (default `Meta-review `, case-insensitive). The first
  column must match a reviewer's full title or a unique short name.
- **Tokens** sum complete `turnTokenUsage` input and output per provider turn
  in batch runs. Input already includes cache reads and writes. `*` marks a
  partial total. This is raw throughput, not quota or billing.

## Limits

- Depends on T3's internal table layout, which can change between builds. On
  an unexpected layout the bench exits 1 with a generic message (so no data
  leaks); the briefs command exits with the error type.
- Finding counts are heuristics on free text. Summary-style reviewers count
  as posting findings like any other reviewer.
- Small samples; reviewers get different batches. Not a controlled model
  comparison.
