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
- **A model's real window bounds the prompt.** The context manager must never
  plan a prompt larger than the active model can accept. When a provider reports
  a fixed `context_window` (local models do; cloud providers leave it `None` and
  keep `max_context_tokens`), the runner passes it to `ContextManager.build`,
  which caps the budget to it, drops optional tools that do not fit, reserves
  headroom above the inference backend's own output guard, and the runner asks
  for a matching `max_tokens`. A budget that is documented but not tied to the
  model is not a control — the call fails at the last moment with an overflow.

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
- Real-model tests are marked `local_model` and run only when
  `AAP_TEST_MODEL_PATH` names a readable GGUF file. They must exercise a real
  model (English and Arabic inference, real token accounting, real tool-call
  emission) and skip — never pass vacuously — when no model is provided. Never
  download a model inside a test.
- Local durability must be proven against a **real SQLite file and a real second
  process**, not an in-memory stand-in. `tests/test_local_durable.py` restarts a
  platform on the same store, reclaims a stranded run, and resumes an approval
  from a genuinely separate process; `tests/test_local_offline.py` runs a full
  local task inside a network namespace with no network. Keep both honest — they
  skip (never pass vacuously) when the sandbox cannot provide the isolation.

## 7. Security rules

- Authentication is on by default; do not weaken it.
- The policy layer is **deny by default**. A new tool must declare its
  permissions and, for sensitive operations, its risk.
- Destructive or outbound operations (delete, send, print, run command) must be
  `REQUIRES_APPROVAL` or `DENIED` unless there is a specific, justified reason.
- Filesystem and shell tools must stay sandboxed to the run workspace.
- On the local runtime, every path must go through `local.sandbox.FilesystemSandbox`;
  never join or open a path directly. Relative paths are anchored to the run
  workspace, so a tool path never depends on the process CWD.
- Local commands run through `local.command.ControlledCommandRunner`: allow-list
  only, `shell=False`, a minimal environment that inherits no secrets, a hard
  timeout, and a CWD inside an approved root. Do not add a `shell=True` path.
- Data that may leave the machine is decided by `local.boundary.DataBoundaryPolicy`
  and queued only through `local.sync`; secrets and confidential data stay local.
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
- Workflow engine phase: durable DAG engine (immutable versioned definitions,
  run/step records, dependency-driven scheduling, conditions, templating,
  per-step retries, approvals), its HTTP API, and workflow-run worker recovery
  (reset stranded `RUNNING` steps, clear the lease, re-enqueue). Green.
- Triggers & scheduler phase: cron/event/webhook/manual triggers, a
  dependency-free cron parser with timezones and misfire policies, an atomic
  durable scheduler claim, HMAC-signed webhook ingestion, a durable firing
  history, and the trigger/scheduler HTTP API. Fired runs go through the same
  workflow engine and worker — one execution path. Green.
- Local/desktop runtime phase: `local/` (lifecycle, declared modes, device
  registry, filesystem sandbox, controlled command runner, data-boundary policy,
  sync queue, capability catalogue, token-authenticated control API), the
  sandboxed local tool set, and the `runtime_kind == "local"` platform wiring
  that registers those tools only for a local build. Green. The desktop
  *application* (a `Transport` implementation, packaging, UI) is not built.
- Local durable persistence phase: `local/persistence/` gives the local build a
  real on-disk SQLite store (SQLAlchemy async, WAL, `NullPool`) behind the same
  store ABCs as the cloud build — workflows, versions, runs/steps, approvals,
  audit, sync queue, idempotency — with its own Alembic branch
  (`migrations_local/`, `local_alembic_version`). Restart, stale-lease recovery,
  cross-process approval resume, tenant isolation and optimistic concurrency are
  all verified against the real file, and a full local task is verified to run
  with no network at all. Green.
- Local model runtime phase: a local model is a first-class `ModelProvider`, not
  a special case in the core. `local/hardware.py` (real detection + conservative
  runnable gate), `local/model_security.py` (path confinement, hash verification,
  config scanning), `local/model_registry.py` (declared capabilities),
  `local/inference.py` (llama.cpp / Ollama / OpenAI-compatible behind one
  protocol, plus `<tool_call>` extraction from content), and `models/routing.py`
  (fail-closed: private data never silently goes to the cloud) are implemented
  and tested. `tests/test_local_model_runtime.py` covers the unit layer always
  and real English/Arabic inference, token accounting and tool-call emission when
  `AAP_TEST_MODEL_PATH` names a GGUF file. Green.
- Native-engine containment: the prebuilt `llama-cpp-python` wheel aborts
  (SIGSEGV/SIGABRT/SIGFPE) on some hardware, and a native crash cannot be caught
  in Python. File-backed engines therefore run in a dedicated child process
  (`local/subprocess_backend.py` + `local/subprocess_worker.py`); a crash is
  converted into a recoverable `ExternalServiceError` and the agent survives.
  Isolation is on by default (`AAP_LOCAL_MODEL_ISOLATE`) and is deliberately
  *not* used in unit tests, where an in-process stub backend is enough — the
  unit layer never exercises a real native engine, so it cannot be flaky. The
  `@local_model` integration tests do run the real model through the isolated
  backend and complete reliably — including a full Arabic agent loop
  (`test_real_model_arabic_agent_loop`) that plans, calls the real `echo` tool
  with an Arabic argument, verifies and finishes. See
  `tests/test_local_agent_loop.py` and `tests/test_subprocess_isolation.py`.
- File, document & project intelligence phase: the agent now operates on real
  files through the same sandbox → policy → approval → audit path. The workspace
  filesystem tool set is extended (`read_file_range`, `edit_file`, `append_file`,
  `create_file`, `copy_file`, `move_file`, `delete_file`, `make_directory`,
  `file_info`, `directory_tree`, `search_files`, `file_type`), with move/delete/
  overwrite gated by `MOVE_FILE`/`DELETE_FILE`/`WRITE_FILE`. `documents/` parses
  text/Markdown/JSON/CSV/HTML/PDF/DOCX/XLSX/PPTX and inspects ZIP/TAR archives
  without extracting, carrying section provenance (page/slide/sheet/line) and
  conservative limits; optional parsers degrade honestly (an absent one reports
  the format as unsupported, never a fake result). Document tools
  (`inspect_document`, `read_document`, `search_document`, `list_documents`,
  `inspect_archive`) and project understanding (`inspect_project`) are ordinary
  `Tool`s, so nothing bypasses policy/audit. Ingested content is scanned for
  likely secrets (`SECRET_DETECTED`; kind + locator only, never the value), a
  loop guard aborts runs that repeat an identical tool call (`LOOP_DETECTED`),
  and a step with a `tool_hint` is advertised only that tool plus the core set.
  Green.
- Computer use, perception & screen control phase: `computer/` implements a
  `ComputerBackend` ABC with a real X11 backend (Pillow capture, `xrandr`
  monitors, `xdotool` input/windows; explicit argv, `shell=False`), an explicit
  logical/physical coordinate model with DPI transforms, pure capture helpers
  (PNG encode, bounded downscale, difference-hash change detection),
  Arabic-first OCR (`TesseractOcr`/`NullOcr`), fused perception with provenance,
  Arabic-aware grounding, a `ComputerPolicy` + `ActionBudget`, and a
  `ComputerRuntime` that enforces observe → act → observe with freshness checks,
  stop, and `changed_since` verification. The screen-data boundary
  (`never_store`/`local_only`/`cloud_allowed`) fails closed so a screenshot
  never reaches a remote model by default. The computer tools
  (`computer_screen`, `computer_metadata`, `computer_pointer`,
  `computer_keyboard`, `computer_window`, `computer_verify`) are ordinary tools
  gated by the existing permission/approval/audit path and registered only for a
  local runtime with `AAP_COMPUTER_CONTROL_ENABLED=true`. `ImageContent` and
  multimodal encoding make the model layer vision-ready, and the context manager
  includes an observation image only when the model can see and the boundary
  allows, charging it against the token budget. Green.
- Browser, web research & autonomous web interaction phase: `browser/`
  implements a `BrowserBackend` ABC with a real Playwright/Chromium backend
  (semantic `data-aap-ref` tagging, bounded DOM extraction, confined downloads,
  dialogs never auto-accepted), deterministic `UrlPolicy` (refuses dangerous
  schemes, `file://` escapes, credential-bearing URLs, and — by default —
  loopback/private/link-local SSRF hosts incl. the cloud-metadata address, with
  allow/deny domain lists), a `BrowserPolicy` + `ActionBudget` (bounded
  observations, per-run caps, tab/download/upload limits, freshness, a
  repeated-navigation loop guard), bounded `BrowserObservation`s and pure
  `content.py` text helpers, a high-precision prompt-injection scanner, a
  per-run `SourceLedger` recorded from observations for evidence-based citation,
  and a `WebDataBoundary` (`never_store`/`local_only`/`cloud_allowed`) that fails
  closed so page content never reaches a remote model by default. The
  `BrowserRuntime` enforces observe → act → observe, validates URLs before the
  engine is touched, refuses a consequential submit or dialog-accept unless the
  trusted `approval_granted` flag (injected by the executor from the stored
  approval record, never a model argument) is set, and audits every step without
  recording page content, secrets or screenshots. The browser tools
  (`browser_navigate`, `browser_observe`, `browser_interact`, `browser_submit`,
  `browser_download`, `browser_upload`, `browser_dialog`, `browser_verify`,
  `browser_research`) are ordinary tools gated by the existing
  permission/approval/audit path and registered only for a local runtime with
  `AAP_BROWSER_ENABLED=true`. Green.
- Tests are 603 when real Postgres/Redis are reachable, a real local model is
  provided, and a real X display and browser are available (unit tests + 36
  Postgres/Redis integration tests + local model and agent-loop tests +
  subprocess-isolation tests + 8 real-X11 computer e2e tests + real-Playwright
  browser e2e tests). Without `AAP_TEST_DATABASE_URL` / `AAP_TEST_REDIS_URL` the
  36 integration tests skip, without `AAP_TEST_MODEL_PATH` the `@local_model`
  inference tests skip by design, without `AAP_TEST_DISPLAY` the real-X11
  computer tests skip, and without `AAP_TEST_BROWSER=1` the real-browser e2e
  tests skip. Run the full set with:
  `AAP_TEST_DATABASE_URL=postgresql+asyncpg://aap:aap_dev_password@localhost:5432/aap_test
  AAP_TEST_REDIS_URL=redis://localhost:6379/15
  AAP_TEST_MODEL_PATH=/path/to/model.gguf
  AAP_TEST_DISPLAY=:99 AAP_TEST_BROWSER=1 pytest` (after `docker compose up -d`,
  `CREATE DATABASE aap_test`, an X server *with a window manager* on `:99`
  (`Xvfb` + `openbox` — without a WM the pointer is pinned at the screen centre
  and two computer e2e tests fail), and `xdotool`/`xrandr` installed). The
  Postgres tests own the schema via `create_all`/`drop_all`, so run `alembic
  check` on a freshly migrated DB, not right after the suite.
- Local-model deadlines: CPU-only inference of a planning + tool-schema prompt
  can take minutes on a contended or low-core host. `AAP_LOCAL_MODEL_TIMEOUT_SECONDS`
  (default 300s) bounds a *single* call and must stay comfortably below the
  per-*run* deadline (`AAP_MAX_RUN_SECONDS`, default 600s); if the call deadline
  is the tighter of the two, ordinary slow inference is misreported as a run
  failure. The 10x repeatability check of
  `test_real_model_completes_agent_loop_offline` failed 4/10 (all
  `run deadline exceeded`, zero native crashes) until the call deadline was
  raised from 120s to 300s and the test's run bound from 180s to 900s; it then
  passed 5/5. `tests/test_subprocess_isolation.py` covers the crash/timeout/
  malformed-frame paths against a real child process and asserts they surface as
  recoverable `ExternalServiceError`s with no cloud fallback under `OFFLINE`.
- Desktop product surface (Phase 10) is implemented: `src/arabic_agent/desktop/`
  (events broker, model catalogue/manager, service, HTTP+SSE, runtime manager,
  pywebview shell, Arabic RTL UI, installer) plus durable local `LocalRunStore`
  and `LocalActionLedger` (migration `9faa23d73417`). Entry points:
  `agent-desktop` (runtime) and `agent-shell` (native window). The Windows-only
  installer steps (registry, Startup shortcut) report `skipped` off Windows —
  never success. The shell bridge is a closed set of named methods, not a
  generic URL proxy; the UI is served with `default-src 'none'` / `connect-src
  'none'`. `recommend`/`health_check` are honest gates, not benchmarks. The
  installer `verify` step really invokes the installed entry point as a
  subprocess (never a green state from a version constant).
- Phase 10W (real Windows acceptance) is **not** run: the development host is
  headless Linux (no Windows/Wine/KVM/display), so no Windows flow is claimed.
  A static audit + clean-venv install fixed two host-independent defects: the
  local Alembic chain is now shipped in the wheel
  (`arabic_agent/_migrations_local`, resolved by `_default_migrations_dir()`),
  and the installer module entry point now runs (`__main__` guard). Phase 10W-C
  then fixed the two Windows production-build blockers, verified with a real
  Linux-side PyInstaller freeze: the generated spec now uses the real **script
  path** (`src/arabic_agent/__main__.py`) as `Analysis`'s entry and packages the
  **tracked** migration chains (`migrations_local/` → `_migrations_local`,
  `migrations/` → `_migrations_cloud`), and the frozen `__main__.py` uses
  absolute imports with asset resolution moved to `core/resources.py` (package-
  relative, never cwd). The spec is one-folder (`COLLECT`) with the runtime-loaded
  hidden imports (`aiosqlite`, `logging.config`, the Alembic runtime). Still
  blocked on real Windows: a Windows computer backend
  (`computer/backends/windows.py` is implemented and unit-tested behind an
  injectable `Win32Adapter`, but the real capture/mouse/keyboard gates need an
  interactive desktop), building/signing the binary installer, code signing,
  tray/background runtime and notifications. Build with
  `scripts/build_windows.ps1` (same flow as `.github/workflows/windows.yml`). See
  ARCHITECTURE.md §18a, `docs/WINDOWS_ACCEPTANCE.md` and TODO.md.
- Local-model production pass: a real 0.5B GGUF model now drives the full
  platform end to end. `ModelProvider.context_window` is set by the local
  provider from the model descriptor, and `AgentRunner` passes it to
  `ContextManager.build`, which caps the budget to the model's real window,
  drops optional tools that do not fit, reserves headroom above the inference
  backend's output guard, and asks for a matching `max_tokens`. Before this, the
  context manager sized prompts to `max_context_tokens` (16k) regardless of a
  small n_ctx, so an agent step advertised all ~37 tool schemas and the request
  was refused at call time with `local model request exceeds the model context
  window` — the agent could not run with a small model at all. The plan is also
  no longer rendered into the system message when its budget reservation fails.
  Verified offline with a real model: an Arabic goal is planned, the model emits
  a real tool call, the run pauses for a real HITL approval (`create_file` /
  `WRITE_FILE`), and after the approval is granted the tool runs, `report.pdf`
  is written, the run completes and the outcome is stored to memory. Regression
  tests: `test_context_manager_sizes_to_a_small_window_not_the_configured_budget`,
  `test_context_manager_without_a_window_keeps_the_configured_budget`.
- Not yet implemented (see `TODO.md`): a packaged/signed binary distribution,
  image/video tools, social connectors, and a cloud web UI (the Arabic-first
  desktop UI exists inside the pywebview shell and now includes a printing
  panel; a browser-served multi-tenant frontend does not). Document *generation*
  is implemented for PDF/DOCX/PPTX/XLSX/CSV/Markdown/HTML/JSON (see
  `documents/generation.py` and the `generate_document` / `create_spreadsheet` /
  `create_presentation` tools). Printing (Phase 11) and calendar (Phase 9)
  tools are implemented and permission-gated; see ARCHITECTURE.md §13f and the
  connector/tool suites.
- Connectors (Phase 9) reach *remote* systems, so unlike the local-only
  computer/browser tools they are available in a cloud runtime too; they are
  still opt-in (`AAP_CONNECTORS_ENABLED`) and every write/send is
  approval-gated. Secrets are referenced by environment variable in
  `AAP_CONNECTORS_JSON` and never stored in source, a spec, or the audit trail.
  Their tests run against real local SMTP/IMAP + HTTP servers (`tests/fake_*`),
  not mocks. As of Phase 10 the action ledger is **durable** in local mode
  (`LocalActionLedger`, SQLite); a Postgres-backed ledger is the cloud swap
  behind the same `ActionLedger` interface.
- Migrations: `migrations/versions/` is the source of truth for the durable
  schema. After changing `persistence/schema.py`, run
  `AAP_DATABASE_URL=<test url> uv run alembic revision --autogenerate -m "..."`,
  review the generated file, then verify `uv run alembic check` reports no
  drift. Never hand-edit an applied migration.
- Observability rules: instrument real code paths only; never put secrets,
  tokens, or raw error text in metric labels or log fields; keep the package
  free of vendor SDK imports at module load (OpenTelemetry is optional).
