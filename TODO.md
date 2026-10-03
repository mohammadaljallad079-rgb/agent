# TODO — Roadmap

Ordered by priority. **Done** means implemented, tested, and passing
`ruff` + `mypy --strict` + `pytest`. **Planned** means not implemented yet —
never assume a planned item works.

Legend: `[x]` done · `[ ]` planned · `[~]` partially done / interface only.

---

## Phase 0 — Foundation (done)

- [x] Repository scaffolding: `pyproject.toml` (hatchling), `README.md`,
      `.gitignore`, `.env.example`, `src/` layout.
- [x] Configuration: `core/config.py` (`Settings`, `AAP_*`, `SecretStr`).
- [x] Core primitives: enums, error taxonomy, IDs, secret redaction.
- [x] Tenancy identity model: Tenant / User / Role / AgentProfile.
- [x] i18n: locale, direction, Arabic-first catalog.
- [x] Composition root: `container.py::Platform` with a DI seam.
- [x] Agent loop: `AgentRunner` (plan → act → observe → verify → recover),
      bounded guards, approval pause/resume.
- [x] Planner and recovery policy (retry/repair/replan/skip/abort).
- [x] Tool abstraction + registry + built-ins (`echo`, `finish`, `read_file`,
      `write_file`, `list_directory`, `shell_command`) with sandboxing.
- [x] Permission / policy engine (allow / deny / requires-approval).
- [x] Approvals as persisted, audited entities.
- [x] Task engine + durable in-memory store.
- [x] Memory store + write/retention policy (secrets refused).
- [x] Context manager with script-aware token budget.
- [x] Model gateway: provider abstraction, registry, retry, fallback, usage.
- [x] OpenAI-compatible provider (cloud + local/offline).
- [x] Audit log with sinks and redaction.
- [x] Integration abstraction + registry + webhook reference implementation.
- [x] Local/Cloud runtime boundary (`RuntimeProfile`, `Transport`).
- [x] HTTP API: auth, rate limiting, `/health`, `/tools`, `/runs`,
      `/runs/{id}`, `/approvals`, `/approvals/{id}/decide`.
- [x] Test suite (69 tests) and static gates green.

---

## Phase 1 — Production persistence & operations (done)

The foundation was correct but held state in memory. This phase made runs
durable and recoverable behind the existing ABCs, with no agent-core changes.

- [x] `PostgresTaskStore`, `PostgresApprovalStore`, `PostgresMemoryStore`,
      `PostgresAuditStore`, and a `PostgresRunStore` against the existing ABCs.
      No interface changes.
- [x] Durable run registry so runs survive a process restart; runs are created
      and read back from Postgres.
- [x] Redis-backed distributed rate limiter, distributed lock and idempotency
      guard, shared across API replicas.
- [x] Redis-backed reliable job queue (ready list + in-flight set, Lua-atomic
      claim/ack/nack/reclaim-stale) and `InMemoryJobQueue` for local/offline.
- [x] Recoverable runtime: `queued` execution mode, `Worker`, `RunExecutor`
      with lease + heartbeat, and a bounded recovery loop that re-enqueues runs
      whose worker lease expired. Runs paused for approval are left untouched.
- [x] Idempotent run creation via an idempotency key.
- [x] Migrations (Alembic) and a documented schema; `docker-compose.yml` for
      local Postgres/Redis; a worker CLI (`worker/main.py`).

---

## Phase 1b — Observability & hardening (done)

Phase 1 made runs durable; this phase made them observable. Observability lives in
its own dependency-light package (`observability/`) so the agent core depends on
interfaces, never on a vendor SDK.

- [x] Structured, correlated JSON logging: request/run/job/tenant/user/worker ids
      bound through `ContextVar`s and injected into every log record; secret
      values redacted in both messages and structured fields.
- [x] In-process metrics registry (counters, gauges, histograms) with
      Prometheus text exposition at `GET /metrics`: run latency and outcomes,
      tool call/retry rates, model call/retry rates, job throughput and queue
      depth, approval latency, rate-limit rejections, recovery counts.
- [x] Tracing: `Tracer` + `span()` covering API → run → model → tool, with an
      optional OpenTelemetry exporter used only when the SDK is installed and
      `AAP_OTEL_ENABLED` is set.
- [x] Readiness at `GET /readyz` probing the live dependencies (Postgres,
      Redis, model route, audit sink); returns 503 when a critical dependency is
      unreachable, while `/health` stays a liveness probe.
- [x] Correlation middleware echoing/generating `X-Request-ID`.
- [x] Test coverage: `tests/test_observability.py`,
      `tests/test_readiness_integration.py`, plus API/worker instrumentation
      assertions.

## Phase 1c — Runtime correctness hardening (done)

A review pass over the durable runtime found several places where a documented
control did not actually do what it claimed. Each fix is covered by a test.

- [x] Durable audit sequencing: `PostgresAuditStore` now derives the event
      sequence from shared storage and retries a lost cross-process race, so the
      API and a worker writing the same run no longer collide on
      `UNIQUE (run_id, sequence)`. Previously each process counted independently
      from an in-process counter and the second writer's insert failed.
- [x] Bounded replanning: the runner now threads the real replan count into
      `RecoveryPolicy.decide`, so `AAP_MAX_REPLANS` is enforced. Previously the
      policy was always consulted with `replans_used=0`, so `REPLAN` was only
      ever stopped by the global step cap.
- [x] Accurate failure accounting: when a run aborts from inside a step, the
      final result now carries the run's accumulated counters instead of a fresh
      per-step snapshot.
- [x] Job requeue backoff: `AAP_JOB_BACKOFF_SECONDS` is now honoured — a failed
      job is deferred (Redis sorted set / in-memory delayed list) rather than
      re-claimed in a tight loop.
- [x] API honesty: removed the silently-ignored `resume`/`locale` fields from
      `POST /runs` (they were accepted and never used); unknown fields are
      rejected with 422.
- [x] `/health` reports `service` and `version` for rollout/orchestration.
- [x] CI now runs `alembic check` in the integration job, so a model change
      without a migration fails the build instead of drifting silently.

## Phase 2 — Model breadth

- [ ] Anthropic provider.
- [ ] Google Gemini provider.
- [ ] Local model hardening: model discovery, streaming, and capability probing
      for Ollama / vLLM / llama.cpp.
- [ ] Vision and speech providers behind `ModelCapability.VISION` / `SPEECH`.
- [ ] Per-task model routing (cheap model for planning, strong model for
      reasoning, local model for private data).
- [ ] Prompt/version management and per-tenant model policy.

## Phase 3 — Tools & documents

- [ ] Document generation tools: PDF, DOCX, XLSX, PPTX, CSV (independent tools).
- [ ] Browser automation tool (sandboxed, permission-gated).
- [ ] Email tool (IMAP/SMTP + provider APIs), `SEND_EMAIL` gated.
- [ ] Calendar tool.
- [ ] Printing tool (`PRINT_DOCUMENT` gated).
- [ ] Image analysis tool.
- [ ] Video understanding tool: frames → OCR → speech-to-text → timeline →
      visual events → understanding. Define the interface first; implement
      incrementally.
- [ ] File move/delete tools (`MOVE_FILE`, `DELETE_FILE`), approval-gated.

## Phase 4 — Integrations

- [ ] ERP connector (generic adapter + at least one concrete ERP).
- [ ] WhatsApp Business API integration.
- [ ] Instagram and Facebook integrations.
- [ ] Google Workspace and Microsoft 365 integrations.
- [ ] Cloud storage integration (S3/GCS/Azure Blob).
- [ ] Accounting systems.
- [ ] Webhook ingestion for inbound events (extend `WebhookIntegration`).

## Phase 5 — Workflow engine

- [ ] Scheduler and triggers (cron, event, webhook) over the task engine.
- [ ] Workflow definition model: trigger → agent task → tools → approval →
      result, with persisted workflow state.
- [ ] Example workflow: "every day at 09:00 review email and, on price
      requests, prepare a report and send it."
- [ ] Retries, backoff, and dead-letter handling for workflow runs.

## Phase 6 — Desktop & offline runtime

- [ ] Desktop runtime implementing `Transport`, running the agent core locally.
- [ ] Local tool set: files, local apps, printing, browser.
- [ ] Local model runtime and offline mode end to end.
- [ ] Secure sync between desktop (local runtime) and cloud runtime, including
      conflict handling and least-privilege tokens.
- [ ] Desktop packaging and update mechanism.

## Phase 7 — Product surface

- [ ] Web UI with full RTL/LTR support, locale switching, plan/timeline view,
      approval inbox, and tool-run visibility. Must be backed by the real API.
- [ ] Admin console: tenants, users, roles, policies, integrations, audit search.
- [ ] Billing/quotas for SaaS.

## Phase 8 — Hardening

- [x] CI pipeline running `ruff check`, `ruff format --check`, `mypy`, `pytest`
      (`.github/workflows/ci.yml`).
- [ ] Load and soak tests for the agent loop and API.
- [ ] Threat model and security review; secrets rotation; dependency scanning.
- [ ] Rate-limit and cost controls per tenant/model.
- [ ] Backup/restore and disaster-recovery runbooks.
- [ ] Accessibility review for Arabic RTL interfaces.

---

## Known gaps / honest notes

- Persistence is in-memory *by default*; with `AAP_DATABASE_URL` set, runs,
  approvals, tasks, memory and audit history are durable in Postgres. The
  in-memory default remains for local/offline use and for the unit test suite.
- Only one provider family (OpenAI-compatible) is implemented.
- Only the tool set listed in Phase 0 exists; the rest are planned.
- The desktop runtime and workflow engine are designed (interfaces exist) but
  not implemented.
- The web UI does not exist yet; the HTTP API is the only interface.
- There is one known third-party deprecation warning from `starlette.testclient`
  with `httpx`; it does not affect runtime behavior.

## Verification commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```
