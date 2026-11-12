"""Deterministic reports and a metadata-only, read-only loopback API."""
from __future__ import annotations

import ipaddress
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .telemetry import encode


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def session_fetcher(url):
    import urllib.request
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or not loopback(parsed.hostname or '') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('OpenCode API must use a loopback HTTP URL without credentials')
    def fetch(path):
        with urllib.request.urlopen(url.rstrip('/') + path, timeout=15) as response:
            return json.load(response)
    return fetch


def report(value, fmt='json'):
    if fmt == 'json':
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    if isinstance(value, dict) and 'comparison' in value:
        rows = value['comparison']
        return '\n'.join(['| Metric | Baseline | Candidate |', '|---|---:|---:|'] +
            [f'| {k} | {v[0]} | {v[1]} |' for k, v in rows.items()] + ['', 'Verdict: ' + value['verdict']])
    if isinstance(value, list) and value and 'task_id' in value[0]:
        return '\n'.join(['| Task | Parent | Status | Direct known USD | Descendant known USD |', '|---|---|---|---:|---:|'] +
            [f"| {t['task_id']} | {t.get('parent_task_id')} | {t.get('status')} | {t.get('direct_known_usd')} | {t.get('descendant_known_usd')} |" for t in value])
    return '```json\n' + report(value) + '\n```'


def api(store, path):
    parts = urllib.parse.urlsplit(path).path.strip('/').split('/')
    if parts == ['api', 'v1', 'runs']:
        return store.runs()
    if parts[:3] == ['api', 'v1', 'benchmarks'] and len(parts) == 5 and parts[4] == 'comparisons':
        from .benchmarks import comparisons
        return comparisons(store, parts[3])
    if parts[:3] != ['api', 'v1', 'runs'] or len(parts) < 4:
        raise ValueError('Unknown endpoint')
    run = parts[3]
    # Validate existence for every endpoint, including empty tree/timeline.
    metrics = store.metrics(run)
    suffix = parts[4:]
    if not suffix or suffix == ['metrics']:
        return metrics
    if suffix == ['tree']:
        return store.tree(run)
    if suffix == ['timeline']:
        return store.timeline(run)
    if suffix == ['costs']:
        return {'cost': metrics['cost'], 'calls': [{**c, 'charges': store.call_charges(c['call_id'])} for c in store.calls(run)]}
    if len(suffix) in (2, 3) and suffix[0] == 'tasks':
        task = suffix[1]
        if not any(t['task_id'] == task for t in store.tree(run)):
            raise ValueError('Unknown task')
        if len(suffix) == 3 and suffix[2] == 'events':
            return store.timeline(run, task)
        if len(suffix) == 2:
            return store.metrics(run, task)
    raise ValueError('Unknown endpoint')


def serve(store, host='127.0.0.1', port=8765):
    if not loopback(host):
        raise ValueError('Remote HTTP binding requires authentication and TLS; only loopback is supported')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Prevent browser DNS-rebinding access to local telemetry.
            authority = urllib.parse.urlsplit('http://' + self.headers.get('Host', '')).hostname or ''
            if not loopback(authority) or self.headers.get('Origin'):
                self.send_error(403)
                return
            try:
                body = encode(api(store, self.path)).encode()
                self.send_response(200)
            except ValueError:
                body = b'{"error":"not_found"}'
                self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    import socket
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ':' in host else socket.AF_INET
    return Server((host, port), Handler)
