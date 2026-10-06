# Windows Acceptance Handoff (Phase 10W)

This document is the exact, reproducible procedure for running the Phase 10W
acceptance flow on **actual Windows**. It exists because the Phase 10W work was
prepared on a host that is *not* Windows (see "Host status" below), so the flow
could not be executed and is **not** claimed to have passed.

Nothing here is a substitute for running it on real Windows. Do not mark any
gate PASS from a Linux run.

---

## 1. Host status (why this is a handoff, not a result)

The environment used to prepare this was:

| Property | Value |
|---|---|
| OS | Debian GNU/Linux 13 (trixie), kernel 6.8.0 |
| Architecture | x86_64 (container) |
| Windows | absent (no `cmd.exe` / `powershell.exe` / `pwsh`) |
| Wine | absent |
| Hardware virtualization | absent (`/dev/kvm` missing) — no real Windows VM |
| Display | absent (`DISPLAY` unset, no Xvfb) — no interactive desktop |
| Windows CI runner | not reachable (no git remote; CI is Ubuntu-only) |

Consequently: **no Windows acceptance gate was executed.** The Linux-side
evidence that *is* real (wheel build, clean-venv install, migration creation,
702 unit tests) is explicitly not Windows evidence.

---

## 2. Product blockers independent of the host OS

These cannot pass on any Windows machine until the product implements them.
They are tracked in `TODO.md` (Phase 10W) and `ARCHITECTURE.md` §18a.

1. **Windows computer backend is design-only.** `src/arabic_agent/computer/backends/`
   implements only Linux/X11. `build_computer_backend()` returns a
   `NullComputerBackend` for `PlatformKind.WINDOWS` with the reason
   *"the Windows computer backend is not implemented yet (design only)"*.
   → Gates 30–37 (screen capture, DPI, multi-monitor, mouse, keyboard, Arabic
   typing, visual Agent E2E, stale-observation) have **no implementation**.

2. **No binary Windows installer.** `src/arabic_agent/desktop/installer.py` is a
   real per-user *script* installer (launcher `.cmd`, manifest, per-user
   registry uninstall entry, Startup-folder shortcut). There is **no MSI/MSIX/EXE**,
   no bundled Python runtime, and the launcher depends on the interpreter that
   ran the installer. → Gates 8/73 (installer build, installer SHA-256) cannot
   produce an artifact until packaging is added.

3. **Browser runtime is an optional extra.** `playwright` is in
   `[project.optional-dependencies].browser`; a normal user must run
   `playwright install chromium` themselves. → Gate 27 (browser packaging) is a
   FAIL until the installer manages the download.

4. **Code signing, tray/background runtime, Windows notifications** are not
   implemented.

---

## 3. Prerequisites for a clean Windows machine

Target: **Windows 10/11 x64**, an **interactive desktop session** (not a
headless service account), and a non-admin user where possible.

- Python **3.11–3.13** (3.13 recommended; matches CI).
- [`uv`](https://docs.astral.sh/uv/) for the build steps.
- A C toolchain is only needed if `llama-cpp-python` is built from source for a
  GPU backend; a CPU-only GGUF via the bundled runtime needs none.
- For the GUI/computer-control gates: a real logged-in desktop (physical
  machine, RDP session, or a full Windows VM **with** an interactive session).
  A Windows Server core / service session cannot satisfy these.

No developer checkout is required for the *end user* once a binary installer
exists; until then, the build steps below are developer steps.

---

## 4. Exact build commands

Run in PowerShell from the repository root.

```powershell
# 0. Environment
uv venv --python 3.13
uv pip install -e ".[dev]"

# 1. Quality gates (must be green before packaging)
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest                      # expect: 702 passed, 55 skipped, 0 failed

# 2. Build the wheel (contains the desktop UI asset and the packaged
#    migration chain arabic_agent/_migrations_local)
uv build --wheel --out-dir dist

# 3. Record the artifact hash
Get-FileHash dist\arabic_ai_agent_platform-0.1.0-py3-none-any.whl -Algorithm SHA256

# 4. Clean-room install check (fresh venv, no repo on sys.path)
python -m venv C:\tmp\aap-verify
C:\tmp\aap-verify\Scripts\pip install dist\arabic_ai_agent_platform-0.1.0-py3-none-any.whl
C:\tmp\aap-verify\Scripts\python -m arabic_agent.desktop.installer version
#   -> prints 0.1.0

# 5. Install the desktop product for the current user
C:\tmp\aap-verify\Scripts\python -m arabic_agent.desktop.installer install
#   -> JSON report; every Windows step must be "ok" (not "skipped")

# 6. Launch through the installed launcher (NOT via python/uv)
& "$env:LOCALAPPDATA\ArabicAIAgent\Arabic AI Agent.cmd"
```

### Producing a real binary installer (not yet implemented)

To satisfy gates 8/73 a packaging step must be added. The intended options:

- **PyInstaller** → a one-folder/one-file `agent-shell.exe` plus an Inno Setup
  or NSIS script that wraps it into `ArabicAIAgent-Setup-0.1.0.exe`; or
- **MSIX** via `MakeAppx`/`msixpackaging`; or
- **WiX Toolset** → an MSI.

Until one of these exists and is committed, there is no installer artifact and
no installer SHA-256 to report.

---

## 5. Acceptance procedure (maps to the Phase 10W matrix)

Run each and record the real result. Gates marked **[interactive]** require a
real logged-in Windows desktop; **[credential]** requires external credentials.

| # | Gate | How to run | Requires |
|---|---|---|---|
| 1 | Actual Windows env | Record OS build, arch, CPU/RAM/GPU/VRAM, free disk, admin? | — |
| 2 | Production build | `uv build --wheel` | — |
| 3 | Installer build | PyInstaller/MSIX/WiX (see §4) | packaging (missing) |
| 4 | Installer SHA-256 | `Get-FileHash` on the artifact | packaging (missing) |
| 5 | Clean install | §4 step 5 on a clean user | — |
| 6 | Launch from installed app | §4 step 6 | — |
| 7 | No terminal required | Confirm the launcher starts the UI without a console | — |
| 8 | Runtime auto-start | UI reports control plane ready | — |
| 9 | Loopback control plane | `netstat -ano \| findstr :<port>` shows `127.0.0.1` only | — |
| 10 | IPC authentication | Request `/desktop/*` with no/incorrect token → 401 | — |
| 11 | First-run | UI first-run flow completes | — |
| 12 | Hardware detection | Compare UI values to `Get-CimInstance Win32_Processor/VideoController` | — |
| 13 | Local persistence | Create a task, close app, relaunch, task present | — |
| 14 | Migrations | Locate the store dir (config `AAP_LOCAL_STORE_DIR`, default `.agent-data`); confirm `local.db` there has `local_alembic_version` | — |
| 15 | Durable ActionLedger | See §6 | — |
| 16 | Restart dedup | See §6 | — |
| 17 | Model Manager | Catalogue + recommendation shown | — |
| 18 | Real GGUF download | Install a real model; record name/size/source/hash | network |
| 19 | Hash verification | Tamper the file → install reports failure | — |
| 20 | Real model health | Real load/inference, not "file exists" | model |
| 21 | Real local inference | One prompt through the installed GGUF | model |
| 22 | Agent file E2E | NL goal → file create+read+verify | model |
| 23 | Windows paths | `C:\Users\...\Documents\اختبار\تقرير.txt` | — |
| 24 | Arabic filename/content | UTF-8 file name and contents round-trip | — |
| 25 | Workspace escape test | `..\..\Windows\System32` traversal denied | — |
| 26 | Arabic Agent E2E | Arabic goal, Arabic answer | model |
| 27 | Browser packaging | Installer provides chromium, or explicit progress | packaging (missing) |
| 28 | Browser Agent E2E | Local test site: find Settings, change pref, verify | model + browser |
| 29 | Public web research | ≥2 sources, provenance, synthesis | network |
| 30 | Screen capture **[interactive]** | Backend captures the real desktop | Windows backend (missing) |
| 31 | DPI **[interactive]** | Test at 100/125/150% (only claim what is tested) | Windows backend (missing) |
| 32 | Multi-monitor **[interactive]** | Test only if ≥2 monitors physically present | Windows backend (missing) |
| 33 | Real mouse **[interactive]** | Observe→locate→click→verify | Windows backend (missing) |
| 34 | Real keyboard **[interactive]** | Type / Enter / Tab / safe hotkey | Windows backend (missing) |
| 35 | Arabic typing **[interactive]** | Arabic text into a controlled field | Windows backend (missing) |
| 36 | Visual Agent E2E **[interactive]** | Observe→understand→act→verify | Windows backend (missing) |
| 37 | Stale observation **[interactive]** | Change screen between observe and act → act refused | Windows backend (missing) |
| 38 | Emergency stop | Stop a multi-step run; tools/input cease | model |
| 39 | Human takeover **[interactive]** | Pause → change state → resume re-observes | Windows backend (missing) |
| 40 | Approval UI | Consequential action shows action/target/risk | connector |
| 41 | Approval binding | Approve; digest matches; executes once | connector |
| 42 | Approval mutation | Change recipient → old approval invalid | connector |
| 43 | Connector desktop E2E | Real local SMTP or local HTTP business API | local service |
| 44 | Duplicate prevention after restart | See §6 | connector |
| 45 | Offline LOCAL_ONLY | Block network at OS level; real task; `remote_requests == 0` | model |
| 46 | Offline browser semantics | Local site works; public fails clearly; no remote fallback | browser |
| 47 | Cloud mode **[credential]** | Real cloud provider | cloud creds |
| 48 | Hybrid routing **[credential]** | Real routing | cloud creds |
| 49 | Privacy UI | Local/Cloud/Hybrid and toggles clearly shown | — |
| 50 | Model process crash | Kill the model worker; app survives, bounded respawn | model |
| 51 | Runtime crash | Kill the runtime; UI detects disconnect, state survives | — |
| 52 | Full app restart | Reopen from shortcut; settings/models/tasks/ledger persist | — |
| 53 | Tray/background | Test if implemented; else report NOT IMPLEMENTED | (not implemented) |
| 54 | Windows notifications | Test if implemented; else NOT IMPLEMENTED | (not implemented) |
| 55 | Start with Windows | Enable → logoff/logon → disable; must be opt-in | — |
| 56 | Diagnostics | Real status for each subsystem (no false green) | — |
| 57 | Secret-safe diagnostics export | Grep export for keys/tokens/passwords | — |
| 58 | IPC security | missing/invalid/malformed/oversized/forged → rejected | — |
| 59 | CSP/webview isolation | Inject script; must not gain privileged access | — |
| 60 | Closed shell bridge | JS can call only named methods | — |
| 61 | Local token security | Token not in page JS / logs / insecure storage | — |
| 62 | Windows command safety | Quoting, spaces, Unicode path, timeout, exit code | — |
| 63 | PowerShell/CMD safety | Shell metacharacters must not inject | — |
| 64 | Process tree termination | Cancel a parent+child; both cleaned up | — |
| 65 | Oversized model | UI warns/blocks; do not destabilize the machine | — |
| 66 | Disk-space failure | Insufficient space → blocked before download | — |
| 67 | Corrupt model | Hash/health failure; clear error; no crash | — |
| 68 | Installer upgrade path | Reinstall over prior; user data preserved | — |
| 69 | Uninstall | App/shortcuts/registry/startup removed | — |
| 70 | Reinstall | Clean startup after reinstall | — |
| 71 | No dev dependency | Installed app runs without repo/uv/pytest | — |
| 72 | Clean-machine prereqs | Record every prerequisite required | — |
| 73 | Installer hash | `Get-FileHash` (see §4) | packaging (missing) |
| 74 | Code signing | `Get-AuthenticodeSignature` | cert (missing) |
| 75–76 | Performance / resource release | Measure launch, readiness, idle RAM, model load, shutdown | — |
| 77–79 | Arabic RTL / LTR / accessibility | Inspect the real UI; keyboard nav | — |
| 80 | Real Agent strength test | Multi-step NL task with real model | model |
| 81 | Cross-capability E2E | Document → Browser/API → Approval → Verify | model + browser |
| 82 | Do not overclaim model intelligence | Separate platform capability from model quality | — |

---

## 6. Durable ledger + restart-dedup procedure (gates 15, 16, 44)

1. Configure a connector to a **local** service (local SMTP or local HTTP
   business API). Set `AAP_CONNECTORS_ENABLED=true`.
2. Prepare a consequential action and drive it to a recorded completion, or to
   an ambiguous (`UNKNOWN`) result, via the Agent.
3. Kill the desktop runtime (`Stop-Process` on the runtime PID) mid-flow.
4. Relaunch through the installed launcher.
5. Inspect the durable ledger (store dir is `AAP_LOCAL_STORE_DIR`, default
   `.agent-data`):
   ```powershell
   # via the app's diagnostics export, or directly:
   sqlite3 "<store-dir>\local.db" "select * from local_action_ledger;"
   ```
6. Expected: the previous action/idempotency state is still present, and the
   Agent recovery does **not** repeat the external action. A duplicate delivery
   at the provider = FAIL.

---

## 7. What still requires an interactive Windows machine

Even once the two product blockers above are implemented, these gates need a
real interactive desktop and cannot be run headlessly:

- 30–37 (screen capture, DPI, multi-monitor, mouse, keyboard, Arabic typing,
  visual Agent E2E, stale-observation protection)
- 39 (human takeover, which depends on observation)
- 53–55 (tray/background, notifications, start-with-Windows login behaviour)

Cloud/hybrid gates (47, 48) additionally require real cloud credentials, which
were not provided.

---

## 8. Reporting

After running on real Windows, report the Phase 10W matrix with a real
`PASS`/`FAIL`/`NOT VERIFIED` per gate, the installer SHA-256, the exact model
used, and exact test counts. Until then the verdict is:

**PHASE 10W PARTIALLY VERIFIED — WINDOWS BLOCKERS REMAIN.**
