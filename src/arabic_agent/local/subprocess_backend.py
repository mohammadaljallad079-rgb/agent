"""Process-isolated local inference.

``llama-cpp-python`` runs a large native library in the same address space as
the agent. Some builds of that library are unstable — we observed a prebuilt
wheel abort or segfault intermittently inside ggml during *agent-loop* prompts
(planning plus a large tool schema) even though identical simple calls were
reliable. A native crash cannot be caught, retried, or written to the audit
trail from inside the process: it takes the whole agent down.

This backend moves the engine into a dedicated child process. If the child
dies, the parent observes a non-zero exit and raises an ordinary
:class:`~arabic_agent.core.errors.ExternalServiceError`, so the agent's normal
recovery path (retry, alternative plan, surfaced error) applies instead of a
process-wide crash. The child is respawned lazily on the next call.

It is deliberately thin: one request per call over length-prefixed JSON on the
child's stdin/stdout, no shared state. The child re-verifies the model file and
reuses the exact same :class:`~arabic_agent.local.inference.LlamaCppBackend`, so
parsing, context guarding and accounting live in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import sys
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from ..core.errors import ExternalServiceError
from ..core.process import process_group_kwargs, terminate_process
from ..models.base import ModelRequest, ModelResponse, ModelStreamDelta
from .model_security import verify_model_file

logger = logging.getLogger(__name__)

#: An 8-byte big-endian length prefixes every frame, so a message containing
#: newlines can never be mistaken for a frame boundary.
_HEADER = struct.Struct(">Q")
#: Cap a single frame at 256 MiB: large enough for any prompt, small enough to
#: fail loudly on a desynchronised stream rather than allocating unboundedly.
_MAX_FRAME = 256 * 1024 * 1024
#: Exit code the child uses to report a recovered platform-level error.
_ERROR_EXIT = 3


async def _write_frame(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    writer.write(_HEADER.pack(len(data)) + data)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length > _MAX_FRAME:
        raise ExternalServiceError("local inference frame exceeded the maximum size")
    body = await reader.readexactly(length)
    decoded: Any = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ExternalServiceError("local inference frame was not a JSON object")
    return decoded


class SubprocessInferenceBackend:
    """A :class:`LocalInferenceBackend` that contains the engine in a child.

    Satisfies the same protocol as :class:`LlamaCppBackend`, so it is a drop-in
    replacement; the provider, gateway, agent loop and audit trail are blind to
    the difference. Streaming is intentionally unsupported — the contract marks
    it optional — and raises ``NotImplementedError`` so callers fall back to a
    single complete().
    """

    name = "llama_cpp_subprocess"

    def __init__(
        self,
        *,
        model_path: str,
        context_window: int = 4096,
        n_threads: int | None = None,
        n_gpu_layers: int = 0,
        chat_format: str | None = None,
        sha256: str | None = None,
        timeout_seconds: float = 300.0,
        worker_module: str = "arabic_agent.local.subprocess_worker",
    ) -> None:
        self._model_path = model_path
        self._context_window = context_window
        self._n_threads = n_threads
        self._n_gpu_layers = n_gpu_layers
        self._chat_format = chat_format
        self._sha256 = sha256
        self._timeout = timeout_seconds
        #: Dotted module run as the child. Overridable only so tests can drive
        #: the real parent protocol against a controlled child process.
        self._worker_module = worker_module
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._loaded = False
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail_lines: deque[str] = deque(maxlen=40)

    # -- process lifecycle -------------------------------------------------
    async def _spawn(self) -> asyncio.subprocess.Process:
        args = [
            sys.executable,
            "-m",
            self._worker_module,
            "--model-path",
            self._model_path,
            "--context-window",
            str(self._context_window),
            "--n-gpu-layers",
            str(self._n_gpu_layers),
        ]
        if self._n_threads:
            args += ["--n-threads", str(self._n_threads)]
        if self._chat_format:
            args += ["--chat-format", self._chat_format]
        if self._sha256:
            args += ["--sha256", self._sha256]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_kwargs(),
        )
        self._process = process
        self._stderr_tail_lines = deque(maxlen=40)
        self._stderr_task = asyncio.create_task(self._drain_stderr(process))
        return process

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        """Continuously read the child's stderr.

        A chatty native engine (llama.cpp logs every load) quickly fills the
        OS pipe buffer; if nobody reads it the child blocks on write and the
        parent waits forever. We drain it for the child's whole lifetime and
        keep only the last few lines for error reporting, so no unbounded
        memory is retained.
        """
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            self._stderr_tail_lines.append(line.decode("utf-8", "replace").rstrip())

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        if process is not None:
            logger.warning(
                "local inference worker exited (code %s); respawning", process.returncode
            )
        process = await self._spawn()
        assert process.stdout is not None
        # The child verifies the (potentially large) weights file before it
        # signals readiness, so allow the load timeout here too.
        try:
            ready = await asyncio.wait_for(_read_frame(process.stdout), timeout=self._timeout)
        except TimeoutError as exc:
            await self._terminate(process)
            raise ExternalServiceError(
                "local inference worker did not become ready in time",
                details={"backend": self.name},
            ) from exc
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            BrokenPipeError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            struct.error,
        ):
            detail = self._stderr_tail()
            await self._terminate(process)
            raise ExternalServiceError(
                "local inference worker failed during startup",
                details={"model": self._model_path, "stderr_tail": detail},
            ) from None
        if not ready.get("ready"):
            await self._terminate(process)
            reason = ready.get("error")
            raise ExternalServiceError(
                f"local inference worker failed to start: {reason or 'no readiness signal'}"
            )
        self._loaded = True
        return process

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        await terminate_process(process)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self._process is process:
            self._process = None
            self._loaded = False

    def _stderr_tail(self) -> str:
        if not self._stderr_tail_lines:
            return ""
        return self._stderr_tail_lines[-1]

    # -- LocalInferenceBackend --------------------------------------------
    async def load(self) -> None:
        async with self._lock:
            await self._ensure_process()

    async def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        async with self._lock:
            process = await self._ensure_process()
            assert process.stdin is not None and process.stdout is not None
            # stderr is drained continuously by _drain_stderr, so a chatty
            # native engine cannot fill the pipe buffer and deadlock the child.
            try:
                await _write_frame(
                    process.stdin,
                    {
                        "op": "complete",
                        "request": request.model_dump(mode="json"),
                        "timeout": timeout_seconds,
                    },
                )
                reply = await asyncio.wait_for(_read_frame(process.stdout), timeout=timeout_seconds)
            except ExternalServiceError:
                # Raised by _read_frame itself (e.g. a frame over the size cap):
                # the stream is no longer trustworthy, so drop the worker.
                await self._terminate(process)
                raise
            except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
                detail = self._stderr_tail()
                await self._terminate(process)
                raise ExternalServiceError(
                    "local inference worker crashed mid-request",
                    details={"model": self._model_path, "stderr_tail": detail},
                ) from None
            except (json.JSONDecodeError, UnicodeDecodeError, struct.error, KeyError):
                # A malformed frame means the stream is desynchronised; drop the
                # worker rather than trust the rest of the pipe.
                await self._terminate(process)
                raise ExternalServiceError(
                    "local inference worker sent a malformed frame",
                    details={"backend": self.name},
                ) from None
            except TimeoutError as exc:
                await self._terminate(process)
                raise ExternalServiceError(
                    f"local model exceeded its {timeout_seconds:.0f}s deadline",
                    details={"backend": self.name},
                ) from exc
            if reply.get("error") is not None:
                raise ExternalServiceError(
                    f"local model failed: {reply['error']}",
                    details={"backend": self.name},
                )
            try:
                return ModelResponse.model_validate(reply["response"])
            except (KeyError, TypeError, ValueError) as exc:
                await self._terminate(process)
                raise ExternalServiceError(
                    "local inference worker returned an invalid response",
                    details={"backend": self.name},
                ) from exc

    def stream(
        self, request: ModelRequest, *, timeout_seconds: float
    ) -> AsyncIterator[ModelStreamDelta]:
        raise NotImplementedError("the subprocess backend does not stream")

    async def healthy(self) -> bool:
        return self._process is not None and self._process.returncode is None and self._loaded

    async def close(self) -> None:
        async with self._lock:
            if self._process is not None:
                await self._terminate(self._process)
            elif self._stderr_task is not None:
                self._stderr_task.cancel()
                self._stderr_task = None
            self._loaded = False

    def model_info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "model_path": self._model_path,
            "isolated": True,
            "context_window": self._context_window,
        }


def build_isolated_backend(
    *,
    model_path: str,
    context_window: int = 4096,
    n_threads: int | None = None,
    n_gpu_layers: int = 0,
    chat_format: str | None = None,
    sha256: str | None = None,
    timeout_seconds: float = 300.0,
) -> SubprocessInferenceBackend:
    """Return a process-isolated backend for ``model_path``.

    Integrity is checked here (when a hash is configured) exactly as the
    in-process path does, so a tampered file is rejected before it is loaded.
    """
    from pathlib import Path

    verify_model_file(Path(model_path), sha256=sha256)
    return SubprocessInferenceBackend(
        model_path=model_path,
        context_window=context_window,
        n_threads=n_threads,
        n_gpu_layers=n_gpu_layers,
        chat_format=chat_format,
        sha256=sha256,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "SubprocessInferenceBackend",
    "build_isolated_backend",
]
