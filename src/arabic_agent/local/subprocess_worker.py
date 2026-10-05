"""Child-process entry point for :mod:`arabic_agent.local.subprocess_backend`.

Runs a single :class:`LlamaCppBackend` and serves requests over
length-prefixed JSON on stdin/stdout. Kept deliberately small: all inference
logic, parsing and context guarding live in ``inference.LlamaCppBackend`` so
there is exactly one implementation. The parent sends ``{"op": "complete",
"request": {...}}`` and receives ``{"response": {...}}`` or ``{"error": "..."}``.

If the native engine aborts inside this process, the parent sees a broken pipe
and converts it into a recoverable :class:`ExternalServiceError`; the agent is
unaffected. Nothing here trusts the parent beyond bounds already enforced by
the backend (model path verification, context guard).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
from pathlib import Path

from ..models.base import ModelRequest
from .inference import LlamaCppBackend
from .model_security import verify_model_file

_HEADER = struct.Struct(">Q")


def _read_exact(n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            return None  # parent closed the pipe
        buf.extend(chunk)
    return bytes(buf)


def _read_frame() -> dict[str, object] | None:
    header = _read_exact(_HEADER.size)
    if header is None:
        return None  # parent closed the pipe
    (length,) = _HEADER.unpack(header)
    body = _read_exact(length)
    if body is None:
        return None
    decoded = json.loads(body.decode("utf-8"))
    return decoded if isinstance(decoded, dict) else None


def _write_frame(payload: dict[str, object]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(_HEADER.pack(len(data)) + data)
    sys.stdout.buffer.flush()


async def _serve(backend: LlamaCppBackend) -> int:
    while True:
        request = await asyncio.to_thread(_read_frame)
        if request is None:
            return 0
        if request.get("op") != "complete":
            _write_frame({"error": f"unsupported operation: {request.get('op')!r}"})
            continue
        try:
            model_request = ModelRequest.model_validate(request["request"])
            timeout = request.get("timeout", 300.0)
            response = await backend.generate(
                model_request,
                timeout_seconds=float(timeout) if isinstance(timeout, (int, float)) else 300.0,
            )
        except Exception as exc:
            _write_frame({"error": f"{type(exc).__name__}: {exc}"})
            continue
        _write_frame({"response": response.model_dump(mode="json")})


async def _main_async(args: argparse.Namespace) -> int:
    try:
        verify_model_file(Path(args.model_path), sha256=args.sha256 or None)
    except Exception as exc:
        _write_frame({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    backend = LlamaCppBackend(
        model_path=args.model_path,
        context_window=args.context_window,
        n_threads=args.n_threads,
        n_gpu_layers=args.n_gpu_layers,
        chat_format=args.chat_format,
    )
    try:
        await backend.load()
    except Exception as exc:
        _write_frame({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _write_frame({"ready": True, "backend": backend.name})
    try:
        return await _serve(backend)
    finally:
        await backend.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arabic_agent.local.subprocess_worker")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--context-window", type=int, default=4096)
    parser.add_argument("--n-threads", type=int, default=None)
    parser.add_argument("--n-gpu-layers", type=int, default=0)
    parser.add_argument("--chat-format", default=None)
    parser.add_argument("--sha256", default="")
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
