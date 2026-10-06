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
- [x] Test suite (130 tests) and static gates green.

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
- [x] Local model hardening: in-process llama.cpp (`LlamaCppBackend`), Ollama,
      and any OpenAI-compatible local endpoint (`OpenAICompatibleLocalProvider`),
      behind one `LocalInferenceBackend` protocol. Includes real hardware
      detection and a conservative runnable gate (`local/hardware.py`), model
      path confinement + hash verification + config scanning
      (`local/model_security.py`), a descriptor registry with declared (never
      guessed) capabilities (`local/model_registry.py`), streaming, and
      `<tool_call>` extraction from content for models that emit tool calls as
      text. Fail-closed routing pins private data to a local model and refuses
      rather than falling back to the cloud (`models/routing.py`).
- [ ] Vision and speech providers behind `ModelCapability.VISION` / `SPEECH`.
- [ ] Per-task model routing (cheap model for planning, strong model for
      reasoning, local model for private data).
- [ ] Prompt/version management and per-tenant model policy.

## Phase 3a — File, document & project intelligence (done)

The agent can now operate on real files like an operator, through the same
sandbox → policy → approval → audit path as every other tool. Nothing here is a
stub: parsers read the actual bytes, archives are inspected without extraction,
and secrets are reported by kind + locator only, never by value.

- [x] Extended workspace filesystem tools: `read_file_range`, `edit_file`
      (exact replacement), `append_file`, `create_file`, `copy_file`,
      `move_file`, `delete_file`, `make_directory`, `file_info`, `directory_tree`,
      `search_files`, `file_type`. Move/delete/overwrite are `MOVE_FILE` /
      `DELETE_FILE` / `WRITE_FILE` gated and approval-aware.
- [x] Document ingestion layer (`documents/`): real parsers for text/Markdown/
      JSON/CSV/HTML/PDF/DOCX/XLSX/PPTX plus ZIP/TAR inspection, with section
      provenance (page/slide/sheet/line) and conservative parse limits. Optional
      dependencies degrade honestly — an absent parser reports the format as
      unsupported, it does not fake a result.
- [x] Document tools: `inspect_document` (structure only), `read_document`
      (bounded, provenance-bearing), `search_document` (lexical retrieval of the
      best sections), `list_documents`, `inspect_archive`.
- [x] Secret detection on ingested content (`SECRET_DETECTED` audit event;
      kind + locator only).
- [x] Project understanding (`project/`): `inspect_project` detects languages,
      manifests, tests, entrypoints and docs from real filesystem evidence.
- [x] Loop detection (`agent/loops.py`): repeated identical tool calls abort the
      run with `LOOP_DETECTED` instead of spinning forever.
- [x] Per-step tool advertisement: a step with a `tool_hint` sees only that tool
      plus the core set, keeping the prompt small and a small local model on the
      planned tool.

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
- [x] File move/delete tools (`MOVE_FILE`, `DELETE_FILE`), approval-gated.

## Phase 4 — Integrations

- [ ] ERP connector (generic adapter + at least one concrete ERP).
- [ ] WhatsApp Business API integration.
- [ ] Instagram and Facebook integrations.
- [ ] Google Workspace and Microsoft 365 integrations.
- [ ] Cloud storage integration (S3/GCS/Azure Blob).
- [ ] Accounting systems.
- [ ] Webhook ingestion for inbound events (extend `WebhookIntegration`).

## Phase 5 — Workflow engine

- [x] Durable DAG workflow engine: immutable versioned definitions, run/step
      records, dependency-driven scheduling, conditions and templating,
      per-step retries with backoff, and human-in-the-loop approvals. Runs pin
      the definition version they started from, so editing a workflow can never
      change an in-flight run. Durable in Postgres; in-memory default for
      local/offline use.
- [x] HTTP API: create/get/update workflows, list versions, start runs, read
      runs and step runs, and resolve workflow approvals through the shared
      approval inbox.
- [x] Scheduler and triggers (cron, event, webhook) that start workflow runs:
      dependency-free cron parser with timezones and misfire policies; a
      scheduler loop that claims each occurrence atomically and durably;
      HMAC-signed webhook ingestion; internal event triggers; manual "fire now";
      a durable firing history; and `MANAGE_WORKFLOW`/`RUN_WORKFLOW` gating.
      Fired runs go through the existing workflow engine, so there is one
      execution path.
- [ ] Example workflow: "every day at 09:00 review email and, on price
      requests, prepare a report and send it." (Depends on the email tool in
      Phase 3.)
- [x] Workflow-run worker recovery: the recovery sweep resets steps stranded in
      `RUNNING`, clears the expired lease and re-enqueues the run, so an
      interrupted workflow run is re-driven from where it stopped.

## Phase 6 — Desktop & offline runtime

Foundation is implemented and tested; the desktop *application* (packaging, UI,
tray) is not. The runtime, sandbox, tool set, control API and sync boundary are
real, not interface-only.

- [x] Local runtime orchestration: `local/runtime.py` (`LocalRuntime`,
      `build_local_runtime`, `LocalRuntimeStatus`) — lifecycle state machine,
      mode manager, device registry, boundary policy and audit wiring.
- [x] Local operating modes (`local/modes.py`): online / offline / hybrid, with
      a declared (never guessed) mode and local routing forced when offline.
- [x] Filesystem sandbox (`local/sandbox.py`): allow-list roots, deny-list,
      traversal and symlink-escape refusal, sensitive-location protection, and
      relative paths anchored to the run workspace.
- [x] Controlled command execution (`local/command.py`): allow-list only,
      `shell=False`, no inherited secrets, hard timeout, cwd confined to
      approved roots.
- [x] Local tool set (`tools/builtin/local.py`): read, list, search, write,
      copy, move, mkdir, delete, and run-command — every path through the
      sandbox, every write/delete permission-gated and approval-gated.
- [x] Capability catalogue (`local/capabilities.py`): implemented filesystem +
      command capabilities; clipboard/screen/input/application declared with
      `implemented=False` (no fake functionality).
- [x] Data-boundary policy (`local/boundary.py`): secrets and confidential data
      never leave the machine; offline forces local routing.
- [x] Sync boundary (`local/sync.py`): a durable queue of a small, closed set of
      sync kinds, secret-key redaction, and rejection of anything that may not
      cross the boundary.
- [x] Local control API + HTTP surface (`local/control.py`, `local/http.py`):
      token-authenticated status, capabilities, and lifecycle control.
- [x] Desktop entry point (`local/main.py`, console script `agent-desktop`):
      builds the local platform, refuses to start without a control token when
      auth is on, and serves the control app on loopback.
- [x] Platform wiring: a `runtime_kind == "local"` build assembles the local
      runtime, registers the local tools, and threads the sandbox/command
      policy into the executor.
- [x] Durable local persistence (`local/persistence/`): the local runtime stores
      workflows, runs/steps, approvals, audit events, the sync queue and
      idempotency keys in an embedded SQLite file (SQLAlchemy async engine,
      WAL, `NullPool`), migrated by its own Alembic branch with a separate
      `local_alembic_version` table. `Platform.durable` reports whether the
      store is on disk; a local build uses no Postgres and no Redis.
- [x] Restart and recovery: state written by one local platform is read by the
      next; a run left mid-flight by a lost worker is reclaimed
      (`Worker.recover_stale`) and driven to completion on real durable state,
      with the recovery recorded in the durable audit trail. A pending approval
      survives a real process restart and resumes afterwards.
- [~] Desktop runtime implementing `Transport` — the local runtime and its
      process entry point exist; the `Transport` implementation and a desktop
      shell (tray, installer) are not built yet.
- [x] Offline isolation verified: an end-to-end local run completes inside a
      Linux network namespace with no network, where any outbound connection
      fails at the OS level (`tests/test_local_offline.py`).
- [x] Local model runtime end to end: a GGUF model is discovered, its path is
      confined to the approved store and hash-verified, loaded (`llama-cpp-python`,
      in a dedicated child process by default), and registered as a first-class
      `local/<model>` provider that the existing gateway routes to. Loading is
      non-fatal and fail-closed: if the model cannot load, local-required requests
      are refused rather than sent to the cloud. Verified with real English and
      Arabic inference, real token accounting, and real tool-call emission
      (`tests/test_local_model_runtime.py`). Offline isolation is verified
      separately by running a full local run inside a network namespace with no
      network (`tests/test_local_offline.py`).
- [x] Native-engine crash containment: the prebuilt `llama-cpp-python` wheel can
      abort at the native layer, which Python cannot catch. File-backed engines
      now run in a child process (`local/subprocess_backend.py`,
      `local/subprocess_worker.py`); a crash becomes a recoverable
      `ExternalServiceError`, the worker is respawned, and the agent loop
      completes. Verified by running the real agent loop repeatedly on a real
      GGUF model (`tests/test_local_agent_loop.py`, `@local_model`).
- [~] Secure sync between desktop (local runtime) and cloud runtime — the queue
      and boundary exist; conflict handling and a cloud endpoint are not built.
- [ ] Desktop packaging and update mechanism.

## Phase 7a — Computer use, perception & screen control (done)

- [x] Computer subsystem (`computer/`): `ComputerBackend` ABC with value types
      (`CapturedImage`, `ScreenMetadata`, `WindowInfo`), an explicit coordinate
      model (`CoordinateSpace`, `Point`, `Rect`, `MonitorInfo`, `virtual_bounds`,
      `clamp_point`, DPI transforms), and pure capture helpers (PNG encode,
      bounded downscale, crop, difference-hash change detection).
- [x] Real X11 backend (`computer/backends/linux_x11.py`): Pillow capture,
      `xrandr` monitors, `xdotool` input/windows — explicit argv, `shell=False`,
      honest `available`/`unavailable_reason`, display-socket probe at
      construction. Null backend for unsupported platforms (design only).
- [x] OCR layer (`computer/ocr.py`): `OcrEngine` protocol, `TesseractOcr`
      (Arabic-first language list) and `NullOcr` that reports itself unavailable
      instead of returning empty text.
- [x] Perception + Arabic-aware grounding (`computer/perception.py`,
      `computer/grounding.py`): fused `Observation` with provenance; target by
      element, role+label, recognised text, or point; diacritic/tatweel folding
      and longest-overlap matching; a miss raises rather than clicking blindly.
- [x] Policy and budget (`computer/policy.py`): coordinate clamping, allowed
      keys, max scroll, per-run pointer/keyboard/capture caps and a minimum
      interval — the anti-runaway layer.
- [x] Runtime (`computer/runtime.py`): single entry point enforcing
      observe → act → observe, freshness checks, stop/cancellation, and
      `changed_since` verification; `ObservationAttachmentProvider` offers the
      latest capture to the model only when the screen-data boundary and model
      capability allow.
- [x] Screen-data boundary (`computer/screen_boundary.py`): `never_store` /
      `local_only` / `cloud_allowed` mapped onto the existing data-boundary
      policy; fails closed so a screenshot never reaches a remote model by
      default.
- [x] Tools (`tools/builtin/computer.py`): `computer_screen`, `computer_metadata`,
      `computer_pointer`, `computer_keyboard`, `computer_window`,
      `computer_verify` — registered only for a local runtime with computer
      control enabled, and gated by the existing permission/approval/audit path.
- [x] Vision models (`models/base.py`, OpenAI-compatible provider): `ImageContent`
      (base64, repr-hidden) and multimodal content encoding; the gateway reports
      `VISION` support so the context manager includes an image only when the
      model can see, charging it against the token budget.
- [x] Capability catalogue now marks `screen.capture`, `input.pointer`,
      `input.keyboard` and `window.control` as implemented **only** when the
      operator enables computer control.
- [x] Audit events for every step, with typed text recorded by length only.
- [x] Tests: `test_computer.py`, `test_computer_tools.py`, `test_vision_context.py`,
      `test_computer_container.py`, and real-X11 `test_computer_e2e.py`
      (opt-in via `AAP_TEST_DISPLAY`).

## Phase 7b — Browser, web research & autonomous web interaction (done)

- [x] Browser subsystem (`browser/`): `BrowserBackend` ABC with value types
      (`ElementInfo`, `FormInfo`, `FormFieldInfo`, `TabInfo`, `DialogInfo`,
      `DownloadedFile`, `Screenshot`, `PageSnapshot`), plus `available` /
      `unavailable_reason` so an unusable backend reports why.
- [x] Real Playwright/Chromium backend (`browser/backends/playwright.py`):
      semantic `data-aap-ref` tagging (the model never writes a selector),
      bounded DOM extraction, confined downloads with a size cap, and dialogs
      that are never auto-accepted — a click that opens one returns promptly and
      leaves it open for explicit resolution. Null backend for unsupported hosts.
- [x] URL/domain safety (`browser/url_security.py`): refuses dangerous schemes,
      `file://` outside an allowed root, credential-bearing URLs, and by default
      loopback/private/link-local hosts (SSRF, incl. the cloud-metadata address),
      with allow/deny domain lists and a `UrlRisk` so a URL can be refused or
      require approval.
- [x] Policy and budget (`browser/policy.py`): bounded observations, per-run
      navigation/interaction/observation caps, a minimum interval, tab limits,
      download count/size limits, an upload allow-list, a freshness requirement
      and a repeated-navigation loop guard.
- [x] Observations and content (`browser/observations.py`, `browser/content.py`):
      bounded, freshness-tracked page views and pure, bounded text helpers —
      never a raw DOM dump.
- [x] Injection scan and sources (`browser/injection.py`, `browser/sources.py`):
      high-precision prompt-injection indicators surfaced as warnings, and a
      per-run source ledger recorded from observations for evidence-based
      citation.
- [x] Web-content boundary (`browser/boundary.py`): `never_store` / `local_only` /
      `cloud_allowed` mapped onto the existing data-boundary policy; fails closed
      so page content never reaches a remote model by default.
- [x] Runtime (`browser/runtime.py`): single entry point enforcing
      observe → act → observe, pre-engine URL validation, consequential-submit
      refusal, budgets, cancellation and `changed_since` verification;
      `BrowserAttachmentProvider` offers a page screenshot only when the boundary
      and model capability allow.
- [x] Tools (`tools/builtin/browser.py`): `browser_navigate`, `browser_observe`,
      `browser_interact`, `browser_submit`, `browser_download`, `browser_upload`,
      `browser_dialog`, `browser_verify`, `browser_research` — registered only for
      a local runtime with browsing enabled, gated by the existing
      permission/approval/audit path.
- [x] Trusted approval gate: the `approval_granted` signal is injected by the
      executor from the stored approval record, never read from model arguments,
      so a forged `"approved": true` cannot bypass the gate.
- [x] Audit events for every step; page content, typed secrets and screenshots are
      never recorded.
- [x] Tests: `test_browser_url_security.py`, `test_browser_policy.py`,
      `test_browser_helpers.py`, `test_browser_runtime.py`, `test_browser_tools.py`,
      `test_browser_container.py`, and real-Playwright `test_browser_e2e.py`
      (opt-in via `AAP_TEST_BROWSER=1`).

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
  approvals, tasks, memory and audit history are durable in Postgres. A local
  build (`AAP_RUNTIME_KIND=local`) instead uses an embedded SQLite store
  (`AAP_LOCAL_STORE_DIR`) that is durable across restarts with no external
  service; `Platform.durable` reports which is in force. The in-memory default
  remains for the unit test suite.
- Only one cloud provider family (OpenAI-compatible) is implemented. A local
  model is a first-class provider: llama.cpp (in-process), Ollama, and any
  OpenAI-compatible local endpoint all run behind the same `ModelProvider`
  contract and route through the same gateway.
- The tool set now covers the workspace filesystem, document ingestion/search
  and project inspection (Phase 3a). Document *generation*, browser, email,
  calendar, printing, image and video tools are still planned (Phase 3).
- The desktop runtime foundation (local runtime, filesystem sandbox, controlled
  command execution, local tool set, capability catalogue, data-boundary policy,
  sync queue, and control API) is implemented and tested. The desktop
  *application* — a `Transport` implementation, packaging, UI — is not built.
- The workflow engine core (definitions, versions, runs, steps, conditions,
  templating, approvals, retries), its HTTP API, the trigger/scheduler layer
  (cron, event, webhook, manual) and workflow-run worker recovery are
  implemented and tested.
- The web UI does not exist yet; the HTTP API is the only interface.
- There is one known third-party deprecation warning from `starlette.testclient`
  with `httpx`; it does not affect runtime behavior.
- Phase 5C closeout: the full suite is green (352 passed, 0 skipped) against
  real Postgres + Redis and a real GGUF model, and the repository tree contains
  no model weights, databases or secrets. A 10x repeatability run of the real
  end-to-end loop exposed a *latency* failure mode — 4/10 runs failed with
  `run deadline exceeded`, never a native crash — caused by the per-call local
  model deadline (120s) being exceeded under CPU contention and being tighter
  than the run deadline. Raising `AAP_LOCAL_MODEL_TIMEOUT_SECONDS` to 300s and
  the test's run bound to 900s made it pass 5/5. Crash/timeout/malformed-frame
  handling is now covered by `tests/test_subprocess_isolation.py`, which drives
  the real parent against a controlled child process.
- Phase 6 closeout: the full suite is green (406 passed, 0 skipped) against real
  Postgres + Redis and a real GGUF model. Phase 6 added the extended workspace
  filesystem tools, the document ingestion/search layer, project understanding
  and the loop guard (Phase 3a). Along the way two accounting bugs were fixed:
  the context budget and the local-model guard now both count a tool's *full*
  JSON schema (name + description + `parameters`) via one shared estimator, and
  the token estimator is script-aware for Arabic. The real-model Arabic agent
  loop, which had regressed when the tool set grew, is fixed by advertising only
  the planned step's tool plus the core set; the regression is pinned by
  `test_step_advertises_only_planned_and_core_tools`.
- Phase 7 closeout: the full suite is green — 481 passed, 0 skipped — with real
  Postgres + Redis, a real GGUF model, and a real X11 display (`AAP_TEST_DISPLAY`).
  Phase 7 added computer use: a real X11 backend, an explicit coordinate model,
  Arabic-aware OCR and grounding, a per-run action budget, the observe→act→observe
  runtime, the screen-data boundary, the computer tools, and multimodal vision
  context. Two notes are recorded honestly: (1) computer control is opt-in
  (`AAP_COMPUTER_CONTROL_ENABLED`, default false) and local-only — a cloud build
  never registers the tools, and the capability catalogue only marks screen
  capture / input control implemented when the operator enables it; (2) the
  screen-data boundary defaults to `local_only`, so a screenshot is never routed
  to a remote model unless the operator explicitly sets `cloud_allowed` *and* the
  data-boundary policy permits it. The real-X11 end-to-end suite is opt-in via
  `AAP_TEST_DISPLAY`; without a display it skips rather than passing vacuously.
- Phase 8 closeout: the full suite is green — 603 passed, 0 skipped — with real
  Postgres + Redis, a real GGUF model, a real X11 display and a real
  Playwright/Chromium browser. Phase 8 added autonomous browsing and web
  research: a real Playwright backend with semantic element refs, a deterministic
  `UrlPolicy` that blocks SSRF/`file://`/credential/dangerous-scheme URLs before
  the engine is touched, a per-run action budget, bounded freshness-tracked
  observations, a prompt-injection scanner, a per-run source ledger for
  evidence-based citation, the web-content data boundary, the browser tools, and
  the observe→act→observe runtime. Notes recorded honestly: (1) browsing is
  opt-in (`AAP_BROWSER_ENABLED`, default false) and local-only — a cloud build
  never registers the tools; (2) the web-content boundary defaults to
  `local_only`, so page content and screenshots never reach a remote model unless
  the operator explicitly sets `cloud_allowed` *and* the data-boundary policy
  permits it; (3) a form submit or dialog-accept requires a real approval — the
  `approval_granted` flag is injected by the executor from the stored approval
  record and is never read from model arguments, so a forged `"approved": true`
  cannot bypass the gate. The real-Playwright end-to-end suite is opt-in via
  `AAP_TEST_BROWSER=1`.

## Verification commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Full integration verification (real Postgres + Redis, no mocks):

```bash
docker compose up -d
docker exec aap-postgres psql -U aap -d aap -c "CREATE DATABASE aap_test"
export AAP_DATABASE_URL=postgresql+asyncpg://aap:aap_dev_password@localhost:5432/aap_test
uv run alembic upgrade head && uv run alembic check   # on a freshly migrated DB
export AAP_TEST_DATABASE_URL=$AAP_DATABASE_URL
export AAP_TEST_REDIS_URL=redis://localhost:6379/15
# Optional: exercise the real local model (Phase 5) end to end. Without it the
# @local_model tests skip; everything else still runs.
export AAP_TEST_MODEL_PATH=/path/to/model.gguf
# Optional: exercise the real X11 computer-use path (Phase 7). Without it the
# computer e2e tests skip; everything else still runs. The display needs a
# window manager (Xvfb :99 + openbox) and xdotool/xrandr installed, or the
# pointer is pinned at the screen centre and two computer e2e tests fail.
export AAP_TEST_DISPLAY=:99
# Optional: exercise the real Playwright/Chromium browsing path (Phase 8).
# Without it the browser e2e tests skip; everything else still runs.
export AAP_TEST_BROWSER=1
uv run pytest                                          # 603 passed, 0 skipped
```

The 36 Postgres/Redis tests own the schema via `create_all`/`drop_all`, so run
`alembic check` before the suite (or on a fresh DB) — after the suite the shared
test DB is intentionally left empty.
