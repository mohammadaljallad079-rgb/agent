# Arabic Voice Acceptance (GATE 4B)

This is the release-acceptance procedure for the Arabic voice / speech layer.
It states plainly what is proven by automation and what still requires a real
Windows desktop with a working microphone and speaker.

Nothing here is claimed from a Linux run. The automated checks below run
everywhere; the interactive gates run only on real Windows.

---

## 1. What the voice layer is

The desktop product can be driven by voice, in Arabic, end to end:

```
microphone → transcribe (local Whisper, or a configured cloud STT)
           → the transcript lands in an editable box
           → the user edits it if it was mis-heard
           → the goal runs as a durable task (voice specialist)
           → the run pauses for human approval where policy requires it
           → the user approves
           → the real tool executes (e.g. a real Arabic PDF is written)
           → the result is spoken back (local Piper/SAPI, or cloud TTS)
```

Every stage is a real engine or an honest "unavailable" with a reason — there is
no stubbed transcription, no fake synthesis, and no placeholder audio. When the
audio backend or a speech model is absent the UI says so instead of pretending.

## 2. Automated evidence (runs on any OS)

| What | Where | Assertion |
|---|---|---|
| Spoken Arabic → transcript → durable run → approval → real `%PDF` → spoken reply | `tests/test_desktop_voice.py::test_voice_command_creates_a_real_pdf_through_approval` | The real `DesktopService` facade is driven: `voice_transcribe` → `voice_start` → `get_run` (polls to `waiting_approval`) → `decide_approval` → a real `%PDF` exists in the run workspace → `voice_speak` writes a real audio file. Only the LLM is scripted; the executor, policy engine, approval store and the `generate_document` tool are real. |
| Cancellable turns: an honest stop | `tests/test_desktop_voice.py`, `tests/test_speech.py` | A cancel token is terminal and non-retryable; a stopped turn reports `cancelled` and the UI reflects the stopped state, not a fabricated success. |
| Policy gating | `tests/test_desktop_voice.py` | `MICROPHONE_RECORD` / `SPEAKER_PLAYBACK` are allowed or denied by tenant policy; `WRITE_FILE` pauses for approval (default posture). |
| Packaged voice surface | `ArabicAIAgent.exe selfcheck` → `voice_surface` check | The speech modules, the cancellable backend loops and the STT/TTS model catalogue ship in the frozen payload; the cancel token is exercised for real. A host without an audio device is an honest `skipped`, never a fabricated pass. |

Run them:

```bash
uv run pytest tests/test_desktop_voice.py tests/test_speech.py tests/test_selfcheck.py
uv run python -m arabic_agent.desktop.selfcheck
```

The real-model path (a genuine local Whisper + Piper load and a PDF written
through approval) is additionally covered by
`tests/test_local_agent_loop.py`, which is skipped unless `AAP_TEST_MODEL_PATH`
points at a real model file.

## 3. Interactive acceptance on real Windows

These gates need a real logged-in desktop with a microphone and speaker. Run
them and record the real result; do not mark any PASS from a headless run.

| # | Gate | How to run | Expected |
|---|---|---|---|
| V1 | Audio devices present | Open the voice panel; confirm the microphone and speaker selectors list real devices | Real device names, not an empty list or a fabricated entry |
| V2 | Local speech models | Install the Arabic STT and TTS models from the panel | Download completes, hash verifies, panel shows them installed |
| V3 | Transcribe Arabic | Press record, speak an Arabic command, stop | The editable transcript shows the Arabic text |
| V4 | Editable transcript | Correct a word in the transcript box, then run | The corrected text is what runs (the run goal matches the edited box) |
| V5 | Full flow | Run the corrected command | Run reaches `waiting_approval`; approve; the real tool executes; the reply is spoken |
| V6 | Approval is real | Confirm the run does not write the file until approval is given | Nothing is written before the approval; the approval appears in the action log |
| V7 | Honest stop | Start a long turn, press stop | The turn reports stopped; no result is fabricated; the UI state is truthful |
| V8 | Denied capability | Deny `MICROPHONE_RECORD` in policy | Record is blocked with a clear reason, not a silent failure |
| V9 | Offline voice | Run in `LOCAL_ONLY` with the network blocked | Transcription and synthesis stay local; `remote_requests == 0` |
| V10 | Arabic RTL | Inspect the voice panel | Layout is RTL and the transcript/commands render correctly |

## 4. What is NOT claimed

- No gate in §3 has been run on real Windows by the automated work; they remain
  a human acceptance step.
- The Windows audio backend (WinMM) and the Windows SAPI voice are implemented
  and selected, but their *behaviour* on a real device is verified only by §3.
- Cloud speech (STT/TTS) is built only when the operator enables it **and**
  supplies an endpoint; it is not exercised by the automated suite.

## 5. Verdict

**GATE 4B AUTOMATED ACCEPTANCE: PASS** (full voice→PDF→speak flow through the
real facade; packaged voice surface self-checked).

**GATE 4B DEVICE ACCEPTANCE: PENDING** — requires a real Windows desktop per §3.
