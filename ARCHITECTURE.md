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
linted (`ruff`), type-checked (`mypy --strict`) and tested (`pytest`, 69 tests).

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
| `context` | Context assembly with a token budget |
| `audit` | Audited events, sinks, redaction, sequence |
| `integrations` | External-service abstraction + registry (webhook reference impl) |
| `runtime` | Local/Cloud profile and `Transport` boundary |
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
  retries, max replans, and a wall-clock deadline terminate any run.
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
| `write_file` | `WRITE_FILE` |
| `list_directory` | `READ_FILE` |
| `shell_command` | `RUN_COMMAND` |

Filesystem tools resolve every path against the run workspace and refuse
escapes (`tools/builtin/_paths.py`). `shell_command` runs with the workspace as
its working directory. Future tools (browser, email, ERP, WhatsApp, social,
documents, spreadsheets, presentations, video, image, calendar) are declared in
`TODO.md` and plug into the same interface.

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
- **Workflow engine** — trigger → agent task → tools → approval → result, built
  on the existing task engine + integrations.
- **Desktop agent** — a local runtime implementing `Transport`, registering
  local tools and a local model.

None of these require rewriting the agent core; each is an implementation behind
an existing abstraction. Durable Postgres persistence, the Redis queue and the
recoverable worker runtime are now implemented (see §3a).

---

## 19. Testing strategy

The foundation is verified, not assumed.

- **Unit / integration tests** (`tests/`, 93 tests) exercise real code paths:
  - `test_permissions.py` — policy decisions (allow/deny/requires-approval),
    risk ceilings, tenant policy overrides.
  - `test_tools.py` — argument validation, sandbox escape refusal, execution.
  - `test_model_gateway.py` — resolution, retry, fallback, usage accounting.
  - `test_agent_loop.py` — planning, tool execution, recovery, approval pause
    and resume, guards.
  - `test_memory.py` — write policy (secrets refused), scoping, expiry.
  - `test_tasks_context.py` — plan transitions and context budgeting.
  - `test_core.py` — config, IDs, redaction.
  - `test_api.py` — auth, runs, approvals, rate limiting, error mapping.
  - `test_worker_recovery.py` — worker execution, lease-based recovery,
    approval-safe recovery, poison-job failure bounds (in-memory stores).
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
  uv run pytest
  ```
  `docker compose up -d` starts the Postgres and Redis used by these tests.
- **Static gates**: `ruff check`, `ruff format --check`, `mypy --strict`.
- **CI expectation** (to add): run all four gates on every change.

Rules: no mocks of code under test, no fake APIs, no fake database results. When
an external system is needed, it is a real implementation behind an ABC or an
explicitly injected test double for that boundary only.
