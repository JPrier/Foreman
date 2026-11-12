"""Append-only events with rebuildable projections; never an orchestration authority."""
from __future__ import annotations

import hashlib
from collections import Counter
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .accounting import money, normalize
from .config import data_dir


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, sort_keys=True, default=str, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def private(value):
    """Defence in depth; adapters must still construct metadata-only payloads."""
    denied = {'authorization', 'api_key', 'key', 'prompt', 'messages', 'text', 'content', 'stdout',
              'stderr', 'full_output', 'summary', 'input', 'output', 'command', 'error'}
    if isinstance(value, dict):
        return {k: private(v) for k, v in value.items() if k.lower() not in denied or (k.lower() in ('input', 'output') and isinstance(v, (int, float)))}
    if isinstance(value, list):
        return [private(v) for v in value]
    return value


class Telemetry:
    def __init__(self, path=None):
        self.path = Path(path or data_dir() / 'telemetry.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.elapsed_seconds = 0.0
        self.cpu_seconds = 0.0
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_migrations VALUES(1);
                CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL, task_id TEXT, kind TEXT NOT NULL, timestamp_utc TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
                CREATE INDEX IF NOT EXISTS events_task ON events(run_id,task_id,seq);
                CREATE TABLE IF NOT EXISTS calls(call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS calls_run ON calls(run_id);
                CREATE TABLE IF NOT EXISTS charges(charge_id TEXT PRIMARY KEY, call_id TEXT NOT NULL,
                    provider_scope TEXT NOT NULL, generation_id TEXT, source TEXT NOT NULL, amount_usd TEXT NOT NULL,
                    observed_utc TEXT NOT NULL, native_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS charges_call ON charges(call_id);
                CREATE TABLE IF NOT EXISTS tasks(run_id TEXT NOT NULL, task_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(run_id,task_id));
            ''')
            version = db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
            if version != 1:
                raise ValueError('Unsupported telemetry schema version')
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def db(self):
        start, cpu = time.perf_counter(), time.process_time()
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
            self.elapsed_seconds += time.perf_counter() - start
            self.cpu_seconds += time.process_time() - cpu

    def event(self, run_id, kind, *, task_id=None, event_id=None, payload=None, **envelope):
        event_id = event_id or 'evt_' + uuid.uuid4().hex
        record = {'schema_version': 1, 'event_id': event_id, 'run_id': run_id, 'kind': kind,
                  'task_id': task_id, 'timestamp_utc': now(), **envelope, 'payload': private(payload or {})}
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO events(event_id,run_id,task_id,kind,timestamp_utc,data) VALUES(?,?,?,?,?,?)',
                (event_id, run_id, task_id, kind, record['timestamp_utc'], encode(record)))
        return event_id

    def run(self, run_id, **fields):
        with self.db() as db:
            old = db.execute('SELECT data FROM runs WHERE run_id=?', (run_id,)).fetchone()
            record = json.loads(old[0]) if old else {'run_id': run_id, 'started_utc': now(), 'status': 'running'}
            record.update(private(fields))
            db.execute('INSERT OR REPLACE INTO runs VALUES(?,?)', (run_id, encode(record)))
        return record

    def runs(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM runs ORDER BY run_id')]

    def task(self, run_id, task_id, **fields):
        with self.db() as db:
            old = db.execute('SELECT data FROM tasks WHERE run_id=? AND task_id=?', (run_id, task_id)).fetchone()
            record = json.loads(old[0]) if old else {'task_id': task_id, 'parent_task_id': None}
            record.update(private(fields))
            db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?)', (run_id, task_id, encode(record)))

    def update_call(self, call_id, **fields):
        with self.db() as db:
            old = db.execute('SELECT data FROM calls WHERE call_id=?', (call_id,)).fetchone()
            record = json.loads(old[0]) if old else {'call_id': call_id, 'started_utc': now(), 'accounting_status': 'unknown'}
            record.update(private(fields))
            db.execute('INSERT OR REPLACE INTO calls VALUES(?,?,?)', (call_id, record['run_id'], encode(record)))

    def calls(self, run_id):
        with self.db() as db:
            return [c for r in db.execute('SELECT data FROM calls WHERE run_id=? ORDER BY call_id', (run_id,)) if (c := json.loads(r[0])).get('accounting_status') != 'boundary']

    def charge(self, call_id, scope, generation, source, amount, native=None):
        amount = money(amount)
        if amount is None:
            return
        native = private(native or {})
        # Immutable snapshots; newest snapshot is selected, old facts survive corrections.
        ident = digest([call_id if not generation else None, scope, generation, source, str(amount), native])
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO charges VALUES(?,?,?,?,?,?,?,?)',
                (ident, call_id, scope, generation, source, str(amount), now(), encode(native)))

    def call_charges(self, call_id):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM charges WHERE call_id=? ORDER BY observed_utc, rowid', (call_id,))]

    def timeline(self, run_id, task_id=None):
        with self.db() as db:
            sql, params = 'SELECT data FROM events WHERE run_id=?', [run_id]
            if task_id is not None:
                sql += ' AND task_id=?'
                params.append(task_id)
            return [json.loads(r[0]) for r in db.execute(sql + ' ORDER BY seq', params)]

    def opencode(self, run_id, task_id, event, *, role='worker', attempt_id=None, model=None, billing_mode=None):
        session = event.get('sessionID')
        part = event.get('part') or {}
        kind = event.get('type')
        if not session or not isinstance(part, dict):
            self.event(run_id, 'adapter.failure', task_id=task_id, event_id=digest([run_id, event]), payload={'adapter': 'opencode', 'reason': 'missing_session_or_part'})
            return
        step = part.get('id') or part.get('messageID')
        if kind == 'step_start':
            self.event(run_id, 'llm.started', task_id=task_id,
                event_id=digest([run_id, session, part.get('messageID'), 'started']),
                actor_role=role, attempt_id=attempt_id, source='opencode_jsonl',
                payload={'session_id': session, 'message_id': part.get('messageID')})
        if kind == 'step_finish':
            if not step:
                self.event(run_id, 'adapter.failure', task_id=task_id, payload={'reason': 'missing_step_identity'})
                return
            call_id = digest([run_id, session, step])
            usage = normalize(part.get('tokens'), 'opencode')
            generation = part.get('generationID') or (part.get('metadata') or {}).get('generation_id')
            starts = [e['timestamp_utc'] for e in self.timeline(run_id, task_id)
                if e['kind'] == 'llm.started' and e['payload'].get('session_id') == session
                and e['payload'].get('message_id') == part.get('messageID')]
            fields = {'started_utc': starts[-1]} if starts else {}
            self.update_call(call_id, **fields, run_id=run_id, task_id=task_id, attempt_id=attempt_id, session_id=session,
                message_id=part.get('messageID'), actor_role=role, model_id=model,
                provider_id='openrouter' if (model or '').startswith('openrouter/') else 'opencode',
                generation_id=generation, usage=usage, billing_mode=billing_mode, finished_utc=now(), accounting_status='observed')
            if billing_mode != 'subscription':
                self.charge(call_id, 'default', generation, 'opencode_observed', part.get('cost'),
                    {'tokens': part.get('tokens'), 'finish_reason': part.get('reason')})
            self.event(run_id, 'llm.completed', task_id=task_id, event_id=call_id,
                attempt_id=attempt_id, actor_role=role, span_id=call_id, source='opencode_jsonl', payload={'call_id': call_id, 'session_id': session, 'finish_reason': part.get('reason')})
        elif kind == 'tool_use':
            state = part.get('state') or {}
            tool_id = part.get('callID') or part.get('id')
            if tool_id:
                self.event(run_id, 'tool.finished' if state.get('status') in ('completed', 'error') else 'tool.started',
                    task_id=task_id, event_id=digest([run_id, session, tool_id, state.get('status')]), attempt_id=attempt_id,
                    payload={'tool_name': part.get('tool'), 'status': state.get('status'), 'time': state.get('time'),
                             'input_sha256': digest(state.get('input'))})
        if kind in ('step_start', 'step_finish', 'text', 'tool_use'):
            self.event(run_id, 'session.linked', task_id=task_id, event_id=digest([run_id, session, 'linked']),
                payload={'session_id': session})

    def metrics(self, run_id, task_id=None):
        runs = {r['run_id']: r for r in self.runs()}
        if run_id not in runs:
            raise ValueError('Unknown run: ' + run_id)
        calls = self.calls(run_id)
        if task_id is not None:
            ids = {t['task_id'] for t in self.tree(run_id) if t['task_id'] == task_id or task_id in t['ancestors']}
            calls = [c for c in calls if c.get('task_id') in ids]
        # Group aliases before aggregation. Never timestamp-join generations.
        groups = {}
        for call in calls:
            key = (call.get('provider_scope', 'default'), call['generation_id']) if call.get('generation_id') else ('call', call['call_id'])
            groups.setdefault(key, []).append(call)
        totals = {}
        selected, unknown, settled = Decimal(0), 0, 0
        sources, roles, categories = set(), {}, {}
        breakdown = {'role': {}, 'model': {}, 'session': {}}
        input_sizes = []
        for aliases in groups.values():
            charges = [ch for c in aliases for ch in self.call_charges(c['call_id'])]
            by_source = {}
            for charge in sorted(charges, key=lambda c: c['observed_utc']):
                by_source[charge['source']] = money(charge['amount_usd'])
            for source, amount in by_source.items():
                totals[source] = totals.get(source, Decimal(0)) + amount
            source = next((s for s in ('openrouter_settled', 'openrouter_response', 'opencode_observed', 'estimated', 'mock') if s in by_source), None)
            if source:
                selected += by_source[source]
                sources.add(source)
            else:
                unknown += 1
            settled += 'openrouter_settled' in by_source
            c = next((a for a in aliases if a.get('usage', {}).get('usage_complete')), aliases[0])
            role = c.get('actor_role', 'unattributed')
            roles[role] = roles.get(role, 0) + 1
            usage = c.get('usage') or {}
            for dimension, label in [('role', role), ('model', c.get('model_id') or 'unknown'), ('session', c.get('session_id') or 'none')]:
                bucket = breakdown[dimension].setdefault(label, {'calls': 0, 'known_usd': Decimal(0), 'usage': {}})
                bucket['calls'] += 1
                bucket['known_usd'] += by_source.get(source, Decimal(0))
                for key in ('input_total_tokens', 'output_total_tokens', 'reasoning_tokens', 'input_cache_read_tokens', 'input_cache_write_tokens'):
                    bucket['usage'].setdefault(key, []).append(usage.get(key))
            for name in ('input_uncached_tokens', 'input_total_tokens', 'output_total_tokens', 'reasoning_tokens', 'input_cache_read_tokens', 'input_cache_write_tokens'):
                values = categories.setdefault(name, [])
                values.append(usage.get(name))
            if usage.get('input_total_tokens') is not None:
                input_sizes.append(usage['input_total_tokens'])
        for buckets in breakdown.values():
            for bucket in buckets.values():
                bucket['known_usd'] = str(bucket['known_usd'])
                bucket['usage'] = {k: sum(v) if all(n is not None for n in v) else None for k, v in bucket['usage'].items()}
        events = self.timeline(run_id, task_id)
        recovered = {e['payload'].get('session_id') for e in events if e['kind'] == 'session.recovered'}
        flags = [e for e in events if e['kind'] in ('adapter.failure', 'session.incomplete', 'telemetry.failure')
            and not (e['kind'] == 'session.incomplete' and e['payload'].get('session_id') in recovered)]
        # Unverified child enumeration is always a coverage gap.
        sessions = {e['payload']['session_id'] for e in events if e['kind'] == 'session.linked'}
        enumerated = {e['payload']['session_id'] for e in events if e['kind'] == 'session.enumerated'}
        gaps = len(sessions - enumerated) + len(flags) + int(bool(runs[run_id].get('measurement_incomplete')))
        count = len(groups)
        usage = {k: sum(v) if v and all(x is not None for x in v) else None for k, v in categories.items()}
        usage.update(input_tokens=usage.get('input_total_tokens'), output_tokens=usage.get('output_total_tokens'),
            cache_read_tokens=usage.get('input_cache_read_tokens'), cache_write_tokens=usage.get('input_cache_write_tokens'))
        usage['cached_input_ratio'] = (usage['cache_read_tokens'] / usage['input_tokens']) if usage.get('input_tokens') and usage.get('cache_read_tokens') is not None else None
        usage['coverage'] = 'complete' if count and not gaps and all((c.get('usage') or {}).get('usage_complete') for c in calls) else 'incomplete'
        timestamps = runs[run_id]
        wall = None
        if timestamps.get('finished_utc'):
            wall = (datetime.fromisoformat(timestamps['finished_utc']) - datetime.fromisoformat(timestamps['started_utc'])).total_seconds()
        evidence = [e for e in events if e['kind'] == 'verify.finished']
        sizes = sorted(input_sizes)
        return {'schema_version': 1, **timestamps, 'wall_seconds': wall, 'llm_calls': count, 'calls_by_role': roles,
            'tasks': {'total': len(self.tree(run_id)), **{s: sum(t.get('status') == s for t in self.tree(run_id)) for s in ('passed', 'failed', 'blocked')}},
            'unattributed_calls': sum(c.get('task_id') is None for c in calls), 'breakdown': breakdown,
            'usage': usage, 'context': {'p50': sizes[len(sizes)//2] if sizes else None,
            'p95': sizes[min(len(sizes)-1, int(len(sizes)*.95))] if sizes else None, 'max': max(sizes) if sizes else None},
            'cost': {**{s + '_usd': str(v) for s, v in totals.items()},
                'selected_total_usd': str(selected) if not unknown and not gaps else None,
                'selected_known_usd': str(selected), 'selected_total_source': next(iter(sources)) if len(sources) == 1 else 'mixed',
                'unreconciled_calls': count - settled, 'unknown_cost_calls': unknown,
                'provider_coverage': 'complete' if count and settled == count and not gaps else 'incomplete',
                'provider_coverage_ratio': settled / count if count else None},
            'evidence': {s: sum(e['payload'].get('status') == s for e in evidence) for s in ('pass', 'fail', 'unverified')},
            'model_wall_seconds': sum(max(0, (datetime.fromisoformat(c['finished_utc']) - datetime.fromisoformat(c['started_utc'])).total_seconds())
                for c in calls if c.get('finished_utc') and c.get('started_utc')),
            'cost_per_verified_condition_usd': str(selected / sum(e['payload'].get('status') == 'pass' for e in evidence)) if not unknown and not gaps and any(e['payload'].get('status') == 'pass' for e in evidence) else None,
            'coverage_gaps': gaps, 'activity': {
                'tool_calls': sum(e['kind'] == 'tool.finished' for e in events),
                'retries': sum(e['kind'] == 'task.retried' for e in events),
                'recoveries': sum(e['kind'] == 'session.recovered' for e in events),
                'repeated_tool_inputs': sum(n - 1 for n in Counter(
                    (e['payload'].get('tool_name'), e['payload'].get('input_sha256')) for e in events
                    if e['kind'] == 'tool.finished' and e['payload'].get('input_sha256')).values())},
            'overhead': {**timestamps.get('telemetry_overhead', {}), 'query_seconds': self.elapsed_seconds,
                'query_cpu_seconds': self.cpu_seconds, 'storage_bytes': self.path.stat().st_size}}

    def tree(self, run_id):
        with self.db() as db:
            tasks = {r[0]: json.loads(r[1]) for r in db.execute('SELECT task_id,data FROM tasks WHERE run_id=?', (run_id,))}
        for ident, task in tasks.items():
            ancestors, parent = [], task.get('parent_task_id')
            while parent:
                if parent == ident or parent in ancestors:
                    raise ValueError('Task hierarchy cycle')
                ancestors.append(parent)
                parent = tasks.get(parent, {}).get('parent_task_id')
            task['ancestors'] = ancestors
        costs = {i: Decimal(0) for i in tasks}
        events = self.timeline(run_id)
        enumerated = {e['payload'].get('session_id') for e in events if e['kind'] == 'session.enumerated'}
        recovered = {e['payload'].get('session_id') for e in events if e['kind'] == 'session.recovered'}
        unknown = {e['task_id'] for e in events if
            (e['kind'] == 'session.linked' and e['payload'].get('session_id') not in enumerated) or
            (e['kind'] == 'session.incomplete' and e['payload'].get('session_id') not in recovered) or
            e['kind'] in ('adapter.failure', 'telemetry.failure')}
        if None in unknown:
            unknown.update(tasks)
        units = {}
        for call in self.calls(run_id):
            key = (call.get('provider_scope', 'default'), call.get('generation_id') or call['call_id'])
            units.setdefault(key, []).append(call)
        for aliases in units.values():
            owners = {c.get('task_id') for c in aliases}
            if len(owners) != 1:
                unknown.update(owners.intersection(costs))
                continue
            owner = next(iter(owners))
            if owner not in costs:
                continue
            snapshots = [ch for c in aliases for ch in self.call_charges(c['call_id'])]
            charges = {c['source']: money(c['amount_usd']) for c in sorted(snapshots, key=lambda ch: ch['observed_utc'])}
            source = next((s for s in ('openrouter_settled', 'openrouter_response', 'opencode_observed', 'estimated', 'mock') if s in charges), None)
            if source:
                costs[owner] += charges[source]
            else:
                unknown.add(owner)
        for ident, task in tasks.items():
            children = [i for i, t in tasks.items() if ident in t['ancestors']]
            task.update(direct_known_usd=str(costs[ident]), descendant_known_usd=str(sum((costs[i] for i in children), Decimal(0))),
                inclusive_known_usd=str(costs[ident] + sum((costs[i] for i in children), Decimal(0))),
                cost_complete=not bool(unknown.intersection({ident, *children})))
        return [tasks[i] for i in sorted(tasks)]

    def import_state(self, state, directory=None):
        """Historical state import is deterministic and explicitly best effort."""
        run_id = state.get('telemetry_run_id') or 'run_legacy_' + state['id']
        self.run(run_id, task_root_id=state['id'], status=state['status'], started_utc=state['created_at'],
                 finished_utc=state.get('updated_at'))
        self.task(run_id, state['id'], status=state['status'])
        for ident, assignment in state.get('assignments', {}).items():
            self.task(run_id, ident, parent_task_id=assignment.get('parent_task_id') or state['id'],
                status=assignment['status'], retry_of=assignment.get('retry_of'), review_of=assignment.get('review_of'))
        for index, event in enumerate(state.get('events', [])):
            kind = event.get('kind')
            if kind not in ('director', 'worker') or event.get('telemetry_call_id'):
                continue
            task_id = event.get('id', state['id'])
            imported = False
            if kind == 'worker' and directory:
                path = Path(directory) / 'workers' / (task_id + '.jsonl')
                if path.is_file():
                    for line in path.read_text(encoding='utf-8').splitlines():
                        try:
                            obj = json.loads(line)
                            self.opencode(run_id, task_id, obj, model=event.get('model'))
                            imported |= obj.get('type') == 'step_finish'
                        except (ValueError, TypeError):
                            self.event(run_id, 'adapter.failure', task_id=task_id, event_id=digest([run_id, task_id, line]), payload={'adapter': 'legacy_jsonl'})
            if imported:
                continue
            ident = digest([run_id, 'legacy', index])
            raw = event.get('usage') or {}
            usage = normalize(raw, 'openrouter') if kind == 'director' and 'prompt_tokens' in raw else normalize({}, 'unknown')
            self.update_call(ident, run_id=run_id, task_id=task_id, actor_role=kind,
                model_id=event.get('model'), provider_id='openrouter' if kind == 'director' else 'opencode', usage=usage)
            self.charge(ident, 'default', None, 'openrouter_response' if kind == 'director' else 'opencode_observed',
                event.get('cost_usd', event.get('reported_cost_usd')))
        if not state.get('telemetry_run_id'):
            self.event(run_id, 'session.incomplete', event_id=digest([run_id, 'legacy_coverage']),
                payload={'reason': 'historical_child_sessions_and_billing_not_verified'})
        return run_id


class Recorder:
    """Normal runs survive measurement failure; strict benchmarks fail closed."""
    def __init__(self, state, strict=False):
        import warnings
        self.state, self.strict = state, strict
        self.store = None
        try:
            self.store = Telemetry()
        except Exception:
            state['telemetry_incomplete'] = True
            if strict:
                raise
            warnings.warn('Telemetry unavailable; measurement incomplete', RuntimeWarning)

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            import warnings
            try:
                if self.store is not None:
                    return getattr(self.store, name)(*args, **kwargs)
            except Exception:
                self.state['telemetry_incomplete'] = True
                if self.strict:
                    raise
                warnings.warn('Telemetry write failed; measurement incomplete', RuntimeWarning)
        return invoke


def safe_opencode_event(event):
    """Persist replayable accounting records without assistant/tool content."""
    part = event.get('part') or {}
    result = {k: event[k] for k in ('type', 'sessionID', 'timestamp') if k in event}
    result['part'] = {k: part[k] for k in ('id', 'messageID', 'sessionID', 'tokens', 'cost', 'reason', 'tool', 'callID', 'generationID') if k in part}
    metadata = part.get('metadata') or {}
    if metadata.get('generation_id'):
        result['part']['metadata'] = {'generation_id': metadata['generation_id']}
    if part.get('state'):
        result['part']['state'] = {k: part['state'][k] for k in ('status', 'time') if k in part['state']}
    return result
