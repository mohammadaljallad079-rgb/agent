# Architecture — Arabic AI Agent Platform

> This document is the authoritative design for the platform. It describes what
> exists today (the foundation) and the boundaries along which the rest of the
> product grows. Anything described as *future* is intentionally not implemented
> yet and is tracked in [`TODO.md`](TODO.md). Nothing here is a mockup.

---

## 1. Product principle

The product is an **Agent Operating Platform**, not a chatbot.

```
Chatbot:  User → Message → Answer
Agent:    User → Goal → Plan → Tools → Execute → Observe → Verify → Result
```

The LLM is a **reasoning engine behind an abstraction**. The durable value of
the product is the runtime around it: the agent loop, the tool system, the
permission layer, the task engine, memory, integrations, the audit trail, and
the local/cloud boundary. Replacing the model must never require changing the
agent core.

---

## 2. Repository state (inspected before design)

The repository was an **empty greenfield** at the start of this phase:

- `git init` with no commits, no files, no remotes.
- No existing technology stack, no reusable code, no legacy constraints.

Therefore this architecture is a clean design from zero, laid down as a real,
runnable foundation rather than a specification. The foundation is implemented,
linted (`ruff`), type-checked (`mypy --strict`) and tested (`pytest`, 130 tests).

---

## 3. System architecture

Layers, from the outside in. Dependencies point **downward only**; no lower
layer imports a higher one.

```
┌──────────────────────────────────────────────────────────────────┐
│ Interfaces        HTTP API (FastAPI)   ·  CLI  ·  Worker  · (future) Desktop│
├──────────────────────────────────────────────────────────────────┤
│ Runtime boundary  Transport: Local Runtime  |  Cloud Runtime       │
├──────────────────────────────────────────────────────────────────┤
│ Orchestration     AgentRunner (the loop) · Planner · RecoveryPolicy│
│                   TaskEngine · ContextManager · Worker/RunExecutor │
├──────────────────────────────────────────────────────────────────┤
│ Capabilities      ToolRegistry/Tools · Permissions/Policy/Approvals│
│                   ModelGateway/Providers · Memory · Integrations   │
├──────────────────────────────────────────────────────────────────┤
│ Foundations       Audit · Config/Settings · Errors · Redaction ·   │
│                   i18n · Tenancy · IDs                             │
├──────────────────────────────────────────────────────────────────┤
│ Infrastructure    Persistence (Postgres) · Redis (queue/lock/      │
│                   rate-limit/idempotency) · Migrations (Alembic)   │
└──────────────────────────────────────────────────────────────────┘
```

### Module map

| Package | Responsibility |
|---|---|
| `core` | Settings, enums, error taxonomy, IDs, secret redaction |
| `tenancy` | Tenant / User / Role / AgentProfile identity model |
| `i18n` | Locale, text direction, message catalog (Arabic-first) |
| `permissions` | Permission enum, Principal, Rule, PolicySet, PolicyEngine, Approvals |
| `tools` | Tool abstraction, registry, built-in tools |
| `models` | Provider abstraction, gateway (retry/fallback/usage), registry |
| `memory` | Memory entries, store abstraction, write/retention policy |
| `tasks` | Task plans/steps, durable store, TaskEngine transitions |
| `workflows` | Durable DAG engine: definitions, versions, runs, steps, conditions, approvals |
| `triggers` | Cron/event/webhook triggers + scheduler that start workflow runs |
| `context` | Context assembly with a token budget |
| `audit` | Audited events, sinks, redaction, sequence |
| `integrations` | External-service abstraction + registry (webhook reference impl) |
| `runtime` | Local/Cloud profile and `Transport` boundary |
| `local` | Local/desktop runtime: lifecycle, modes, devices, sandbox, command runner, boundary, sync, control API |
| `computer` | Computer use: backends (X11), coordinate model, capture, OCR, perception, grounding, policy, screen-data boundary, runtime |
| `browser` | Autonomous web: backends (Playwright), URL/domain policy, observations, injection scan, sources, web-content boundary, runtime |
| `agent` | The loop, planner, recovery policy, tool executor, prompts |
| `runs` | `RunRecord` lifecycle/state machine + `RunStore` ABC |
| `persistence` | SQLAlchemy models, `Database`, Postgres store implementations |
| `infrastructure` | Redis-backed rate limiter, distributed lock, idempotency guard |
| `worker` | Reliable job queue, `RunExecutor`, `Worker`, recovery loop, worker CLI |
| `api` | FastAPI app, auth, rate limiting, schemas, `RunService` |

---

## 3a. Persistence, queue and recoverable runtime (Phase 2)

The foundation held all state in process memory. Phase 2 makes runs **durable**
and **recoverable** without changing any agent-core boundary: every addition is
an implementation behind an existing ABC (`RunStore`, `TaskStore`,
`ApprovalStore`, `MemoryStore`, `AuditSink`, `JobQueue`).

**Data layer.** `persistence/database.py::Database` owns an async SQLAlchemy
engine and a `transaction()` context manager. `persistence/models.py` defines the
tables (runs, run steps, task plans/steps, approvals, memory, audit). Schema is
managed by **Alembic** (`migrations/`), not `create_all` in production.
`persistence/stores/*` are the Postgres implementations; the composition root
selects them when `AAP_DATABASE_URL` is set.

**Optimistic concurrency.** `RunRecord` carries a `version`; every write goes
through `RunStore.update(record, expected_version=...)`, a conditional `UPDATE`
whose row count is checked. A stale writer gets `ConflictError` and re-reads —
this is what makes the state machine safe across replicas.

**Reliable queue.** `worker/queue.py::RedisJobQueue` keeps a ready list and an
in-flight sorted set. Claiming atomically moves a job ready→inflight; `ack`
removes it; `nack` requeues or drops; `reclaim_stale` returns jobs whose worker
died before acking. Lua scripts make each step atomic. `InMemoryJobQueue` is the
local/offline and test implementation.

**Recoverable runtime.** In `queued` execution mode the API never runs an agent
inline: `RunService.start()` persists a run, enqueues a `START_RUN` job and
returns immediately. A `Worker` claims the job and `RunExecutor` executes it
under a **lease** refreshed by a heartbeat. If the worker dies, the lease
expires; the recovery loop re-enqueues a `RECOVER_RUN` job, and the executor
re-reads durable state (completed task steps are terminal) and resumes. Recovery
is bounded by `job_max_attempts`; a run waiting for approval is never touched.

**Idempotency.** `RunService.start()` accepts an idempotency key backed by
`infrastructure/idempotency.py`; a repeated request returns the original run
instead of creating a duplicate.

**Distributed coordination.** `infrastructure/rate_limit.py` (Redis token
bucket), `lock.py` (SET NX PX lock) and `idempotency.py` are shared across API
replicas; the in-memory variants remain for the local runtime.



**Composition root.** `arabic_agent/container.py::Platform` is the single place
where concrete implementations are chosen and wired. Nothing else constructs its
own dependencies. `Platform.build()` is the dependency-injection seam: tests and
the desktop runtime inject a deterministic/local model provider and in-memory
stores, while production builds providers from configuration. If no model
provider is configured, `build()` raises `ConfigurationError` — the platform
refuses to start in a half-working state.

---

## 4. Agent loop

`arabic_agent/agent/runner.py::AgentRunner` is the heart of the platform. Every
transition is explicit so behaviour is testable and observable.

```
run(principal, goal)
  ├─ prepare sandboxed workspace (per run, under a tenant-scoped root)
  ├─ recall relevant memory (policy-filtered)
  ├─ resolve plan:  resume existing  |  Planner.plan(goal)  |  single-step fallback
  ├─ for each pending step (bounded by max_steps_per_run):
  │     ├─ guard checks (steps / retries / replans / wall-clock deadline)
  │     ├─ ContextManager.build(...)  → system + history + memory + plan + tool specs
  │     ├─ ModelGateway.complete(request)
  │     ├─ for each tool call:
  │     │     ├─ ToolExecutor.execute(...)
  │     │     │     ├─ PolicyEngine.evaluate(principal, tool, permission, risk)
  │     │     │     │     ├─ DENIED            → PermissionDeniedError
  │     │     │     │     └─ REQUIRES_APPROVAL → create Approval, raise ApprovalRequiredError
  │     │     │     ├─ Tool.validate_arguments → Tool.run (sandboxed)
  │     │     │     └─ ToolResult (ok / error)
  │     │     └─ observe → append tool result to history
  │     ├─ on failure → RecoveryPolicy.decide(error_class, attempts, replans)
  │     │     RETRY | REPAIR | REPLAN | APPROVE | SKIP | ABORT
  │     └─ on success → TaskEngine.complete_step
  ├─ verify final result (model-assisted, bounded)
  ├─ persist task outcome to memory (policy-filtered, redacted)
  └─ audit every transition → AgentRunResult
```

Key invariants:

- **The first execution is never assumed correct.** Tool results are read,
  failures classified, the plan revised, and the result verified.
- **Loops are impossible by construction.** Guards on max steps, max tool
  retries, max replans, and a wall-clock deadline terminate any run. On top of
  those bounds, `agent/loops.py::LoopGuard` detects a run that repeats an
  identical tool call and aborts it with a `LOOP_DETECTED` audit event, so a
  model that spins on the same action fails fast instead of burning the budget.
- **Context is the smallest sufficient set.** The context manager advertises a
  step's planned tool (`tool_hint`) plus the always-available core tools, so an
  unrelated tool is never dangled in front of a small local model, and the
  prompt stays inside the window.
- **Sensitive operations pause.** When policy returns `REQUIRES_APPROVAL`, the
  runner persists an `Approval` and returns a `PENDING_APPROVAL` result instead
  of executing. `resume_after_approval()` continues exactly where it stopped.

### Result contract

`AgentRunResult` carries `status`, `result`, `verified`, the final `plan`,
`pending_approval_id`, error, and counters (`steps_executed`, `tool_calls`,
`retries`, `replans`, `usage`). Callers never have to guess what happened.

---

## 5. Tool system

`arabic_agent/tools/base.py` defines the abstraction; the agent core knows
nothing about any specific tool.

- `Tool` (ABC): `name`, `description`, `parameters` (JSON schema, `ClassVar`),
  `permissions`, `risks`; `validate_arguments()` enforces the schema before
  execution; `execute()` wraps `run()` and normalises results.
- `ToolResult`: `ok`, `summary`, `data`, `duration_ms`; `data` is redacted.
- `ToolContext`: run/tenant/user identity, workspace root, scratchpad.
- `ToolRegistry`: registration, lookup, and spec listing for the model.

Adding a tool = subclass `Tool`, declare permissions, register it in
`Platform.build()` (or pass `extra_tools`). **No change to the agent core.**

Built-in today (real, sandboxed, tested):

| Tool | Permissions |
|---|---|
| `echo` | none |
| `finish` | none (control signal that ends a step) |
| `read_file` | `READ_FILE` |
| `read_file_range` | `READ_FILE` |
| `write_file` | `WRITE_FILE` |
| `edit_file` (exact replacement) | `WRITE_FILE` |
| `append_file` | `WRITE_FILE` |
| `create_file` | `WRITE_FILE` |
| `copy_file` | `WRITE_FILE` |
| `move_file` | `MOVE_FILE` |
| `delete_file` | `DELETE_FILE` |
| `make_directory` | `WRITE_FILE` |
| `file_info` | `READ_FILE` |
| `directory_tree` | `LIST_DIRECTORY` |
| `search_files` | `LIST_DIRECTORY` |
| `file_type` | `READ_FILE` |
| `list_directory` | `LIST_DIRECTORY` |
| `run_command` | `RUN_COMMAND` |
| `inspect_document` | `FILES_READ` |
| `read_document` | `FILES_READ` |
| `search_document` | `FILES_READ` |
| `inspect_archive` | `FILES_READ` |
| `list_documents` | `FILES_SEARCH` |
| `inspect_project` | `FILES_READ`, `FILES_SEARCH` |

Filesystem tools resolve every path against the run workspace and refuse
escapes (`tools/builtin/_paths.py`); the heavy lifting is shared in
`tools/builtin/_fileops.py` so the workspace and local-runtime tools behave
identically. `run_command` runs with the workspace as its working directory.

`documents/` is a real ingestion layer: parsers read the actual bytes for
text/Markdown/JSON/CSV/HTML/PDF/DOCX/XLSX/PPTX and inspect ZIP/TAR archives
without extracting. Each section carries provenance (page/slide/sheet/line) and
parse limits are conservative. Optional parser dependencies degrade honestly —
an absent parser reports the format as unsupported rather than fabricating a
result. Ingested content is scanned for likely secrets; a finding is reported by
kind + locator only (`SECRET_DETECTED`), never by value. `project/` builds a
project summary from real filesystem evidence (languages, manifests, tests,
entrypoints, docs).

Future tools (document generation, email, ERP, WhatsApp, social, image, video,
calendar, printing) are declared in `TODO.md` and plug into the same interface.
Browser tools are no longer in this list — they are implemented (see §13c).

---

## 6. Permission system

`arabic_agent/permissions/` — a real policy layer between the agent and every
tool, never an advisory message.

- `Permission` enum: `READ_FILE`, `WRITE_FILE`, `MOVE_FILE`, `DELETE_FILE`,
  `RUN_COMMAND`, `PRINT_DOCUMENT`, `SEND_EMAIL`, `CREATE_INVOICE`,
  `DELETE_INVOICE`, `SEND_WHATSAPP`, `SEND_SOCIAL_MESSAGE`, … extensible.
- `RiskLevel`: `LOW | MEDIUM | HIGH | CRITICAL`.
- `Principal`: `tenant_id`, `user_id`, `roles`, `granted_permissions`;
  `has_role()` and `may_hold()`.
- `Rule`: matches on role, tool, permission, and a risk ceiling.
- `PolicySet`: an ordered list of rules producing a `PolicyDecision`.
- `PolicyDecision`: `allowed | denied | requires_approval`.
- `PolicyEngine`: per-tenant policies over a deny-by-default platform policy;
  `set_tenant_policy()` / `policy_for()` keep tenants isolated.

Decision model:

| Outcome | Meaning |
|---|---|
| `ALLOWED` | execute immediately |
| `DENIED` | refuse; raise `PermissionDeniedError` |
| `REQUIRES_APPROVAL` | create an `Approval` and pause the run |

The default platform policy is **deny-by-default for destructive/high-risk
operations**: deleting files/invoices, sending email/WhatsApp/social messages,
printing, and running shell commands require approval or are denied.

---

## 7. Human-in-the-loop approvals

Approvals are first-class, persisted, audited entities — not a chat message.

- `Approval`: `id`, `run_id`, `tenant_id`, `user_id`, `tool_name`, `permission`,
  `arguments`, `summary`, `status`, `created_at`, `expires_at`, `resolved_by`,
  `reason`.
- `ApprovalStore` (ABC) with `InMemoryApprovalStore` for the local runtime and
  `PostgresApprovalStore` for production; resolution is a one-shot conditional
  update, so an approval can never be decided twice.
- TTL from `settings.approval_ttl_seconds`; expired approvals can never be
  resolved as approved.
- API: `GET /approvals` lists pending approvals for the tenant;
  `POST /approvals/{id}/decide` resolves and **resumes the run** on approval, or
  terminates it as `CANCELLED` on denial.
- Both the request and the decision are written to the audit log.

Example the design supports end-to-end: *"42 invoices were created. Send them to
customers?"* → the send tool's permission is `REQUIRES_APPROVAL` → the run pauses
with a pending approval → the user approves → the run resumes and sends.

---

## 8. Task engine

`arabic_agent/tasks/` owns plan lifecycle, kept out of the agent loop.

- `TaskStep`: `id`, `index`, `description`, `status`, `attempts`,
  `result_summary`, `result_data`, `error_class`, `error_message`, timestamps,
  and a free-form `scratchpad`.
- `TaskPlan`: ordered steps, `revision`, `render()` for prompt injection,
  `with_step()` for immutable updates, `from_descriptions()` for construction.
- `TaskStore` (ABC) with `InMemoryTaskStore` today; `save`, `get(tenant_id)`,
  `get_by_run`.
- `TaskEngine`: `create_plan`, `start_step`, `complete_step`, `fail_step`,
  `block_step`, `skip_step`, `replan` — every transition persisted.

Because state is durable, a run that stops (error, approval, timeout) can be
resumed and knows exactly which step it reached. A multi-step goal such as
*"prepare customer invoices and send them"* decomposes into identify → query →
validate → generate → render PDFs → prepare messages → request approval → send →
verify → report, and survives interruption at any step.

---

## 8a. Workflow engine (Phase 5)

`arabic_agent/workflows/` is a durable DAG engine, separate from the single-run
agent loop. It is the deterministic counterpart to the LLM-driven loop: a
workflow is authored once as a graph, then runs repeatably.

**Definition and versioning.** `Workflow` holds identity and status
(`DRAFT | PUBLISHED | ARCHIVED`). `WorkflowVersion` is an immutable snapshot of
the graph, and every `WorkflowRun` pins the `workflow_version_id` it started
from. Editing a workflow creates a new version; a run already in flight is never
affected. This is the core safety property of the engine.

**Steps.** `WorkflowStep` declares `kind`, `depends_on`, `condition`, and its
payload. Kinds: `DETERMINISTIC` (a registered action such as `set`/`sum`),
`TOOL` (a platform tool call), `AGENT` (a nested agent goal), `APPROVAL`
(human-in-the-loop gate). Each step has a `RetryPolicy` (attempts, backoff) and
an `error_policy` (`FAIL`, `CONTINUE`, `SKIP`), plus a per-step timeout.

**Execution.** `WorkflowEngine` schedules a step once all its dependencies are
in a successful terminal state. Steps whose `condition` evaluates false are
skipped. The engine re-derives what is runnable from persisted rows on every
pass, so it resumes correctly after an interruption and never re-runs completed
steps. Parallelism is bounded (`MAX_PARALLELISM`).

**Templating and conditions.** `workflows/expressions.py` renders `{{ ... }}`
templates over a scope of `inputs`, `steps.<id>.output`, and `env`, and
evaluates conditions over the same scope. Conditions are evaluated only before a
step first starts, so an approval step that pauses and resumes is not re-skipped.

**Approvals.** An `APPROVAL` step pauses the run in `WAITING_APPROVAL` and opens
a real approval through the same permissions/approvals store used by agent runs.
The approval is routed back to the workflow engine by run-id prefix, and every
decision is written to the audit log. Resuming re-drives the paused step.

**Persistence.** `WorkflowStore`, `WorkflowVersionStore` and `WorkflowRunStore`
are ABCs with in-memory defaults and Postgres implementations
(`persistence/stores/workflow.py`). Run records carry lease/heartbeat fields so
an interrupted run can be reclaimed by a worker.

**Service and HTTP.** `api/workflow_service.py` owns the rules: persist before
execute, idempotency-key reuse, a fixed per-run workspace, and approval
transitions. The API exposes create/get/update workflows, list versions, start
runs, read runs and step runs; approvals are resolved through the shared
`POST /approvals/{id}/decide` endpoint.

**Recovery.** The worker's recovery sweep reclaims a workflow run whose lease
expired: it resets any step stranded in `RUNNING` back to `PENDING`
(`WorkflowRunStore.reset_running_steps`), clears the run's lease, and enqueues a
`RECOVER_WORKFLOW` job. Because the engine re-derives runnable work from
persisted rows, the recovered run is re-driven from where it stopped rather than
restarted, and completed steps are never re-run.

---

## 8b. Triggers and scheduler (Phase 3B)

`arabic_agent/triggers/` turns "something happened" into a workflow run without a
human calling the API. It is a *thin* layer over the workflow engine: every kind
converges on `WorkflowService.start_run`, so triggered work is executed by the
same worker and engine as any other run. There is no second execution path.

**Kinds.** `TriggerKind`: `MANUAL` (authenticated "fire now"), `CRON` (a
schedule), `WEBHOOK` (an inbound HTTP call authenticated by an HMAC signature),
`EVENT` (an internal domain event such as `invoice.created`).

**Cron.** `triggers/cron.py` is a dependency-free 5-field cron parser with
timezone support and Vixie day-of-month/day-of-week semantics, plus macros
(`@daily`, …). `CronExpression.next_after` is strictly-after, so a scheduler can
never spin on the same instant. `MisfirePolicy` governs a missed occurrence:
`SKIP` advances past it, `RUN_ONE` fires once, `RUN_ALL` catches up (bounded by
`AAP_SCHEDULER_MAX_CATCHUP`).

**Scheduler.** `TriggerScheduler` scans due triggers and **claims** each
occurrence atomically with a compare-and-swap that advances `next_fire_at` and
sets a lease (`TriggerStore.claim_due`). The claim is durable, so a scheduler
that crashes after claiming does not re-fire the occurrence on restart, and two
schedulers racing on the same trigger cannot both fire it. The scheduler only
*starts* runs; the queue consumer executes them.

**Webhooks.** A webhook trigger has a public `webhook_id` and a stored
HMAC-SHA256 secret. `triggers/signing.py` verifies `sha256=<hmac(secret,
timestamp + "." + body)>` against a tolerance window; a missing, malformed, or
stale signature fails closed. The secret is returned exactly once (at creation),
never by a read, and never written to the audit log.

**Idempotency.** Each firing derives an idempotency key from its cause (a cron
occurrence, a webhook `X-Delivery-ID`, an event id). The run is created with that
key, so a redelivered webhook, a retried event, or a scheduler that crashed after
starting a run yields one run, not many. The firing itself is recorded as a
`TriggerRun` row.

**Authorisation.** Creating/updating a trigger requires `MANAGE_WORKFLOW`;
firing one requires `RUN_WORKFLOW`. A fired run is authorised as the trigger
owner (their roles and permission ceiling were captured at creation), so a
webhook or cron job can never run with more authority than its author had.

**Service and HTTP.** `triggers/service.py` owns create/update/enable/disable/
delete and the fire paths. The API exposes `/triggers` CRUD, `/triggers/{id}/
fire`, `/triggers/{id}/firings`, `POST /webhooks/{webhook_id}` (signature-auth,
no API key), and `POST /events` (authenticated event ingestion, optionally
restricted to `AAP_ALLOWED_EVENT_TYPES`).

---

## 9. Model gateway (Model Abstraction)

`arabic_agent/models/` decouples the agent from any single model.

- `ModelProvider` (ABC): `complete`, `stream`, `supports(capability)`, `aclose`.
- `ModelCapability`: `CHAT | TOOL_USE | VISION | SPEECH | EMBEDDING`.
- `ProviderRegistry`: `provider/model` id splitting, resolution, lifecycle.
- `ModelGateway`: selects the provider for a model id and capability, retries
  with backoff, falls back across a candidate list, and accounts usage
  (`GatewayStats`).
- `ModelRequest` / `ModelResponse` / `ChatMessage` / `ToolSpec` / `ToolCall` /
  `Usage` are provider-neutral.

`OpenAICompatibleProvider` serves both cloud OpenAI and any local
OpenAI-compatible server (Ollama, vLLM, llama.cpp) — the offline path. Adding a
provider (Anthropic, Gemini, a local vision or speech model) means implementing
`ModelProvider` and registering it; the agent core is untouched.

Switching model = changing configuration (`AAP_DEFAULT_MODEL`, fallbacks), not
code.

**Native-engine isolation.** File-backed engines (llama.cpp via
`llama-cpp-python`) run inside the same process as the agent by default, but the
native library can abort on some hardware — a SIGSEGV/SIGABRT that no Python
`try/except` can catch. When `AAP_LOCAL_MODEL_ISOLATE` is on (the default),
`local/subprocess_backend.py` runs the engine in a dedicated child process
(`local/subprocess_worker.py`) and speaks a length-prefixed JSON protocol over
stdin/stdout. A native abort then surfaces to the agent as a recoverable
`ExternalServiceError`; the worker is respawned on the next call and the agent
loop continues. The child verifies the weights file and signals readiness before
serving, so a bad model fails fast rather than mid-request. This is transparent
to the rest of the system: `build_backend(..., isolate=True)` returns the same
`LocalInferenceBackend` protocol the provider and gateway already consume.

---

## 10. Memory

`arabic_agent/memory/` is separate from the agent core and deliberately
conservative.

- `MemoryKind`: `CONVERSATION | USER_PREFERENCE | COMPANY_CONTEXT | TASK |
  KNOWLEDGE | WORKFLOW`.
- `MemoryEntry`: tenant/user scope, kind, content, `metadata`, importance,
  `expires_at`.
- `MemoryStore` (ABC) with `InMemoryMemoryStore`; `query` filters by tenant,
  user scope, kind, and recency.
- `MemoryPolicy`: per-kind retention, and `check_write(kind, content)` which
  **refuses to persist secrets** and enforces size limits.

Nothing is stored automatically. The runner writes a redacted task outcome only
after a run, subject to policy. Access is tenant- and user-scoped.

---

## 11. Context management

`arabic_agent/context/manager.py::ContextManager` decides what reaches the model.

Inputs considered: user message, conversation history, relevant memory, tool
definitions, tool results, workspace files, knowledge, current task state, and
locale.

- A token budget (`settings.max_context_tokens`) is enforced by a script-aware
  estimator in `context/budget.py` (Arabic costs more tokens than Latin text).
- History is windowed (`_HISTORY_WINDOW`), memory is top-k relevant, and tool
  specs are only included when the model supports tool use.
- The system prompt is composed with the plan, memory lines, and the **Arabic
  language directive** (locale + text direction).

The model never receives the whole store; it receives the necessary slice.

---

## 12. Audit & observability

`arabic_agent/audit/` records every meaningful transition.

- `AuditEventType`: run/task/step lifecycle, tool calls and results, permission
  decisions, approvals, model calls, errors, retries, replans, verification.
- `AuditEvent`: sequence, tenant/user/run/step ids, type, message, data,
  timestamp.
- Sinks: `LoggingAuditSink` (structured logging) and `InMemoryAuditSink`
  (queryable history). `AuditLog` fans out to all sinks and assigns sequence
  numbers per run.
- **Redaction**: `core/redaction.py` masks secret-looking keys and values before
  they are stored or logged. Secrets, tokens, and passwords never reach the log.

The audit log answers: what was the plan, which tools ran, what did they return,
what failed, what was retried, who approved what, and how long it took.

### 12a. Observability package (Phase 1b)

`arabic_agent/observability/` is a dependency-light package the whole platform
depends on — the agent core never imports a vendor SDK.

- **Correlation** (`context.py`): a frozen `CorrelationContext`
  (`request_id`, `run_id`, `job_id`, `tenant_id`, `user_id`, `worker_id`) held in
  a `ContextVar`, so it is isolated per task and survives `await` boundaries.
  `correlation_scope(...)` sets/restores fields; the API middleware generates or
  echoes `X-Request-ID`, the worker binds run/job ids around execution.
- **Logging** (`logging.py`): `JsonLogFormatter` injects the correlation context
  into every record and redacts secrets in both the message and structured
  fields. `configure_logging()` installs it process-wide.
- **Metrics** (`metrics.py`): a thread-safe registry of counters, gauges and
  histograms with Prometheus text exposition (`render_prometheus`). `Metrics` is
  the typed facade instrumented by the runtime: runs started/finished/failed/
  recovered, run and tool and model latency, tool/model retries, job
  enqueue/claim/ack/nack/fail, queue depth, approval latency, rate-limit
  rejections, dependency up/down. Label values are redacted and bounded (raw
  error text is never a label).
- **Tracing** (`tracing.py`): `Tracer` + `span()` cover API → run → model → tool;
  spans record correlation ids, status and error. `otel.py` bridges finished
  spans to OpenTelemetry **only** when the SDK is installed and enabled.
- **Readiness** (`readiness.py`): `ReadinessService` runs `DatabaseProbe`,
  `RedisProbe`, `ModelProbe` and `AuditProbe` with a timeout. Critical failures
  make `GET /readyz` return 503; `/health` stays a liveness probe. Probe errors
  are reported as `unreachable` so a connection string never leaks.

---

## 13. Local / Cloud runtime boundary

`arabic_agent/runtime/boundary.py` makes the split explicit.

- `Capability`: `LOCAL_FILES | LOCAL_APPS | PRINTING | BROWSER | LOCAL_LLM |
  NETWORK | PERSISTENT_STORE | GPU`.
- `RuntimeProfile.local()` — files, local apps, printing, browser, local LLM,
  persistent store; **no network** by default.
- `RuntimeProfile.cloud()` — network and persistent store; **no** local files,
  apps, printing, or GPU.
- `Transport` (ABC) — `submit_run(tenant, user, goal)` / `poll_run(...)`: the
  contract for a desktop runtime talking to a cloud runtime.

The rule is: **never assume everything needs the cloud, and never assume
everything can run offline.** The same agent core runs in both; only the
registered tools, providers, and stores differ. A desktop runtime registers
local tools and a local model; a cloud runtime registers integration tools and
cloud providers.

### 13a. Local runtime implementation (Phase 4)

`arabic_agent/local/` is the concrete local runtime. A build with
`AAP_RUNTIME_KIND=local` assembles it in the composition root and registers the
local tools; a cloud build registers none of them, so the local tools cannot
even be named by the model.

Components:

- **`runtime.py`** — `LocalRuntime` orchestrates lifecycle, mode, devices,
  boundary, audit and the local tool registry. `build_local_runtime(settings)`
  constructs it from configuration; `LocalRuntimeStatus` is the honest,
  non-secret status surface (state, mode, platform, capability ids, workspace
  roots).
- **`lifecycle.py`** — `LifecycleStateMachine` with the states
  `stopped → starting → ready → running ⇄ paused → stopping → stopped` and an
  explicit `accepts_tools` predicate; illegal transitions raise.
- **`modes.py`** — `ModeManager` holds the *declared* mode
  (`online | offline | hybrid`). The mode is never inferred from a failed
  request; `offline` forces local routing.
- **`device.py`** — `DeviceIdentity` / `DeviceRegistry` identify a device by an
  operator-provided `instance_fingerprint`, **not** by serial number or MAC
  address (no hardware identifiers are collected).
- **`sandbox.py`** — `FilesystemPolicy` + `FilesystemSandbox`. Allow-list of
  roots, explicit deny-list, textual `..` refusal, NUL-byte refusal,
  sensitive-location protection (`/etc`, `~/.ssh`, `~/.aws`, `*.pem`, …) applied
  *before* the allow-list, and symlink-escape detection. Relative paths are
  anchored to the run workspace, so a tool path is deterministic and cannot
  depend on the process CWD.
- **`command.py`** — `CommandPolicy` + `ControlledCommandRunner`. Commands are
  allow-listed by basename, executed with `shell=False` (so `;`, `|`, `$(...)`
  are literal arguments, never a second command), with a minimal environment
  that inherits **no** secrets, a hard timeout, and a CWD confined to approved
  roots.
- **`capabilities.py`** — `LocalCapability` catalogue. Only the filesystem and
  command capabilities are `implemented=True`; clipboard, screen capture, input
  control and application launch are declared with `implemented=False` so the
  vocabulary and audit surface are stable without pretending to work.
- **`boundary.py`** — `DataBoundaryPolicy` decides where a piece of data may be
  processed (`LOCAL | REMOTE | EITHER`) given its sensitivity. `SECRET` never
  routes remotely; `CONFIDENTIAL` is local-only by default; `offline` forces
  local.
- **`sync.py`** — `SyncQueue` + `SyncCoordinator`. Only a closed set of
  `SyncKind`s ever crosses the boundary; secret-like payload keys are redacted
  and a payload the boundary refuses is marked `REJECTED`, not uploaded.
- **`control.py` / `http.py`** — `LocalControlAPI` and a small FastAPI app for
  the desktop UI: token-authenticated `/local/status`, `/local/capabilities`,
  `/local/start|stop|pause|resume`. Auth fails closed when a token is required
  but not configured.
- **`main.py`** — the `agent-desktop` entry point. It builds the local platform,
  refuses to start without `AAP_LOCAL_CONTROL_TOKEN` when auth is on, and serves
  the control app on loopback (`AAP_LOCAL_CONTROL_HOST`/`_PORT`).
- **`sandbox_provider.py`** — `SandboxProvider` is the single seam that turns a
  tool call into a sandbox; the local tools build every sandbox through it, so
  the workspace/extra-root/deny-list merge lives in one place.

The local tool set (`tools/builtin/local.py`) is deliberately small and every
path goes through the sandbox: `read_local_file`, `list_local_directory`,
`search_local_files`, `write_local_file`, `copy_local_file`, `move_local_file`,
`create_local_directory`, `delete_local_file`, `run_local_command`. Writes,
moves and deletes are approval-gated by the permission policy; reads are not.

**Durable local persistence.** A local build is durable, not in-memory.
`local/persistence/` provides `LocalDatabase` — an async SQLAlchemy engine over
an embedded SQLite file (WAL journal, `NullPool` so connections close
deterministically and no aiosqlite thread lingers) — plus local implementations
of the *same* store ABCs the cloud build uses: workflows, versions, runs and
steps, approvals, audit, the sync queue and idempotency keys. The schema is
owned by Alembic (`migrations_local/`, its own `local_alembic_version` table so
it never collides with the Postgres branch) and the composition root runs the
migration at build time. `Platform.durable` reports whether the store is on
disk; `Platform.local_database` is the handle. A local build uses no Postgres
and no Redis. Tenant isolation and optimistic concurrency are enforced by the
local stores exactly as in the Postgres ones (wrong-tenant reads raise
`NotFoundError`; a stale `expected_version` raises `ConflictError`).

**Restart and recovery.** Because the local store is durable, the recoverable
worker runtime (§3a) works locally too. A run left `RUNNING` with an expired
lease by a lost worker is reclaimed by `Worker.recover_stale` on the next local
platform and driven to completion, and the recovery is written to the durable
audit trail. A run waiting on approval survives a real process restart: the next
process sees the paused run and its pending approval from disk alone, resolves
it, and `WorkflowEngine.resume_run` completes the run. Offline operation is
verified by running a full local agent task inside a Linux network namespace
with no network, where any outbound connection fails at the OS level.

Not built yet: a `Transport` implementation and desktop process, a cloud sync
endpoint, desktop packaging, and the local-model hardening in Phase 2.

---

## 13b. Computer use, perception & screen control (Phase 7)

The agent can *see* and *operate* the machine it runs on, under the same
governance as every other tool. This is a local-only capability: on a cloud
runtime the tools do not exist, so the model cannot even name them.

### The `computer/` subsystem

The OS logic lives in `src/arabic_agent/computer/`, behind an interface, never
inside the agent core or a tool handler:

- **`base.py`** — `ComputerBackend` (ABC): `capture_screen`, `get_screen_metadata`,
  `list_windows`, `get_active_window`, `focus_window`, `pointer_move/click/scroll`,
  `keyboard_type/key/hotkey`, plus `available` / `unavailable_reason` and
  `require_available()`. A backend that cannot work reports *why* instead of
  pretending. `CapturedImage`, `ScreenMetadata`, `WindowInfo`, `PointerButton`
  are the value types.
- **`coords.py`** — the coordinate model. `CoordinateSpace` (`LOGICAL` vs
  `PHYSICAL`) is explicit, never assumed: a `Point`/`Rect` carries its space, and
  `to_physical` / `to_logical` convert via a monitor's DPI scale. `MonitorInfo`,
  `virtual_bounds` (handles negative origins in a multi-monitor layout),
  `monitor_for_point`, and `clamp_point` (an off-screen target is pulled back in
  rather than silently clicking the wrong place).
- **`capture.py`** — pure image helpers: PNG encode, bounded downscale
  (`resize_to_fit`), crop, and a perceptual hash (`difference_hash`) for
  change detection. No OS access.
- **`ocr.py`** — `OcrEngine` Protocol with `NullOcr` and `TesseractOcr`
  (Arabic-first: `build_ocr_engine(languages=("ara","eng"))`). A missing
  tesseract yields `NullOcr`, which reports `available=False` rather than
  returning empty text as if it had read the screen.
- **`perception.py`** — `PerceptionLayer` fuses the backend and OCR into an
  `Observation`: image dimensions, monitors, cursor, active window, and
  `PerceivedElement`s / `TextBlock`s with boxes and provenance (`sources`).
  `Observation` is a frozen, Arabic-aware model with a `summary()` for the model.
- **`grounding.py`** — turns a *semantic* target into a point. `GroundingTarget`
  can be an element id, a role+label, recognised text, or raw coordinates.
  `normalize_text` is Arabic-aware (strips diacritics/tatweel, folds
  presentation forms, normalises alef/ya/ta-marbuta), and text matching prefers
  the longest overlap. A miss raises `NotFoundError` — the agent must re-observe,
  not click blindly.
- **`policy.py`** — `ComputerPolicy` and `ActionBudget`: coordinate clamping,
  allowed keys, max scroll, and per-run caps on pointer/keyboard/capture actions
  plus a minimum interval between actions. `ResourceExhaustedError` when a cap is
  hit. This is the anti-runaway layer.
- **`observations.py`** — `ObservationStore`: the in-memory, run-scoped store of
  recent observations with freshness checks.
- **`runtime.py`** — `ComputerRuntime` is the single entry point the tools call.
  It bundles the backend, perception, policy, budget, observation store, the
  screen-data boundary and the audit log, and enforces the
  **observe → act → observe** discipline: an action must reference a fresh
  observation (or capture one first), and every action is budgeted and audited.
  `changed_since` compares a fresh capture against a prior observation's hash.
- **`attachments.py`** — `ObservationAttachmentProvider` offers the latest
  observation to the runner as a `ContextAttachment`, but only when the
  screen-data boundary permits the destination and the model can see.
- **`screen_boundary.py`** — `ScreenDataPolicy` (`never_store` | `local_only`
  | `cloud_allowed`) mapped onto the existing `DataBoundaryPolicy` and
  `DataSensitivity`. A screenshot is treated as `SECRET` under `never_store` and
  `CONFIDENTIAL` under `local_only`, so a remote model never receives one; the
  mapping fails closed.
- **`backends/`** — `linux_x11.py` is a *real* backend: capture through Pillow's
  `ImageGrab`, monitors via `xrandr`, input and windows via `xdotool`, all with
  `shell=False` and an explicit argv (no model value can become a second
  command). It probes the display socket at construction and reports itself
  unavailable with a reason when `DISPLAY`, `xdotool`, `xrandr` or Pillow are
  missing. `null.py` is the honest "not available" backend; `build_computer_backend`
  selects per platform (Windows/macOS return a Null backend with a clear reason —
  design only, no pretence).

### Tools (`tools/builtin/computer.py`)

Ordinary `Tool`s, registered only for a local runtime with
`AAP_COMPUTER_CONTROL_ENABLED=true`, so the permission/policy/approval/audit path
is the *existing* one — computer control is not a bypass:

- `computer_screen` (`SCREEN_READ`) — capture and return a structured perception.
- `computer_metadata` (`SCREEN_READ`) — monitors, bounds, cursor, active window;
  no pixels.
- `computer_pointer` (`POINTER_CONTROL`) — move/click/double-click/right-click/scroll,
  targeting by element, role+label, text, or coordinates.
- `computer_keyboard` (`KEYBOARD_CONTROL`) — type Unicode (Arabic included),
  press a key, or a hotkey.
- `computer_window` (`WINDOW_CONTROL`) — list, active, focus.
- `computer_verify` (`SCREEN_READ`) — re-capture and report whether the screen
  changed, so the agent confirms an effect instead of assuming it.

`SCREEN_READ`/`POINTER_CONTROL`/`KEYBOARD_CONTROL` are `HIGH` risk and
`WINDOW_CONTROL` is `MEDIUM`; the default policy requires approval for the
input-control permissions. `INPUT_CONTROL` remains reserved as a `CRITICAL`
catch-all.

### Vision models & attachments

The model layer is multimodal-ready without depending on any one provider:
`ChatMessage` can carry `ImageContent` (base64, `repr`-hidden), and the
OpenAI-compatible provider encodes images as a `data:` URI content array.
`ModelGateway.default_supports(VISION)` lets the context manager include an image
*only* when the active model can see; `default_is_local()` decides whether the
destination is remote. `ContextManager.build(attachments=…, allow_images=…)`
charges each image against the token budget and drops one that does not fit
rather than overflowing the window. The runner asks an `AttachmentProvider` for
the turn's attachments and treats a provider failure as "no attachment", never as
a run failure.

### Audit

Every step is audited: `computer.screen_captured`, `computer.pointer_moved`,
`computer.clicked`, `computer.scrolled`, `computer.text_typed` (**length only**,
never the typed text), `computer.key_pressed`, `computer.window_focused`,
`computer.action_verified`, `computer.action_failed`, `computer.stop_requested`,
`computer.action_throttled`, `computer.observation_stale`,
`computer.boundary_decision`. The boundary decision is recorded so "why did this
screenshot not reach the model" is answerable.

### Settings

`AAP_COMPUTER_CONTROL_ENABLED` (default false), `AAP_COMPUTER_BACKEND` (`auto`),
`AAP_COMPUTER_OCR_LANGUAGES` (`ara,eng`), `AAP_COMPUTER_SCREEN_POLICY`
(`local_only`), `AAP_COMPUTER_MAX_IMAGE_DIMENSION`,
`AAP_COMPUTER_REQUIRE_FRESH_OBSERVATION`,
`AAP_COMPUTER_MAX_OBSERVATION_AGE_SECONDS`, and the per-run caps
`AAP_COMPUTER_MAX_POINTER_ACTIONS` / `_KEYBOARD_ACTIONS` / `_CAPTURES`.

---

## 13c. Autonomous browsing, web research & web interaction (Phase 8)

The agent can *open, read, search and operate* the web under the same governance
as every other tool. Like computer use, this is a local-only, opt-in capability:
on a cloud runtime, or when browsing is disabled, the tools do not exist, so the
model cannot even name them. It is deliberately **not** a separate "browser
agent" — the tools are ordinary `Tool`s that run through the existing executor,
permission/policy gate, approval flow and audit trail.

### The `browser/` subsystem

All web logic lives in `src/arabic_agent/browser/`, behind an interface, never
inside the agent core or a tool handler:

- **`base.py`** — `BrowserBackend` (ABC): `navigate`, `snapshot`, `screenshot`,
  `click`, `type_text`, `press_key`, `select_option`, `set_checked`, `scroll`,
  `wait_for`, `download`, `upload`, `list_tabs`/`open_tab`/`switch_tab`/`close_tab`,
  `dialog`/`accept_dialog`/`dismiss_dialog`, plus `available` /
  `unavailable_reason`. Value types: `ElementInfo`, `FormInfo`, `FormFieldInfo`,
  `TabInfo`, `DialogInfo`, `DownloadedFile`, `Screenshot`, `PageSnapshot`.
  A backend that cannot work reports *why* instead of pretending.
- **`url_security.py`** — `UrlPolicy` answers, deterministically and with no
  network probe, whether a URL may be opened. It refuses dangerous schemes
  (`javascript:`, `data:`, `blob:` …), `file://` outside an allowed root,
  credential-bearing URLs, and — by default — loopback/private/link-local hosts
  (including the cloud-metadata address `169.254.169.254`), i.e. SSRF is blocked
  before the engine is touched. Domain allow/deny lists are matched against the
  registrable host. The result is a `UrlDecision` carrying a `UrlRisk`, so a URL
  can be *refused* or merely *require approval*.
- **`policy.py`** — `BrowserPolicy` and `ActionBudget`: bounded observations
  (elements/text), per-run caps on navigations/interactions/observations, a
  minimum interval between actions, tab limits, download count/size limits, an
  upload allow-list, a freshness requirement, and a repeated-navigation loop
  guard. Every refusal is a classified `PlatformError` the loop can recover from.
- **`observations.py`** — `BrowserObservation` and `ObservationStore`: a bounded,
  normalised view of the page (URL, title, headings, interactive elements, forms,
  links, dialog, scroll position) with a freshness timestamp and a truncation
  flag. Never a raw DOM dump; `build_observation` caps text.
- **`content.py`** — pure, bounded web-text helpers: whitespace/blank-line
  normalisation, a bounded extract that *reports* it was bounded, and a
  term-search that returns only a bounded window of context around each match
  with a character offset for provenance.
- **`injection.py`** — scans page text for a small, high-precision set of
  prompt-injection phrases. A hit is recorded as an `InjectionIndicator` on the
  observation and on the audit trail and surfaced to the model as a *warning*,
  never an instruction. Detection flags; the boundary/permission/sandbox layers
  are the hard defences.
- **`sources.py`** — `SourceLedger`: a bounded per-run ledger of pages the agent
  actually read (recorded from observations, never from the model), with a source
  quality heuristic, so the final answer cites real sources instead of invented
  ones.
- **`boundary.py`** — `WebDataBoundary`: maps a `WebContentPolicy`
  (`never_store` | `local_only` | `cloud_allowed`) onto the existing
  `DataBoundaryPolicy`/`DataSensitivity` and answers one question
  deterministically: *may this page content be sent to a remote model?* The
  mapping fails closed.
- **`runtime.py`** — `BrowserRuntime` is the single entry point the tools call.
  It bundles the backend, URL policy, browser policy, budget, observation store,
  source ledger, web-data boundary and audit log, and enforces the
  **observe → act → observe** discipline: an action must reference a fresh
  observation, navigation is validated before the engine is touched, a `submit`
  or an accepted dialog is treated as consequential and refused unless the tool
  layer has cleared it, and every action is budgeted and audited. `changed_since`
  compares a fresh observation against a prior one.
- **`attachments.py`** — `BrowserAttachmentProvider` offers the latest
  observation's screenshot as a `ContextAttachment`, but only when the
  web-content boundary permits the destination and the model can see; it mirrors
  `computer/attachments.py` rather than inventing a parallel contract.
- **`backends/`** — `playwright.py` is a *real* backend driving Chromium through
  Playwright's async API. Each observation tags visible interactive elements with
  a stable `data-aap-ref`, and the model acts on that `ref`, never a CSS/XPath
  selector. Downloads are confined to the backend's own directory with a
  sanitised filename and a size cap. A JavaScript dialog is **never**
  auto-accepted: a click that opens one returns promptly (the backend races the
  click against dialog detection rather than wedging on Playwright's blocked
  click), the dialog stays open, and the runtime returns the prior observation
  with the dialog attached so the agent resolves it explicitly. `null.py` is the
  honest "not available" backend; `build_browser_backend` selects per platform and
  reports the missing dependency as the reason.

### Tools (`tools/builtin/browser.py`)

Ordinary `Tool`s, registered only for a local runtime with
`AAP_BROWSER_ENABLED=true`, so the permission/policy/approval/audit path is the
*existing* one:

- `browser_navigate` (`BROWSER_NAVIGATE`, MEDIUM) — validate then navigate.
- `browser_observe` (`BROWSER_READ`, MEDIUM) — the bounded, freshness-tracked view.
- `browser_interact` (`BROWSER_INTERACT`, HIGH) — click/type/press/select/check/scroll.
- `browser_submit` (`BROWSER_SUBMIT`, CRITICAL) — consequential form submit.
- `browser_download` (`BROWSER_DOWNLOAD`, MEDIUM) / `browser_upload`
  (`BROWSER_UPLOAD` + `FILES_READ`, HIGH).
- `browser_dialog` (`BROWSER_INTERACT`, HIGH) — get/accept/dismiss a dialog.
- `browser_verify` (`BROWSER_READ`, MEDIUM) — re-observe and report change.
- `browser_research` (`BROWSER_READ`, LOW) — the recorded source list, for
  evidence-based citation.

### The approval gate is trusted, not model-supplied

Submitting a form or accepting a dialog is consequential, so the runtime refuses
it unless the call carries an approval. The signal is **not** a tool argument the
model (or a hostile page) could forge: the executor injects
`context.metadata["approval_granted"]` only on the resume path, after re-checking
policy against the stored, granted `ApprovalRecord`. `ToolExecutor._merge_context`
threads the trusted flag through, and the submit/dialog tools read it — so a
model that emits `"approved": true` in its own arguments still gets an approval
request rather than an executed submit.

### Audit

Every step is audited: `browser.navigated`, `browser.tab_opened`/`_switched`/
`_closed`, `browser.observed`, `browser.acted`, `browser.form_submitted`,
`browser.dialog_resolved`, `browser.waited`, `browser.downloaded`,
`browser.uploaded`, `browser.action_verified`, `browser.action_failed`,
`browser.stop_requested`, `browser.boundary_decision`. Page content, typed
secrets and screenshots are never written to the audit trail — only metadata
(counts, refs, URLs, indicator kinds).

### Settings

`AAP_BROWSER_ENABLED` (default false), `AAP_BROWSER_BACKEND` (`auto`),
`AAP_BROWSER_HEADLESS`, `AAP_BROWSER_EXECUTABLE_PATH`, `AAP_BROWSER_CONTENT_POLICY`
(`local_only`), `AAP_BROWSER_ALLOWED_DOMAINS` / `_BLOCKED_DOMAINS`,
`AAP_BROWSER_ALLOW_LOCAL_ADDRESSES`, `AAP_BROWSER_ALLOW_FILE_URLS` /
`_ALLOWED_FILE_ROOTS`, `AAP_BROWSER_MAX_IMAGE_DIMENSION`,
`AAP_BROWSER_REQUIRE_FRESH_OBSERVATION`, `AAP_BROWSER_MAX_OBSERVATION_AGE_SECONDS`,
`AAP_BROWSER_REQUIRE_APPROVAL_FOR_SUBMIT`, `AAP_BROWSER_TIMEOUT_MS`,
`AAP_BROWSER_MAX_DOWNLOAD_BYTES`, and the per-run caps
`AAP_BROWSER_MAX_NAVIGATIONS` / `_INTERACTIONS` / `_OBSERVATIONS` / `_TABS`.

---

## 14. Security

Baseline, designed in from the start:

- **Authentication**: API-key auth (`api/auth.py`); keys map to a `Principal`
  with tenant, user, and roles. Auth is on by default (`AAP_REQUIRE_AUTH=true`).
- **Authorization**: the permission/policy layer gates every tool; deny by
  default.
- **Tenant isolation**: every store and query is scoped by `tenant_id`; run
  registry, approvals, tasks, memory, and audit all enforce it.
- **Secrets management**: configuration is environment-only (`AAP_*`),
  `SecretStr` for credentials, `.env` git-ignored, no secrets in source.
- **Token protection / redaction**: payloads and logs are redacted.
- **Sandboxing**: filesystem and shell tools are confined to the run workspace.
- **Rate limits**: token-bucket limiter per API key (`api/rate_limit.py`).
- **Input/output validation**: pydantic schemas at the API edge; JSON-schema
  validation of tool arguments; error messages never leak internals in
  production.
- **Audit**: security-relevant events are recorded.

---

## 15. Multi-tenancy

The identity model in `tenancy/models.py`:

```
Tenant ─┬─ User ── Role ── permissions
        ├─ AgentProfile ── tools
        ├─ Integration
        ├─ Memory
        ├─ Task
        ├─ Workflow (future)
        └─ AuditLog
```

Every long-lived entity carries `tenant_id`. A tenant can never read another
tenant's runs, approvals, tasks, memory, or audit events — enforced at the store
and API boundaries, not by convention. This is the shape a SaaS deployment needs
from day one.

---

## 16. Integration layer

`arabic_agent/integrations/` keeps external systems out of the agent core.

- `Integration` (ABC): `name`, `capabilities`, `call(IntegrationCall)`,
  `healthcheck()`, `aclose()`.
- `IntegrationCall` / `IntegrationResult`: provider-neutral request/response.
- `IntegrationRegistry`: register, resolve, healthcheck, lifecycle.
- `WebhookIntegration` is the reference implementation (a generic outbound
  webhook), proving the contract.

Future integrations — Email, ERP, WhatsApp Business, Instagram, Facebook,
Google, Microsoft, Calendar, Storage, Accounting — implement `Integration` and
are registered like any other. The agent selects them through tools that wrap
the integration; integration logic never lives in the loop.

---

## 17. Arabic-first & i18n

Arabic is a design input, not a later translation.

- `Locale`: `AR`, `EN`; `Direction`: `RTL`, `LTR` (`i18n/catalog.py`).
- `translate(key, locale)` with an Arabic-first catalog.
- The system prompt always states the response language and text direction.
- The context budget is **script-aware**: Arabic text is estimated at a higher
  token cost than Latin text, so the budget is honest for Arabic.
- API schemas and error messages are locale-aware; `Accept-Language` selects the
  locale for a request.

The UI layer (future) must render RTL/LTR from the same locale signal.

---

## 18. Future capabilities (interfaces reserved, not implemented)

These have clear insertion points and are tracked in `TODO.md`:

- **Document generation** — PDF/DOCX/XLSX/PPTX/CSV as independent tools.
- **Video understanding** — frames → OCR → speech-to-text → timeline → visual
  events, behind a video tool interface.
- **Workflow scheduler** — cron/event/webhook triggers that start workflow runs
  are implemented; see §8b. The workflow engine itself (definitions, versions,
  runs, steps, conditions, templating, approvals, retries) is in §8a.
- **Desktop agent** — a local runtime implementing `Transport`, registering
  local tools and a local model.

None of these require rewriting the agent core; each is an implementation behind
an existing abstraction. Durable Postgres persistence, the Redis queue and the
recoverable worker runtime are now implemented (see §3a).

---

## 19. Testing strategy

The foundation is verified, not assumed.

- **Unit / integration tests** (`tests/`, 160+ tests) exercise real code paths:
  - `test_permissions.py` — policy decisions (allow/deny/requires-approval),
    risk ceilings, tenant policy overrides.
  - `test_tools.py` — argument validation, sandbox escape refusal, execution.
  - `test_model_gateway.py` — resolution, retry, fallback, usage accounting.
  - `test_agent_loop.py` — planning, tool execution, recovery, approval pause
    and resume, guards.
  - `test_memory.py` — write policy (secrets refused), scoping, expiry.
  - `test_tasks_context.py` — plan transitions and context budgeting.
  - `test_workflows.py` — the workflow engine: versioning, dependency
    scheduling, condition skip, templating, retries, approval pause/resume,
    error policies, idempotency and tenant scoping.
  - `test_core.py` — config, IDs, redaction.
  - `test_api.py` — auth, runs, approvals, rate limiting, error mapping,
    workflow CRUD/run/approval over HTTP, and cross-tenant 404 isolation.
  - `test_worker_recovery.py` — worker execution, lease-based recovery,
    approval-safe recovery, poison-job failure bounds (in-memory stores).
  - `test_observability.py` — correlation context propagation and isolation,
    metrics aggregation and Prometheus rendering, span recording and error
    status, log redaction, readiness aggregation with critical vs non-critical
    probes and timeouts.
  - `test_readiness_integration.py` — **real Postgres/Redis**: the readiness
    probes reach the live dependency and report it, and an unreachable database
    flips readiness to not-ready without leaking the connection string.
  - `test_persistence_postgres.py` — **real Postgres**: round-tripping of every
    new field, idempotency-key uniqueness, optimistic-concurrency conflicts,
    tenant isolation, `list_stale`/heartbeat semantics, one-shot approvals,
    memory upsert. Marked `postgres`.
  - `test_redis_infra.py` — **real Redis**: token-bucket rate limiting,
    distributed lock exclusivity, idempotency lifecycle, and the reliable
    queue's claim/ack/nack/reclaim lifecycle. Marked `redis`.
  - `test_runtime_e2e.py` — the Phase 2 acceptance path: a run created through
    `RunService` in `queued` mode is persisted to Postgres, enqueued on Redis,
    executed by a real `Worker`, and recovered after simulated worker loss.
  - `test_computer.py` — Phase 7: coordinate model and DPI round-trips, Arabic
    normalisation/grounding (diacritics, longest overlap), perception fusing,
    policy clamps and budgets, the observe→act→observe discipline, stop and
    staleness, the screen-data boundary, and OCR-engine construction — against a
    scripted backend (only the OS is doubled).
  - `test_computer_tools.py` — the computer tools run through the **real**
    executor, policy engine, approval store and audit log: allow / deny /
    requires-approval, Arabic typing audited by length only, and honest failure
    when no runtime is present.
  - `test_vision_context.py` — `ImageContent`, the OpenAI-compatible multimodal
    encoding, the gateway capability query, and context-manager attachment
    inclusion/budget-dropping.
  - `test_computer_container.py` — the composition root registers computer tools
    only for a local runtime with computer control enabled; an unknown screen
    policy fails closed; a CSV OCR-language env value parses.
  - `test_computer_e2e.py` — **real X11** (opt-in via `AAP_TEST_DISPLAY`): the
    actual display is captured and real input is synthesised through `xdotool`,
    driving capture → perceive → ground → act → verify. Skips when no display is
    provided. The display must run a window manager (`Xvfb :99` + `openbox`),
    otherwise the pointer is pinned at the screen centre and two cases fail.
  - `test_browser_url_security.py` — Phase 8: dangerous schemes, `file://`
    escapes, credential-bearing URLs, SSRF/loopback/private/link-local refusal
    (incl. the cloud-metadata address), and domain allow/deny matching.
  - `test_browser_policy.py` — observation bounds, per-run caps, the
    minimum-interval and repeated-navigation loop guards, tab/download/upload
    limits, and freshness.
  - `test_browser_helpers.py` — bounded text extraction and search, the
    prompt-injection scanner, and source-ledger provenance/quality.
  - `test_browser_runtime.py` — the observe→act→observe discipline, URL
    validation before the engine is touched, the web-content boundary, the
    consequential-submit/dialog approval gate, `changed_since`, and the audit
    trail (typed text by length only) — against a scripted backend.
  - `test_browser_tools.py` — the browser tools run through the **real**
    executor, policy engine, approval store and audit log: allow / deny /
    requires-approval, a forged `approved` argument still raising an approval,
    and honest failure when no runtime is present.
  - `test_browser_container.py` — the composition root registers browser tools
    only for a local runtime with browsing enabled, and an unknown content
    policy fails closed.
  - `test_browser_e2e.py` — **real Playwright/Chromium** (opt-in via
    `AAP_TEST_BROWSER=1`): a real local site is served, navigated, observed,
    screenshot, typed, submitted, downloaded and driven through a real
    JavaScript dialog and a download. Skips when no browser is available.
- **Deterministic model provider**: tests inject a scripted `ModelProvider`, so
  the agent loop is tested without a network or a real LLM. No mocks of internal
  logic — real components are wired through `Platform.build()`.
- **Infrastructure tests are opt-in.** The default `pytest` run needs no
  services; Postgres/Redis modules are skipped unless `AAP_TEST_DATABASE_URL`
  and `AAP_TEST_REDIS_URL` are set. When set they run against a **dedicated test
  database** whose schema is created and dropped per test — never a mock:
  ```bash
  AAP_TEST_DATABASE_URL=postgresql+asyncpg://aap:aap_dev_password@localhost:5432/aap_test \
  AAP_TEST_REDIS_URL=redis://localhost:6379/15 \
  AAP_TEST_MODEL_PATH=/path/to/model.gguf \
  AAP_TEST_DISPLAY=:99 AAP_TEST_BROWSER=1 \
  uv run pytest
  ```
  `docker compose up -d` starts the Postgres and Redis used by these tests. The
  computer e2e tests additionally need `xdotool`/`xrandr` and an X server with a
  window manager on `:99`; the browser e2e tests need Playwright's Chromium.
- **Static gates**: `ruff check`, `ruff format --check`, `mypy --strict`.
- **CI expectation** (to add): run all four gates on every change.

Rules: no mocks of code under test, no fake APIs, no fake database results. When
an external system is needed, it is a real implementation behind an ABC or an
explicitly injected test double for that boundary only.
