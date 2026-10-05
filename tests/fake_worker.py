"""A controllable child process for exercising the subprocess protocol.

This is a *test double for the child*, never for the parent: the real
:class:`~arabic_agent.local.subprocess_backend.SubprocessInferenceBackend` spawns
it through the ordinary ``-m`` path and speaks the real length-prefixed JSON
protocol to it. Each failure mode is selected by ``FAKE_WORKER_MODE`` so the
parent's crash/timeout/malformed-frame handling can be asserted against a real
process boundary rather than a mock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
from pathlib import Path

from arabic_agent.models.base import ChatMessage, ModelResponse, Usage

_HEADER = struct.Struct(">Q")


def _read_exact(n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_frame() -> dict[str, object] | None:
    header = _read_exact(_HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    body = _read_exact(length)
    if body is None:
        return None
    decoded = json.loads(body.decode("utf-8"))
    return decoded if isinstance(decoded, dict) else None


def _write_raw(payload: bytes) -> None:
    sys.stdout.buffer.write(_HEADER.pack(len(payload)) + payload)
    sys.stdout.buffer.flush()


def _write_frame(payload: dict[str, object]) -> None:
    _write_raw(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _ok_response() -> dict[str, object]:
    response = ModelResponse(
        message=ChatMessage.assistant("ok"),
        model="fake",
        usage=Usage(prompt_tokens=1, completion_tokens=1),
    )
    return {"response": response.model_dump(mode="json")}


def _crash_once(marker: str) -> bool:
    """Return True the first time (and create the marker); blocking by design."""
    if os.path.exists(marker):
        return False
    Path(marker).touch()
    return True


async def _main(mode: str, marker: str) -> int:
    if mode == "never_ready":
        await asyncio.sleep(3600)
        return 0
    if mode == "malformed_ready":
        _write_raw(b"this is not json")
        return 1
    if mode == "ready_false":
        _write_frame({"ready": False, "error": "boom"})
        return 1

    _write_frame({"ready": True, "backend": "fake"})

    if mode == "exit_after_ready":
        return 0

    while True:
        request = await asyncio.to_thread(_read_frame)
        if request is None:
            return 0
        if mode == "silent":
            await asyncio.sleep(3600)
        if mode == "crash":
            os._exit(1)
        if mode == "crash_once" and await asyncio.to_thread(_crash_once, marker):
            os._exit(1)
        if mode == "malformed":
            _write_raw(b"garbage frame body")
            continue
        if mode == "oversize":
            _write_raw(b"")
            sys.stdout.buffer.write(_HEADER.pack(512 * 1024 * 1024))
            sys.stdout.buffer.flush()
            continue
        if mode == "invalid_response":
            _write_frame({"response": {"not": "a response"}})
            continue
        if mode == "sleep_forever":
            await asyncio.sleep(3600)
        _write_frame(_ok_response())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.fake_worker")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--context-window", type=int, default=4096)
    parser.add_argument("--n-threads", type=int, default=None)
    parser.add_argument("--n-gpu-layers", type=int, default=0)
    parser.add_argument("--chat-format", default=None)
    parser.add_argument("--sha256", default="")
    parser.parse_args(argv)
    mode = os.environ.get("FAKE_WORKER_MODE", "ok")
    marker = os.environ.get("FAKE_WORKER_MARKER", "/tmp/fake_worker_marker")
    return asyncio.run(_main(mode, marker))


if __name__ == "__main__":
    raise SystemExit(main())
