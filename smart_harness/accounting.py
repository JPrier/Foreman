"""Source-specific usage normalization and exact, auditable provider accounting."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation


def money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize(raw, source):
    """Unknown categories stay null; reasoning is never added to inclusive output."""
    raw = raw if isinstance(raw, dict) else {}
    result = dict.fromkeys(('input_uncached_tokens', 'input_cache_read_tokens',
        'input_cache_write_tokens', 'input_total_tokens', 'output_total_tokens',
        'reasoning_tokens', 'total_tokens'))
    if source == 'opencode':
        cache = raw.get('cache') or {}
        inp, read, write, out, reasoning = [counter(x) for x in
            (raw.get('input'), cache.get('read'), cache.get('write'), raw.get('output'), raw.get('reasoning'))]
        result.update(input_uncached_tokens=inp, input_cache_read_tokens=read,
            input_cache_write_tokens=write, reasoning_tokens=reasoning,
            # OpenCode v1 records output and reasoning separately.
            output_total_tokens=out + reasoning if out is not None and reasoning is not None else None,
            input_total_tokens=inp + read + write if all(x is not None for x in (inp, read, write)) else None,
            total_tokens=counter(raw.get('total')))
        method = 'opencode-v1-exclusive-input-and-output'
    elif source == 'openrouter':
        details = raw.get('prompt_tokens_details') or {}
        inp, out = counter(raw.get('prompt_tokens')), counter(raw.get('completion_tokens'))
        read, write = counter(details.get('cached_tokens')), counter(details.get('cache_write_tokens'))
        result.update(input_total_tokens=inp, output_total_tokens=out,
            input_cache_read_tokens=read, input_cache_write_tokens=write,
            reasoning_tokens=counter((raw.get('completion_tokens_details') or {}).get('reasoning_tokens')),
            total_tokens=counter(raw.get('total_tokens')))
        # Cache writes may overlap prompt categories. Only derive when both are known.
        if all(x is not None for x in (inp, read, write)) and inp >= read + write:
            result['input_uncached_tokens'] = inp - read - write
        method = 'openrouter-inclusive-prompt-and-completion'
    else:
        method = 'unsupported-schema'
    return {**result, 'usage_complete': all(result[k] is not None for k in ('input_uncached_tokens', 'input_cache_read_tokens',
                'input_cache_write_tokens', 'input_total_tokens', 'output_total_tokens', 'reasoning_tokens')),
            'normalization_method': method, 'source_raw': raw}


class OpenRouterClient:
    def __init__(self, key=None, opener=None, sleep=time.sleep):
        self.key = key or os.environ.get('OPENROUTER_API_KEY')
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep

    def get(self, path):
        if not self.key:
            raise ValueError('OPENROUTER_API_KEY is required for reconciliation')
        req = urllib.request.Request('https://openrouter.ai/api/v1/' + path,
            headers={'Authorization': 'Bearer ' + self.key})
        with self.opener(req, timeout=20) as response:
            return json.load(response, parse_float=Decimal)['data']

    def key_usage(self):
        raw = self.get('key')
        return {k: raw.get(k) for k in ('usage', 'usage_daily', 'usage_monthly', 'byok_usage')}

    def reconcile(self, store, run_id, scope='default', retries=3):
        results = []
        for call in store.calls(run_id):
            generation = call.get('generation_id')
            if call.get('provider_id') != 'openrouter':
                continue
            status = 'missing_generation_id'
            if generation:
                for attempt in range(retries + 1):
                    try:
                        raw = self.get('generation?' + urllib.parse.urlencode({'id': generation}))
                        if raw.get('id') != generation or money(raw.get('total_cost')) is None:
                            status = 'adapter_failure'
                            break
                        allowed = ('id', 'total_cost', 'model', 'native_tokens_prompt', 'native_tokens_completion',
                            'native_tokens_cached', 'native_tokens_reasoning', 'cache_discount', 'provider_name',
                            'is_byok', 'upstream_inference_cost', 'created_at')
                        raw = {k: raw[k] for k in allowed if k in raw}
                        store.charge(call['call_id'], scope, generation, 'openrouter_settled', raw['total_cost'], raw)
                        if raw.get('is_byok') and money(raw.get('upstream_inference_cost')) is not None:
                            store.charge(call['call_id'], scope, generation, 'upstream_provider', raw['upstream_inference_cost'], raw)
                        observed = store.call_charges(call['call_id'])
                        discrepancies = [str(money(c['amount_usd']) - money(raw['total_cost'])) for c in observed
                            if c['source'] == 'opencode_observed' and money(c['amount_usd']) != money(raw['total_cost'])]
                        status = 'provider_disagreement' if discrepancies or (call.get('model_id') and raw.get('model') != call['model_id'].removeprefix('openrouter/')) else 'settled'
                        store.event(run_id, 'cost.reconciled', task_id=call.get('task_id'),
                            payload={'generation_id': generation, 'status': status, 'deltas_usd': discrepancies})
                        break
                    except urllib.error.HTTPError as exc:
                        status = {401: 'auth_failure', 403: 'auth_failure', 404: 'not_yet_available', 429: 'rate_limited'}.get(exc.code, 'provider_error')
                        transient = exc.code in (404, 429) or exc.code >= 500
                        if not transient or attempt == retries:
                            if exc.code == 404:
                                status = 'not_found'
                            break
                        self.sleep(min(2 ** attempt, 8))
                    except (OSError, ValueError, KeyError, TypeError):
                        status = 'adapter_failure'
                        break
            store.update_call(call['call_id'], reconciliation_status=status, provider_scope=scope)
            if status not in ('settled', 'provider_disagreement'):
                store.event(run_id, 'cost.unresolved', task_id=call.get('task_id'),
                    payload={'generation_id': generation, 'status': status})
            results.append({'call_id': call['call_id'], 'status': status})
        return results


class ReconciliationQueue:
    """One background reconciler; inference never waits for provider lookup retries.

    The caller joins on close so pending records are durable before CLI exit.
    Work is rediscoverable from stored calls after a process crash.
    """
    def __init__(self, store, client=None, scope='default'):
        from concurrent.futures import ThreadPoolExecutor
        self.store, self.client, self.scope = store, client or OpenRouterClient(), scope
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='foreman-costs')
        self.futures = []

    def submit(self, run_id):
        self.futures.append(self.pool.submit(self.client.reconcile, self.store, run_id, self.scope))

    def close(self):
        self.pool.shutdown(wait=True)
        return [future.result() for future in self.futures]
