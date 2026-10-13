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

- [x] Document generation tools: `generate_document` (real PDF/DOCX/Markdown/
      TXT/HTML/JSON) and `create_spreadsheet` (real XLSX/CSV), plus
      `document_generation_formats` to report which formats this install can
      actually produce. Arabic is first-class: PDF shapes it with a resolved
      Unicode font, DOCX/HTML are written RTL-aware and UTF-8, and CSV carries a
      BOM so Excel opens Arabic correctly. Writes are `WRITE_FILE` gated and
      approval-aware.
- [x] PPTX generation (independent tool): `create_presentation` writes a real
      `.pptx` (python-pptx), marks Arabic paragraphs right-to-left, and reports
      the real byte size; a format whose library is absent is refused, not faked.
- [x] Browser automation tool (sandboxed, permission-gated): `browser_*` tools
      over the Playwright backend. Registered only when the browser runtime is
      available; the backend reports itself honestly unavailable otherwise.
- [x] Email tool (IMAP/SMTP + provider APIs), `SEND_EMAIL` gated: `email_send`
      and `email_search` over the provider-neutral SMTP/IMAP connector; sending
      is `CONSEQUENTIAL_WRITE` and bound to the message digest at approval.
- [x] Calendar tool: provider-neutral `CalendarConnector` (`calendar.py`), read
      (`calendar_read` / `CALENDAR_READ`), create/update (`calendar_write` /
      `CALENDAR_WRITE`, approval-bound + verified) and delete (`calendar_delete` /
      `CALENDAR_DELETE`, approval-bound + verified). Bundled
      `GoogleCalendarProvider` (Calendar v3 + OAuth refresh); another backend is
      a new `CalendarProvider`. Tested in `test_calendar.py` against a real local
      HTTP server.
- [x] Printing (Phase 11): provider-neutral `PrintBackend` seam over the real OS
      spooler (CUPS on POSIX via `lp`/`lpstat`/`lpoptions`, Win32 spooler on
      Windows; macOS honest-unavailable), `PrintPolicy` + per-run `JobBudget`,
      `PrintRuntime` enforcing size/policy/the data boundary before the spooler
      is touched, a tenant-isolated durable job-metadata store, and the
      permission-gated tools `list_printers`, `print_document` (`PRINT`,
      approval-gated), `print_job_status`, `list_print_jobs` (read-only) and
      `cancel_print_job` (approval-gated). Paths resolve through the filesystem
      sandbox; the desktop surface exposes `/desktop/printing` and
      `/desktop/printing/jobs`. See ARCHITECTURE.md §13f.
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
- [~] Desktop runtime implementing `Transport` — the local runtime, its process
      entry point, a desktop shell (pywebview), an installer and durable
      desktop state exist (see Phase 10); the cloud `Transport` implementation
      and a secure cloud sync endpoint are not built yet.
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
- [x] Desktop packaging: a wheel ships the UI asset and both console scripts
      (`agent-desktop`, `agent-shell`); a per-user Windows installer
      (`desktop/installer.py`) writes the launcher, manifest, uninstall entry and
      Startup shortcut and verifies the install. A packaged/signed binary
      distribution and an update mechanism remain (see Phase 10).

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

## Phase 9 — Communications, external apps & real-world actions (done)

- [x] Connector subsystem (`connectors/`): `Connector` ABC with
      `healthcheck`/`call`/`operations` and value types (`ConnectorCall`,
      `ConnectorResult`, `OperationSpec`/`OperationKind`, `ConnectorHealth`,
      `ConnectorInfo`, `ConnectorRegistry`); an unusable connector reports why.
- [x] Credentials (`connectors/credentials.py`): `CredentialRef` names a secret
      (provider + env var), never inlines it; resolved only at the execution
      boundary, never stored on a connector, a spec or the audit trail.
- [x] HTTP connector (`connectors/http.py`): URL validated by the existing
      `UrlPolicy` (SSRF/private/link-local/metadata and dangerous schemes
      refused), permitted methods only, size-capped and redacted responses, and
      redirects re-validated with the credential attached only to the configured
      origin.
- [x] Email connector (`connectors/email.py`): provider-neutral SMTP send and
      IMAP search; attachments read only from workspace-authorized paths; a
      received body is scanned for prompt-injection and returned as a warning.
- [x] Business connector (`connectors/business.py`): generic ERP/CRM adapter over
      declarative `ResourceMapping`s (collection path + derived item template);
      `record.search`/`get` (READ) and `record.create`/`update` (WRITE). No
      vendor logic.
- [x] Messaging connector (`connectors/messaging.py`): provider-neutral over a
      `MessagingProvider` ABC; the only bundled provider is the official WhatsApp
      Business Cloud API.
- [x] Webhook connector (`connectors/webhooks.py`): HMAC-SHA256 signing of the
      exact bytes with a timestamp and delivery id (`X-AAP-Signature` /
      `X-AAP-Timestamp` / `X-AAP-Delivery-Id`).
- [x] Action ledger (`connectors/ledger.py`): cause-derived idempotency key and
      `ActionRecord` state machine, so a retry/redelivery does not repeat an
      effect; `canonical_digest` is the single source of truth for approval
      binding.
- [x] Runtime (`connectors/runtime.py`): the one enforcement boundary —
      availability → data boundary → approval binding → idempotency claim →
      execute → verify → audit; a consequential timeout is `UNKNOWN` and raises a
      non-retryable `AmbiguousActionError` rather than retrying blindly.
- [x] Tools (`tools/builtin/connectors.py`): `connector_list`, `http_request`,
      `email_send`, `email_search`, `message_send`, `webhook_send`,
      `business_records`, `connector_verify` — registered when connectors are
      enabled, gated by the existing permission/approval/audit path.
- [x] Trusted approval gate: the `approval_granted` signal is injected by the
      executor from the stored approval record, never read from model arguments;
      the approval is bound to the exact payload digest and a changed payload is
      refused.
- [x] Audit events for every step (metadata only — never a secret, a message body
      or a record payload).
- [x] OAuth 2.0 (`connectors/oauth.py`): provider-neutral authorization-code +
      PKCE flow, refresh and revoke; single-use TTL-bounded `state`, verifier held
      server-side, `SecretStr` tokens, and a rejected grant classified for
      reauthentication.
- [x] Connector policy (`connectors/policy.py`): declarative enable/disable,
      force-approval, deny and sensitivity overrides.
- [x] Tests: `test_connectors.py` (real local SMTP/IMAP + HTTP servers),
      `test_connector_runtime.py` (real executor/policy/approval/audit),
      `test_connector_container.py` and `test_connector_oauth.py` (real local
      token endpoint).

## Phase 10 — Windows desktop productization, Local AI Manager & installer (done)

Turns the existing local runtime into an installable desktop product. No agent
logic is added; the desktop package is a facade over the run service, model
manager, policy engine, approvals, audit and local runtime. Two things that were
in-memory in the local runtime are made durable here.

- [x] Durable desktop state (migration `9faa23d73417`, local SQLite branch):
      `local_runs` (`LocalRunStore`) — the same `RunStore` contract as Postgres,
      optimistic concurrency included — so a desktop session survives closing
      the app and a crash mid-run. `local_action_ledger` (`LocalActionLedger`)
      — the same `ActionLedger` contract — so an external effect is not repeated
      after a restart; only safe metadata and a canonical digest are stored, and
      `prune_before` retains an unresolved `UNKNOWN` row.
- [x] Composition root wiring (`container.py`): the local build uses the durable
      run store and, when connectors are enabled, the durable action ledger; a
      purely in-memory build falls back to the in-memory stores.
- [x] Desktop subsystem (`desktop/`): version single-source, `DesktopEventBroker`
      (an `AuditSink` mirroring real audit events to bounded per-tenant
      subscribers with SSE + replay), model catalogue/manager, product service,
      authenticated loopback HTTP+SSE surface, runtime manager (start/stop a real
      child process, readiness, restart), pywebview shell bridge, and an
      Arabic-first RTL UI.
- [x] Local AI Manager honesty: `recommend` is a hardware-gated recommendation
      (never a benchmark), `install` is real (verified size + SHA-256, hardware
      gate), `health_check` reports `UNKNOWN` without a real probe (a present
      file is not health), `remove` refuses the active model, and every path
      stays inside the model store.
- [x] Shell bridge is a closed set of named product methods — no generic
      URL/proxy method — and never hands the control token to JavaScript. The UI
      is served with `default-src 'none'` / `connect-src 'none'`.
- [x] Windows installer (`desktop/installer.py`): per-user install dir, launcher
      and manifest, registry uninstall entry and Startup shortcut (Windows), a
      real verification step, and clean uninstall with optional data removal.
      Windows-only steps report `skipped` with a reason off Windows.
- [x] Entry points: `agent-desktop` (runtime) and `agent-shell` (native window);
      `python -m arabic_agent.desktop.installer [uninstall|version]`.
- [x] Tests: `test_desktop_events.py`, `test_desktop_model_manager.py`,
      `test_desktop_durable.py`, `test_desktop_app.py`, `test_desktop_runtime.py`,
      `test_desktop_installer.py` — real SQLite files, a real local HTTP server,
      a real child process, the real desktop app under uvicorn, and the real
      platform (only the LLM is faked).
- [ ] Packaged/signed binary distribution (PyInstaller/MSIX) and an auto-update
      mechanism. The wheel ships the UI asset and both console scripts.

## Phase 10W — Real Windows acceptance & installer verification (blocked)

Acceptance must run on **actual Windows**. The current development host is a
headless Linux container (Debian 13, no Windows, no Wine, no `/dev/kvm`, no
display), so no Windows acceptance flow can be executed here and none is
claimed. See ARCHITECTURE.md §18a for the full status.

Two genuine defects found by a static audit + clean-venv install (both are
host-independent and were fixed with regression tests):

- [x] Install-blocking: the local Alembic chain was **not shipped in the wheel**
      and resolved to a repo-relative `parents[4]` path, so an installed product
      could not create its SQLite schema (`local migrations directory not
      found`). The chain now ships as `arabic_agent/_migrations_local`
      (`force-include` in `pyproject.toml`) and `_default_migrations_dir()`
      prefers the packaged copy. Proven by installing the wheel into a clean venv
      and creating the schema. Regression:
      `test_local_durable.py::test_migrations_resolve_for_installed_package`.
- [x] Installer `verify` step did not actually invoke the runtime (it read a
      version constant in-process) and `python -m arabic_agent.desktop.installer`
      had no `__main__` guard, so the module entry point was dead.
      `_verify_runtime` now runs the installed entry point as a subprocess and
      reports failure honestly. Regressions in `test_desktop_installer.py`.

Still required before Phase 10W can pass (needs real Windows + a packaged
binary; not implementable in this environment):

- [x] A real Windows computer backend (`computer/backends/windows.py`): Win32
      screen capture, mouse, keyboard (Unicode/Arabic), DPI and multi-monitor,
      behind an injectable `Win32Adapter` so it is unit-tested on any OS
      (`tests/test_windows_backend.py`). `build_computer_backend` now selects it
      for `PlatformKind.WINDOWS`. Real screen/mouse/keyboard/visual E2E still
      need an interactive Windows desktop to *accept*.
- [x] Packaging inputs for a binary Windows installer: `desktop/packaging.py`
      generates a one-folder PyInstaller spec and an Inno Setup script (per-user
      `{localappdata}`, no UAC, opt-in startup), and
      `.github/workflows/windows.yml` runs `scripts/build_windows.ps1` to freeze,
      smoke-test, compile and upload `ArabicAIAgent-Setup-*.exe`. The artifact,
      its SHA-256 and an upgrade-over-prior test still require a real Windows
      build.
- [x] Phase 10W-C transfer-readiness: fixed the two build blockers that would
      have failed the Windows production build. (1) The generated PyInstaller
      spec used a **module name** (`arabic_agent.__main__`) as `Analysis`'s entry,
      which PyInstaller resolves as a *file path* and rejects with
      "Unable to find …", and it referenced a `_migrations_local` copy that no
      build step ever created. The spec now names the real script path
      (`src/arabic_agent/__main__.py`) and packages the **tracked** chains
      (`migrations_local/` → `arabic_agent/_migrations_local`, `migrations/` →
      `_migrations_cloud`), verifying every `datas` source exists before writing.
      (2) Freezing the package `__main__.py` and running it crashed with
      "attempted relative import with no known parent package"; the entry now uses
      absolute imports and asset resolution moved to `core/resources.py`
      (relative to the package, not the cwd). The spec also (3) became one-folder
      (`COLLECT`) to match the Inno `SourceDir`/CI expectations and (4) declares
      the runtime-loaded hidden imports (`aiosqlite`, `logging.config`, the Alembic
      runtime) that a frozen migration run needs. Verified as far as Linux allows:
      a real PyInstaller freeze of the generated spec succeeds, the frozen
      `ArabicAIAgent.exe version` runs, and a frozen app migrates a fresh SQLite
      store to head using only the bundled chain. Regressions:
      `tests/test_desktop_packaging.py`, `tests/test_core_resources.py`.
- [x] Browser runtime provisioning surface: `browser/provisioning.py` +
      `/desktop/browser{,/provision,/progress,/cancel}` and the desktop UI
      section let the product install/verify the Chromium runtime with real
      progress and audit, instead of telling the user to run `playwright install`.
- [ ] Code signing certificate and signing.
- [ ] Tray/background runtime and Windows notifications (not implemented).
- [ ] Run the full acceptance matrix on real Windows (install, first run, real
      GGUF model, real Agent file/browser/computer tasks, Arabic, approval,
      emergency stop, offline `remote_requests == 0`, crash/restart, uninstall).

## Phase 7 — Product surface

- [ ] Cloud/multi-tenant web UI with full RTL/LTR support, locale switching,
      plan/timeline view, approval inbox, and tool-run visibility. Must be backed
      by the real API. (The desktop UI inside the pywebview shell exists — see
      Phase 10 — but a browser-served SaaS frontend does not.)
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
- Two cloud provider families are implemented: OpenAI-compatible (OpenAI,
  Azure OpenAI, and any OpenAI-compatible gateway) and Anthropic-compatible
  (the Messages API). A local model is a first-class provider: llama.cpp
  (in-process), Ollama, and any OpenAI-compatible local endpoint all run behind
  the same `ModelProvider` contract and route through the same gateway. Adding a
  provider is a registration, not an agent-core change. Gemini is not yet
  implemented.
- The tool set now covers the workspace filesystem, document ingestion/search,
  document *generation* (PDF/DOCX/XLSX/CSV/Markdown/HTML/JSON, Arabic-aware) and
  project inspection (Phase 3a). Browser, email, calendar, printing, image,
  video and PPTX generation tools are still planned (Phase 3).
- The desktop runtime foundation (local runtime, filesystem sandbox, controlled
  command execution, local tool set, capability catalogue, data-boundary policy,
  sync queue, and control API) is implemented and tested. The desktop product
  surface (durable sessions and action ledger, authenticated loopback UI, Local
  AI Manager, pywebview shell, Windows installer) is implemented and tested
  (Phase 10). Still not built: a packaged/signed binary distribution, an
  auto-update mechanism, a cloud `Transport` implementation and a secure cloud
  sync endpoint.
- The workflow engine core (definitions, versions, runs, steps, conditions,
  templating, approvals, retries), its HTTP API, the trigger/scheduler layer
  (cron, event, webhook, manual) and workflow-run worker recovery are
  implemented and tested.
- A cloud/multi-tenant web UI does not exist yet; the HTTP API is the only
  cloud interface. The Arabic-first RTL UI inside the desktop pywebview shell
  is implemented (Phase 10) but is not a browser-served SaaS frontend.
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
- Phase 9 closeout: the full suite is green — 663 passed, 0 failed, 0 errors —
  with real Postgres + Redis, a real GGUF model, a real X11 display, a real
  Playwright/Chromium browser, and real local SMTP/IMAP + HTTP servers. Phase 9
  added communications and real-world actions: a provider-neutral connector
  subsystem (HTTP, email, generic ERP/CRM, messaging, signed webhooks), a
  credential *reference* model (a secret is named, never inlined, and resolved
  only at the execution boundary), an action ledger that makes an external effect
  happen at most once, approval binding to the exact payload digest, honest
  `UNKNOWN` handling for an ambiguous timeout (never a blind retry), and the
  connector tools. Notes recorded honestly: (1) connectors are opt-in
  (`AAP_CONNECTORS_ENABLED`, default false); unlike the local-only
  computer/browser tools they are available in a cloud runtime too, because they
  reach remote SaaS/SMTP/webhook systems — every write/send stays
  approval-gated; (2) secrets are referenced by environment variable in
  `AAP_CONNECTORS_JSON` and never stored in source, a spec, or the audit trail;
  (3) the data boundary still applies — a `LOCAL_ONLY` payload is refused to a
  remote destination; (4) the action ledger is in-memory for the local runtime;
  a Postgres-backed ledger is the cloud deployment's swap behind the same
  interface. The tests use real local servers (no fake APIs) rather than mocks.
- Phase 10 closeout: the full suite is green — 699 passed, 55 skipped (skips are
  the opt-in Postgres/Redis/model/display/browser suites), 0 failed — with the
  local runtime. Phase 10 productized the desktop: durable local runs and a
  durable local action ledger (migration `9faa23d73417`), an authenticated
  loopback HTTP+SSE desktop surface, a `DesktopEventBroker` mirroring real audit
  events, a Local AI Manager, a pywebview shell bridge, an Arabic-first RTL UI,
  a runtime manager that drives a real child process, and a per-user Windows
  installer. Notes recorded honestly: (1) the desktop package adds no agent
  logic — it is a facade over the run service, model manager, policy engine,
  approvals, audit and local runtime; (2) `recommend` and `health_check` are
  honest gates, never benchmarks — "installed" is never reported as "healthy"
  without a real probe; (3) the shell bridge is a closed set of named methods
  with no generic URL/proxy method and never hands the control token to
  JavaScript; the UI is served with `default-src 'none'` / `connect-src 'none'`;
  (4) the installer's Windows-only steps report `skipped` with a reason off
  Windows, never success; (5) the action ledger is now durable in local mode
  (the in-memory ledger remains only for a storeless build); (6) a packaged,
  signed binary distribution and an auto-update mechanism are not built yet. The
  desktop tests use a real SQLite file, a real local HTTP server, a real child
  process, the real app under uvicorn and the real platform (only the LLM is
  faked) — no mocks of code under test.
- Final closeout: the full suite is green — **841 passed, 55 skipped, 0 failed**
  with the local runtime (skips are the opt-in Postgres/Redis/model/display/
  browser suites; run them with `docker compose up -d` plus the `AAP_TEST_*`
  variables). Two honest defects were found and fixed in this pass, both with
  regression tests. (1) The desktop activation audit calls omitted the
  required `run_id` keyword, so activating a local model raised
  `TypeError: AuditLog.record() missing keyword-only argument 'run_id'`
  (`desktop/service.py`; now audited with `run_id="local"`, matching the
  settings-audit convention). (2) On a genuinely fresh install (no key, no
  local weights) the setup-required provider was registered but **absent from
  the router's candidate list**, so a task failed with a generic fail-closed
  `PermissionDeniedError` instead of the actionable `ConfigurationError` the
  code documents; the setup placeholder is now routed as a local model
  (`container.py`), so first-run tasks fail honestly and actionably. Coverage
  added: `tests/test_desktop_activation.py` (model activation end to end —
  activate switches the gateway default, readiness is real, activation is
  audited, selection persists across a restart, a failed health check does not
  report ready), `tests/test_setup_provider.py` (setup provider honesty and
  gateway default-model switching). `ruff` and `mypy --strict` are clean
  (`mypy`: 218 source files, no issues). The docs' earlier "130 tests" line in
  ARCHITECTURE.md §2 refers to the Phase 0 foundation snapshot and is retained
  as historical; this closeout section and the suite counts above are the
  current source of truth.

## Local-model production-pass closeout

- The full suite is green — **977 passed, 55 skipped, 0 failed** — plus the
  opt-in real-GGUF integration tests (4 passed, 18 deselected) run with
  `AAP_TEST_MODEL_PATH`. `ruff` is clean and `mypy --strict` reports no issues
  in 229 source files; the wheel and sdist build.
- Two real defects were found by driving a real 0.5B local model through the
  full platform and fixed, both with regression tests:
  1. **The context budget ignored the local model's window.** The context
     manager sized every prompt to `max_context_tokens` (16k by default) even
     when the active model was a fixed-window local model, so an agent step
     advertised all ~37 tool schemas and the inference backend rightly refused
     the request at call time (`local model request exceeds the model context
     window`). The agent — and a plain chat request against a fresh install —
     could not run at all with a small model. Fix: `ModelProvider` now exposes a
     `context_window`; the local provider sets it from the model descriptor; the
     runner passes it into the context manager, which caps the budget to the
     window, drops optional tools that do not fit, reserves headroom for the
     backend's own output guard, and asks for a matching `max_tokens`
     (`models/base.py`, `local/model.py`, `context/manager.py`, `agent/runner.py`).
  2. **The plan was rendered into the system message even when the budget
     dropped it.** A failed plan reservation charged no budget yet still emitted
     the plan text, silently pushing a composed prompt past the window. The plan
     is now omitted from the system message whenever its reservation fails.
- Demonstrated end to end, offline, with the real model: an Arabic goal
  (`أنشئ ملف PDF ...`) is planned, the model emits a real tool call, the run
  pauses for a real Human-in-the-Loop approval (`create_file` / `WRITE_FILE`),
  and after the approval is granted (with a fresh resolved record) the approved
  tool runs, `report.pdf` is written to the workspace, the step completes,
  verification passes, the run is marked `completed`, and the outcome is written
  to memory. No FakeProvider, no injected tool call.

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
