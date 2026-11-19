#!/usr/bin/env python3
"""List T3 threads whose latest run stopped (cancelled, interrupted or failed), read-only.

Each line is one JSON object; "skip" is null when nothing obvious blocks resuming it,
otherwise the reason. It never marks, sends or writes anything in T3.
"""
import argparse
import importlib.util
import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import time

_spec = importlib.util.spec_from_file_location('t3_recovery_config', Path(__file__).with_name('recovery.py'))
_recovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_recovery)
CONFIG, STATE, expand = _recovery.CONFIG, _recovery.STATE, _recovery.expand
DB = Path(os.environ.get('T3_STATE_DB') or Path.home() / '.t3/userdata/statev2.sqlite')
CATEGORIES = ('fable', 'non-fable', 'codex')


def retired_ids(path=None):
    """Thread ids to skip, one per line ('#' starts a comment). No file configured: none."""
    path = CONFIG['retired_threads_file'] if path is None else path
    if not path:
        return set()
    lines = expand(path).read_text().splitlines()
    return {line.split('#', 1)[0].strip() for line in lines} - {''}


def usage_holds(path=None, state=None, now=None):
    """Usage categories currently held, or None when no usage tracking is configured.

    With a latch file configured, a missing or unreadable file raises: unknown is not
    permission to resume. A state-dir category-holds.json ({category: until_epoch}) adds
    timed holds either way.
    """
    path = CONFIG['usage_holds_file'] if path is None else path
    state = STATE if state is None else state
    now = time.time() if now is None else now
    holds = None
    if path:
        data = json.loads(expand(path).read_text())
        latches = data['latches']
        if not isinstance(latches, dict):
            raise ValueError('invalid usage state')
        holds = set()
        for latch in latches.values():
            category = latch['category']
            if category not in CATEGORIES:
                raise ValueError('unknown usage category')
            holds.add(category)  # Only the latch writer releases latches, not a guessed end time.
    try:
        categories = json.loads((state / 'category-holds.json').read_text())
    except FileNotFoundError:
        categories = {}
    for category, until in categories.items():
        if category not in CATEGORIES:
            raise ValueError('unknown category hold')
        holds = set() if holds is None else holds
        if float(until) > now:
            holds.add(category)
    return holds


def category(selection):
    model = selection.get('model', '').lower()
    if model.startswith('gpt-') or selection.get('instanceId') == 'codex':
        return 'codex'
    if model.startswith('claude-'):
        return 'fable' if 'fable' in model else 'non-fable'
    return None


def candidates(connection, *, retired, holds=None, handled=(), now=None):
    """holds=None means usage is not tracked, so model category is not checked."""
    now = time.time() if now is None else now
    rows = connection.execute('''
      SELECT r.thread_id,r.run_id,r.status,r.completed_at,r.payload_json,t.payload_json
      FROM orchestration_v2_projection_runs r
      JOIN orchestration_v2_projection_threads t USING(thread_id)
      WHERE r.status IN ('cancelled','interrupted','failed') AND r.completed_at IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM orchestration_v2_projection_runs newer
          WHERE newer.thread_id=r.thread_id AND newer.ordinal>r.ordinal
            AND newer.status NOT IN ('queued','rolled_back')
            AND NOT (newer.status='cancelled'
              AND json_extract(newer.payload_json,'$.startedAt') IS NULL
              AND json_extract(newer.payload_json,'$.restartContinuationOfRunId') IS NULL))
      ORDER BY r.thread_id,r.ordinal DESC
    ''').fetchall()
    for tid, rid, status, done, raw_run, raw_thread in rows:
        run, thread = json.loads(raw_run), json.loads(raw_thread)
        if run.get('startedAt') is None and run.get('restartContinuationOfRunId') is None:
            continue  # An untouched cancelled queued message is not stopped execution.
        command = 'command:restart-continuation:' + rid
        skip = None
        if tid in retired or thread.get('roleRetirement') is not None:
            skip = 'retired role'
        elif thread.get('deletedAt') or thread.get('archivedAt') or thread.get('settledOverride') == 'settled':
            skip = 'closed thread'
        elif thread.get('snoozedUntil') and datetime.fromisoformat(thread['snoozedUntil'].replace('Z', '+00:00')).timestamp() > now:
            skip = 'snoozed thread'
        elif connection.execute('''SELECT 1 FROM orchestration_v2_projection_runs
                WHERE thread_id=? AND status IN ('preparing','starting','running','waiting','queued') LIMIT 1''', (tid,)).fetchone():
            skip = 'existing active or queued run'
        elif run.get('modelSelection') != thread.get('modelSelection'):
            skip = 'selection changed; native reconciliation required'
        elif holds is not None and category(thread.get('modelSelection') or {}) is None:
            skip = 'unknown usage category'
        elif holds is not None and category(thread.get('modelSelection') or {}) in holds:
            skip = 'usage category held'
        elif connection.execute('''SELECT 1 FROM orchestration_v2_projection_runtime_requests
                WHERE thread_id=? AND status='pending' LIMIT 1''', (tid,)).fetchone():
            skip = 'pending human request'
        elif connection.execute('''SELECT 1 FROM orchestration_v2_projection_turn_items
                WHERE thread_id=? AND type='secret_request'
                  AND json_extract(payload_json,'$.secretStatus')='pending' LIMIT 1''', (tid,)).fetchone():
            skip = 'pending secret request'
        elif rid in handled:
            skip = 'legacy attempt recorded; reconcile'
        elif connection.execute('SELECT 1 FROM orchestration_command_receipts WHERE command_id=?', (command,)).fetchone():
            skip = 'native command receipt; reconcile'
        elif connection.execute('''SELECT 1 FROM orchestration_v2_projection_runs
                WHERE thread_id=? AND json_extract(payload_json,'$.restartContinuationOfRunId')=? LIMIT 1''', (tid, rid)).fetchone():
            skip = 'existing continuation run'
        elif connection.execute('''SELECT 1 FROM orchestration_v2_projection_messages
                WHERE thread_id=? AND message_id=? LIMIT 1''', (tid, 'message:restart-continuation:' + rid)).fetchone():
            skip = 'existing continuation message'
        elif connection.execute('SELECT 1 FROM orchestration_v2_effect_outbox WHERE effect_id=?',
                                ('effect:restart-continuation:' + rid,)).fetchone():
            skip = 'native continuation effect; reconcile'
        yield dict(thread_id=tid, run_id=rid, status=status, completed_at=done,
                   command_id=command, skip=skip)


def inventory(db=DB, state=None):
    holds = usage_holds(state=state)
    retired = retired_ids()
    uri = Path(db).resolve().as_uri() + '?mode=ro'
    if not Path(db).exists():
        raise FileNotFoundError(db)
    with closing(sqlite3.connect(uri, uri=True, timeout=.25)) as connection:
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')  # Consistent metadata snapshot; never a T3 write.
        return list(candidates(connection, retired=retired, holds=holds))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DB,
                        help='T3 state database, opened read-only (default: $T3_STATE_DB or %(default)s)')
    parser.add_argument('--eligible', action='store_true', help='only print threads with no skip reason')
    args = parser.parse_args()
    for source in inventory(args.db):
        if not args.eligible or source['skip'] is None:
            print(json.dumps(source))
