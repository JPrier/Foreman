# Benchmarking, accounting, and local observability

Foreman records normal runs and benchmark trials in `telemetry.sqlite3` under
`SMART_HARNESS_HOME` (default `~/.smart-harness`). Operational `state.json` remains
the orchestration authority. No additional Python dependencies are required.

## Normal runs and inspection

```sh
smart-coder --live run "Implement the change" --metrics-json
smart-coder metrics --task task_123 --format json
smart-coder observe tree --run run_abc --format markdown
smart-coder observe task --run run_abc --task worker_id
smart-coder observe timeline --run run_abc
smart-coder costs audit --run run_abc
smart-coder bench import --task task_legacy --from-existing-state
```

Use actual IDs returned by Foreman. A continuation retains its run ID and records
a new attempt ID. Worker attempts and provider generations are separate identities.
`--metrics-json` emits only JSON, including when operational work fails.

Metrics contain exclusive input counters, inclusive output, reasoning detail,
source-native usage, role/model/session breakdowns, context percentiles, verification
results, retry/repeated-tool counts, wall time, model time, and telemetry overhead.
Unknown categories are null. Model wall time is cumulative and can overlap for
parallel workers; it is not the run's critical-path latency.

Normal ledger evidence is labeled `worker_report` and unverified. Benchmark checks
are executed by the runner and produce independent pass/fail evidence.

The tree labels **direct**, **descendant**, and **inclusive known** costs. Inclusive
rows overlap; never sum them for a run total. Run totals deduplicate exact known
generation IDs within an account scope. Conflicting ownership stays unresolved.

## Reconcile OpenRouter billing

```sh
smart-coder costs reconcile --run run_abc --provider openrouter --scope experiment-key
smart-coder costs audit --run run_abc --key-check
```

Set `OPENROUTER_API_KEY` in the environment. The scope is a non-secret label for
the account/key whose generation IDs are being queried. Use the matching key;
Foreman does not create or revoke keys. Key totals are a coarse sanity check,
never a ledger of task charges.

Cost authority, strongest first:

1. `openrouter_settled`: known generation lookup, immutable correction snapshots.
2. `openrouter_response`: provisional completed-response cost.
3. `opencode_observed`: native OpenCode step cost.
4. `estimated`: explicitly labeled price arithmetic.
5. Unknown liability: no manufactured zero.

Amounts are exact decimal strings in JSON. Accounting sums use `Decimal`, not
binary floating-point sums. Budget enforcement retains its existing numeric API.
BYOK upstream cost is reported separately and never added to OpenRouter debits.
Provider disagreement preserves both sources and emits a reconciliation event.
404, 429, and server errors receive bounded backoff. Authentication, schema,
missing-ID, unavailable-record, and disagreement states remain inspectable.
Repeated imports and reconciliation preserve totals; reconciliation events and
new correction snapshots may increase the audit history.

Direct OpenRouter IDs and usage are captured before parsing model content.
OpenCode steps use session/part identity, with message identity as a fallback.
Unknown IDs are never inferred from timestamps. OpenCode auxiliary generations
are counted when exposed by its stream/history; missing native visibility remains
a limitation, not evidence of free title/compaction requests.

## Recover nested OpenCode sessions

Start OpenCode's local server in the relevant working directory and configure:

```json
{
  "telemetry": {
    "strict": false,
    "opencode_url": "http://127.0.0.1:4096"
  }
}
```

Or recover on demand:

```sh
smart-coder costs audit --run run_abc --opencode-url http://127.0.0.1:4096
```

The adapter recursively reads `/session/{id}/message` and
`/session/{id}/children`. Retained step parts replace stream gaps without adding
message aggregates to already imported steps. Terminal history can resolve a
stream gap; absent, inaccessible, or unexpected child/session records leave
coverage incomplete. No server is started automatically and no credentials are
sent to the session API. Endpoint overrides must be loopback HTTP URLs.

Compatibility validation: OpenCode **1.19.5** was installed and a real local
server session was created, enumerated, inspected, and deleted. Empty history
correctly stayed incomplete. Populated step/child streams are covered by retained
wire-format fixtures. Real paid model inference and OpenRouter settlement require
user credentials and were not executed in the implementation environment.

The optional plugin/Broadcast experiment in the design is deliberately not made
a dependency. Native `generationID` or `metadata.generation_id`, when exposed,
is retained. This does not assert that every OpenCode version supplies those fields.
Do not enable an unverified plugin or claim universal provider coverage.

## Repeated benchmark trials

The checked-in smoke suite has a broken arithmetic fixture, an external trusted
scorer, and baseline/helper/recursive variants. Generate a local trusted registry:

```sh
python -c "import json,sys,pathlib; pathlib.Path('checks.local.json').write_text(json.dumps({'repair-simple':[sys.executable,str(pathlib.Path('benchmarks/checks/repair_simple.py').resolve())]}))"
smart-coder --mock bench run --suite benchmarks/suites/smoke.json --checks checks.local.json --variant baseline --variant tiny-prompt-helper --repeats 3 --output json
smart-coder --live bench run --suite benchmarks/suites/smoke.json --checks checks.local.json --variant baseline --variant tiny-prompt-helper --repeats 5 --reconcile
smart-coder bench compare --suite benchmarks/suites/smoke.json --baseline baseline --candidate tiny-prompt-helper --format markdown
```

The example check deliberately fails until a real worker fixes the fixture.
Mock results are always labeled `mock`, never verified economic successes.
`--reconcile` joins a background lookup queue before returning final reports.
Without this flag, run `costs reconcile` separately and compare again.

Suite paths are relative to the suite file. Pin `revision` to the SHA-256 of the
canonical sorted map of relative file paths and content SHA-256 values; obtain it
with `smart_harness.benchmarks.fixture_revision(path)`. Symlinks and paths escaping
the suite directory are rejected. Every repeat copies the same validated snapshot
into a fresh temporary directory. The fixture is not modified by trials.

The runner records suite/config/prompt/fixture fingerprints, seed, version,
models, variant, case, repetition, and cache policy. Trials are interleaved with
a seeded shuffle. `cache_policy: uncontrolled` describes provider caching honestly;
the runner cannot force a remote provider to evict its cache.

Strategies are `direct` (one worker), `delegate-with-helper` (a read-only helper
then an implementation worker), and `adaptive-recursion` (the existing engine).
All use the same source adapters and telemetry store. Variant overrides may change
model selections; comparisons of differing configurations stay inconclusive.

Acceptance definitions and command argv arrays are loaded into runner memory
before dispatch. Referenced scorer files are fingerprinted before dispatch and checked again before scoring; modified scorers invalidate the trial. Workers never supply scoring commands. Keep trusted check scripts
and expected outputs outside writable fixtures; fixture-local tests are agent
outputs and cannot alone establish independent correctness. `allowed_paths` matches
exact paths or directory prefixes ending in `/`, and includes untracked/deleted
files. No command shell is used by the scorer.

Temporary directory isolation is for reproducibility, **not an OS security
sandbox**. Run untrusted/destructive integration tasks in a container with scoped
credentials. Live workers retain the existing OpenCode permission mechanism;
auto-approval is disabled for benchmark workers. Benchmark check commands are
trusted user configuration and may use external services only when intentionally
configured. The runner does not impose a new wall-clock limit on checks or proof
work.

Comparisons include raw trials, pass rate, all-run spend per success, token
categories, call counts, p50/p95/stddev latency, and provider coverage. A win
requires matching case/fixture/prompt/config/cache/suite cohorts, at least three
trials by default, complete provider coverage by default, all acceptance checks
passing, and non-overlapping observed cost ranges. Thresholds are configurable;
this is a conservative empirical gate, not a statistical confidence interval.
Failures keep their bill. Partial data remains directional/inconclusive.

Recording elapsed/CPU time and storage size are reported. Suites can set
`max_telemetry_seconds` (default 5) to fail a trial on instrumentation overhead;
this limit applies only to measured telemetry operations, not model/proof time.

## Local read-only API

```sh
smart-coder observe serve --host 127.0.0.1 --port 8765
```

Routes:

- `/api/v1/runs`
- `/api/v1/runs/{run_id}` and `/metrics`
- `/api/v1/runs/{run_id}/tree`, `/timeline`, `/costs?source=all`
- `/api/v1/runs/{run_id}/tasks/{task_id}` and `/events`
- `/api/v1/benchmarks/{suite_id}/comparisons`

The server only supports GET, rejects remote binding and foreign Host/Origin
headers, and disables caching. Metadata endpoints never serve prompts, worker
reports, source files, tool arguments, or authorization headers. Worker accounting
JSONL is redacted before persistence; the existing operational worker reports
remain in the task's normal private storage.

SQLite uses WAL, short transactions, indexes, a busy timeout, and schema version 1.
Events and charge snapshots are append-only; calls/tasks/runs are query projections.
Normal recording failure marks measurement incomplete and warns while orchestration
continues. `telemetry.strict: true` fails closed; benchmark writers always fail
closed. Do not treat estimates, mock costs, or incomplete coverage as settled billing.

## Verification

Run `python -m unittest discover -s tests -v`. Tests exercise accounting invariants,
interrupted and duplicate streams, two-level child discovery, restarted attempts,
BYOK/disagreement/retries, read-only HTTP, trusted benchmark scoring, pinning,
isolation, comparison gates, privacy, normal telemetry failure, and strict mode.
