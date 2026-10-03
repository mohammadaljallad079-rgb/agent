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
- [ ] Structured, correlated logging (tenant/user/run ids) and metrics
      (latency, tool error rate, retry rate, approval latency).
- [ ] OpenTelemetry traces spanning API → agent loop → model → tool.
- [ ] Health/readiness endpoints that check dependencies (DB, model, sinks).

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

- Persistence is in-memory; restarting the process loses runs, approvals,
  tasks, memory, and audit history. This is intentional for the foundation and
  is the first thing Phase 1 fixes.
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
