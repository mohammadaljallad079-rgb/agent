# Arabic AI Agent Platform

An Arabic-first **Agent Operating Platform** — not a chatbot.

The user gives a *goal*, not a message. The platform plans it, decomposes it into
steps, selects and executes tools, observes the results, verifies the outcome,
and recovers from failure — pausing for human approval on sensitive operations.

```
User → Goal → Plan → Tools → Execute → Observe → Verify → Result
```

The value of the product lives in the runtime: the agent loop, the tool system,
the permission layer, the task engine, memory, integrations and the model
gateway. The LLM is a reasoning engine behind an abstraction — swappable for a
cloud, free or local model without touching the agent core.

## Status

This repository is the **foundation phase**. It is real, runnable code with a
tested core — not a mockup. What is implemented and what is deliberately left
for later is listed in [`TODO.md`](TODO.md); nothing pretends to work.

- ✅ Agent loop with bounded recovery (retry / repair / replan / skip / abort)
- ✅ Tool abstraction + registry, with real sandboxed filesystem and shell tools
- ✅ Permission / policy engine with allow / deny / requires-approval
- ✅ Human-in-the-loop approvals as first-class, audited entities
- ✅ Task engine with durable, resumable plans
- ✅ Memory layer with a write policy (secrets never persisted)
- ✅ Context manager with a script-aware (Arabic-aware) token budget
- ✅ Model gateway with retry, fallback and usage accounting
- ✅ Audit log with secret redaction
- ✅ Multi-tenant identity model and API-key authentication
- ✅ Local/Cloud runtime boundary and integration layer
- ✅ Durable Postgres persistence (runs, tasks, approvals, memory, audit) with
  Alembic migrations
- ✅ Redis-backed distributed rate limiting, locking and idempotency
- ✅ Reliable Redis job queue and a recoverable worker runtime (leases,
  heartbeats, bounded recovery after worker loss)
- ✅ Observability: correlated structured logs, Prometheus metrics at `/metrics`,
  in-process tracing with an optional OpenTelemetry bridge, and a dependency-aware
  readiness probe at `/readyz`
- ✅ Durable DAG workflow engine: versioned definitions, runs/steps, conditions
  and templating, per-step retries, and human-in-the-loop approvals (HTTP API),
  plus cron/event/webhook/manual triggers and a durable scheduler
- ✅ Local/desktop runtime: filesystem sandbox, controlled command runner,
  local tool set, data-boundary policy, sync queue and a loopback control API
  (`agent-desktop`) — the desktop *application* is not built yet
- ⏳ Document and media tools, social/ERP connectors, desktop packaging,
  web UI (see `TODO.md`)

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design. Repository rules
for contributors and agents are in [`AGENTS.md`](AGENTS.md).

## Quick start

```bash
# 1. Install
uv venv && uv pip install -e ".[dev]"

# 2. Configure a model (cloud or local). Never commit secrets.
export AAP_OPENAI_API_KEY=...          # cloud
# or point at a local OpenAI-compatible server (Ollama, vLLM, llama.cpp):
# export AAP_LOCAL_MODEL_BASE_URL=http://localhost:11434/v1

# 3. Run the API
uv run agent-platform
```

Health check:

```bash
curl localhost:8000/health   # liveness: process is up
curl localhost:8000/readyz   # readiness: DB/Redis/model/audit reachable (503 if not)
curl localhost:8000/metrics  # Prometheus metrics
```

### Production (durable + queued)

With Postgres and Redis configured, the API persists runs and enqueues them
instead of executing inline; a separate worker executes them and recovers runs
whose worker was lost.

```bash
docker compose up -d                    # Postgres + Redis
export AAP_DATABASE_URL=postgresql+asyncpg://aap:aap_dev_password@localhost:5432/aap
export AAP_REDIS_URL=redis://localhost:6379/0
export AAP_EXECUTION_MODE=queued

uv run alembic upgrade head             # apply the schema
uv run agent-platform                   # API
uv run agent-worker                     # one or more workers
```

### Local / desktop runtime

The local runtime runs the same agent core on the machine, against the local
filesystem and a controlled command runner, behind a loopback-only control API
for a desktop client. Nothing about the cloud runtime changes; a `local` build
simply registers the local tools and no other.

```bash
export AAP_RUNTIME_KIND=local
export AAP_LOCAL_CONTROL_TOKEN=...        # required; the surface fails closed without it
export AAP_LOCAL_WORKSPACE_ROOTS=$HOME/work   # extra roots local tools may touch
export AAP_LOCAL_COMMAND_ALLOWLIST=ls,cat     # commands the runner may execute

uv run agent-desktop                      # serves 127.0.0.1:8765
curl -H "X-Local-Token: $AAP_LOCAL_CONTROL_TOKEN" localhost:8765/local/status
```

Every local path goes through the filesystem sandbox and every write, move or
delete is approval-gated. Clipboard, printing and application control are
declared but **not implemented** — the capability catalogue reports
`implemented: false` rather than pretending.

### Computer use (opt-in)

The agent can see and operate the local machine, but only when the operator
explicitly turns it on. It is a local-only capability — a cloud build never
registers the tools.

```bash
export AAP_COMPUTER_CONTROL_ENABLED=true   # default false
export AAP_COMPUTER_SCREEN_POLICY=local_only   # screenshots never go remote
export AAP_COMPUTER_OCR_LANGUAGES=ara,eng      # Arabic-first
```

The X11 backend captures the screen (Pillow), enumerates monitors (`xrandr`)
and synthesises input (`xdotool`) with explicit arguments and `shell=False`. The
tools are ordinary tools: `SCREEN_READ`, `POINTER_CONTROL`, `KEYBOARD_CONTROL`
and `WINDOW_CONTROL` are approval-gated and audited, typed text is recorded by
length only, and a per-run action budget stops runaway loops. The real-X11
end-to-end tests run only when `AAP_TEST_DISPLAY` names a display.

### Browsing & web research (opt-in)

The agent can open, read, search and operate the web — again local-only and off
by default. The tools are ordinary tools running through the same
permission/approval/audit path, not a separate browser agent.

```bash
export AAP_BROWSER_ENABLED=true              # default false
export AAP_BROWSER_CONTENT_POLICY=local_only # page content never goes remote
export AAP_BROWSER_ALLOWED_DOMAINS=example.com,example.org   # optional allow-list
```

Navigation is validated *before* the engine is touched: dangerous schemes,
`file://` escapes, credential-bearing URLs and (by default) loopback/private/
link-local SSRF hosts — including the cloud-metadata address — are refused, with
optional domain allow/deny lists. Page content is treated as untrusted data: it
is scanned for prompt-injection indicators, never treated as instructions, and
kept behind a data boundary that fails closed so it never reaches a remote model
by default. Observations are bounded and freshness-tracked; the model acts on a
semantic element `ref`, never a raw selector; downloads are confined and size-
capped; and a form submit or dialog-accept is refused unless a real approval was
granted (the flag is injected by the executor, not read from model arguments).
Every page the agent reads is recorded in a source ledger so answers cite real
sources. The real-Playwright end-to-end tests run only with `AAP_TEST_BROWSER=1`.

## Development

```bash
uv run pytest          # tests (unit; infra tests skip without services)
uv run mypy            # type checking (strict)
uv run ruff check .    # lint
uv run ruff format .   # format
```

Run the full suite including Postgres/Redis integration tests, the local model,
and the real computer/browser end-to-end tests:

```bash
docker compose up -d
# one-time: CREATE DATABASE aap_test; and an X server with a window manager
# (Xvfb :99 + openbox) plus xdotool/xrandr for the computer e2e tests.
AAP_TEST_DATABASE_URL=postgresql+asyncpg://aap:aap_dev_password@localhost:5432/aap_test \
AAP_TEST_REDIS_URL=redis://localhost:6379/15 \
AAP_TEST_MODEL_PATH=/path/to/model.gguf \
AAP_TEST_DISPLAY=:99 AAP_TEST_BROWSER=1 \
uv run pytest
```

## Security

- No secrets in source or Git. Configuration is environment-only (`AAP_*`).
- Deny-by-default policy; sensitive tools require explicit approval.
- All tool and audit payloads are redacted before they are stored or logged.
- Filesystem and shell tools are sandboxed to the run workspace.

## License

Proprietary. All rights reserved.
