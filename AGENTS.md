# AGENTS.md — working rules for this repository

This file is the persistent memory and rulebook for anyone (human or agent)
changing this codebase. Read it before writing code. It also points to the
design: [`ARCHITECTURE.md`](ARCHITECTURE.md) and the roadmap
[`TODO.md`](TODO.md).

## 1. What this project is

An **Arabic-first Agent Operating Platform**. The user gives a *goal*; the
platform plans it, decomposes it, selects and executes tools, observes results,
verifies the outcome, and recovers from failure — pausing for human approval on
sensitive operations.

The LLM is a reasoning engine behind the `ModelGateway` abstraction. The value
of the product is the runtime: agent loop, tools, permissions, execution,
verification, memory, integrations, and the local/cloud boundary.

## 2. Non-negotiable rules

These are hard constraints. A change that violates one is not acceptable, even
if it makes a demo look better.

- **No mocks for core functionality.** Real code paths only.
- **No fake APIs, no fake database results, no placeholder buttons** that claim
  to work.
- **No hardcoded secrets** — never in source, tests, docs, or Git. Configuration
  is environment-only (`AAP_*`).
- **Never ignore errors.** Classify them; recover or surface them.
- **Never build a UI without a real backend.**
- **If a feature is not implemented, say so** — mark it clearly (e.g. in
  `TODO.md`) rather than pretending.
- **No secrets in logs, audit events, or stored payloads.** Use
  `core/redaction.py`.

## 3. Architecture rules

- **Dependencies point downward only.** Interfaces → runtime boundary →
  orchestration → capabilities → foundations. A lower layer must never import a
  higher one.
- **The composition root is `container.py::Platform`.** It is the only place
  that constructs concrete implementations. Do not instantiate dependencies
  inside other modules; add a parameter to `Platform.build()` or the relevant
  constructor.
- **The agent core must not know about specific tools, models, or integrations.**
  New tools implement `Tool`; new models implement `ModelProvider`; new external
  systems implement `Integration`. Register them — never special-case them in
  `AgentRunner`.
- **All state that matters is tenant-scoped.** Every store method takes
  `tenant_id`; queries must filter by it.
- **Everything meaningful is audited.** Add an `AuditEventType` and emit it when
  you introduce a new class of transition.
- **Audit sequences are owned by shared storage, not by a process.** When the
  API and a worker both write events for one run they must not each count from
  memory. `AuditLog` asks a durable sink for the next `sequence`; the Postgres
  sink derives it from the max stored sequence and retries a lost race. Never
  reintroduce a per-process counter in a shared-storage path.
- **Bounded loops.** Every retry/replan/requeue path must consume a configured
  budget (`max_tool_retries`, `max_replans`, `job_max_attempts`) and the *real*
  count must be passed to the policy — a control that is documented but always
  called with zero is not a control. Failed jobs requeue with backoff, never in
  a tight loop.

## 4. Coding standards

- Python 3.11+; `from __future__ import annotations` at the top of modules.
- Pydantic v2 models for data crossing a boundary; dataclasses for internal
  mutable state.
- Full type annotations. `mypy --strict` must pass.
- Prefer small, single-purpose modules and functions. Split a file before it
  grows unwieldy.
- Write comments only for non-obvious invariants or trade-offs. Do not restate
  the code or narrate change history.
- Place all imports at the top of the file.
- No `print()` in library code — use `logging`.
- Keep public surfaces in `__all__`.

## 5. Required checks before a change is "done"

Run all of these from the repository root and make them pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

"Looks correct" is not done. Tests must exercise the real code path, and static
checks must be green. If you add behavior, add a test for it in `tests/`.

## 6. Testing rules

- Test real code paths. Do not mock the code under test.
- Mocks or test doubles are allowed **only** at an external boundary (e.g. a
  scripted `ModelProvider` for the network), and must be justified.
- Wire components through `Platform.build()` with injected stores/providers;
  do not reach into internals.
- Cover edge cases: denial, approval pause/resume, retry/replan limits, sandbox
  escape attempts, secret redaction.
- Infrastructure-backed tests (Postgres, Redis) live in `tests/` and are marked
  `postgres` / `redis`. They run only when `AAP_TEST_DATABASE_URL` and
  `AAP_TEST_REDIS_URL` point at throwaway services (`docker compose up -d`), and
  use a dedicated test database they create and drop themselves. Never point
  them at a shared or production database.

## 7. Security rules

- Authentication is on by default; do not weaken it.
- The policy layer is **deny by default**. A new tool must declare its
  permissions and, for sensitive operations, its risk.
- Destructive or outbound operations (delete, send, print, run command) must be
  `REQUIRES_APPROVAL` or `DENIED` unless there is a specific, justified reason.
- Filesystem and shell tools must stay sandboxed to the run workspace.
- Validate input at the API edge and tool arguments against the JSON schema.
- Never return internal error details in production responses.
- New secrets go in `Settings` as `SecretStr` and in `.env.example` as an empty
  placeholder — never with a value.

## 8. Localization rules

- Arabic is a first-class locale, not a translation afterthought.
- Any user-facing string goes through `i18n.catalog` with a `MessageKey`.
- Preserve RTL/LTR handling wherever text is rendered or composed into prompts.
- Keep the context budget script-aware for Arabic.

## 9. Documentation rules

- `ARCHITECTURE.md` describes the system design. Update it when boundaries
  change.
- `TODO.md` is the roadmap. Keep it honest: move items between "done" and
  "planned" as reality changes; never claim something works when it does not.
- `README.md` is the entry point for a new developer.
- Keep this file (`AGENTS.md`) current with new conventions and learnings.

## 10. Git & workflow

- Keep commits focused and descriptive.
- Do not commit `.env`, secrets, `node_modules`, build artifacts, or runtime
  data (`.agent-data/`).
- Do not push to a shared branch or open a PR unless asked.
- Update `TODO.md` as part of the change when scope shifts.

## 11. Current state (keep this current)

- Foundation phase: agent loop, tools, permissions, approvals, tasks, memory,
  context, model gateway, audit, tenancy, i18n, local/cloud boundary, HTTP API,
  and tests are implemented and green (`ruff`, `mypy --strict`, `pytest`).
- Persistence & operations phase: Postgres stores (runs, tasks, approvals,
  memory, audit), Alembic migrations, Redis-backed rate limiter / lock /
  idempotency guard / reliable job queue, and a recoverable worker runtime
  (`queued` execution mode, leases, heartbeats, bounded recovery). Green.
- Observability & readiness phase: dependency-light `observability/` package
  (correlation context, JSON logging with redaction, in-process metrics with
  Prometheus exposition, tracing with an optional OpenTelemetry bridge,
  readiness probes). `/metrics` and `/readyz` are live; `/health` is liveness.
  Green.
- Runtime correctness hardening: durable audit sequencing across processes,
  enforced `max_replans`, job requeue backoff, honest `POST /runs` validation,
  `service`/`version` on `/health`, and `alembic check` in CI. Green.
- Tests are 130 (105 run by default; 25 Postgres/Redis integration tests are
  skipped unless `AAP_TEST_DATABASE_URL` / `AAP_TEST_REDIS_URL` are set — see
  `ARCHITECTURE.md` §19).
- Not yet implemented (see `TODO.md`): desktop runtime, workflow triggers,
  document/media tools, social/ERP connectors, browser/printing/email tools.
- Observability rules: instrument real code paths only; never put secrets,
  tokens, or raw error text in metric labels or log fields; keep the package
  free of vendor SDK imports at module load (OpenTelemetry is optional).
