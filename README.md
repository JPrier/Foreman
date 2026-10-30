# Foreman

**A general-purpose premium-director / cheap-worker agent loop.** Give it a task, not a repository. A smart director produces short structured worker prompts; cheap tool-enabled workers execute them; their summaries go back to the director; it dispatches more work or finishes. This can be used for research, writing, coding, analysis, and other tasks supported by the worker's tools.

This is a **working first-pass prototype**, not production-hardened software. It has no API credentials bundled, makes zero paid calls until explicitly put in `live` mode, and has been tested offline with mocks. Its live provider integrations have **not** been authenticated or end-to-end verified.

## Included

- Python 3.10+, **stdlib only** runtime; `smart-coder` and `smart-harness` global commands
- JSON-only director interface: `dispatch`, `finish`, `ask_user`
- Real OpenRouter director adapter (default: Opus 5.5)
- Alternative OpenCode director adapter for *ChatGPT-sign-in* (not an OpenAI API key), with a dedicated no-tools agent
- Cheap OpenCode workers (default: GLM 5.3 Flash via OpenRouter), with normal tool access
- Sequential dependent rounds; optional parallel **read-only** tasks
- Task history and resumable follow-up requests; per-worker full reports stored separately
- Premium request preflight estimates, per-task budget, worker-cost reporting (best effort)
- Deterministic mock mode and automated tests

There is **no Git or repository logic in the core**, no forced working directory, no `--repo`, and no mandatory OpenCode project setup. The worker can navigate using whatever tools its configured runtime and permissions provide. The default worker starts in your home directory so invocation location does not matter. You can optionally give it workspace location hints in configuration, but this is never required.

## 1. Install on Windows

PowerShell, inside this starter directory:

```powershell
py -m pip install -e .
smart-coder init
smart-coder "Draft a polite two-sentence introduction to a meeting"
```

The first run uses **mock mode**: it demonstrates the director→worker→director loop without actually writing the introduction. Try `smart-coder status`, `smart-coder history`, and inspect files under `%USERPROFILE%\.smart-harness\tasks`.

If `smart-coder` is not on PATH after installation, use `py -m smart_harness` from any folder while Python can import the installed package.

You can also run directly without installing: `py -m smart_harness "task"` from this starter directory.

On Linux, replace `py` with `python3` or `python`.

## 2. Configure live agents **when you're ready**

You'll need OpenCode installed and authenticated for the cheap worker:

```powershell
opencode --help
opencode
```

In OpenCode, use `/connect` to connect **OpenRouter**. Make sure `openrouter/z-ai/glm-5.3-flash` resolves with `/models`. Exact model slugs and permissions can change; check OpenCode and provider documentation before live use.

Your user config is `%USERPROFILE%\.smart-harness\config.json` (Linux: `~/.smart-harness/config.json`). `smart-coder init` creates it. Set `"mode": "live"` only after authentication.

### Director option A — Opus through OpenRouter (metered)

```powershell
$env:OPENROUTER_API_KEY = "YOUR_OPENROUTER_KEY"
```

In config, use:

```json
"director": {
  "backend": "openrouter",
  "model": "anthropic/claude-opus-5.5"
}
```

The snippet shows relevant fields only; **edit those existing keys**, do not replace the entire `director` section because other fields control budgeting and output limits.

Opus has **no tools** in this adapter. It gets a system prompt, compact task history, and returns validated JSON. It makes one API call per decision round.

### Director option B — GPT through ChatGPT sign-in (subscription limits)

Connect **OpenAI → Sign in with ChatGPT** using OpenCode `/connect`. This is OpenCode's subscription authentication, **not** the OpenAI paid API. Install the restricted global agent (explicit opt-in):

```powershell
smart-coder install-director-agent
```

Change this existing config key:

```json
"director": {
  "backend": "opencode",
  "opencode_model": "openai/gpt-6.1-sol#high",
  "opencode_agent": "smart-harness-director"
}
```

When this backend is selected, the harness sends the director prompt through OpenCode. The installed director agent denies all tools. It is not possible to meter ChatGPT subscription consumption in dollars through this harness, and your plan's limits still apply. Verify the exact model name and subscription sign-in support in *your* OpenCode installation before live use.

**Both accounts may be usable in one OpenCode installation**: OpenRouter for GLM workers and OpenAI sign-in for the director. Verify this behavior in your installed version before relying on it.

## 3. Run arbitrary tasks

```powershell
smart-coder "Review the pros and cons of these two approaches and write a recommendation"
smart-coder "Investigate IRL PR #7 and propose fixes"
smart-coder "Create a draft project plan with milestones"

smart-coder status
smart-coder history
smart-coder show
smart-coder continue "Use the second approach and explain why"
```

A path, URL, or project is only relevant *if your task involves one*. The harness itself does not try to discover Git repositories or force you into one. Workers can only access things their OpenCode tools and credentials can access; this starter does not automatically connect GitHub or web services.

**Warning:** `smart-coder "task"` always begins a fresh task. `smart-coder continue "..."` resumes the latest task. Use `continue --task task_...` for an older one. A `needs_input` task requires a follow-up before resumption. Tasks that completed cannot be resumed.

## 4. How it works

```text
You: "Do anything"
         |
         v
Director (OpenRouter JSON-only, or restricted OpenCode subscription agent)
         |
         +-- dispatch [{id, prompt, read_only}, ...] -> workers
         +-- ask_user {question}                 -> paused task
         +-- finish {answer}                     -> done
         |
         v
OpenCode workers (normal tools, cheap model)
         |
         +-- concise final reports + full reports on disk
         |
         v
Director reviews original objective + compact reports -> next decision
```

The worker returns evidence in its final report. The harness stores that report and passes a bounded summary to the director. It **cannot independently prove** that a worker's claims are true; the director can ask a second worker to verify. Follow-up work is dispatched in a new round. Parallelism is allowed only if the director marks the batch parallel *and every task read-only*, using OpenCode's configured `plan` agent; regular mutating work runs sequentially.

## 5. Budgets and safety

Defaults in `config.json`:

| Limit | Default |
| --- | ---: |
| Premium director target budget (OpenRouter) | $0.50 per task |
| Premium output token cap | 1,400 per decision |
| Maximum director calls | 9 |
| Maximum dispatch rounds | 8 |
| Maximum workers / round | 3 |
| Maximum total workers | 18 |
| Parallel workers (read-only only) | 3 |

OpenRouter premium preflight reserves a *conservative estimate* before each call. When the provider returns an exact cost, it is recorded; if cost is missing, the reserve remains. This is **not a guaranteed hard spending cap**: tokens, caching, provider pricing, retries, or a request interrupted after billing may differ. Use OpenRouter's account-side credit/API-key caps too. The quoted $4/$20 per million input/output token rates for Opus 5.5 are only a starting default; **update config if pricing changes**. The premium budget governs the OpenRouter director only and **does not cap cheap-model API spending** through OpenCode.

OpenCode `--format json` can omit final billing events, so worker cost numbers may be partial. `status` explicitly labels incomplete accounting. ChatGPT subscription usage has no USD measurement here. If the director or budget fails, the task stops and preserves state; an unfinished tool-enabled worker may already have made changes. **The harness cannot roll back side effects.**

OpenCode workers normally run with configured permissions; `worker.auto_approve` defaults to false. Changing it to true permits unattended tool approvals for actions OpenCode doesn't explicitly deny. Only enable it if you trust the worker and its environment. Read-only agents are directed to OpenCode's `plan` agent, but you should still review the effective permission configuration. Separate workers have separate sessions and may not share conversational context; use the director's explicit prompts for handoffs.

Worker outputs and task descriptions are stored as **plaintext** in `~/.smart-harness/tasks`. Do not pass secrets through prompts or copy private outputs into public repositories. Protect that directory according to your machine's security needs. Workers cannot access arbitrary external services without appropriate tool/provider setup.

## 6. Test without keys

```powershell
py -m unittest discover -s tests -v
smart-coder "Research a topic and write a summary"
smart-coder status
```

Mocks exercise two delegation rounds and the completion decision, but **mock workers do not perform actual work**. The test suite covers schema validation, budget blocking, resumability, no-repository invocation, failure reports and OpenCode event parsing.

## 7. Main extension points

- `smart_harness/contracts.py`: director's JSON grammar and compact context
- `smart_harness/engine.py`: budgeted delegation/evaluation loop
- `smart_harness/backends.py`: OpenRouter director / OpenCode worker adapters
- `smart_harness/config.py`: configuration defaults and validation
- `smart_harness/storage.py`: durable task history and full worker output

**First-pass limitations:** The OpenCode worker is a tool-enabled agent but its available tools are determined by your OpenCode installation; no embedded browser or computer-use implementation is bundled. Paid worker use is not strictly capped here. There's no human approval UI for specific dispatched tasks, no cross-worker artifact indexing beyond file paths, no guaranteed recovery of interrupted worker subprocesses, no reliable end-to-end live integration test without credentials, and no sandbox isolation. These are follow-up engineering tasks, not features claimed complete.

### Documentation checked during construction

- OpenCode v2 CLI and agents: https://dev.opencode.ai/v2/docs/cli/commands/ and https://dev.opencode.ai/v2/docs/agents/
- OpenRouter API model: https://openrouter.ai/anthropic/claude-opus-5.5
- OpenRouter structured output format: https://openrouter.ai/docs/features/structured-outputs
