"""Pinned, isolated repeated trials with trusted scoring outside worker control."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import platform
import random
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path

from .backends import OpenCodeWorker, MockWorker
from .engine import Engine
from .storage import new_task
from .telemetry import Telemetry, digest, now

STRATEGIES = {'direct', 'delegate-with-helper', 'adaptive-recursion'}


def snapshot(path):
    entries = {}
    for item in sorted(path.rglob('*')):
        relative = item.relative_to(path).as_posix()
        if '.git' in item.relative_to(path).parts:
            continue
        if item.is_symlink():
            raise ValueError('Fixture and result symlinks are not supported: ' + relative)
        if item.is_file():
            entries[relative] = hashlib.sha256(item.read_bytes()).hexdigest()
    return entries


def fixture_revision(path):
    return digest(snapshot(Path(path)))


def resolve_file(base, name):
    path = (base / name).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('Suite paths must remain under the suite directory')
    return path


def load_suite(path, registry):
    path = Path(path).resolve()
    suite = json.loads(path.read_text(encoding='utf-8'))
    if suite.get('schema_version') != 1 or not suite.get('suite_id') or not suite.get('tasks'):
        raise ValueError('Invalid benchmark suite v1')
    variants = suite.get('variants', {})
    if not variants or any(v.get('strategy') not in STRATEGIES for v in variants.values()):
        raise ValueError('Unknown benchmark strategy')
    ids = set()
    for task in suite['tasks']:
        if task['case_id'] in ids:
            raise ValueError('Duplicate benchmark case')
        ids.add(task['case_id'])
        fixture = resolve_file(path.parent, task['repo_fixture'])
        if fixture_revision(fixture) != task['revision']:
            raise ValueError('Fixture content hash does not match pinned revision: ' + task['case_id'])
        prompt = resolve_file(path.parent, task['prompt_file']).read_text(encoding='utf-8')
        for check in task['acceptance']:
            if check['kind'] == 'command':
                command = registry.get(check['registered_check'])
                if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
                    raise ValueError('Unknown trusted registered check: ' + check['registered_check'])
            elif check['kind'] != 'git-diff':
                raise ValueError('Unknown acceptance kind')
        task['_fixture'], task['_prompt'] = str(fixture), prompt
    return suite


def score(workspace, before, acceptance, registry, store, run_id, task_id):
    """Registry and acceptance are loaded before dispatch, never from worker outputs."""
    results = []
    for check in acceptance:
        ident = digest(check)
        store.event(run_id, 'verify.started', task_id=task_id, payload={'check_id': check['id'], 'fingerprint': ident})
        start = time.monotonic()
        reason, code = None, None
        try:
            if check['kind'] == 'command':
                proc = subprocess.run(registry[check['registered_check']], cwd=workspace,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                code = proc.returncode
                passed = code == 0
            else:
                after = snapshot(workspace)
                changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
                allowed = check['allowed_paths']
                passed = all(any(p == prefix or (prefix.endswith('/') and p.startswith(prefix)) for prefix in allowed) for p in changed)
        except (OSError, ValueError):
            passed, reason = False, 'check_execution_failed'
        result = {'check_id': check['id'], 'fingerprint': ident, 'status': 'pass' if passed else 'fail',
                  'exit_status': code, 'elapsed_seconds': time.monotonic() - start, 'reason': reason}
        store.event(run_id, 'verify.finished', task_id=task_id, payload=result)
        results.append(result)
    return results


def run_suite(path, variants, repeats, cfg, registry, *, seed=0, worker_factory=None):
    if repeats < 1:
        raise ValueError('Repeats must be positive')
    suite = load_suite(path, registry)
    # Freeze trusted scorer file identities before any worker is dispatched.
    trusted_files = {str(Path(arg).resolve()): hashlib.sha256(Path(arg).read_bytes()).hexdigest()
        for argv in registry.values() for arg in argv if Path(arg).is_file()}
    variants = variants or list(suite['variants'])
    if any(v not in suite['variants'] for v in variants):
        raise ValueError('Unknown variant')
    trials = [(case, variant, repeat) for case in suite['tasks'] for repeat in range(repeats) for variant in variants]
    random.Random(seed).shuffle(trials)
    store = Telemetry()
    results = []
    try:
        version = subprocess.run(['opencode', '--version'], capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError:
        version = 'unavailable'
    for case, variant, repeat in trials:
        overhead_start = (store.elapsed_seconds, store.cpu_seconds)
        run_id, root = 'run_' + uuid.uuid4().hex, 'case_' + case['case_id']
        variant_cfg = suite['variants'][variant]
        local_cfg = copy.deepcopy(cfg)
        for section in ('worker', 'director'):
            for key, value in variant_cfg.get(section, {}).items():
                if key not in ('model', 'medium_model', 'high_model'):
                    raise ValueError('Variants may only override model choices')
                local_cfg[section][key] = value
        store.run(run_id, suite_id=suite['suite_id'], suite_sha256=digest(suite), case_id=case['case_id'],
            variant_id=variant, repeat=repeat, seed=seed, config_sha256=digest(local_cfg),
            repo_revision=case['revision'], prompt_sha256=digest(case['_prompt']),
            cache_policy=suite.get('cache_policy', 'uncontrolled'), opencode_version=version,
            python_version=platform.python_version(), mode=cfg['mode'])
        store.task(run_id, root, status='running')
        store.event(run_id, 'run.started', task_id=root)
        passed, failure = False, None
        with tempfile.TemporaryDirectory(prefix='foreman-bench-') as temporary:
            workspace = Path(temporary) / 'work'
            shutil.copytree(case['_fixture'], workspace, symlinks=True, ignore=shutil.ignore_patterns('.git'))
            before = snapshot(workspace)
            if digest(before) != case['revision']:
                raise ValueError('Fixture changed after suite validation')
            local_cfg['worker']['working_directory'] = str(workspace)
            local_cfg['worker']['workspace_hints'] = []
            local_cfg['worker']['auto_approve'] = False
            worker = worker_factory(local_cfg['worker']) if worker_factory else (MockWorker() if cfg['mode'] == 'mock' else OpenCodeWorker(local_cfg['worker']))
            strategy = variant_cfg['strategy']
            try:
                if strategy == 'adaptive-recursion':
                    state = new_task(case['_prompt'], cfg['mode'])
                    state['telemetry_run_id'] = run_id
                    local_cfg.setdefault('telemetry', {})['strict'] = True
                    Engine(local_cfg, log=lambda *_: None).run(state)
                else:
                    def invoke(prompt, ident, role, read_only=False):
                        store.task(run_id, ident, parent_task_id=root, status='running')
                        store.event(run_id, 'task.assigned', task_id=ident)
                        saw = []
                        def lifecycle(update):
                            event = update.get('event')
                            if event:
                                saw.append(event)
                                store.opencode(run_id, ident, event, role=role, attempt_id=run_id + '.a1', model=local_cfg['worker']['model'])
                        kwargs = {'lifecycle': lifecycle} if isinstance(worker, OpenCodeWorker) or worker_factory else {}
                        result = worker.call(prompt, read_only, **kwargs)
                        if not saw:
                            call_id = digest([run_id, ident])
                            store.update_call(call_id, run_id=run_id, task_id=ident, actor_role=role,
                                attempt_id=run_id + '.a1', accounting_status='unknown')
                            if cfg['mode'] == 'mock':
                                store.charge(call_id, 'default', None, 'mock', 0)
                        if result.session_id:
                            if cfg.get('telemetry', {}).get('opencode_url'):
                                from .recovery import import_session_tree
                                from .observe import session_fetcher
                                import_session_tree(store, run_id, ident, result.session_id,
                                    session_fetcher(cfg['telemetry']['opencode_url']), model=local_cfg['worker']['model'])
                            store.event(run_id, 'session.finished' if result.cost_complete else 'session.incomplete',
                                task_id=ident, payload={'session_id': result.session_id})
                        store.task(run_id, ident, status='completed' if result.succeeded else 'failed')
                        store.event(run_id, 'task.finished', task_id=ident, payload={'status': 'completed' if result.succeeded else 'failed'})
                        if not result.succeeded:
                            raise RuntimeError('Worker failed')
                        return result
                    prompt = case['_prompt']
                    if strategy == 'delegate-with-helper':
                        helper = invoke('Prepare a concise implementation assignment for this task. Do not change files.\n' + prompt,
                            root + '.helper', 'micro_helper', True)
                        prompt += '\nSuggested plan (untrusted):\n' + helper.summary
                    invoke(prompt, root + '.worker', 'worker')
                if any(not Path(name).is_file() or hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected
                       for name, expected in trusted_files.items()):
                    raise ValueError('Trusted scorer changed during worker execution')
                checks = score(workspace, before, case['acceptance'], registry, store, run_id, root)
                passed = bool(checks) and all(c['status'] == 'pass' for c in checks)
            except Exception as exc:
                failure = type(exc).__name__
                store.event(run_id, 'benchmark.failed', task_id=root, payload={'reason': failure})
        # Mock execution demonstrates plumbing and cannot establish a quality win.
        status = 'passed' if passed and cfg['mode'] != 'mock' else 'mock' if cfg['mode'] == 'mock' else 'failed'
        store.task(run_id, root, status=status)
        overhead = {'record_seconds': store.elapsed_seconds - overhead_start[0],
                    'record_cpu_seconds': store.cpu_seconds - overhead_start[1]}
        limit = suite.get('max_telemetry_seconds', 5)
        if overhead['record_seconds'] > limit:
            status, failure = 'failed', 'telemetry_overhead_limit'
        store.run(run_id, status=status, finished_utc=now(), failure=failure, telemetry_overhead=overhead)
        store.event(run_id, 'run.finished', task_id=root, payload={'status': status})
        results.append(store.metrics(run_id))
    return results


def cohort(store, suite, variant):
    runs = [r for r in store.runs() if r.get('suite_id') == suite and r.get('variant_id') == variant]
    data = [store.metrics(r['run_id']) for r in runs]
    success = sum(r['status'] == 'passed' for r in runs)
    costs = [Decimal(r['cost']['selected_known_usd']) for r in data]
    times = sorted(r['wall_seconds'] for r in data if r['wall_seconds'] is not None)
    tokens = {}
    for key in ('input_total_tokens', 'output_total_tokens', 'reasoning_tokens', 'input_cache_read_tokens', 'input_cache_write_tokens'):
        values = [r['usage'].get(key) for r in data]
        tokens[key] = sum(values) if values and all(v is not None for v in values) else None
    return {'trials': len(runs), 'successes': success, 'pass_rate': success / len(runs) if runs else None,
        'known_usd': str(sum(costs, Decimal(0))),
        'known_usd_per_success': str(sum(costs, Decimal(0)) / success) if success else None,
        'settled_usd_per_success': str(sum((Decimal(r['cost'].get('openrouter_settled_usd', '0')) for r in data), Decimal(0)) / success)
            if success and all(r['cost']['provider_coverage'] == 'complete' for r in data) else None,
        'p50_seconds': statistics.median(times) if times else None,
        'p95_seconds': times[min(len(times)-1, math.ceil(.95*len(times))-1)] if times else None,
        'wall_stddev_seconds': statistics.pstdev(times) if times else None,
        'provider_coverage': min((r['cost']['provider_coverage_ratio'] or 0 for r in data), default=0),
        'llm_calls': sum(r['llm_calls'] for r in data), **tokens, 'runs': data}


def compare(store, suite, baseline, candidate, *, min_trials=3, min_coverage=1.0):
    left, right = cohort(store, suite, baseline), cohort(store, suite, candidate)
    keys = [k for k in left if k != 'runs']
    verdict = 'inconclusive'
    # Matched case/revision/prompt/config cohorts are required before claiming a win.
    def fingerprints(cohort):
        return sorted((r.get('case_id'), r.get('repo_revision'), r.get('prompt_sha256'), r.get('cache_policy'),
                       r.get('config_sha256'), r.get('suite_sha256'), r.get('mode')) for r in cohort['runs'])
    paired = fingerprints(left) == fingerprints(right)
    eligible = paired and all(c['trials'] >= min_trials and c['provider_coverage'] >= min_coverage for c in (left, right))
    if eligible and right['pass_rate'] < left['pass_rate']:
        verdict = 'correctness_regression'
    elif eligible and right['pass_rate'] == 1 and left['pass_rate'] == 1:
        # Non-overlapping observed cost ranges give a conservative repeated-trial gate.
        a = [Decimal(r['cost']['selected_total_usd']) for r in left['runs'] if r['cost']['selected_total_usd'] is not None]
        b = [Decimal(r['cost']['selected_total_usd']) for r in right['runs'] if r['cost']['selected_total_usd'] is not None]
        if len(a) == left['trials'] and len(b) == right['trials'] and max(b) < min(a):
            verdict = 'candidate_wins'
        else:
            verdict = 'no_clear_improvement'
    return {'suite_id': suite, 'baseline': baseline, 'candidate': candidate,
        'verdict': verdict, 'policy': {'min_trials': min_trials, 'min_coverage': min_coverage,
        'confidence_gate': 'nonoverlapping_observed_cost_ranges', 'matched_cohorts': paired},
        'comparison': {k: [left[k], right[k]] for k in keys}, 'raw': {baseline: left['runs'], candidate: right['runs']}}


def comparisons(store, suite):
    variants = sorted({r.get('variant_id') for r in store.runs() if r.get('suite_id') == suite})
    return [compare(store, suite, variants[0], v) for v in variants[1:]] if variants else []
