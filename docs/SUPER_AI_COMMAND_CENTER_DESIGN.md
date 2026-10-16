# SUPER AI AGENT — Command Center Design & Implementation Plan

This is the Milestone 1 deliverable: the real audit of the shipped desktop
surface, the preserved/missing functionality ledger, the CI baseline, and the
screen architecture the redesign implements. It describes what exists; it does
not claim anything that has not been built and verified.

Product name: **Super AI Agent**. Brand: **SUPER AI**.

---

## 1. Audit of the shipped surface (0.1.0-rc.2, `338d6b72`)

### 1.1 What the user actually sees today

A single-file page, `src/arabic_agent/desktop/ui/index.html` (1240 lines),
loaded into a pywebview window (`desktop/shell.py::_load_index_html`). It is a
flat left-hand navigation with fifteen technical sections and a generic dark
dashboard look:

`workspace, voice, approvals, models, providers, tools, artifacts, workflows,
connectors, computer, printing, memory, browser, settings, diagnostics`

The brand string in the header is `الوكيل العربي` (Arabic) / `Arabic AI Agent`
(English). The page title is `الوكيل العربي | Arabic AI Agent`.

### 1.2 Real backend surface (preserved, unchanged)

The bridge (`desktop/shell.py::DesktopBridge`) is a closed set of **named
methods** forwarding to the local runtime's authenticated loopback API
(`desktop/http.py`, 58 routes). Every section already reads real data:

| Area | Endpoint(s) | Real data |
|---|---|---|
| Bootstrap | `GET /desktop/bootstrap` | one payload: runs, approvals, models, specialists, tools, artifacts, voice, settings, permissions, diagnostics |
| Runs | `GET/POST /desktop/runs`, `/{id}`, `/{id}/timeline`, `/{id}/cancel` | durable run records + audit timeline |
| Approvals | `GET /desktop/approvals`, `POST /desktop/approvals/{id}` | real HITL records |
| Models | `/desktop/models*` (install/verify/health/activate/readiness/test-inference) | model catalogue, real readiness |
| Specialists | `GET /desktop/specialists` | catalogue + readiness vs registered tools |
| Artifacts | `GET /desktop/artifacts`, `/{run}/artifacts`, `POST .../open` | real workspace file listing |
| Voice | `/desktop/voice*` (devices, models, actions, start, transcribe, synthesize, speak, play, stop) | `DesktopVoiceService` over `SpeechRuntime` |
| Tools/Workflows/Connectors/Computer/Printing/Memory/Providers/Browser | matching `GET`/`POST` | real views |

76 registered tool names exist across `tools/` and `local/tools/`. Five
specialists exist (`general, voice, video, coding, education`) with honest
`available`/`degraded`/`unavailable` readiness.

### 1.3 Character foundation (preserved)

`speech/character.py` already defines `SUPER_AI_CHARACTER` — id `super_ai`, name
`المساعد الذكي الخارق` / `Super AI Assistant`, an Arabic-first persona, a voice
profile, an Arabic-aware viseme set and the visual states
(`idle, listening, thinking, speaking, waiting_approval, error, success`).
`desktop/voice.py` already exposes it via `voice_view()["character"]`, and the UI
already animates the mouth from the viseme map. **This is the single source of
truth for the character; the redesign reuses it, it does not add a second one.**

---

## 2. Preserved vs missing ledger

### 2.1 Preserved (do not regress)

- Every HTTP route and every bridge method — the redesign is a *presentation*
  change; it wires the same calls.
- All 1145 tests (1085 passed / 60 skipped locally), `ruff`, `mypy --strict`.
- Data layout: `%LOCALAPPDATA%\ArabicAIAgent` (`product_paths.APP_DIR_NAME`),
  the local SQLite store, both migration chains, `AAP_*` config.
- Permission/approval/audit path; voice privacy policy; local/cloud boundary.
- The `super_ai` character definition and the viseme animation.

### 2.2 Missing / honest gaps (must be shown, not faked)

- **Video & Content Studio** and **Education** specialists advertise studio and
  education tools (`studio_project_create`, `education_course_plan`,
  `image_generate`, …) that are **not registered**. Their readiness is honestly
  `unavailable`/`degraded`; the Agent Hub must show that plainly.
- **No project grouping** — runs are a flat list; there is no "project" entity in
  the backend. "Projects" in the UI must be an honest grouping of runs by
  workspace, labelled as such, not an invented entity.
- **Voice** needs real hardware; CI reports `voice_surface: skipped`. Interactive
  voice acceptance stays **BLOCKED**, never PASS.
- **No code-signing** — SmartScreen warnings remain.
- **Installer name** is `ArabicAIAgent-Setup-*.exe` and the install dir is
  `ArabicAIAgent`; the product display name must change to **Super AI Agent**
  while the *on-disk identity stays* for backward compatibility.

### 2.3 CI baseline (measured, this branch, Linux)

- `pytest` → 1145 collected, **1085 passed, 60 skipped, 0 failed**
- `ruff check .` → All checks passed
- `ruff format --check .` → 345 files already formatted
- `mypy` → Success, 255 source files
- Windows CI on `338d6b72`: 1085 passed / 60 skipped on 3.11/3.12/3.13; installer
  job green; frozen selfcheck `{"ok": true}`.

---

## 3. Screen architecture (target)

The Command Center replaces the flat nav with four top-level destinations plus
the assistant presence that is always visible.

```
┌──────────────────────────────────────────────────────────────────┐
│  SUPER AI            [الرئيسية | الوكلاء | المشاريع | النشاط | ⚙] │
├──────────────────────────────────────────────────────────────────┤
│  ── Assistant presence (character + state + mic) ──────────────  │
│  ── Command bar: text input  +  🎤 voice  +  specialist  + go ─  │
│  ── Home: readiness, active tasks, recent artifacts, specialists │
└──────────────────────────────────────────────────────────────────┘
```

1. **الرئيسية / Home** — assistant presence, command bar (text + voice),
   readiness strip (local model, offline/online, mic/TTS), active tasks, recent
   artifacts, specialist shortcuts, quick actions. First-run friendly.
2. **الوكلاء / Agent Hub** — one card per specialist with real icon, description,
   capabilities, readiness, missing capabilities, entry action, recent tasks.
3. **المشاريع / Projects & Artifacts** — runs grouped by workspace, artifact
   browser, open real files, filter/search.
4. **النشاط / Execution & Activity** — current execution, step timeline, tool
   usage, pending approvals, errors, output files, cancel, audit detail.
5. **الإعدادات / Settings** — appearance, language, voice & mic, audio output,
   local models, online providers, permissions, workspace, privacy, about.
6. **المساعد الصوتي / Voice** — the premium voice surface (Milestone 5), reachable
   from the presence and the command bar, not buried.

Technical views that do not fit a destination (tools, workflows, connectors,
computer, printing, memory, browser, diagnostics) remain reachable from Settings
→ Advanced and from the Activity center, so no capability is lost.

---

## 4. Brand design system

### 4.1 Name

- Product: **Super AI Agent**
- Brand: **SUPER AI**
- Arabic product line: **سوبر إي آي**

User-facing strings change; **on-disk identity stays** (`ArabicAIAgent` data dir,
`UNINSTALL_KEY`, mutex name, distribution name) so an upgrade from rc.2 keeps the
user's data, runs, settings and documents. A display-name change is presentation;
an identity change is a migration, and none is required here.

### 4.2 Tokens

Colour, spacing, radius, type and motion are defined once as CSS custom
properties on `:root`, with a `[data-theme="light"]` override. The character's
`primary_color`/`accent_color` from `speech/character.py` seed the palette so the
brand and the character cannot drift.

- **Primary** — deep teal `#0f766e` (from the character), a calm, intelligent
  signature that is not a copy of any competitor's blue/violet.
- **Accent** — amber `#f59e0b` for attention and the voice/listening state.
- **Surfaces** — near-black navy `#0b1220` (dark), warm white `#f7f8fa` (light).
- **Semantic** — ok `#22c55e`, warn `#f59e0b`, danger `#ef4444`, info `#38bdf8`.
- **Type** — `"Segoe UI", "Noto Naskh Arabic", "Noto Sans Arabic", system-ui`;
  a size ramp (11–32px) and a 4px spacing scale.
- **Radius** — 8/12/16/999.
- **Motion** — 120ms (micro), 220ms (transition), 400ms (enter); all respect
  `prefers-reduced-motion`.

RTL/LTR is handled with logical properties (`inset-inline-start`, `margin-inline`)
so one stylesheet serves both directions.

### 4.3 Icons & assets

Icons are original inline SVG paths (no icon font, no third-party set), so there
is no licence obligation and no network fetch (the CSP forbids remote origins).
The character is drawn with CSS/SVG from the existing viseme data. No external
fonts are downloaded; only OS-installed families are referenced.

---

## 5. Implementation order & test evidence

Each milestone ends with `ruff`, `mypy`, `pytest` green plus a targeted test.

- **M2** branding + tokens → brand test, backward-compat test.
- **M3** Home + command workspace → UI structure tests.
- **M4** Agent Hub + Projects → specialist-honesty test, artifact wiring test.
- **M5** voice surface + character → voice-wiring tests.
- **M6** settings/activity/responsive/a11y → a11y + responsive assertions.
- **M7** full regression + Windows packaging + frozen selfcheck.

Interactive voice acceptance is reported **BLOCKED** without real Windows audio
hardware. No security protection is disabled for the installer.
