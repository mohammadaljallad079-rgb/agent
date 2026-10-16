# Windows Release Candidate — Verified Build Record

This records a **real, verified** Windows release candidate: the exact commit,
the artifact, its checksum, and the automated evidence that produced it. Nothing
here is claimed from a Linux run — every line comes from the Windows CI job on
`windows-latest`.

## Artifact

| Property | Value |
|---|---|
| Product | Arabic AI Agent Platform |
| Version | 0.1.0 |
| Artifact | `ArabicAIAgent-Setup-0.1.0.exe` |
| Size | 38,035,732 bytes (≈36.3 MiB) |
| SHA-256 | `DF2518EE3555A10BB2F23AA5E43F4740F99F689FEC0BF4B341D32AFC79094C6F` |
| Format | Inno Setup 6.7.1, per-user install, no UAC |
| Source commit | `9bd52d75` (branch `release/windows-rc-1`) |
| Windows job | run `38526511670`, job "Build the Windows installer" — **success** |
| Download | GitHub Actions artifact `ArabicAIAgent-Setup` on that run |

The downloaded artifact was re-hashed after download and matched the value the
build printed — the file in hand is the file that was built.

## What the build verified on real Windows

`scripts/build_windows.ps1` runs the whole flow, and the job fails if any step
does. In order:

1. **Full test suite** on `windows-latest`, Python 3.11 / 3.12 / 3.13 — all
   green (the `tests` job gates the `package` job).
2. **PyInstaller freeze** of the generated spec into `dist\ArabicAIAgent\`.
3. **Smoke test** — `ArabicAIAgent.exe version` returns 0.
4. **Self-check of the frozen payload** — `ArabicAIAgent.exe selfcheck
   --report`. Result: **ok, all four checks pass**:

   ```json
   {"ok": true, "checks": [
     {"name": "bundled_resources", "status": "ok",
      "detail": "resolved under ...\\dist\\ArabicAIAgent\\_internal\\arabic_agent"},
     {"name": "document_generation", "status": "ok",
      "detail": "produced pdf(46769B), docx(36759B), pptx(29253B), xlsx(4937B)"},
     {"name": "local_database", "status": "ok", "detail": "12 tables migrated to head"},
     {"name": "local_api", "status": "ok", "detail": "/local/status answered 200"}]}
   ```

   The 46.8 KB Arabic PDF is produced with real Arabic shaping and a real
   Unicode font on Windows — not a stub.

5. **Inno Setup compile** → `dist\ArabicAIAgent-Setup-0.1.0.exe`, then SHA-256.
6. **Install → verify → uninstall**: the produced setup is run silently, the
   *installed* executable at
   `%LOCALAPPDATA%\ArabicAIAgent\ArabicAIAgent.exe` is self-checked, and the app
   is uninstalled. The installed self-check also returned **ok, all four
   checks pass**, proving the installer's file layout (not just the PyInstaller
   payload) is correct.

## What this record does NOT claim

The automated gates above do not replace a human on a live desktop. Still
**not** verified, and not claimed as passing:

- First-run wizard and the interactive desktop shell (pywebview).
- A real local GGUF model load and inference.
- Real Agent file / browser / computer-use tasks driven from the UI, including
  real screen capture and Arabic keyboard input.
- The approval UX, emergency stop, and offline `remote_requests == 0` on a live
  run, and crash/restart recovery.
- Code signing (the installer is unsigned).

Those remain in `TODO.md` Phase 10W as interactive gates.
